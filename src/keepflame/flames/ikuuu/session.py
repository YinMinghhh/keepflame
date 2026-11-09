"""ikuuu login-document classification and private credential file.

The public login document decides whether a password may be submitted. A
challenge marker stops the run. Nothing in this module solves a captcha,
talks to a challenge service, or reads a browser cookie database.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

from keepflame.capabilities.http import HttpResponse
from keepflame.flames.ikuuu.discovery import DEFAULT_HOST_PATTERN, rendered_document

# Hosts named by the public /gt4.js fetched read-only on 2026-11-09.
# Used only after the login document itself references GeeTest.
CAPTCHA_ASSET_HOSTS = (
    "gcaptcha4.geetest.com",
    "gcaptcha4.geevisit.com",
    "gcaptcha4.gsensebot.com",
    "monitor.geetest.com",
    "static.geetest.com",
    "static.geevisit.com",
)
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_ASSET = re.compile(r"<(?:script|link)\b[^>]+(?:src|href)\s*=\s*['\"]https://([^/'\"\s]+)", re.I)
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}$")
_CHALLENGE_GROUPS = (
    ("geetest", ("/gt4.js", "initgeetest", "geetest", "gcaptcha4", "geevisit")),
    ("captcha", ("recaptcha", "hcaptcha", "turnstile", "captchaid")),
    ("mfa", ("2fa-code", "二步验证", "两步验证", "login-email-code", "reverse-email-verify")),
)
_LOGIN_CHALLENGE = ("验证结果", "二步", "两步", "验证码", "geetest")
_LOGIN_REJECTED = ("邮箱或者密码", "密码错误")


class CredentialError(ValueError):
    """Credential input is unusable. Messages never include the email or password."""


@dataclass(frozen=True, slots=True)
class Credentials:
    email: str
    password: str
    fingerprint: str


def public_host(host: str) -> bool:
    if not _HOST.fullmatch(host):
        return False
    labels = host.split(".")
    return not all(label.isdigit() for label in labels)


def challenge_markers(body: bytes) -> tuple[str, ...]:
    """Fixed labels for challenge widgets present in the login document.

    The decoded document is not returned. An undecodable wrapper is itself a
    reason to stop, because the form cannot be shown to be challenge-free.
    """
    text = rendered_document(body)
    if text is None:
        return ("unreadable_document",)
    lowered = text.lower()
    found = []
    for label, needles in _CHALLENGE_GROUPS:
        if any(needle in lowered or needle in text for needle in needles):
            found.append(label)
    return tuple(found)


def extra_hosts(body: bytes) -> tuple[str, ...]:
    """HTTPS asset hosts the login document itself references, plus GeeTest hosts when marked."""
    text = rendered_document(body) or ""
    found: list[str] = []
    for match in _ASSET.finditer(text):
        host = match.group(1).lower().rstrip(".")
        if public_host(host) and host not in found:
            found.append(host)
        if len(found) >= 8:
            break
    if "geetest" in challenge_markers(body):
        for host in CAPTCHA_ASSET_HOSTS:
            if host not in found:
                found.append(host)
    return tuple(found[:16])


def redirect_evidence(base: str, location: str, *, host_pattern: str = DEFAULT_HOST_PATTERN) -> dict[str, str]:
    """Path of a redirect. The query string is dropped. An unrelated host is not stored."""
    target = urlsplit(urljoin(base + "/user/checkin", location))
    path = target.path if target.path.startswith("/") and len(target.path) <= 80 else "/"
    data = {"evidence": "redirect", "redirect_path": path}
    host = (target.hostname or "").lower()
    request_host = (urlsplit(base).hostname or "").lower()
    if host and host != request_host:
        data["redirect_host"] = host if re.fullmatch(host_pattern, host) else "other"
    return data


def login_form(body: bytes, credentials: Credentials) -> bytes:
    """Public email/password fields only. Call this only when ``challenge_markers`` is empty."""
    text = rendered_document(body) or ""
    fields = {"email": credentials.email, "password": credentials.password}
    if re.search(r"name\s*=\s*['\"]remember['\"]", text, re.I):
        fields["remember"] = "on"
    elif re.search(r"name\s*=\s*['\"]remember_me['\"]", text, re.I):
        fields["remember_me"] = "true"
    return urlencode(fields).encode("utf-8")


def classify_login(response: HttpResponse, base: str) -> str:
    """``accepted``, ``rejected``, ``challenge`` or ``unexpected``. No body is copied out."""
    for header in ("location", "hx-redirect"):
        value = response.header(header) or ""
        if not value:
            continue
        path = urlsplit(urljoin(base + "/auth/login", value)).path or ""
        if path.startswith("/user"):
            return "accepted"
        if path.startswith("/auth"):
            return "challenge"
    text = response.body.decode("utf-8", "replace")
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        message = payload.get("msg") if isinstance(payload.get("msg"), str) else ""
        if any(token in message.lower() or token in message for token in _LOGIN_CHALLENGE):
            return "challenge"
        if any(token in message for token in _LOGIN_REJECTED):
            return "rejected"
        if payload.get("ret") == 1:
            return "accepted"
    return "unexpected"


def parse_credentials(text: str) -> Credentials:
    try:
        data = json.loads(text)
    except ValueError:
        raise CredentialError("credential file is not valid JSON") from None
    if not isinstance(data, dict):
        raise CredentialError("credential file is not an object")
    email, password = data.get("email"), data.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        raise CredentialError("credential file needs email and password strings")
    email = email.strip().lower()
    if (not _EMAIL.fullmatch(email) or not password or len(password) > 256
            or any(char in password for char in "\r\n\x00")):
        raise CredentialError("credential file fields are not usable")
    fingerprint = hashlib.sha256(f"{email}\0{password}".encode("utf-8")).hexdigest()[:16]
    return Credentials(email, password, fingerprint)


def write_credentials(path: Path, credentials: Credentials) -> None:
    """Atomically replace ``path`` with a mode 0600 credential file."""
    if not path.is_absolute():
        raise CredentialError("credential file must be an absolute path")
    directory = path.parent
    if not directory.is_dir():
        raise CredentialError("credential file directory does not exist")
    if path.is_symlink():
        raise CredentialError("credential file must not be a symlink")
    payload = (json.dumps(
        {"version": 1, "email": credentials.email, "password": credentials.password},
        ensure_ascii=False,
    ).encode("utf-8") + b"\n")
    old_umask = os.umask(0o077)
    try:
        fd, temporary = tempfile.mkstemp(prefix=".ikuuu-auth-", dir=directory)
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
