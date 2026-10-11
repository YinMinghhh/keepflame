"""Keep one profile's no-expiry httpOnly cookies inside that profile.

Chrome drops a cookie that has no Expires and no Max-Age when the browser
process exits. JoinQuant's login cookie is that kind of cookie. This module
records only those httpOnly cookies in the enrolled profile that received
them, adds a bounded expiry, and installs them on the next browser process
for the same profile and origin.

The cookie value is not logged, not returned in errors, and not written
outside the profile. Persistent cookies stay with the browser. Installing a
retained cookie is not proof that the remote service still accepts it.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

STORE_NAME = ".keepflame-session-cookies.json"
RETENTION_SECONDS = 30 * 24 * 60 * 60
_MAX_STORE_BYTES = 16_384
_MAX_COOKIES = 8
_MAX_VALUE_LEN = 4096
_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def records_from_set_cookie(headers: list[str], *, request_url: str, now: float) -> list[dict]:
    """Return installable records for httpOnly cookies that have no expiry."""
    parts = urlsplit(request_url)
    host = parts.hostname
    if parts.scheme != "https" or not host:
        return []
    records = []
    for header in headers:
        record = _record_from_header(header, host=host, now=now)
        if record is not None:
            records.append(record)
    return records


def retain_response_cookies(profile: Path, response, *, request_url: str, account_id: str, origin: str, now: float | None = None) -> None:
    """Record qualifying Set-Cookie headers inside profile. Never raises."""
    try:
        if not _same_origin(request_url, origin):
            return
        headers = _set_cookie_values(response)
        if not headers:
            return
        records = records_from_set_cookie(headers, request_url=request_url, now=time.time() if now is None else now)
        if not records:
            return
        _write_merged(profile, account_id=account_id, origin=origin, records=records, now=time.time() if now is None else now)
    except Exception:
        return


def apply_retained_cookies(context, profile: Path, *, account_id: str, origin: str, now: float | None = None) -> None:
    """Install this profile's retained cookies. Does not read or export them."""
    adder = getattr(context, "add_cookies", None)
    if not callable(adder):
        return
    try:
        records = load_retained_cookies(profile, account_id=account_id, origin=origin, now=time.time() if now is None else now)
        if records:
            adder(records)
    except Exception:
        return


def load_retained_cookies(profile: Path, *, account_id: str, origin: str, now: float | None = None) -> list[dict]:
    """Return unexpired cookies stored for this account and origin, or []."""
    store = _read_store(profile)
    if store is None or store.get("account_id") != account_id or not _same_origin(store.get("origin"), origin):
        return []
    moment = time.time() if now is None else now
    loaded = []
    for record in store.get("cookies", []):
        if isinstance(record, dict) and _installable(record, origin=origin, now=moment):
            loaded.append({key: record[key] for key in record if key in {
                "name", "value", "url", "domain", "path", "expires", "httpOnly", "secure", "sameSite",
            }})
    return loaded


def _record_from_header(header: str, *, host: str, now: float) -> dict | None:
    if not isinstance(header, str) or not header or any(char in header for char in "\r\n"):
        return None
    pieces = [piece.strip() for piece in header.split(";")]
    if not pieces or "=" not in pieces[0]:
        return None
    name, value = pieces[0].split("=", 1)
    name = name.strip()
    value = value.strip().strip('"')
    if not _NAME.fullmatch(name) or not value or len(value) > _MAX_VALUE_LEN:
        return None
    if any(ord(char) < 0x20 or char in " ;," for char in value):
        return None
    attributes: dict[str, str | None] = {}
    for piece in pieces[1:]:
        if not piece:
            continue
        if "=" in piece:
            key, raw = piece.split("=", 1)
            attributes[key.strip().casefold()] = raw.strip()
        else:
            attributes[piece.casefold()] = None
    if "httponly" not in attributes or "expires" in attributes or "max-age" in attributes:
        return None
    path = attributes.get("path") or "/"
    if not isinstance(path, str) or not path.startswith("/") or any(char in path for char in " ?#"):
        return None
    secure = "secure" in attributes
    same_site = _same_site(attributes.get("samesite"))
    if same_site is None or (same_site == "None" and not secure):
        return None
    expires = float(now) + RETENTION_SECONDS
    record = {"name": name, "value": value, "expires": expires, "httpOnly": True, "secure": secure, "sameSite": same_site}
    domain = attributes.get("domain")
    if domain:
        normalized = domain.lstrip(".").rstrip(".").casefold()
        if not normalized or not _domain_matches(host.casefold(), normalized):
            return None
        record["domain"] = normalized
        record["path"] = path
    else:
        record["url"] = f"https://{host}{path}"
    return record


def _same_site(raw: str | None) -> str | None:
    if raw is None:
        return "Lax"
    return {"lax": "Lax", "strict": "Strict", "none": "None"}.get(raw.casefold())


def _domain_matches(host: str, domain: str) -> bool:
    if host == domain:
        return True
    return "." in domain and host.endswith("." + domain)


def _same_origin(url: object, origin: object) -> bool:
    if not isinstance(url, str) or not isinstance(origin, str):
        return False
    left, right = urlsplit(url), urlsplit(origin)
    return left.scheme == "https" and right.scheme == "https" and bool(left.hostname) and left.hostname == right.hostname and left.port == right.port


def _installable(record: dict, *, origin: str, now: float) -> bool:
    name, value, expires = record.get("name"), record.get("value"), record.get("expires")
    if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(value, str) or not value:
        return False
    if isinstance(expires, bool) or not isinstance(expires, (int, float)) or float(expires) <= now:
        return False
    host = urlsplit(origin).hostname
    if not host:
        return False
    url = record.get("url")
    domain = record.get("domain")
    if isinstance(url, str):
        return urlsplit(url).scheme == "https" and urlsplit(url).hostname == host
    return isinstance(domain, str) and _domain_matches(host.casefold(), domain.casefold())


def _set_cookie_values(response) -> list[str]:
    try:
        raw = getattr(response, "headers_array", None)
    except Exception:
        raw = None
    if callable(raw):
        try:
            raw = raw()
        except Exception:
            raw = None
    values = _values_from_array(raw)
    if raw is not None:
        return values
    try:
        headers = getattr(response, "headers", None)
    except Exception:
        return []
    if callable(headers):
        try:
            headers = headers()
        except Exception:
            return []
    if not isinstance(headers, dict):
        return []
    found = headers.get("set-cookie", headers.get("Set-Cookie"))
    if isinstance(found, str):
        return [line for line in found.split("\n") if line]
    if isinstance(found, list):
        return [item for item in found if isinstance(item, str) and item]
    return []


def _values_from_array(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    values = []
    for item in raw:
        if isinstance(item, dict):
            name, value = item.get("name", ""), item.get("value", "")
        else:
            name, value = getattr(item, "name", ""), getattr(item, "value", "")
        if isinstance(name, str) and name.casefold() == "set-cookie" and isinstance(value, str) and value:
            values.append(value)
    return values


def _write_merged(profile: Path, *, account_id: str, origin: str, records: list[dict], now: float) -> None:
    current = _read_store(profile)
    kept: list[dict] = []
    if current is not None and current.get("account_id") == account_id and _same_origin(current.get("origin"), origin):
        kept = [record for record in current.get("cookies", []) if isinstance(record, dict) and _installable(record, origin=origin, now=now)]
    merged: dict[tuple, dict] = {}
    for record in [*kept, *records]:
        merged[_identity(record)] = record
    cookies = list(merged.values())[-_MAX_COOKIES:]
    payload = {"version": 1, "account_id": account_id, "origin": origin, "cookies": cookies}
    _atomic_write(profile, payload)


def _identity(record: dict) -> tuple:
    return (record.get("name"), record.get("url"), record.get("domain"), record.get("path"))


def _read_store(profile: Path) -> dict | None:
    target = profile / STORE_NAME
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(target, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            return None
        if info.st_mode & 0o077 or info.st_size > _MAX_STORE_BYTES:
            return None
        raw = os.read(fd, info.st_size)
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("ascii"))
    except (UnicodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("cookies"), list):
        return None
    if not isinstance(data.get("account_id"), str) or not isinstance(data.get("origin"), str):
        return None
    return data


def _atomic_write(profile: Path, payload: dict) -> None:
    try:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        return
    if len(data) > _MAX_STORE_BYTES:
        return
    temporary = profile / f".{STORE_NAME}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temporary, flags, 0o600)
    except OSError:
        return
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            return
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, profile / STORE_NAME)
        os.chmod(profile / STORE_NAME, 0o600)
        # The directory entry has to survive a kill after this call returns.
        directory = os.open(profile, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        return
