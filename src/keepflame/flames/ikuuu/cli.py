"""`python -m keepflame ikuuu ...`: cookie import, credential file, profile enrollment and login window."""

from __future__ import annotations

import getpass
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from keepflame.core.config import ConfigError, FlameConfig, _repository_root, load_config
from keepflame.entrypoint import module_command
from keepflame.flames.ikuuu.cookies import CookieError, SessionCookie, parse_import, write_file
from keepflame.flames.ikuuu.discovery import (
    DEFAULT_HOST_PATTERN, DEFAULT_NAV_SEEDS, LOGIN_PATH, DomainFinder, looks_like_login, normalize_base,
)
from keepflame.flames.ikuuu.session import CredentialError, challenge_markers, extra_hosts, parse_credentials, write_credentials


def add_parser(subparsers, default_config) -> None:
    ikuuu = subparsers.add_parser("ikuuu", help="ikuuu check-in helpers")
    commands = ikuuu.add_subparsers(dest="ikuuu_command", required=True)
    for name in ("import-cookie", "import-credentials", "discover", "enroll-profile", "login"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, default=default_config, help="runtime TOML configuration path")
        command.add_argument("--flame", help="ikuuu Flame id (optional when only one is configured)")
        if name == "import-cookie":
            command.add_argument("--base", help="login base URL the cookie belongs to, e.g. https://ikuuu.top")
            command.add_argument("--from-clipboard", action="store_true", help="read the cookie with pbpaste")
        if name in ("enroll-profile", "login"):
            command.add_argument("--execute", action="store_true",
                                 help="create the profile or open the login window; default only prints the plan")


def _flame(config, selected: str | None) -> FlameConfig:
    flames = [flame for flame in config.flames if flame.keeper == "ikuuu"]
    if selected is not None:
        flames = [flame for flame in flames if flame.id == selected]
    if len(flames) != 1:
        raise ConfigError("--flame must identify exactly one configured ikuuu Flame")
    return flames[0]


def _read_input(from_clipboard: bool, stdin=None) -> str:
    stdin = stdin or sys.stdin
    if from_clipboard:
        completed = subprocess.run(["pbpaste"], capture_output=True, timeout=10, check=False)
        if completed.returncode != 0:
            raise CookieError("clipboard is unavailable")
        return completed.stdout.decode("utf-8", "replace")
    if stdin.isatty():
        # Hidden prompt; long cookies are better piped: pbpaste | keepflame ikuuu import-cookie ...
        return getpass.getpass("Paste the ikuuu Cookie header (input hidden): ")
    return stdin.read(65537)


def import_cookie(args, *, stdin=None, out=None) -> int:
    out = out or sys.stdout
    config = load_config(args.config)
    flame = _flame(config, args.flame)
    pattern = flame.settings.get("host_pattern", DEFAULT_HOST_PATTERN)
    cookie_file = flame.settings.get("cookie_file")
    if not isinstance(cookie_file, str) or not cookie_file.startswith("/"):
        raise ConfigError("ikuuu cookie_file must be an absolute path")
    path = Path(cookie_file).expanduser()
    root = _repository_root()
    if root is not None and path.resolve().is_relative_to(root):
        raise ConfigError("ikuuu cookie_file must be outside the repository")
    raw = _read_input(args.from_clipboard, stdin)
    if len(raw) > 65536:
        raise CookieError("cookie input is too large")
    parsed = parse_import(raw, host_pattern=pattern)
    base = parsed.base_url
    if args.base:
        base = normalize_base(args.base, pattern)
        if base is None:
            raise CookieError("--base must be an https URL whose host matches host_pattern")
    elif base is None and isinstance(flame.settings.get("base_url"), str):
        base = normalize_base(flame.settings["base_url"], pattern)
    if base is None:
        raise CookieError("pass --base with the domain you logged in on")
    cookie = SessionCookie(parsed.header, base)
    write_file(path, cookie)
    print(json.dumps({
        "event": "ikuuu_cookie_imported", "flame_id": flame.id, "base_url": base,
        "cookie_file": str(path), "cookie_names": list(cookie.names), "fingerprint": cookie.fingerprint,
        "next": (f"{module_command()} run-once --config {shlex.quote(str(Path(args.config).resolve()))} --flame {flame.id}"
                 " --via-daemon --force --execute  (service running; or run-once --execute when it is stopped,"
                 " or wait for the next scheduled recheck)"),
    }, ensure_ascii=False), file=out)
    return 0


def discover(args, *, http=None, out=None) -> int:
    from keepflame.capabilities import UrllibHttpClient

    out = out or sys.stdout
    config = load_config(args.config)
    flame = _flame(config, args.flame)
    settings = flame.settings
    finder = DomainFinder(http or UrllibHttpClient(), seeds=tuple(settings.get("nav_seeds", DEFAULT_NAV_SEEDS)),
                          host_pattern=settings.get("host_pattern", DEFAULT_HOST_PATTERN))
    preferred = [settings["base_url"]] if isinstance(settings.get("base_url"), str) else []
    found = finder.discover(preferred)
    # Show every candidate, not only the first working one.
    print(json.dumps({
        "event": "ikuuu_discovery", "flame_id": flame.id, "base_url": found.base_url,
        "candidates": list(found.candidates), "probed": list(found.probed),
        "seeds_reached": found.seeds_reached, "methods": list(found.methods),
    }, ensure_ascii=False), file=out)
    return 0 if found.base_url else 1


def _outside_repo(path: Path) -> None:
    root = _repository_root()
    if root is not None and path.resolve().is_relative_to(root):
        raise ConfigError("ikuuu secret and profile paths must be outside the repository")


def import_credentials(args, *, prompts=None, out=None) -> int:
    """Store email and password. Does not log in and does not print either value."""
    out = out or sys.stdout
    config = load_config(args.config)
    flame = _flame(config, args.flame)
    credential_file = flame.settings.get("credential_file")
    if not isinstance(credential_file, str) or not credential_file.startswith("/"):
        raise ConfigError("ikuuu credential_file must be an absolute path")
    path = Path(credential_file).expanduser()
    _outside_repo(path)
    prompts = prompts or (lambda: (
        getpass.getpass("ikuuu email (input hidden): "),
        getpass.getpass("ikuuu password (input hidden): "),
    ))
    email, password = prompts()
    try:
        credentials = parse_credentials(json.dumps({"version": 1, "email": email, "password": password}))
    except CredentialError as exc:
        raise CredentialError(str(exc)) from None
    write_credentials(path, credentials)
    print(json.dumps({
        "event": "ikuuu_credentials_stored", "flame_id": flame.id, "credential_file": str(path),
        "fingerprint": credentials.fingerprint, "values_printed": False,
        "login_performed": False,
        "note": "A stored password is submitted only when the login document has no captcha, MFA or email-code field.",
    }, ensure_ascii=False), file=out)
    return 0


def enroll_profile(args, *, out=None) -> int:
    """Create an empty enrolled profile directory. Does not copy any Chrome profile."""
    out = out or sys.stdout
    config = load_config(args.config)
    flame = _flame(config, args.flame)
    raw = flame.settings.get("browser_profile_dir")
    if not isinstance(raw, str) or not raw.startswith("/"):
        raise ConfigError("ikuuu browser_profile_dir must be an absolute path before enroll-profile")
    path = Path(raw).expanduser()
    _outside_repo(path)
    parent = path.parent
    if not parent.is_dir():
        raise ConfigError("browser_profile_dir parent must already exist")
    parent_info = parent.stat()
    if parent_info.st_uid != os.getuid() or parent_info.st_mode & 0o022:
        raise ConfigError("browser profile parent must be owned by this user and not group or world writable")
    planned = {
        "event": "ikuuu_profile_enroll", "flame_id": flame.id, "account_id": flame.account_id,
        "browser_profile_dir": str(path), "executed": bool(args.execute),
        "copies_existing_chrome": False, "reads_cookies": False,
    }
    if not args.execute:
        print(json.dumps({**planned, "dry_run": True}, ensure_ascii=False), file=out)
        return 0
    if path.exists():
        if not path.is_dir() or path.is_symlink():
            raise ConfigError("browser_profile_dir must be a real directory")
    else:
        path.mkdir(mode=0o700)
    marker = path / ".keepflame-profile.json"
    payload = json.dumps({"version": 1, "account_id": flame.account_id, "purpose": "keepflame"},
                         separators=(",", ":")).encode("utf-8")
    if marker.exists():
        if marker.is_symlink() or marker.read_bytes() != payload:
            raise ConfigError("browser profile marker belongs to a different account or purpose")
    else:
        marker.write_bytes(payload)
    path.chmod(0o700)
    marker.chmod(0o600)
    print(json.dumps({**planned, "enrolled": True}, ensure_ascii=False), file=out)
    return 0


def login_window(args, *, http=None, opener=None, out=None) -> int:
    """Open the dedicated profile on the configured origin. Does not check in or read cookies."""
    out = out or sys.stdout
    config = load_config(args.config)
    flame = _flame(config, args.flame)
    pattern = flame.settings.get("host_pattern", DEFAULT_HOST_PATTERN)
    base = normalize_base(flame.settings.get("base_url") or "", pattern) if isinstance(flame.settings.get("base_url"), str) else None
    profile = flame.settings.get("browser_profile_dir")
    command = module_command()
    plan = {
        "event": "ikuuu_login_window", "flame_id": flame.id, "base_url": base,
        "browser_profile_dir": profile if isinstance(profile, str) else None,
        "executed": bool(args.execute), "checkin_performed": False, "cookies_read": False,
        "command": f"{command} ikuuu login --config {shlex.quote(str(Path(args.config).resolve()))} --flame {flame.id} --execute",
    }
    if not isinstance(profile, str) or not profile.startswith("/") or base is None:
        raise ConfigError("ikuuu login requires absolute browser_profile_dir and https base_url")
    if not args.execute:
        print(json.dumps({**plan, "dry_run": True, "browser_opened": False, "network": False}, ensure_ascii=False), file=out)
        return 0
    from keepflame.capabilities import UrllibHttpClient
    from keepflame.capabilities.http import HttpRequest
    from keepflame.flames.ikuuu.discovery import BROWSER_UA

    client = http or UrllibHttpClient()
    response = client.send(HttpRequest("GET", base + LOGIN_PATH, headers={
        "User-Agent": BROWSER_UA, "Accept": "text/html",
    }), timeout_seconds=float(flame.settings.get("timeout_seconds", 15) or 15))
    if response.status != 200 or not looks_like_login(response.body):
        print(json.dumps({**plan, "browser_opened": False, "login_document": False}, ensure_ascii=False), file=out)
        return 1
    markers = list(challenge_markers(response.body))
    hosts = extra_hosts(response.body)
    if opener is None:
        from keepflame.flames.ikuuu.panel import open_panel
        opener = open_panel
    from keepflame.capabilities.browser import ProfileNotAuthorized
    from keepflame.capabilities.profile_owner import ProfileInUse
    from keepflame.flames.ikuuu.panel import PanelError

    try:
        with opener(profile_dir=profile, account_id=flame.account_id, origin=base,
                    allowed_hosts=hosts, headless=False, login_window=True) as window:
            window.open_login(base + LOGIN_PATH)
            closed = window.wait_closed(15 * 60 * 1000)
    except (PanelError, ProfileNotAuthorized, ProfileInUse) as exc:
        print(json.dumps({**plan, "browser_opened": False, "error_type": getattr(exc, "reason", type(exc).__name__)},
                         ensure_ascii=False), file=out)
        return 1
    print(json.dumps({
        **plan, "browser_opened": True, "window": closed, "challenge_markers": markers,
        "asset_hosts": list(hosts),
        "note": "Closing the window does not prove a login. The next check-in is the proof. Cookies were not read.",
    }, ensure_ascii=False), file=out)
    return 0


def main(args) -> int:
    if args.ikuuu_command == "import-cookie":
        return import_cookie(args)
    if args.ikuuu_command == "import-credentials":
        return import_credentials(args)
    if args.ikuuu_command == "enroll-profile":
        return enroll_profile(args)
    if args.ikuuu_command == "login":
        return login_window(args)
    return discover(args)
