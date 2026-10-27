"""One shared async HTTP client: connection pooling, HTTP/2, per-host concurrency limits,
retries with exponential backoff (honours Retry-After), and conditional GETs (ETag)."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger("jobagent.http")
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass(slots=True)
class FetchResult:
    status: int
    data: Any = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    error: str | None = None
    elapsed_ms: int = 0
    nbytes: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None and (self.not_modified or 200 <= self.status < 300)


class HttpClient:
    def __init__(self, max_concurrency: int = 24, per_host: int = 6, timeout: float = 20.0,
                 max_retries: int = 4, user_agent: str = "riya-job-agent/0.1"):
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(timeout, connect=10.0),
            limits=httpx.Limits(max_connections=max_concurrency, max_keepalive_connections=max_concurrency),
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=True,
        )
        self._global = asyncio.Semaphore(max_concurrency)
        self._per_host_limit = per_host
        self._hosts: dict[str, asyncio.Semaphore] = {}
        self.max_retries = max_retries
        self.stats = {"requests": 0, "retries": 0, "errors": 0, "not_modified": 0, "bytes": 0}

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    def _host_sem(self, url: str) -> asyncio.Semaphore:
        host = urlparse(url).netloc
        if host not in self._hosts:
            self._hosts[host] = asyncio.Semaphore(self._per_host_limit)
        return self._hosts[host]

    async def get_json(self, url: str, *, params: dict | None = None, etag: str | None = None,
                       last_modified: str | None = None, expect_404: bool = False) -> FetchResult:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        attempt = 0
        loop = asyncio.get_running_loop()
        while True:
            attempt += 1
            start = loop.time()
            try:
                # Host slot first, so a busy host never holds global slots while it waits.
                async with self._host_sem(url), self._global:
                    self.stats["requests"] += 1
                    resp = await self._client.get(url, params=params, headers=headers)
                elapsed = int((loop.time() - start) * 1000)
                if resp.status_code == 304:
                    self.stats["not_modified"] += 1
                    return FetchResult(304, not_modified=True, etag=etag, last_modified=last_modified, elapsed_ms=elapsed)
                if resp.status_code in RETRY_STATUS and attempt <= self.max_retries:
                    delay = self._retry_delay(resp, attempt)
                    self.stats["retries"] += 1
                    log.debug("HTTP %s from %s, retry %d in %.1fs", resp.status_code, url, attempt, delay)
                    await asyncio.sleep(delay)
                    continue
                if resp.status_code >= 400:
                    if not (expect_404 and resp.status_code == 404):
                        self.stats["errors"] += 1
                    return FetchResult(resp.status_code, error=f"HTTP {resp.status_code}", elapsed_ms=elapsed)
                body = resp.content
                self.stats["bytes"] += len(body)
                try:
                    data = resp.json()
                except ValueError:
                    return FetchResult(resp.status_code, error="invalid JSON", elapsed_ms=elapsed, nbytes=len(body))
                return FetchResult(resp.status_code, data=data, etag=resp.headers.get("ETag"),
                                   last_modified=resp.headers.get("Last-Modified"), elapsed_ms=elapsed,
                                   nbytes=len(body))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt <= self.max_retries:
                    self.stats["retries"] += 1
                    delay = min(30.0, 2 ** attempt + random.random())
                    log.debug("%s on %s, retry %d in %.1fs", type(exc).__name__, url, attempt, delay)
                    await asyncio.sleep(delay)
                    continue
                self.stats["errors"] += 1
                return FetchResult(0, error=f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                return min(60.0, float(ra))
            except ValueError:
                pass
        return min(30.0, 2 ** attempt + random.random())
