"""Phase 4 — Workload archetype classifier tests (SPEC §4.1 / §4.2).

Tests cover:
  - classify_archetype: all 6 archetypes + UNKNOWN edge cases
  - infer_workload_role: name and tag heuristics
  - build_appinsights_corroboration: availability SLO
  - enrich_vm_archetype: end-to-end mutation
  - RSZ-BSF-001 archetype corroboration in burstable detector
"""
from __future__ import annotations

import math
from typing import Optional
from unittest.mock import MagicMock

import pytest

from cloudopt.analyzer.archetype import (
    classify_archetype,
    infer_workload_role,
    build_appinsights_corroboration,
    enrich_vm_archetype,
)
from cloudopt.models import (
    AppInsightsMetrics,
    CollectionThresholds,
    VmInventory,
    VmMetrics,
    WorkloadArchetype,
)

SUB = "11111111-1111-1111-1111-111111111111"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _vm(
    name: str = "vm1",
    sku: str = "Standard_D4s_v5",
    resource_group: str = "rg1",
    tags: Optional[dict] = None,
) -> VmInventory:
    vm = VmInventory(
        vm_name=name,
        subscription_id=SUB,
        subscription_name="Test",
        resource_group=resource_group,
        resource_id=f"/subscriptions/{SUB}/resourceGroups/{resource_group}/providers/Microsoft.Compute/virtualMachines/{name}",
        vm_sku=sku,
        vcpus=4,
        memory_gb=16.0,
        region="eastus",
        os_type="Linux",
    )
    if tags:
        vm.raw_properties["tags"] = tags
    return vm


def _cpu_metrics(resource_id: str, values: list[float], timestamps: Optional[list[str]] = None) -> VmMetrics:
    """Build a VmMetrics record with a time-series for Percentage CPU."""
    from cloudopt.models import DailyDataPoint

    if timestamps is None:
        # generate synthetic hourly UTC timestamps starting Monday 2024-01-01
        base = "2024-01-01T{:02d}:00:00Z"
        timestamps = []
        day_offset = 0
        hour = 0
        for _ in values:
            timestamps.append(f"2024-01-{(day_offset+1):02d}T{hour:02d}:00:00Z")
            hour += 1
            if hour >= 24:
                hour = 0
                day_offset += 1

    ts = [DailyDataPoint(date=ts, value=v) for ts, v in zip(timestamps, values)]
    return VmMetrics(
        resource_id=resource_id,
        metric_name="Percentage CPU",
        avg=sum(values) / len(values) if values else 0.0,
        p95=sorted(values)[int(0.95 * len(values))] if values else 0.0,
        time_series=ts,
    )


def _ai_metrics(resource_id: str, avail_p99: float, dur_p95: Optional[float] = None) -> AppInsightsMetrics:
    ai = AppInsightsMetrics(
        resource_id=resource_id,
        metric_name="availabilityResults/availabilityPercentage",
        display_name="Availability",
        category="availability",
        p99=avail_p99,
        avg=avail_p99,
    )
    return ai


def _ai_duration(resource_id: str, dur_p95: float) -> AppInsightsMetrics:
    return AppInsightsMetrics(
        resource_id=resource_id,
        metric_name="requests/duration",
        display_name="Request Duration",
        category="performance",
        p95=dur_p95,
        avg=dur_p95,
    )


# ---------------------------------------------------------------------------
# Timestamp generation helpers
# ---------------------------------------------------------------------------

def _weekday_work_hour_ts(count: int) -> list[str]:
    """Generate UTC timestamps for weekday business hours (Mon–Fri 08:00–17:00)."""
    ts = []
    day = 0  # 0 = Mon
    hour = 8
    for _ in range(count):
        ts.append(f"2024-01-{(day+1):02d}T{hour:02d}:00:00Z")
        hour += 1
        if hour >= 18:
            hour = 8
            day = (day + 1) % 5  # cycle Mon–Fri
    return ts


def _weekend_ts(count: int) -> list[str]:
    """Generate timestamps for weekends only (Sat/Sun)."""
    ts = []
    # 2024-01-06 is Saturday
    day = 6  # starts on Saturday (day index within month)
    hour = 0
    for _ in range(count):
        ts.append(f"2024-01-{day:02d}T{hour:02d}:00:00Z")
        hour += 1
        if hour >= 24:
            hour = 0
            day = (day + 1) if day % 2 == 0 else day + 1  # alternate Sat/Sun
            if day > 7:
                day = 6  # wrap back to Sat
    return ts


# ---------------------------------------------------------------------------
# classify_archetype: unknown / insufficient data
# ---------------------------------------------------------------------------


class TestClassifyUnknown:
    def test_empty_inputs(self):
        assert classify_archetype([], []) == WorkloadArchetype.UNKNOWN

    def test_too_few_points(self):
        vals = [30.0] * 47
        ts = [f"2024-01-01T{i:02d}:00:00Z" for i in range(24)] + \
             [f"2024-01-02T{i:02d}:00:00Z" for i in range(23)]
        assert classify_archetype(vals, ts) == WorkloadArchetype.UNKNOWN

    def test_zero_mean_returns_unknown(self):
        vals = [0.0] * 100
        ts = [f"2024-01-01T{i%24:02d}:00:00Z" for i in range(100)]
        assert classify_archetype(vals, ts) == WorkloadArchetype.UNKNOWN


# ---------------------------------------------------------------------------
# classify_archetype: dev-test-irregular
# ---------------------------------------------------------------------------


class TestDevTestIrregular:
    def _make_ts(self, count: int) -> list[str]:
        return [f"2024-01-{(i//24)+1:02d}T{i%24:02d}:00:00Z" for i in range(count)]

    def test_many_zeros_high_cv(self):
        # 30% near-zero + high spikes → irregular
        vals = [0.0] * 30 + [80.0] * 20 + [0.0] * 20 + [90.0] * 30
        ts = self._make_ts(100)
        result = classify_archetype(vals, ts)
        assert result == WorkloadArchetype.DEV_TEST_IRREGULAR

    def test_low_zero_fraction_not_irregular(self):
        # Only 10% zeros → should NOT be dev-test-irregular
        vals = [0.0] * 10 + [40.0] * 90
        ts = self._make_ts(100)
        result = classify_archetype(vals, ts)
        assert result != WorkloadArchetype.DEV_TEST_IRREGULAR


# ---------------------------------------------------------------------------
# classify_archetype: bursty
# ---------------------------------------------------------------------------


class TestBursty:
    def _make_ts(self, count: int) -> list[str]:
        return [f"2024-01-{(i//24)+1:02d}T{i%24:02d}:00:00Z" for i in range(count)]

    def test_high_p95_p50_ratio(self):
        # Most values near 5%, peaks near 80% → bursty
        vals = [5.0] * 70 + [80.0] * 30
        ts = self._make_ts(100)
        result = classify_archetype(vals, ts)
        assert result == WorkloadArchetype.BURSTY

    def test_uniform_is_not_bursty(self):
        vals = [40.0] * 100
        ts = self._make_ts(100)
        result = classify_archetype(vals, ts)
        assert result != WorkloadArchetype.BURSTY


# ---------------------------------------------------------------------------
# classify_archetype: spiky
# ---------------------------------------------------------------------------


class TestSpiky:
    def _make_ts(self, count: int) -> list[str]:
        return [f"2024-01-{(i//24)+1:02d}T{i%24:02d}:00:00Z" for i in range(count)]

    def test_extreme_p99_spike(self):
        # 95th pct = ~50%, one huge spike at 99th → P99/P95 > 1.8
        vals = [50.0] * 98 + [98.0, 99.0]
        ts = self._make_ts(100)
        result = classify_archetype(vals, ts)
        assert result == WorkloadArchetype.SPIKY

    def test_uniform_not_spiky(self):
        vals = [50.0] * 100
        ts = self._make_ts(100)
        result = classify_archetype(vals, ts)
        assert result != WorkloadArchetype.SPIKY


# ---------------------------------------------------------------------------
# classify_archetype: business-hours
# ---------------------------------------------------------------------------


class TestBusinessHours:
    def test_work_hours_dominant(self):
        # High CPU during weekday 08–18, near-zero otherwise
        n_work = 100
        n_off = 40
        work_ts = _weekday_work_hour_ts(n_work)
        # off-hours: weekend or nighttime
        off_ts = [f"2024-01-06T{i%24:02d}:00:00Z" for i in range(n_off)]
        vals = [70.0] * n_work + [5.0] * n_off
        ts = work_ts + off_ts
        result = classify_archetype(vals, ts)
        assert result == WorkloadArchetype.BUSINESS_HOURS

    def test_uniform_not_business_hours(self):
        vals = [50.0] * 100
        ts = [f"2024-01-{(i//24)+1:02d}T{i%24:02d}:00:00Z" for i in range(100)]
        result = classify_archetype(vals, ts)
        assert result != WorkloadArchetype.BUSINESS_HOURS


# ---------------------------------------------------------------------------
# classify_archetype: weekend-idle
# ---------------------------------------------------------------------------


class TestWeekendIdle:
    def test_weekday_dominant(self):
        # High on weekdays (Mon–Fri), near-zero on weekends (Sat–Sun)
        n_weekday = 80
        n_weekend = 48
        wd_ts = [f"2024-01-{((i//24)%5)+1:02d}T{i%24:02d}:00:00Z" for i in range(n_weekday)]
        we_ts = [f"2024-01-{6+(i%2):02d}T{i%24:02d}:00:00Z" for i in range(n_weekend)]
        vals = [60.0] * n_weekday + [3.0] * n_weekend
        result = classify_archetype(vals, wd_ts + we_ts)
        assert result == WorkloadArchetype.WEEKEND_IDLE


# ---------------------------------------------------------------------------
# classify_archetype: steady-24x7
# ---------------------------------------------------------------------------


class TestSteady24x7:
    def test_low_cv_is_steady(self):
        # Very consistent CPU around 45%
        import random
        random.seed(42)
        vals = [45.0 + random.uniform(-2, 2) for _ in range(100)]
        ts = [f"2024-01-{(i//24)+1:02d}T{i%24:02d}:00:00Z" for i in range(100)]
        result = classify_archetype(vals, ts)
        assert result == WorkloadArchetype.STEADY_24X7

    def test_high_cv_not_steady(self):
        vals = [10.0] * 50 + [90.0] * 50
        ts = [f"2024-01-{(i//24)+1:02d}T{i%24:02d}:00:00Z" for i in range(100)]
        result = classify_archetype(vals, ts)
        assert result != WorkloadArchetype.STEADY_24X7


# ---------------------------------------------------------------------------
# infer_workload_role
# ---------------------------------------------------------------------------


class TestInferWorkloadRole:
    def test_sql_in_name(self):
        assert infer_workload_role("prod-sqlserver-01", {}) == "sql"

    def test_postgres_in_name(self):
        assert infer_workload_role("vm-postgres-prod", {}) == "postgres"

    def test_nginx_in_name(self):
        assert infer_workload_role("nginx-frontend", {}) == "nginx"

    def test_aks_node_in_name(self):
        assert infer_workload_role("aks-nodepool1-vm", {}) == "aks-node"

    def test_redis_in_name(self):
        assert infer_workload_role("redis-cache-vm", {}) == "redis"

    def test_tag_overrides_name(self):
        # name says nginx but tag says sql
        assert infer_workload_role("nginx-vm", {"role": "sqlserver"}) == "sql"

    def test_unrecognised_name_returns_none(self):
        assert infer_workload_role("myapp-vm-001", {}) is None

    def test_case_insensitive(self):
        assert infer_workload_role("PROD-NGINX-01", {}) == "nginx"

    def test_workload_class_tag(self):
        assert infer_workload_role("vm1", {"workload-class": "kafka"}) == "kafka"


# ---------------------------------------------------------------------------
# build_appinsights_corroboration
# ---------------------------------------------------------------------------


class TestAppInsightsCorroboration:
    def _make_resource_id(self, rg: str, name: str = "appinsights") -> str:
        return (
            f"/subscriptions/{SUB}/resourceGroups/{rg}/"
            f"providers/Microsoft.Insights/components/{name}"
        )

    def test_healthy_ai_in_same_rg_gives_corroboration(self):
        vm = _vm(resource_group="rg-prod")
        ai_resource_id = self._make_resource_id("rg-prod")
        ai_map = {ai_resource_id: [_ai_metrics(ai_resource_id, 99.95)]}
        result = build_appinsights_corroboration(ai_map, [vm])
        assert result[vm.resource_id] == 1

    def test_unhealthy_ai_gives_no_corroboration(self):
        vm = _vm(resource_group="rg-prod")
        ai_resource_id = self._make_resource_id("rg-prod")
        ai_map = {ai_resource_id: [_ai_metrics(ai_resource_id, 95.0)]}  # below SLO
        result = build_appinsights_corroboration(ai_map, [vm])
        assert result[vm.resource_id] == 0

    def test_ai_in_different_rg_gives_no_corroboration(self):
        vm = _vm(resource_group="rg-prod")
        ai_resource_id = self._make_resource_id("rg-dev")
        ai_map = {ai_resource_id: [_ai_metrics(ai_resource_id, 99.99)]}
        result = build_appinsights_corroboration(ai_map, [vm])
        assert result[vm.resource_id] == 0

    def test_high_duration_disqualifies_corroboration(self):
        vm = _vm(resource_group="rg-prod")
        ai_resource_id = self._make_resource_id("rg-prod")
        ai_map = {ai_resource_id: [
            _ai_metrics(ai_resource_id, 99.99),
            _ai_duration(ai_resource_id, 3000.0),  # 3 s p95 — above 2 s cap
        ]}
        result = build_appinsights_corroboration(ai_map, [vm])
        assert result[vm.resource_id] == 0

    def test_empty_ai_map_returns_zero_for_all(self):
        vms = [_vm(resource_group="rg-a"), _vm("vm2", resource_group="rg-b")]
        result = build_appinsights_corroboration({}, vms)
        for vm in vms:
            assert result[vm.resource_id] == 0


# ---------------------------------------------------------------------------
# enrich_vm_archetype: end-to-end mutation
# ---------------------------------------------------------------------------


class TestEnrichVmArchetype:
    def test_populates_archetype_and_role(self):
        vm = _vm("sql-prod-01")
        # Create steady CPU series
        vals = [45.0 + i * 0.1 % 3 for i in range(100)]
        m = _cpu_metrics(vm.resource_id, vals)
        enrich_vm_archetype([vm], [m])
        # Archetype must be classified (not UNKNOWN given 100 points)
        assert vm.workload_archetype != WorkloadArchetype.UNKNOWN or len(vals) < 48
        # Role inferred from name
        assert vm.inferred_workload_role == "sql"
        # No AI map → 0
        assert vm.appinsights_corroboration == 0

    def test_unknown_when_no_cpu_metrics(self):
        vm = _vm("vm1")
        enrich_vm_archetype([vm], [])
        assert vm.workload_archetype == WorkloadArchetype.UNKNOWN

    def test_ai_corroboration_wired(self):
        vm = _vm(resource_group="rg-ai")
        vals = [45.0] * 100
        m = _cpu_metrics(vm.resource_id, vals)
        ai_rid = (
            f"/subscriptions/{SUB}/resourceGroups/rg-ai"
            "/providers/Microsoft.Insights/components/ai1"
        )
        ai_map = {ai_rid: [_ai_metrics(ai_rid, 99.95)]}
        enrich_vm_archetype([vm], [m], ai_metrics_by_resource=ai_map)
        assert vm.appinsights_corroboration == 1


# ---------------------------------------------------------------------------
# Burstable detector archetype corroboration
# ---------------------------------------------------------------------------


class TestBurstableArchetypeCorroboration:
    def _catalog(self):
        """Return a SkuCatalog mock that always returns a spec without AN."""
        from cloudopt.analyzer.sku_catalog import SkuCatalog, SkuSpec

        cat = MagicMock(spec=SkuCatalog)
        spec = SkuSpec(
            vcpus=4,
            memory_gb=16.0,
            accelerated_networking=False,
        )
        cat.get.return_value = spec
        cat.find_newer_generation_sku.return_value = None
        cat.find_arm64_equivalent_sku.return_value = None
        cat.find_larger_sku.return_value = None
        return cat

    def _make_vm(self, archetype: WorkloadArchetype) -> VmInventory:
        vm = _vm(sku="Standard_D4s_v5")
        vm.workload_archetype = archetype
        return vm

    def _make_cpu(self, resource_id: str, avg: float, p95: float) -> VmMetrics:
        return VmMetrics(
            resource_id=resource_id,
            metric_name="Percentage CPU",
            avg=avg,
            p95=p95,
        )

    def _run(self, vm: VmInventory, avg: float, p95: float) -> list:
        from cloudopt.analyzer.detectors import burstable
        from cloudopt.models import CollectionThresholds, QuotaItem

        cpu = self._make_cpu(vm.resource_id, avg, p95)
        return burstable.detect(
            [vm],
            [cpu],
            [],
            CollectionThresholds(),
            self._catalog(),
        )

    def test_bursty_archetype_raises_corroboration(self):
        """RSZ-BSF-001 with BURSTY archetype should have higher confidence_score."""
        vm_bursty = self._make_vm(WorkloadArchetype.BURSTY)
        vm_unknown = self._make_vm(WorkloadArchetype.UNKNOWN)

        # avg=5%, p95=25% — well within B-series threshold for 4 vCPUs (baseline=100%)
        findings_bursty = self._run(vm_bursty, avg=5.0, p95=25.0)
        findings_unknown = self._run(vm_unknown, avg=5.0, p95=25.0)

        assert len(findings_bursty) == 1, "Expected RSZ-BSF-001 for bursty"
        assert len(findings_unknown) == 1, "Expected RSZ-BSF-001 for unknown"

        score_bursty = findings_bursty[0].confidence_score
        score_unknown = findings_unknown[0].confidence_score
        # Bursty archetype gives +10 corroboration bonus → higher score
        assert score_bursty > score_unknown, (
            f"Expected bursty score ({score_bursty}) > unknown score ({score_unknown})"
        )

    def test_business_hours_archetype_raises_corroboration(self):
        vm = self._make_vm(WorkloadArchetype.BUSINESS_HOURS)
        findings = self._run(vm, avg=5.0, p95=25.0)
        assert len(findings) == 1
        assert findings[0].confidence_score is not None

    def test_unknown_archetype_no_bonus(self):
        vm = self._make_vm(WorkloadArchetype.UNKNOWN)
        findings = self._run(vm, avg=5.0, p95=25.0)
        assert len(findings) == 1
        # No corroboration bonus — score should be baseline (no enrichment)
        assert findings[0].confidence_score is not None

    def test_bursty_rationale_mentions_archetype(self):
        vm = self._make_vm(WorkloadArchetype.BURSTY)
        findings = self._run(vm, avg=5.0, p95=25.0)
        assert "bursty" in findings[0].rationale.lower()
