"""Shared plumbing for source adapters: rate limiting, robots.txt, caching,
retries, and per-source health bookkeeping.

Adapters split into pure `parse_*` functions (tested against fixtures) and thin
`fetch_*` functions that use HttpClient.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class SourceError(Exception):
    """A source failed (outage, bad response, blocked). Callers record and continue."""


class NotFound(SourceError):
    """The resource does not exist (404, delisted ticker, empty result)."""


class RateLimiter:
    """Minimum interval between requests, per key (usually host). Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, key: str, interval: float) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next.get(key, 0.0))
            self._next[key] = start + interval
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)


RATE_LIMITER = RateLimiter()


class RobotsGuard:
    def __init__(self, client: httpx.Client, user_agent: str, ttl: float = 86400) -> None:
        self._client = client
        self._ua = user_agent
        self._ttl = ttl
        self._cache: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            hit = self._cache.get(base)
        if hit is None or time.monotonic() - hit[0] > self._ttl:
            rp: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
            try:
                r = self._client.get(base + "/robots.txt", timeout=15)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp = None  # no robots.txt (or error page) -> allowed
            except httpx.HTTPError:
                rp = None
            hit = (time.monotonic(), rp)
            with self._lock:
                self._cache[base] = hit
        rp = hit[1]
        return True if rp is None else rp.can_fetch(self._ua, url)


class HttpClient:
    """Polite HTTP GET with file cache, retries/backoff and robots.txt checks."""

    def __init__(
        self,
        source: str,
        transport: httpx.BaseTransport | None = None,
        cache_dir: Path | None = None,
        rate_limit: float | None = None,
        retries: int = 3,
        backoff: float = 2.0,
        check_robots: bool = True,
    ) -> None:
        s = get_settings()
        self.source = source
        self.rate_limit = s.rate_limit_seconds if rate_limit is None else rate_limit
        self.retries = retries
        self.backoff = backoff
        self.cache_dir = cache_dir if cache_dir is not None else s.cache_dir
        self.client = httpx.Client(
            headers={"User-Agent": s.user_agent, "Accept-Language": "en-AU,en;q=0.9"},
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True,
            transport=transport,
        )
        self.robots = RobotsGuard(self.client, s.user_agent) if check_robots else None

    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha256(url.encode()).hexdigest()
        return self.cache_dir / self.source / h[:2] / h

    def get(self, url: str, cache_ttl: float = 0, params: dict | None = None) -> bytes:
        if params:
            url = str(httpx.URL(url, params=params))
        cpath = self._cache_path(url)
        if cache_ttl > 0 and cpath.exists() and time.time() - cpath.stat().st_mtime < cache_ttl:
            return cpath.read_bytes()
        if self.robots and not self.robots.allowed(url):
            raise SourceError(f"robots.txt disallows {url}")

        host = urlsplit(url).netloc
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            RATE_LIMITER.wait(host, self.rate_limit)
            try:
                r = self.client.get(url)
            except httpx.HTTPError as e:
                last_exc = SourceError(f"{type(e).__name__}: {e}")
            else:
                if r.status_code == 200:
                    if cache_ttl > 0:
                        cpath.parent.mkdir(parents=True, exist_ok=True)
                        tmp = cpath.with_suffix(".tmp")
                        tmp.write_bytes(r.content)
                        tmp.replace(cpath)
                    return r.content
                if r.status_code in (404, 410):
                    raise NotFound(f"{r.status_code} {url}")
                if r.status_code in (429, 500, 502, 503, 504):
                    last_exc = SourceError(f"HTTP {r.status_code} {url}")
                    retry_after = r.headers.get("Retry-After", "")
                    if retry_after.isdigit():
                        time.sleep(min(int(retry_after), 120))
                else:
                    raise SourceError(f"HTTP {r.status_code} {url}")
            if attempt < self.retries - 1:
                time.sleep(self.backoff * (2**attempt))
        # Serve stale cache rather than nothing during an outage.
        if cpath.exists():
            log.warning("%s: using stale cache for %s (%s)", self.source, url, last_exc)
            return cpath.read_bytes()
        raise last_exc or SourceError(f"failed {url}")

    def close(self) -> None:
        self.client.close()
