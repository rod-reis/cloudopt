"""Adaptive rate limiter with exponential backoff for Azure API calls.

Two independent limits are enforced per subscription:

* **Concurrency** — bounded by an :class:`asyncio.Semaphore`.
* **Throughput** — bounded by a token-bucket rate limiter expressed in
  *requests per second*.  This is the primary control: ARM enforces a
  per-subscription read budget (~12,000 reads / hour ≈ 3.3 rps steady,
  with bursts up to 250).  We default to a conservative 20 rps to stay
  well within the burst envelope while still being orders of magnitude
  faster than the previous purely-concurrency-based limiter.

References:
* https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/request-limits-and-throttling
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict


# ---------------------------------------------------------------------------
# Token-bucket rate limiter
# ---------------------------------------------------------------------------


class _TokenBucket:
    """Simple monotonic-clock token bucket.

    ``rate`` tokens are added per second up to ``capacity``.  ``acquire()``
    blocks until at least one token is available, then consumes it.
    """

    __slots__ = ("_rate", "_capacity", "_tokens", "_last", "_lock")

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self._rate = max(0.1, float(rate))
        self._capacity = float(capacity if capacity is not None else max(rate, 1.0))
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                if elapsed > 0:
                    self._tokens = min(
                        self._capacity, self._tokens + elapsed * self._rate
                    )
                    self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait = deficit / self._rate
            await asyncio.sleep(wait)

    def adjust(self, new_rate: float) -> None:
        """Replace the steady-state rate (used after a 429)."""
        self._rate = max(0.1, float(new_rate))
        # Keep capacity unchanged so bursts still drain the same way.


# ---------------------------------------------------------------------------
# Throttle manager (concurrency + rate)
# ---------------------------------------------------------------------------


class ThrottleManager:
    """Per-subscription concurrency + token-bucket throughput limiter.

    Args:
        max_concurrency:  Maximum simultaneous in-flight requests per
                          subscription.  Acts as a hard ceiling.
        rate_per_second:  Steady-state token replenishment rate per
                          subscription (the dominant control).
        burst:            Optional burst capacity.  Defaults to
                          ``rate_per_second`` (one second of headroom).
    """

    def __init__(
        self,
        max_concurrency: int = 50,
        rate_per_second: float = 20.0,
        burst: float | None = None,
    ) -> None:
        self._max = max(1, int(max_concurrency))
        self._rate = max(0.1, float(rate_per_second))
        self._burst = float(burst) if burst is not None else self._rate
        self._semaphores: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(self._max)
        )
        self._levels: dict[str, int] = defaultdict(lambda: self._max)
        self._buckets: dict[str, _TokenBucket] = {}

    def semaphore(self, subscription_id: str) -> asyncio.Semaphore:
        return self._semaphores[subscription_id]

    def bucket(self, subscription_id: str) -> _TokenBucket:
        b = self._buckets.get(subscription_id)
        if b is None:
            b = _TokenBucket(rate=self._rate, capacity=self._burst)
            self._buckets[subscription_id] = b
        return b

    async def acquire_token(self, subscription_id: str) -> None:
        await self.bucket(subscription_id).acquire()

    async def backoff_on_throttle(
        self,
        subscription_id: str,
        retry_after_seconds: float | None = None,
    ) -> None:
        """Called after receiving a 429.

        Halves both the concurrency ceiling and the steady-state RPS, then
        sleeps for ``Retry-After`` (or 30 s default) before allowing the
        caller to retry.
        """
        current = self._levels[subscription_id]
        new_level = max(1, current // 2)
        self._levels[subscription_id] = new_level
        self._semaphores[subscription_id] = asyncio.Semaphore(new_level)

        bucket = self.bucket(subscription_id)
        bucket.adjust(max(0.5, bucket._rate / 2))

        wait = retry_after_seconds if retry_after_seconds is not None else 30.0
        await asyncio.sleep(wait)


async def with_retry(
    coro_fn,
    throttle: ThrottleManager,
    subscription_id: str,
    max_retries: int = 5,
):
    """Execute an async coroutine function with throttle-aware retry logic.

    ``coro_fn`` must be a zero-argument callable that returns an awaitable.
    Acquires a rate-limit token and a concurrency slot before each attempt.
    On HTTP 429 the ThrottleManager halves both limits and we sleep before
    retrying.  On other HTTP errors we do exponential backoff up to
    ``max_retries``.
    """
    from azure.core.exceptions import HttpResponseError

    delay = 2.0
    for attempt in range(max_retries + 1):
        # Token bucket first — gates throughput; cheap if tokens are available.
        await throttle.acquire_token(subscription_id)
        async with throttle.semaphore(subscription_id):
            try:
                return await coro_fn()
            except HttpResponseError as exc:
                status = exc.status_code or 0
                if status == 429:
                    retry_after: float | None = None
                    _resp_headers = getattr(exc.response, "headers", {}) if exc.response else {}
                    if _resp_headers and "Retry-After" in _resp_headers:
                        try:
                            retry_after = float(_resp_headers["Retry-After"])
                        except ValueError:
                            pass
                    await throttle.backoff_on_throttle(subscription_id, retry_after)
                    continue
                if attempt >= max_retries:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
            except Exception:
                if attempt >= max_retries:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

    raise RuntimeError(f"Failed after {max_retries} retries for subscription {subscription_id}")
