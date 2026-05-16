"""Tests for cloudopt.analyzer.confidence — SPEC §6.3 confidence scoring."""
from __future__ import annotations

import pytest

from cloudopt.analyzer.confidence import ScoredConfidence, score
from cloudopt.analyzer.detectors._shared import _best_enriched
from cloudopt.analyzer.taxonomy import Category, Confidence
from cloudopt.enrichment.schema import EnrichedVmMetrics, MonitoringDataPoint, MonitoringConfidence
from cloudopt.models import VmInventory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SUB = "a1b2c3d4-0000-0000-0000-000000000001"

_AUTHORITATIVE_CATS = [
    Category.CLEANUP,
    Category.QUOTA,
    Category.CRR,
    Category.DECOM,
]

_METRIC_CATS = [Category.RIGHTSIZE, Category.SWAP]


def _evm(
    source_tool: str = "datadog",
    metrics: list[str] | None = None,
) -> EnrichedVmMetrics:
    evm = EnrichedVmMetrics(vm_name="vm1", hostname="vm1", source_tool=source_tool)
    for m in metrics or []:
        evm.data_points.append(
            MonitoringDataPoint(
                schema_version="1.0",
                source_tool=source_tool,
                hostname="vm1",
                metric_name=m,
                period_days=30,
                period_end_utc="2025-01-31T00:00:00Z",
                avg_value=50.0,
                p95_value=None,
                max_value=None,
                unit="percent",
            )
        )
    return evm


def _vm(name: str = "vm1") -> VmInventory:
    return VmInventory(
        vm_name=name,
        subscription_id=SUB,
        subscription_name="Test",
        resource_group="rg",
        resource_id=f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/{name}",
        vm_sku="Standard_D4s_v5",
        vcpus=4,
        memory_gb=16.0,
        region="eastus",
        os_type="Linux",
    )


# ---------------------------------------------------------------------------
# Authoritative categories → always HIGH / arm-api
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cat", _AUTHORITATIVE_CATS)
def test_authoritative_categories_high(cat: Category) -> None:
    result = score(None, cat)
    assert result.confidence == Confidence.HIGH


@pytest.mark.parametrize("cat", _AUTHORITATIVE_CATS)
def test_authoritative_categories_evidence_arm_api(cat: Category) -> None:
    result = score(None, cat)
    assert result.evidence_sources == ["arm-api"]


@pytest.mark.parametrize("cat", _AUTHORITATIVE_CATS)
def test_authoritative_categories_no_blockers(cat: Category) -> None:
    result = score(None, cat)
    assert result.blockers_to_high == []


@pytest.mark.parametrize("cat", _AUTHORITATIVE_CATS)
def test_authoritative_ignores_enriched(cat: Category) -> None:
    """Even with enriched data, authoritative categories stay HIGH / arm-api."""
    result = score(_evm(metrics=["os.cpu.percent"]), cat)
    assert result.confidence == Confidence.HIGH
    assert result.evidence_sources == ["arm-api"]


# ---------------------------------------------------------------------------
# No enrichment (enriched=None) → MEDIUM + platform blocker
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cat", _METRIC_CATS)
def test_no_enriched_is_medium(cat: Category) -> None:
    result = score(None, cat)
    assert result.confidence == Confidence.MEDIUM


@pytest.mark.parametrize("cat", _METRIC_CATS)
def test_no_enriched_evidence_is_platform(cat: Category) -> None:
    result = score(None, cat)
    assert result.evidence_sources == ["platform"]


@pytest.mark.parametrize("cat", _METRIC_CATS)
def test_no_enriched_has_blocker(cat: Category) -> None:
    result = score(None, cat)
    assert len(result.blockers_to_high) == 1
    assert "monitoring export" in result.blockers_to_high[0].lower()


# ---------------------------------------------------------------------------
# PLATFORM_ONLY enrichment (source present but no os.* data) → MEDIUM
# ---------------------------------------------------------------------------

def test_platform_only_enriched_is_medium() -> None:
    result = score(_evm(source_tool="datadog", metrics=[]), Category.RIGHTSIZE)
    assert result.confidence == Confidence.MEDIUM


def test_platform_only_enriched_includes_source_tool() -> None:
    result = score(_evm(source_tool="splunk", metrics=[]), Category.RIGHTSIZE)
    assert "splunk" in result.evidence_sources


def test_platform_only_enriched_blocker_mentions_tool() -> None:
    result = score(_evm(source_tool="dynatrace", metrics=[]), Category.RIGHTSIZE)
    assert len(result.blockers_to_high) == 1
    assert "dynatrace" in result.blockers_to_high[0]


# ---------------------------------------------------------------------------
# OS_AWARE enrichment → HIGH
# ---------------------------------------------------------------------------

def test_os_aware_is_high() -> None:
    result = score(_evm(metrics=["os.cpu.percent"]), Category.RIGHTSIZE)
    assert result.confidence == Confidence.HIGH


def test_os_aware_evidence_includes_platform_and_tool() -> None:
    result = score(_evm(source_tool="datadog", metrics=["os.cpu.percent"]), Category.RIGHTSIZE)
    assert "platform" in result.evidence_sources
    assert "datadog" in result.evidence_sources


def test_os_aware_no_blockers() -> None:
    result = score(_evm(metrics=["os.cpu.percent"]), Category.RIGHTSIZE)
    assert result.blockers_to_high == []


def test_os_aware_swap_category() -> None:
    result = score(_evm(metrics=["os.cpu.percent"]), Category.SWAP)
    assert result.confidence == Confidence.HIGH


# ---------------------------------------------------------------------------
# WORKLOAD_AWARE enrichment → HIGH + workload namespace in evidence
# ---------------------------------------------------------------------------

def test_workload_aware_jvm_is_high() -> None:
    result = score(_evm(metrics=["os.cpu.percent", "jvm.heap.used_percent"]), Category.RIGHTSIZE)
    assert result.confidence == Confidence.HIGH


def test_workload_aware_jvm_evidence_includes_jvm() -> None:
    result = score(_evm(metrics=["os.cpu.percent", "jvm.heap.used_percent"]), Category.RIGHTSIZE)
    assert "jvm" in result.evidence_sources


def test_workload_aware_dotnet_evidence_includes_dotnet() -> None:
    result = score(_evm(metrics=["os.cpu.percent", "dotnet.gc.heap_bytes"]), Category.SWAP)
    assert "dotnet" in result.evidence_sources


def test_workload_aware_sql_evidence_includes_sql() -> None:
    result = score(_evm(metrics=["os.cpu.percent", "sql.buffer.page_life_expectancy"]), Category.SWAP)
    assert "sql" in result.evidence_sources


def test_workload_aware_no_blockers() -> None:
    result = score(_evm(metrics=["os.cpu.percent", "jvm.heap.used_percent"]), Category.SWAP)
    assert result.blockers_to_high == []


# ---------------------------------------------------------------------------
# to_kwargs() helper
# ---------------------------------------------------------------------------

def test_to_kwargs_returns_copy() -> None:
    result = score(None, Category.RIGHTSIZE)
    kwargs = result.to_kwargs()
    assert kwargs["confidence"] == Confidence.MEDIUM
    assert kwargs["evidence_sources"] == ["platform"]
    assert isinstance(kwargs["blockers_to_high"], list)
    # Mutating the returned dict must not affect the original ScoredConfidence
    kwargs["evidence_sources"].append("mutated")
    assert "mutated" not in result.evidence_sources


# ---------------------------------------------------------------------------
# _best_enriched helper (from _shared.py)
# ---------------------------------------------------------------------------

def test_best_enriched_returns_none_for_empty_map() -> None:
    vms = [_vm("vm1")]
    assert _best_enriched(vms, None) is None


def test_best_enriched_returns_none_when_vm_not_in_map() -> None:
    vms = [_vm("vm1")]
    rid = vms[0].resource_id
    # map exists but doesn't contain vm1's resource_id
    other_rid = rid.replace("vm1", "other")
    enriched_map = {other_rid: _evm()}
    assert _best_enriched(vms, enriched_map) is None


def test_best_enriched_picks_highest_tier() -> None:
    vm_platform = _vm("vm1")
    vm_os = _vm("vm2")
    platform_enriched = _evm(source_tool="datadog", metrics=[])         # PLATFORM_ONLY
    os_enriched = _evm(source_tool="datadog", metrics=["os.cpu.percent"])  # OS_AWARE

    enriched_map = {
        vm_platform.resource_id: platform_enriched,
        vm_os.resource_id: os_enriched,
    }
    result = _best_enriched([vm_platform, vm_os], enriched_map)
    assert result is os_enriched


def test_best_enriched_picks_workload_over_os() -> None:
    vm_os = _vm("vm1")
    vm_workload = _vm("vm2")
    os_enriched = _evm(source_tool="datadog", metrics=["os.cpu.percent"])
    wl_enriched = _evm(source_tool="datadog", metrics=["os.cpu.percent", "jvm.heap.used_percent"])

    enriched_map = {
        vm_os.resource_id: os_enriched,
        vm_workload.resource_id: wl_enriched,
    }
    result = _best_enriched([vm_os, vm_workload], enriched_map)
    assert result is wl_enriched


def test_best_enriched_single_vm_returns_its_enriched() -> None:
    vm = _vm("vm1")
    enriched = _evm(metrics=["os.cpu.percent"])
    result = _best_enriched([vm], {vm.resource_id: enriched})
    assert result is enriched
