"""Shared HTTP client for all sources.

Provides a single, well-configured httpx client with:
- Realistic User-Agent (identifies us as a crawler for the project)
- Sensible timeouts
- Request rate limiting per-host (so we don't hammer anyone)
- Response caching on disk (so re-running scans doesn't re-fetch)

Usage:
    from watch_app.http import get

    response = get("https://monochrome-watches.com/blog/feed/")
    response.raise_for_status()
    body = response.text

Design notes:
- The cache is a simple on-disk dictionary keyed by URL. Each entry stores
  the response body, status, headers, and timestamp. Entries older than
  CACHE_TTL_SECONDS are re-fetched.
- Rate limiting is per-host: at most one request per MIN_REQUEST_INTERVAL
  seconds to any given host. This is deliberately conservative; tweakable
  if we need to scan faster later.
- We don't use hishel or requests-cache to keep dependencies minimal. The
  cache here is ~40 lines and does exactly what we need.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from watch_app.config import get_settings


logger = logging.getLogger(__name__)


USER_AGENT = (
    "watch-app/0.1 (independent watchmaker discovery; "
    "https://github.com/; non-commercial)"
)

DEFAULT_TIMEOUT = 30.0  # legacy total-budget value, kept for the `timeout` kwarg

# Explicit per-phase timeouts. The previous single-float DEFAULT_TIMEOUT was
# nominally enforced by httpx but in practice could allow individual phases
# to stall well beyond it — observed in a real scheduled scan where a single
# GPHG fetch hung for 17 minutes despite timeout=30. Defining each phase
# explicitly puts a hard ceiling on every operation:
#   - connect: hostname lookup + TCP handshake. Fail fast if DNS or
#     network is down; we'd rather skip the source than wait.
#   - read: time waiting for the next byte from response body. Most
#     legitimately slow sites send at least one byte per second; this
#     value bounds drip-fed/half-dead servers (2W-http-timeout: dropped
#     from 30s to 10s after a scheduled scan saw a GPHG fetch hang for
#     9m 10s because the server sent occasional bytes — the per-byte
#     read timeout never fired even though total time was unreasonable).
#   - write: time to send the request. Tiny GET requests, so very low.
#   - pool: time waiting for a connection from the pool. We don't share
#     pools across calls, so this should be near-instant.
DEFAULT_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=10.0, write=10.0, pool=5.0)
CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours — tune per source later if needed
MIN_REQUEST_INTERVAL = 1.5  # seconds per host


# --- Per-host rate limiting state ---

_host_lock = threading.Lock()
_last_request_at: dict[str, float] = {}


def _rate_limit(url: str) -> None:
    """Block until we're allowed to hit this host again."""
    host = urlparse(url).netloc
    with _host_lock:
        last = _last_request_at.get(host, 0.0)
        now = time.monotonic()
        wait = (last + MIN_REQUEST_INTERVAL) - now
        if wait > 0:
            time.sleep(wait)
        _last_request_at[host] = time.monotonic()


# --- On-disk response cache ---


def _cache_path(url: str) -> Path:
    """Stable cache filename derived from the URL."""
    cache_dir = get_settings().cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return cache_dir / f"{key}.json"


def _read_cache(url: str) -> dict[str, Any] | None:
    path = _cache_path(url)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - data.get("fetched_at", 0) > CACHE_TTL_SECONDS:
        return None
    return data


def _write_cache(url: str, response: httpx.Response) -> None:
    path = _cache_path(url)
    payload = {
        "url": url,
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "text": response.text,
        "fetched_at": time.time(),
    }
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:  # pragma: no cover — disk full etc.
        logger.warning("Failed to write cache for %s: %s", url, exc)


# --- Public API ---


class CachedResponse:
    """A response-like object returned when we hit the cache.

    Mimics the small subset of httpx.Response that callers actually use,
    so upstream code doesn't need to care whether data came from cache or
    the network.
    """

    def __init__(self, data: dict[str, Any]):
        self.status_code: int = data["status_code"]
        self.headers: dict[str, str] = data["headers"]
        self.text: str = data["text"]
        self.url: str = data["url"]
        self.from_cache: bool = True

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code} for {self.url}",
                request=None,  # type: ignore[arg-type]
                response=None,  # type: ignore[arg-type]
            )

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")


def get(
    url: str,
    *,
    use_cache: bool = True,
    timeout: float | httpx.Timeout | None = None,
    user_agent: str | None = None,
) -> httpx.Response | CachedResponse:
    """Fetch a URL, using cache and rate-limiting.

    Returns either an httpx.Response (fresh fetch) or a CachedResponse
    (cache hit). Both support .text, .status_code, .headers, and
    .raise_for_status().

    Use `use_cache=False` to force a fresh fetch (rare — usually you want
    cached).

    `user_agent` overrides the default crawler UA. Useful for sites that
    need a browser-like UA to return full HTML.

    `timeout` defaults to DEFAULT_HTTP_TIMEOUT (per-phase Timeout object).
    A single float, if passed, is interpreted as a total operation budget.
    """
    if use_cache:
        cached = _read_cache(url)
        if cached is not None:
            logger.debug("HTTP cache hit: %s", url)
            return CachedResponse(cached)

    _rate_limit(url)
    logger.info("HTTP fetch: %s", url)
    effective_timeout = timeout if timeout is not None else DEFAULT_HTTP_TIMEOUT
    response = httpx.get(
        url,
        timeout=effective_timeout,
        headers={"User-Agent": user_agent or USER_AGENT, "Accept": "*/*"},
        follow_redirects=True,
    )
    if response.status_code < 400:
        _write_cache(url, response)
    response.from_cache = False  # type: ignore[attr-defined]
    return response
