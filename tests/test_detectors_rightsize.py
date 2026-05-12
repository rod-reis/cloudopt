"""Tests for detectors.rightsize (RSZ-DWN-001)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cloudopt.analyzer.detectors import rightsize
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.models import CollectionThresholds, VmInventory, VmMetrics

SUB = "a1b2c3d4-0000-0000-0000-000000000001"


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


def _catalog(smaller: str | None = "Standard_D2s_v5") -> SkuCatalog:
    cat = MagicMock(spec=SkuCatalog)
    cat.find_smaller_sku.return_value = smaller
    return cat


_THRESHOLDS = CollectionThresholds()


class TestRszDwn001Underutilized:
    def test_emits_finding_when_cpu_and_mem_low(self):
        vm = _vm()
        metrics = [
            _met(vm, "Percentage CPU", avg=5.0, p95=6.0),
            _met(vm, "Available Memory Bytes", avg=14_000_000_000),  # ~18.5% used of 16 GB
        ]
        findings = rightsize.detect([vm], metrics, [], _THRESHOLDS, _catalog())
        codes = [f.code for f in findings]
        assert "RSZ-DWN-001" in codes

    def test_signal_is_underutilized(self):
        vm = _vm()
        metrics = [
            _met(vm, "Percentage CPU", avg=5.0, p95=6.0),
            _met(vm, "Available Memory Bytes", avg=14_000_000_000),
        ]
        findings = rightsize.detect([vm], metrics, [], _THRESHOLDS, _catalog())
        rsz = [f for f in findings if f.code == "RSZ-DWN-001"]
        assert rsz
        assert rsz[0].deltas.get("signal") == "underutilized"

    def test_no_rec_when_catalog_returns_none(self):
        vm = _vm()
        metrics = [
            _met(vm, "Percentage CPU", avg=5.0, p95=6.0),
            _met(vm, "Available Memory Bytes", avg=14_000_000_000),
        ]
        findings = rightsize.detect([vm], metrics, [], _THRESHOLDS, _catalog(None))
        assert not findings


class TestRszDwn001Oversized:
    def test_emits_finding_when_p95_low(self):
        vm = _vm()
        # cpu_avg=30 (above underutilized 15), p95=35 (below oversize 40 default)
        metrics = [
            _met(vm, "Percentage CPU", avg=30.0, p95=35.0),
            _met(vm, "Available Memory Bytes", avg=8_000_000_000),
        ]
        findings = rightsize.detect([vm], metrics, [], _THRESHOLDS, _catalog())
        codes = [f.code for f in findings]
        assert "RSZ-DWN-001" in codes

    def test_signal_is_oversized(self):
        vm = _vm()
        metrics = [
            _met(vm, "Percentage CPU", avg=30.0, p95=35.0),
            _met(vm, "Available Memory Bytes", avg=8_000_000_000),
        ]
        findings = rightsize.detect([vm], metrics, [], _THRESHOLDS, _catalog())
        rsz = [f for f in findings if f.code == "RSZ-DWN-001"]
        assert rsz
        assert rsz[0].deltas.get("signal") == "oversized"

    def test_proposed_sku_is_populated(self):
        vm = _vm()
        metrics = [
            _met(vm, "Percentage CPU", avg=30.0, p95=35.0),
        ]
        findings = rightsize.detect([vm], metrics, [], _THRESHOLDS, _catalog("Standard_D2s_v5"))
        rsz = [f for f in findings if f.code == "RSZ-DWN-001"]
        assert rsz
        assert rsz[0].proposed == "Standard_D2s_v5"


class TestRszEdgeCases:
    def test_no_findings_when_no_metrics(self):
        vm = _vm()
        findings = rightsize.detect([vm], [], [], _THRESHOLDS, _catalog())
        assert findings == []

    def test_only_one_size_rec_per_vm(self):
        vm = _vm()
        # Both underutilized AND p95<threshold — should only emit once
        metrics = [
            _met(vm, "Percentage CPU", avg=5.0, p95=6.0),
            _met(vm, "Available Memory Bytes", avg=14_000_000_000),
        ]
        findings = rightsize.detect([vm], metrics, [], _THRESHOLDS, _catalog())
        assert len([f for f in findings if f.code == "RSZ-DWN-001"]) == 1

    def test_no_rec_when_proposed_is_legacy_sku(self):
        vm = _vm()
        metrics = [
            _met(vm, "Percentage CPU", avg=5.0, p95=6.0),
            _met(vm, "Available Memory Bytes", avg=14_000_000_000),
        ]
        # Standard_D2_v2 is a legacy SKU and should be rejected
        findings = rightsize.detect([vm], metrics, [], _THRESHOLDS, _catalog("Standard_D2_v2"))
        assert findings == []
