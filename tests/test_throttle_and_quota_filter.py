"""Tests for the token-bucket rate limiter and the vCPU-only quota filter."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from cloudopt.collector.throttle import (
    ThrottleManager,
    _TokenBucket,
    with_retry,
)


# ---------------------------------------------------------------------------
# Token-bucket rate limiter
# ---------------------------------------------------------------------------


class TestTokenBucket:
    def test_allows_burst_up_to_capacity_then_throttles(self):
        async def run() -> float:
            bucket = _TokenBucket(rate=10.0, capacity=5.0)
            start = time.monotonic()
            for _ in range(15):
                await bucket.acquire()
            return time.monotonic() - start

        elapsed = asyncio.run(run())
        # 5 free (burst) + 10 paced at 10/s ≈ 1.0 s minimum.
        assert elapsed >= 0.9, f"expected >=0.9s pacing, got {elapsed:.3f}s"

    def test_low_rate_paces_calls(self):
        async def run() -> float:
            bucket = _TokenBucket(rate=5.0, capacity=1.0)
            start = time.monotonic()
            for _ in range(3):
                await bucket.acquire()
            return time.monotonic() - start

        elapsed = asyncio.run(run())
        # 1 free + 2 at 5/s = 0.4 s minimum
        assert elapsed >= 0.3, f"expected >=0.3s pacing, got {elapsed:.3f}s"


class TestThrottleManagerRate:
    def test_per_subscription_buckets_are_isolated(self):
        async def run() -> float:
            tm = ThrottleManager(max_concurrency=10, rate_per_second=5.0, burst=1.0)
            start = time.monotonic()
            await asyncio.gather(
                tm.acquire_token("sub-a"),
                tm.acquire_token("sub-a"),
                tm.acquire_token("sub-b"),
                tm.acquire_token("sub-b"),
            )
            return time.monotonic() - start

        elapsed = asyncio.run(run())
        # Each sub: 1 free + 1 paced at 5/s = 0.2 s minimum, parallel across subs.
        assert elapsed < 0.6, f"per-sub buckets should run in parallel, got {elapsed:.3f}s"


class TestWithRetry:
    def test_acquires_token_before_call(self):
        calls: list[str] = []

        async def coro():
            calls.append("call")
            return "ok"

        async def run():
            tm = ThrottleManager(max_concurrency=5, rate_per_second=20.0)
            return await with_retry(coro, tm, "sub-x")

        result = asyncio.run(run())
        assert result == "ok"
        assert calls == ["call"]


# ---------------------------------------------------------------------------
# Quota collector — vCPUs-only filter
# ---------------------------------------------------------------------------


def _fake_usage(localized_value: str, current: int = 1, limit: int = 10):
    """Build a mock matching ``ComputeManagementClient.usage.list`` items."""
    name = MagicMock()
    name.value = localized_value.replace(" ", "")
    name.localized_value = localized_value
    usage = MagicMock()
    usage.name = name
    usage.current_value = current
    usage.limit = limit
    return usage


class TestQuotaVcpusOnlyFilter:
    def test_skips_non_vcpu_quota_entries(self):
        from cloudopt.collector import quota as quota_mod
        from cloudopt.collector.auth import SubscriptionInfo
        from cloudopt.scope import build_scope

        sub = SubscriptionInfo(
            subscription_id="sub-x",
            subscription_name="Sub-X",
        )

        fake_client = MagicMock()
        fake_client.usage.list.return_value = [
            _fake_usage("Standard DSv5 Family vCPUs", current=8, limit=100),
            _fake_usage("Total Regional vCPUs",       current=8, limit=100),
            _fake_usage("Availability Sets",          current=2, limit=10),
            _fake_usage("Virtual Machines",           current=4, limit=50),
            _fake_usage("Standard NCSv3 Family GPUs", current=0, limit=8),
        ]

        scope = build_scope(locations=["eastus"])

        with patch.object(quota_mod, "ComputeManagementClient", return_value=fake_client):
            results = quota_mod.collect_quota(
                credential=MagicMock(),
                subscriptions=[sub],
                scope=scope,
            )

        display_names = sorted(q.display_name for q in results)
        assert display_names == [
            "Standard DSv5 Family vCPUs",
            "Total Regional vCPUs",
        ]
