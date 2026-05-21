"""Tests for detectors.swap (SWP-FAM-001, SWP-LFC-001)."""
from __future__ import annotations

from unittest.mock import MagicMock

from cloudopt.analyzer.detectors import swap
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.models import CollectionThresholds, VmInventory, VmMetrics

SUB = "a1b2c3d4-0000-0000-0000-000000000002"


def _vm(name: str = "vm1", sku: str = "Standard_D4s_v5", vcpus: int = 4, memory_gb: float = 16.0) -> VmInventory:
    return VmInventory(
        vm_name=name,
        subscription_id=SUB,
        subscription_name="Test",
        resource_group="rg",
        resource_id=f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/{name}",
        vm_sku=sku,
        vcpus=vcpus,
        memory_gb=memory_gb,
        region="eastus",
        os_type="Linux",
    )


def _met(vm: VmInventory, metric: str, avg: float | None = None, p95: float | None = None) -> VmMetrics:
    return VmMetrics(resource_id=vm.resource_id, metric_name=metric, avg=avg, p95=p95)


def _catalog() -> SkuCatalog:
    cat = MagicMock(spec=SkuCatalog)
    cat.find_smaller_sku.return_value = None
    cat.find_newer_generation_sku.return_value = None
    cat.find_arm64_equivalent_sku.return_value = None
    return cat


_T = CollectionThresholds()


class TestSwpFam001:
    def test_memory_bound_emits_fam_finding(self):
        vm = _vm(sku="Standard_D4s_v5")
        # mem >= 70%, cpu < 25%
        metrics = [
            _met(vm, "Percentage CPU", avg=10.0, p95=12.0),
            _met(vm, "Available Memory Bytes", avg=1_600_000_000),  # ~90% used of 16 GB
        ]
        findings = swap.detect([vm], metrics, [], _T, _catalog())
        fam = [f for f in findings if f.code == "SWP-FAM-001"]
        assert fam
        assert fam[0].deltas.get("signal") == "memory-bound"

    def test_compute_bound_emits_fam_finding(self):
        vm = _vm(sku="Standard_D4s_v5")
        # cpu >= 70%, mem < 25% used
        metrics = [
            _met(vm, "Percentage CPU", avg=80.0, p95=85.0),
            _met(vm, "Available Memory Bytes", avg=13_500_000_000),  # ~21.4% used of 16 GB
        ]
        findings = swap.detect([vm], metrics, [], _T, _catalog())
        fam = [f for f in findings if f.code == "SWP-FAM-001"]
        assert fam
        assert fam[0].deltas.get("signal") == "compute-bound"

    def test_fam_suppressed_when_would_downsize(self):
        vm = _vm(sku="Standard_D4s_v5")
        # genuinely underutilized: cpu < 15 AND mem < 20%
        metrics = [
            _met(vm, "Percentage CPU", avg=5.0, p95=6.0),
            _met(vm, "Available Memory Bytes", avg=14_000_000_000),  # ~18.5% used of 16 GB
        ]
        findings = swap.detect([vm], metrics, [], _T, _catalog())
        assert all(f.code != "SWP-FAM-001" for f in findings)

    def test_fam_not_emitted_for_non_d_series(self):
        vm = _vm(sku="Standard_E4s_v5")
        metrics = [
            _met(vm, "Percentage CPU", avg=10.0, p95=12.0),
            _met(vm, "Available Memory Bytes", avg=1_600_000_000),
        ]
        findings = swap.detect([vm], metrics, [], _T, _catalog())
        assert all(f.code != "SWP-FAM-001" for f in findings)


class TestSwpLfc001:
    def test_legacy_sku_emits_lfc_finding(self):
        vm = _vm(sku="Standard_D4_v2")  # no-version legacy
        findings = swap.detect([vm], [], [], _T, _catalog())
        codes = [f.code for f in findings]
        assert "SWP-LFC-001" in codes

    def test_modern_sku_does_not_emit_lfc(self):
        vm = _vm(sku="Standard_D4s_v5")
        findings = swap.detect([vm], [], [], _T, _catalog())
        assert all(f.code != "SWP-LFC-001" for f in findings)


class TestSwpArc001:
    def test_arc_001_not_emitted(self):
        """SWP-ARC-001 was removed; verify it is never produced."""
        vm = _vm(sku="Standard_D8s_v5")
        findings = swap.detect([vm], [], [], _T, _catalog())
        assert all(f.code != "SWP-ARC-001" for f in findings)

    def test_already_arm64_skips_arc(self):
        vm = _vm(sku="Standard_D8ps_v5")
        findings = swap.detect([vm], [], [], _T, _catalog())
        assert all(f.code != "SWP-ARC-001" for f in findings)

    def test_ineligible_family_skips_arc(self):
        vm = _vm(sku="Standard_M8s_v2")
        findings = swap.detect([vm], [], [], _T, _catalog())
        assert all(f.code != "SWP-ARC-001" for f in findings)
