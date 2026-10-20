"""Tushare session cookie file: parsing, normalization and private atomic writes.

The cookie is bound to one fixed origin (https://tushare.pro); there is no domain
discovery. Error messages never include cookie values.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

ORIGIN = "https://tushare.pro"
COOKIE_HOST = "tushare.pro"
MAX_COOKIE_CHARS = 8192
# RFC 6265: cookie-value = *cookie-octet / ( DQUOTE *cookie-octet DQUOTE ). A wholly quoted
# value (Tushare's uid="...") is accepted and kept verbatim, quotes included, when sent.
_PAIR = re.compile(r'^[!#$%&\'*+\-.^_`|~0-9A-Za-z]+=(?:"[^;\s,\\"]*"|[^;\s,\\"]*)$')


class CookieError(ValueError):
    """The cookie input or file is unusable. Messages never include cookie values."""


@dataclass(frozen=True, slots=True)
class SessionCookie:
    header: str

    @property
    def fingerprint(self) -> str:
        """Short one-way tag to notice a re-import without storing the cookie."""
        return hashlib.sha256(self.header.encode("utf-8")).hexdigest()[:16]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(pair.split("=", 1)[0] for pair in self.header.split("; "))


def normalize_header(raw: str) -> str:
    text = raw.strip()
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    if not text or len(text) > MAX_COOKIE_CHARS or any(ch in text for ch in "\r\n\x00"):
        raise CookieError("cookie must be one non-empty header line")
    pairs = [part.strip() for part in text.split(";") if part.strip()]
    if not pairs or not all(_PAIR.match(pair) for pair in pairs):
        raise CookieError("cookie must be name=value pairs separated by semicolons")
    return "; ".join(pairs)


def _domain_allowed(domain: str) -> bool:
    host = domain.strip(". ").lower()
    return host == COOKIE_HOST or host.endswith("." + COOKIE_HOST)


def parse_import(raw: str) -> SessionCookie:
    """Accept a Cookie header line, or a browser-extension JSON export (list of cookies).

    JSON entries for other domains are dropped so a whole-browser export cannot leak
    unrelated cookies into the file; at least one tushare.pro cookie must remain.
    """
    text = raw.strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
        except ValueError:
            raise CookieError("cookie JSON export is not valid JSON") from None
        if not isinstance(items, list) or not items:
            raise CookieError("cookie JSON export must be a non-empty list")
        pairs = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("value"), str):
                raise CookieError("cookie JSON entries need string name and value")
            domain = item.get("domain")
            if isinstance(domain, str) and domain.strip(". ") and not _domain_allowed(domain):
                continue
            pairs.append(f"{item['name']}={item['value']}")
        if not pairs:
            raise CookieError("cookie JSON export holds no tushare.pro cookies")
        return SessionCookie(normalize_header("; ".join(pairs)))
    return SessionCookie(normalize_header(text))


def load_file(text: str) -> SessionCookie:
    """Read the stored file: KeepFlame JSON ({origin, cookie}) or a raw header line."""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            raise CookieError("cookie file is not valid JSON") from None
        if not isinstance(data, dict) or not isinstance(data.get("cookie"), str):
            raise CookieError("cookie file needs a cookie string")
        origin = data.get("origin", ORIGIN)
        if origin != ORIGIN:
            raise CookieError("cookie file origin must be https://tushare.pro")
        return SessionCookie(normalize_header(data["cookie"]))
    return SessionCookie(normalize_header(stripped))


def write_file(path: Path, cookie: SessionCookie) -> None:
    """Atomically replace ``path`` with a 0600 file owned by this user."""
    if not path.is_absolute():
        raise CookieError("cookie_file must be an absolute path")
    directory = path.parent
    if not directory.is_dir():
        raise CookieError("cookie_file directory does not exist")
    if path.is_symlink():
        raise CookieError("cookie_file must not be a symlink")
    payload = json.dumps({"version": 1, "origin": ORIGIN, "cookie": cookie.header},
                         ensure_ascii=False).encode("utf-8") + b"\n"
    old_umask = os.umask(0o077)
    try:
        fd, temporary = tempfile.mkstemp(prefix=".tushare-cookie-", dir=directory)
    finally:
        os.umask(old_umask)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
