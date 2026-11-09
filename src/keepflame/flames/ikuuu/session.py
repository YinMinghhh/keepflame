"""ikuuu login-document classification.

A missing or undecodable document is a challenge, not permission to submit a
password. This module does not store passwords, solve captchas, or read a
browser cookie database.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

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
_CHALLENGE_GROUPS = (
    ("geetest", ("/gt4.js", "initgeetest", "geetest", "gcaptcha4", "geevisit")),
    ("captcha", ("recaptcha", "hcaptcha", "turnstile", "captchaid")),
    ("mfa", ("2fa-code", "二步验证", "两步验证", "login-email-code", "reverse-email-verify")),
)


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
