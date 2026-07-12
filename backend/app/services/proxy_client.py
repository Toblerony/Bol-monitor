"""Residential proxy pool — rotate on block/429/timeout (Bol.com)."""
from __future__ import annotations

import logging
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.request import OpenerDirector, Request

logger = logging.getLogger(__name__)

BLOCK_MARKERS = (
    "access denied",
    "ip-geblokkeerd",
    "geblokkeerd",
    "captcha",
    "blocked",
    "forbidden",
    "too many requests",
)


@dataclass
class ParsedProxy:
    raw: str
    host: str
    port: str
    username: str
    password: str
    label: str = ""

    @property
    def urllib_url(self) -> str:
        return f"http://{self.username}:{self.password}@{self.host}:{self.port}"

    def opener(self) -> OpenerDirector:
        handler = urllib.request.ProxyHandler({"http": self.urllib_url, "https": self.urllib_url})
        ctx = ssl.create_default_context()
        return urllib.request.build_opener(
            handler,
            urllib.request.HTTPSHandler(context=ctx),
        )


def parse_proxy_line(line: str) -> ParsedProxy | None:
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return None
    if "://" in s:
        s = s.split("://", 1)[1]
    if "@" in s:
        auth, hostpart = s.rsplit("@", 1)
        if ":" in auth:
            user, password = auth.split(":", 1)
        else:
            user, password = auth, ""
        if ":" in hostpart:
            host, port = hostpart.rsplit(":", 1)
        else:
            host, port = hostpart, "80"
        return ParsedProxy(raw=line.strip(), host=host, port=port, username=user, password=password)

    parts = s.split(":")
    if len(parts) < 4:
        return None
    host, port, user = parts[0], parts[1], parts[2]
    password = ":".join(parts[3:])
    return ParsedProxy(
        raw=line.strip(),
        host=host,
        port=port,
        username=user,
        password=password,
        label=f"{host}:{port}",
    )


def parse_proxy_lines(text: str) -> list[ParsedProxy]:
    out: list[ParsedProxy] = []
    for line in (text or "").splitlines():
        p = parse_proxy_line(line)
        if p:
            out.append(p)
    return out


class ProxyPool:
    """Round-robin pool — skip recently failed proxies, rotate on block."""

    def __init__(self, proxies: list[ParsedProxy]):
        self._all = list(proxies)
        self._idx = 0
        self._failed_until: dict[str, float] = {}
        self._lock = threading.Lock()
        self._cooldown_sec = 120.0

    @property
    def enabled(self) -> bool:
        return bool(self._all)

    @property
    def count(self) -> int:
        return len(self._all)

    def _available(self) -> list[ParsedProxy]:
        now = time.time()
        return [p for p in self._all if self._failed_until.get(p.raw, 0) <= now]

    def mark_failed(self, proxy: ParsedProxy, reason: str = "") -> None:
        with self._lock:
            self._failed_until[proxy.raw] = time.time() + self._cooldown_sec
        logger.warning("Proxy marked failed (%s): %s", reason or "error", proxy.label or proxy.host)

    def mark_ok(self, proxy: ParsedProxy) -> None:
        with self._lock:
            self._failed_until.pop(proxy.raw, None)

    def next(self) -> ParsedProxy | None:
        with self._lock:
            avail = self._available()
            if not avail:
                self._failed_until.clear()
                avail = list(self._all)
            if not avail:
                return None
            proxy = avail[self._idx % len(avail)]
            self._idx += 1
            return proxy


_pool_singleton: ProxyPool | None = None
_pool_lock = threading.Lock()
_pool_source = ""


def get_proxy_pool(proxy_lines: str, use_proxies: bool) -> ProxyPool | None:
    global _pool_singleton, _pool_source
    if not use_proxies:
        return None
    text = proxy_lines or ""
    with _pool_lock:
        if _pool_singleton is not None and _pool_source == text:
            return _pool_singleton
        proxies = parse_proxy_lines(text)
        _pool_singleton = ProxyPool(proxies) if proxies else None
        _pool_source = text
        return _pool_singleton


def invalidate_proxy_pool() -> None:
    global _pool_singleton, _pool_source
    with _pool_lock:
        _pool_singleton = None
        _pool_source = ""


def is_blocked_response(status: int, body: str = "") -> bool:
    if status in (403, 429, 451):
        return True
    bl = (body or "").lower()[:80000]
    return any(m in bl for m in BLOCK_MARKERS)


def fetch_url(
    url: str,
    *,
    pool: ProxyPool | None,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
    max_attempts: int | None = None,
) -> tuple[int, bytes, str]:
    """Returns (status, body_bytes, error_message). Rotates proxy on failure."""
    attempts = max_attempts or (max(3, pool.count) if pool and pool.enabled else 1)
    last_err = ""

    for _ in range(attempts):
        proxy = pool.next() if pool and pool.enabled else None
        opener = (
            proxy.opener()
            if proxy
            else urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        )
        req = Request(url, headers=headers or {}, method="GET")
        try:
            with opener.open(req, timeout=timeout) as resp:
                code = int(getattr(resp, "status", 200) or 200)
                body = resp.read(1_500_000)
            text = body.decode("utf-8", errors="ignore")
            if is_blocked_response(code, text):
                last_err = f"blocked HTTP {code}"
                if proxy:
                    pool.mark_failed(proxy, last_err)
                continue
            if proxy:
                pool.mark_ok(proxy)
            return code, body, ""
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code}"
            try:
                body = exc.read(50000).decode("utf-8", errors="ignore")
            except Exception:
                body = ""
            if proxy and (exc.code in (403, 429, 451) or is_blocked_response(exc.code, body)):
                pool.mark_failed(proxy, last_err)
                continue
            return int(exc.code), b"", last_err
        except Exception as exc:
            last_err = str(exc)
            if proxy:
                pool.mark_failed(proxy, last_err)
            continue

    return 0, b"", last_err or "all proxies failed"


def test_proxy(
    proxy: ParsedProxy,
    test_url: str = "https://www.bol.com/nl/nl/",
    timeout: float = 25.0,
) -> tuple[bool, str]:
    code, body, err = fetch_url(
        test_url,
        pool=ProxyPool([proxy]),
        timeout=timeout,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        max_attempts=1,
    )
    if err:
        return False, err
    if code >= 400:
        return False, f"HTTP {code}"
    text = body.decode("utf-8", errors="ignore").lower()
    if is_blocked_response(code, text):
        return False, "blocked / IP banned"
    if "bol.com" in text or code == 200:
        return True, f"OK HTTP {code}"
    return True, "connected (verify manually)"


# Backwards-compatible helpers used by sitemap_monitor file loader
def read_proxy_lines(path: Any) -> list[str]:
    from pathlib import Path

    p = Path(path) if path else None
    if not p or not p.is_file():
        return []
    lines: list[str] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            lines.append(s)
    return lines
