"""`keepflame ikuuu ...`: cookie import and read-only domain discovery."""

from __future__ import annotations

import getpass
import json
import subprocess
import sys
from pathlib import Path

from keepflame.core.config import ConfigError, FlameConfig, _repository_root, load_config
from keepflame.flames.ikuuu.cookies import CookieError, SessionCookie, parse_import, write_file
from keepflame.flames.ikuuu.discovery import DEFAULT_HOST_PATTERN, DEFAULT_NAV_SEEDS, DomainFinder, normalize_base


def add_parser(subparsers, default_config) -> None:
    ikuuu = subparsers.add_parser("ikuuu", help="ikuuu check-in helpers")
    commands = ikuuu.add_subparsers(dest="ikuuu_command", required=True)
    for name in ("import-cookie", "discover"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, default=default_config, help="runtime TOML configuration path")
        command.add_argument("--flame", help="ikuuu Flame id (optional when only one is configured)")
        if name == "import-cookie":
            command.add_argument("--base", help="login base URL the cookie belongs to, e.g. https://ikuuu.top")
            command.add_argument("--from-clipboard", action="store_true", help="read the cookie with pbpaste")


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
        "next": f"keepflame run-once --flame {flame.id} --execute  (or wait for the next scheduled recheck)",
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


def main(args) -> int:
    if args.ikuuu_command == "import-cookie":
        return import_cookie(args)
    return discover(args)
