"""Find the current ikuuu login domain from navigation seed pages, read-only."""

from __future__ import annotations

import base64
import binascii
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from keepflame.capabilities.http import HttpClient, HttpRequest, HttpTransportError
from keepflame.flames.ikuuu.jsdecode import DecodeError, decoded_strings

DEFAULT_NAV_SEEDS = ("https://ikuuu.bar/",)
DEFAULT_HOST_PATTERN = r"^ikuuu\.[a-z]{2,24}$"
LOGIN_PATH = "/auth/login"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")
_HOST_TEXT = re.compile(r"(?<![\w.-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![\w-])", re.I)
_SCRIPT = re.compile(r"<script[^>]*>(.*?)</script>", re.S | re.I)
_ORIGIN_BODY = re.compile(r"originBody\s*=\s*\"([A-Za-z0-9+/=]{16,})\"")


class DomainSource(Protocol):
    """Pluggable extra source of candidate login hosts (e.g. an email auto-reply reader).

    ``candidates`` returns hostnames or base URLs; every candidate is still
    probed for a real login form before use. Implementations must be read-only.
    """

    name: str

    def candidates(self) -> Sequence[str]: ...


class EmailReplySource:
    """Placeholder for the find@ikuuu.pro auto-reply fallback.

    Sending or reading mail is intentionally not implemented. A future IMAP
    reader can feed the reply body to :func:`hosts_in_text` and return the hosts.
    """

    name = "email_reply"

    def __init__(self, fetch_reply_text: Callable[[], str | None] | None = None) -> None:
        self.fetch_reply_text = fetch_reply_text

    def candidates(self) -> Sequence[str]:
        if self.fetch_reply_text is None:
            return ()
        text = self.fetch_reply_text()
        return hosts_in_text(text or "")


@dataclass(frozen=True, slots=True)
class Discovery:
    base_url: str | None
    candidates: tuple[str, ...]
    probed: tuple[str, ...]
    seeds_reached: int
    methods: tuple[str, ...] = field(default=())


def normalize_base(value: str, host_pattern: str = DEFAULT_HOST_PATTERN) -> str | None:
    """https://host for an allowed bare host or URL; None for anything else."""
    value = value.strip()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port is not None or parts.username or parts.password:
        return None
    if not re.fullmatch(host_pattern, host):
        return None
    return f"https://{host}"


def hosts_in_text(text: str) -> list[str]:
    seen: list[str] = []
    for match in _HOST_TEXT.finditer(text):
        host = match.group(1).lower()
        if host not in seen:
            seen.append(host)
    return seen


def hosts_in_page(html: str) -> tuple[list[str], list[str]]:
    """Hosts from decoded obfuscated scripts first, then from plain page text."""
    hosts: list[str] = []
    methods: list[str] = []
    for script in _SCRIPT.findall(html):
        try:
            strings = decoded_strings(script)
        except DecodeError:
            continue
        found = [host for value in strings for host in hosts_in_text(value)]
        if found:
            methods.append("obfuscated_script")
        hosts.extend(host for host in found if host not in hosts)
    plain = [host for host in hosts_in_text(html) if host not in hosts]
    if plain:
        methods.append("plain_text")
    return hosts + plain, methods


def looks_like_login(body: bytes) -> bool:
    """True for the panel login form, including its base64 document.write wrapper."""
    text = body.decode("utf-8", "replace")
    match = _ORIGIN_BODY.search(text)
    if match:
        try:
            text = base64.b64decode(match.group(1)).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return False
    lowered = text.lower()
    return ('name="email"' in lowered and ('name="password"' in lowered or 'name="passwd"' in lowered)
            and ("/auth/login" in lowered or "登录" in text))


class DomainFinder:
    def __init__(self, http: HttpClient, *, seeds: Sequence[str] = DEFAULT_NAV_SEEDS,
                 host_pattern: str = DEFAULT_HOST_PATTERN, max_probes: int = 8,
                 timeout_seconds: float = 15.0, pause_seconds: float = 1.0,
                 sleeper: Callable[[float], None] = time.sleep,
                 extra_sources: Iterable[DomainSource] = ()) -> None:
        self.http = http
        self.seeds = tuple(seeds)
        self.host_pattern = host_pattern
        self.max_probes = max_probes
        self.timeout_seconds = timeout_seconds
        self.pause_seconds = pause_seconds
        self.sleeper = sleeper
        self.extra_sources = tuple(extra_sources)
        self._requests = 0

    def _get(self, url: str):
        if self._requests:
            self.sleeper(self.pause_seconds)  # browser-like pacing between requests
        self._requests += 1
        return self.http.send(HttpRequest("GET", url, headers={
            "User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }), timeout_seconds=self.timeout_seconds)

    def probe(self, base_url: str) -> bool:
        """GET {base}/auth/login and require the real login form on that same host."""
        try:
            response = self._get(base_url + LOGIN_PATH)
        except HttpTransportError:
            return False
        if response.status != 200:
            return False
        try:
            final = urlsplit(response.url)
        except ValueError:
            return False
        if f"https://{(final.hostname or '').lower()}" != base_url:
            return False
        return looks_like_login(response.body)

    def candidates(self) -> tuple[list[str], int, list[str]]:
        found: list[str] = []
        methods: list[str] = []
        reached = 0
        for seed in self.seeds:
            try:
                response = self._get(seed)
            except HttpTransportError:
                continue
            if response.status != 200:
                continue
            reached += 1
            hosts, page_methods = hosts_in_page(response.body.decode("utf-8", "replace"))
            methods.extend(m for m in page_methods if m not in methods)
            for host in hosts:
                base = normalize_base(host, self.host_pattern)
                if base and base not in found and base != normalize_base(seed, self.host_pattern):
                    found.append(base)
        for source in self.extra_sources:
            try:
                extra = source.candidates()
            except Exception:
                continue
            for value in extra:
                base = normalize_base(value, self.host_pattern)
                if base and base not in found:
                    found.append(base)
                    if source.name not in methods:
                        methods.append(source.name)
        return found, reached, methods

    def discover(self, preferred: Sequence[str] = ()) -> Discovery:
        probed: list[str] = []
        for value in preferred:
            base = normalize_base(value, self.host_pattern)
            if base and base not in probed and len(probed) < self.max_probes:
                probed.append(base)
                if self.probe(base):
                    return Discovery(base, (), tuple(probed), 0, ("known",))
        found, reached, methods = self.candidates()
        for base in found:
            if base in probed:
                continue
            if len(probed) >= self.max_probes:
                break
            probed.append(base)
            if self.probe(base):
                return Discovery(base, tuple(found), tuple(probed), reached, tuple(methods))
        return Discovery(None, tuple(found), tuple(probed), reached, tuple(methods))
