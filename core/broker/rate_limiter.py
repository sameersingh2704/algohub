"""
API Rate Limiter
=================
Token-bucket rate limiter for Angel One SmartAPI.
Enforces max N requests per second to prevent 429 errors.
"""

from __future__ import annotations

import asyncio
import time
import logging

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Async token-bucket rate limiter.

    Usage:
        limiter = RateLimiter(requests_per_second=5)
        await limiter.acquire()  # Block if rate exceeded
        # ... make API call
    """

    def __init__(self, requests_per_second: float = 5.0) -> None:
        self._rate = requests_per_second
        self._min_interval = 1.0 / requests_per_second
        self._last_call_time = 0.0
        self._lock = asyncio.Lock()
        self._request_count = 0
        self._window_start = time.monotonic()

    async def acquire(self) -> None:
        """Wait if necessary to stay within rate limit."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call_time
            wait_time = self._min_interval - elapsed

            if wait_time > 0:
                logger.debug(f"Rate limit: waiting {wait_time*1000:.1f}ms")
                await asyncio.sleep(wait_time)

            self._last_call_time = time.monotonic()
            self._request_count += 1

            # Log rate every 100 requests
            if self._request_count % 100 == 0:
                window_seconds = time.monotonic() - self._window_start
                effective_rate = self._request_count / window_seconds if window_seconds > 0 else 0
                logger.debug(f"API rate: {effective_rate:.1f} req/sec over last {window_seconds:.0f}s")
