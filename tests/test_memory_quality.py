"""Phase 3 — Memory quality tests (SPEC §3.2 / §3.3).

Tests cover:
  - _resolve_memory_quality source-priority logic
  - _compute_mem_pressure_score from platform min metric
  - _compute_memory_disagreement cross-source comparison
  - enrich_vm_memory_quality end-to-end mutation
  - RSZ-UPS-001 gating via memory_quality
  - KNOWN_SOURCE_TOOLS includes ama / vminsights-classic
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cloudopt.analyzer.detectors._shared import (
    _compute_mem_pressure_score,
    _compute_memory_disagreement,
    _resolve_memory_quality,
    enrich_vm_memory_quality,
)
from cloudopt.analyzer.detectors import upsize
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.enrichment.schema import (
    EnrichedVmMetrics,
    KNOWN_SOURCE_TOOLS,
    MonitoringDataPoint,
)
from cloudopt.models import (
    CollectionThresholds,
    MemoryQuality,
    VmInventory,
    VmMetrics,
)

SUB = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _vm(
    name: str = "vm1",
    sku: str = "Standard_D4s_v5",
    memory_gb: float = 16.0,
) -> VmInventory:
    return VmInventory(
        vm_name=name,
        subscription_id=SUB,
        subscription_name="Test",
        resource_group="rg",
        resource_id=f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/{name}",
        vm_sku=sku,
        vcpus=4,
        memory_gb=memory_gb,
        region="eastus",
        os_type="Linux",
    )


def _avail_mem_metric(
    resource_id: str,
    avg_bytes: float,
    min_bytes: float | None = None,
) -> VmMetrics:
    return VmMetrics(
        resource_id=resource_id,
        metric_name="Available Memory Bytes",
        avg=avg_bytes,
        min=min_bytes if min_bytes is not None else avg_bytes,
    )


def _enriched(vm_name: str, source_tool: str, has_os_mem: bool = True) -> EnrichedVmMetrics:
    e = EnrichedVmMetrics(vm_name=vm_name, hostname=vm_name, source_tool=source_tool)
    if has_os_mem:
        e.data_points.append(MonitoringDataPoint(
            schema_version="1.0",
            source_tool=source_tool,
            hostname=vm_name,
            metric_name="os.memory.used_percent",
            period_days=30,
            period_end_utc="2024-01-01T00:00:00Z",
            avg_value=40.0,
            p95_value=55.0,
            max_value=70.0,
            unit="percent",
        ))
    return e


def _os_cpu_metric(vm_name: str, source_tool: str, cpu_p95: float) -> MonitoringDataPoint:
    return MonitoringDataPoint(
        schema_version="1.0",
        source_tool=source_tool,
        hostname=vm_name,
        metric_name="os.cpu.used_percent",
        period_days=30,
        period_end_utc="2024-01-01T00:00:00Z",
        avg_value=cpu_p95,
        p95_value=cpu_p95,
        max_value=cpu_p95,
        unit="percent",
    )


def _catalog_no_larger() -> SkuCatalog:
    cat = MagicMock(spec=SkuCatalog)
    cat.find_larger_sku.return_value = "Standard_D8s_v5"
    cat.find_newer_generation_sku.return_value = None
    cat.find_arm64_equivalent_sku.return_value = None
    return cat


# ---------------------------------------------------------------------------
# KNOWN_SOURCE_TOOLS must include ama and vminsights-classic
# ---------------------------------------------------------------------------

def test_known_source_tools_includes_ama():
    assert "ama" in KNOWN_SOURCE_TOOLS


def test_known_source_tools_includes_vminsights_classic():
    assert "vminsights-classic" in KNOWN_SOURCE_TOOLS


# ---------------------------------------------------------------------------
# _resolve_memory_quality
# ---------------------------------------------------------------------------

def test_missing_when_no_metrics():
    vm = _vm()
    assert _resolve_memory_quality(vm, {}, None) is MemoryQuality.MISSING


def test_platform_when_available_memory_bytes_present():
    vm = _vm()
    vm_met = {"Available Memory Bytes": _avail_mem_metric(vm.resource_id, avg_bytes=4_000_000_000)}
    assert _resolve_memory_quality(vm, vm_met, None) is MemoryQuality.PLATFORM


def test_customer_from_datadog_with_os_mem():
    vm = _vm()
    e = _enriched(vm.vm_name, source_tool="datadog")
    assert _resolve_memory_quality(vm, {}, e) is MemoryQuality.CUSTOMER


def test_customer_from_prometheus_with_os_mem():
    vm = _vm()
    e = _enriched(vm.vm_name, source_tool="prometheus")
    assert _resolve_memory_quality(vm, {}, e) is MemoryQuality.CUSTOMER


def test_ama_quality():
    vm = _vm()
    e = _enriched(vm.vm_name, source_tool="ama")
    assert _resolve_memory_quality(vm, {}, e) is MemoryQuality.AMA


def test_vminsights_classic_quality():
    vm = _vm()
    e = _enriched(vm.vm_name, source_tool="vminsights-classic")
    assert _resolve_memory_quality(vm, {}, e) is MemoryQuality.VMINSIGHTS_CLASSIC


def test_platform_wins_over_missing_when_enrichment_lacks_os_data():
    """Enrichment without os.* data → falls back to platform if metric present."""
    vm = _vm()
    e = _enriched(vm.vm_name, source_tool="datadog", has_os_mem=False)
    vm_met = {"Available Memory Bytes": _avail_mem_metric(vm.resource_id, avg_bytes=2_000_000_000)}
    assert _resolve_memory_quality(vm, vm_met, e) is MemoryQuality.PLATFORM


def test_ama_case_insensitive():
    """source_tool values should be lower-cased before comparison."""
    vm = _vm()
    e = _enriched(vm.vm_name, source_tool="AMA")
    assert _resolve_memory_quality(vm, {}, e) is MemoryQuality.AMA


# ---------------------------------------------------------------------------
# _compute_mem_pressure_score
# ---------------------------------------------------------------------------

def test_pressure_score_half_available():
    """8 GB available out of 16 GB total → pressure = 0.5."""
    vm = _vm(memory_gb=16.0)
    eight_gb = 8 * (1024 ** 3)
    vm_met = {"Available Memory Bytes": _avail_mem_metric(vm.resource_id, avg_bytes=eight_gb, min_bytes=eight_gb)}
    score = _compute_mem_pressure_score(vm, vm_met)
    assert score == pytest.approx(0.5, abs=1e-6)


def test_pressure_score_none_when_no_metric():
    vm = _vm()
    assert _compute_mem_pressure_score(vm, {}) is None


def test_pressure_score_clamped_to_zero():
    """If min_bytes > total_bytes (pathological), clamp at 0."""
    vm = _vm(memory_gb=4.0)
    bigger_than_ram = 10 * (1024 ** 3)
    vm_met = {"Available Memory Bytes": _avail_mem_metric(vm.resource_id, avg_bytes=bigger_than_ram, min_bytes=bigger_than_ram)}
    assert _compute_mem_pressure_score(vm, vm_met) == 0.0


def test_pressure_score_clamped_to_one():
    """Negative available bytes (pathological) → pressure = 1.0."""
    vm = _vm(memory_gb=16.0)
    vm_met = {"Available Memory Bytes": _avail_mem_metric(vm.resource_id, avg_bytes=-1.0, min_bytes=-1.0)}
    assert _compute_mem_pressure_score(vm, vm_met) == 1.0


# ---------------------------------------------------------------------------
# _compute_memory_disagreement
# ---------------------------------------------------------------------------

def test_no_disagreement_when_no_enrichment():
    vm = _vm()
    vm_met = {"Available Memory Bytes": _avail_mem_metric(vm.resource_id, avg_bytes=4_000_000_000)}
    assert _compute_memory_disagreement(vm, vm_met, None) is None


def test_no_disagreement_when_sources_agree():
    """Platform 50 % used vs customer 55 % used → within 10 % tolerance."""
    vm = _vm(memory_gb=16.0)
    # 8 GB available → 50 % used
    eight_gb = 8 * (1024 ** 3)
    vm_met = {"Available Memory Bytes": _avail_mem_metric(vm.resource_id, avg_bytes=eight_gb)}
    e = EnrichedVmMetrics(vm_name=vm.vm_name, hostname=vm.vm_name, source_tool="datadog")
    e.data_points.append(MonitoringDataPoint(
        schema_version="1.0", source_tool="datadog", hostname=vm.vm_name,
        metric_name="os.memory.used_percent", period_days=30,
        period_end_utc="2024-01-01T00:00:00Z",
        avg_value=55.0, p95_value=55.0, max_value=55.0, unit="percent",
    ))
    assert _compute_memory_disagreement(vm, vm_met, e) is None


def test_disagreement_flagged_when_diff_exceeds_10_pct():
    """Platform 30 % used vs customer 60 % used → 30 % gap → flagged."""
    vm = _vm(memory_gb=16.0)
    # 30 % used → 11.2 GB available (70 % of 16)
    avail_bytes = 0.70 * 16.0 * (1024 ** 3)
    vm_met = {"Available Memory Bytes": _avail_mem_metric(vm.resource_id, avg_bytes=avail_bytes)}
    e = EnrichedVmMetrics(vm_name=vm.vm_name, hostname=vm.vm_name, source_tool="datadog")
    e.data_points.append(MonitoringDataPoint(
        schema_version="1.0", source_tool="datadog", hostname=vm.vm_name,
        metric_name="os.memory.used_percent", period_days=30,
        period_end_utc="2024-01-01T00:00:00Z",
        avg_value=60.0, p95_value=60.0, max_value=60.0, unit="percent",
    ))
    diff = _compute_memory_disagreement(vm, vm_met, e)
    assert diff is not None
    assert diff == pytest.approx(30.0, abs=1.0)


# ---------------------------------------------------------------------------
# enrich_vm_memory_quality (end-to-end mutation)
# ---------------------------------------------------------------------------

def test_enrich_sets_platform_quality():
    vm = _vm()
    metrics = [_avail_mem_metric(vm.resource_id, avg_bytes=4_000_000_000, min_bytes=3_000_000_000)]
    enrich_vm_memory_quality([vm], metrics, enriched_map=None)
    assert vm.memory_quality is MemoryQuality.PLATFORM


def test_enrich_sets_mem_pressure_score():
    vm = _vm(memory_gb=16.0)
    four_gb = 4 * (1024 ** 3)
    eight_gb = 8 * (1024 ** 3)
    metrics = [_avail_mem_metric(vm.resource_id, avg_bytes=eight_gb, min_bytes=four_gb)]
    enrich_vm_memory_quality([vm], metrics, enriched_map=None)
    # pressure = 1 - (4GB / 16GB) = 0.75
    assert vm.mem_pressure_score == pytest.approx(0.75, abs=1e-6)


def test_enrich_sets_customer_quality_from_enriched_map():
    vm = _vm()
    e = _enriched(vm.vm_name, source_tool="dynatrace")
    enriched_map = {vm.vm_name: e}
    enrich_vm_memory_quality([vm], [], enriched_map=enriched_map)
    assert vm.memory_quality is MemoryQuality.CUSTOMER


def test_enrich_flags_memory_disagreement():
    vm = _vm(memory_gb=16.0)
    avail_bytes = 0.70 * 16.0 * (1024 ** 3)  # ~30 % used
    metrics = [_avail_mem_metric(vm.resource_id, avg_bytes=avail_bytes, min_bytes=avail_bytes)]
    e = _enriched(vm.vm_name, source_tool="datadog")
    # Override the os.memory.used_percent to 60% (30% gap)
    e.data_points[0] = MonitoringDataPoint(
        schema_version="1.0", source_tool="datadog", hostname=vm.vm_name,
        metric_name="os.memory.used_percent", period_days=30,
        period_end_utc="2024-01-01T00:00:00Z",
        avg_value=60.0, p95_value=60.0, max_value=60.0, unit="percent",
    )
    enriched_map = {vm.vm_name: e}
    enrich_vm_memory_quality([vm], metrics, enriched_map=enriched_map)
    assert vm.memory_disagreement_pct is not None
    assert vm.memory_disagreement_pct > 10.0


def test_enrich_missing_when_no_data():
    vm = _vm()
    enrich_vm_memory_quality([vm], [], enriched_map=None)
    assert vm.memory_quality is MemoryQuality.MISSING
    assert vm.mem_pressure_score is None


# ---------------------------------------------------------------------------
# RSZ-UPS-001 gating via memory_quality
# ---------------------------------------------------------------------------

def _pressure_enriched(vm_name: str, source_tool: str) -> EnrichedVmMetrics:
    """Build enriched metrics that trigger RSZ-UPS-001 (cpu P95=90, mem P95=90)."""
    e = EnrichedVmMetrics(vm_name=vm_name, hostname=vm_name, source_tool=source_tool)
    for metric, value in [("os.cpu.used_percent", 90.0), ("os.memory.used_percent", 90.0)]:
        e.data_points.append(MonitoringDataPoint(
            schema_version="1.0", source_tool=source_tool, hostname=vm_name,
            metric_name=metric, period_days=30, period_end_utc="2024-01-01T00:00:00Z",
            avg_value=value, p95_value=value, max_value=value, unit="percent",
        ))
    return e


def test_upsize_fires_when_memory_quality_is_customer():
    vm = _vm()
    vm.memory_quality = MemoryQuality.CUSTOMER
    e = _pressure_enriched(vm.vm_name, "datadog")
    findings = upsize.detect([vm], [], [], CollectionThresholds(), _catalog_no_larger(), enriched_map={vm.resource_id: e})
    assert any(f.code == "RSZ-UPS-001" for f in findings)


def test_upsize_fires_when_memory_quality_is_ama():
    vm = _vm()
    vm.memory_quality = MemoryQuality.AMA
    e = _pressure_enriched(vm.vm_name, "ama")
    findings = upsize.detect([vm], [], [], CollectionThresholds(), _catalog_no_larger(), enriched_map={vm.resource_id: e})
    assert any(f.code == "RSZ-UPS-001" for f in findings)


def test_upsize_blocked_when_memory_quality_is_platform():
    vm = _vm()
    vm.memory_quality = MemoryQuality.PLATFORM
    e = _pressure_enriched(vm.vm_name, "datadog")
    findings = upsize.detect([vm], [], [], CollectionThresholds(), _catalog_no_larger(), enriched_map={vm.resource_id: e})
    assert not any(f.code == "RSZ-UPS-001" for f in findings)


def test_upsize_blocked_when_memory_quality_is_missing_and_no_os_enrichment():
    """memory_quality=MISSING → fall back to confidence_tier check → PLATFORM_ONLY → blocked."""
    vm = _vm()
    # memory_quality stays MISSING (default), enriched has no os.* data
    e = _enriched(vm.vm_name, source_tool="datadog", has_os_mem=False)
    findings = upsize.detect([vm], [], [], CollectionThresholds(), _catalog_no_larger(), enriched_map={vm.resource_id: e})
    assert not any(f.code == "RSZ-UPS-001" for f in findings)
