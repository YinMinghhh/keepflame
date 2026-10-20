"""`keepflame tushare import-cookie`: store the shared Tushare session cookie."""

from __future__ import annotations

import getpass
import json
import shlex
import subprocess
import sys
from pathlib import Path

from keepflame.core.config import ConfigError, _repository_root, load_config
from keepflame.flames.tushare.cookies import ORIGIN, CookieError, parse_import, write_file


def add_parser(subparsers, default_config) -> None:
    tushare = subparsers.add_parser("tushare", help="Tushare daily points helpers")
    commands = tushare.add_subparsers(dest="tushare_command", required=True)
    command = commands.add_parser("import-cookie", help="store the tushare.pro session cookie (mode 600)")
    command.add_argument("--config", type=Path, default=default_config, help="runtime TOML configuration path")
    command.add_argument("--flame", help="any Tushare Flame id (optional when all share one cookie_file)")
    command.add_argument("--from-clipboard", action="store_true", help="read the cookie with pbpaste")


def _cookie_target(config, selected: str | None) -> tuple[list[str], str]:
    """Return the Flame ids sharing one cookie file, and that file."""
    flames = [flame for flame in config.flames if flame.keeper == "tushare"]
    if selected is not None:
        chosen = [flame for flame in flames if flame.id == selected]
        if len(chosen) != 1:
            raise ConfigError("--flame must identify a configured Tushare Flame")
        target = chosen[0].settings.get("cookie_file")
    else:
        files = {flame.settings.get("cookie_file") for flame in flames}
        if len(files) != 1:
            raise ConfigError("pass --flame: Tushare Flames are missing or use different cookie_file paths")
        (target,) = files
    if not isinstance(target, str) or not target.startswith("/"):
        raise ConfigError("tushare cookie_file must be an absolute path")
    sharing = [flame.id for flame in flames if flame.settings.get("cookie_file") == target]
    return sharing, target


def _read_input(from_clipboard: bool, stdin=None) -> str:
    stdin = stdin or sys.stdin
    if from_clipboard:
        completed = subprocess.run(["pbpaste"], capture_output=True, timeout=10, check=False)
        if completed.returncode != 0:
            raise CookieError("clipboard is unavailable")
        return completed.stdout.decode("utf-8", "replace")
    if stdin.isatty():
        # Hidden prompt; long cookies are better piped: pbpaste | keepflame tushare import-cookie ...
        return getpass.getpass("Paste the tushare.pro Cookie header (input hidden): ")
    return stdin.read(65537)


def import_cookie(args, *, stdin=None, out=None) -> int:
    out = out or sys.stdout
    config = load_config(args.config)
    flame_ids, cookie_file = _cookie_target(config, args.flame)
    path = Path(cookie_file).expanduser()
    root = _repository_root()
    if root is not None and path.resolve().is_relative_to(root):
        raise ConfigError("tushare cookie_file must be outside the repository")
    raw = _read_input(args.from_clipboard, stdin)
    if len(raw) > 65536:
        raise CookieError("cookie input is too large")
    cookie = parse_import(raw)
    write_file(path, cookie)
    print(json.dumps({
        "event": "tushare_cookie_imported", "flame_ids": flame_ids, "origin": ORIGIN,
        "cookie_file": str(path), "cookie_names": list(cookie.names), "fingerprint": cookie.fingerprint,
        "next": (f"keepflame run-once --config {shlex.quote(str(Path(args.config).resolve()))} --flame {flame_ids[0]}"
                 " --via-daemon --force --execute  (service running; or run-once --execute when it is stopped,"
                 " or wait for the next scheduled recheck)"),
    }, ensure_ascii=False), file=out)
    return 0


def main(args) -> int:
    return import_cookie(args)
