"""Tests for detectors.burstable (RSZ-BSF-001, RSZ-BSM-001)."""
from __future__ import annotations

from unittest.mock import MagicMock

from cloudopt.analyzer.detectors import burstable
from cloudopt.analyzer.sku_catalog import SkuCatalog, SkuSpec
from cloudopt.models import CollectionThresholds, DailyDataPoint, VmInventory, VmMetrics

SUB = "a1b2c3d4-0000-0000-0000-000000000098"
_T = CollectionThresholds(lookback_days=7)


def _vm(
    name: str = "vm1",
    sku: str = "Standard_D4s_v5",
    vcpus: int = 4,
) -> VmInventory:
    return VmInventory(
        vm_name=name,
        subscription_id=SUB,
        subscription_name="Test",
        resource_group="rg",
        resource_id=f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/{name}",
        vm_sku=sku,
        vcpus=vcpus,
        memory_gb=16.0,
        region="eastus",
        os_type="Linux",
        power_state="powerstate/running",
    )


def _cpu(vm: VmInventory, avg: float, p95: float) -> VmMetrics:
    return VmMetrics(
        resource_id=vm.resource_id,
        metric_name="Percentage CPU",
        avg=avg,
        p95=p95,
        time_series=[DailyDataPoint(date=f"2026-04-{i + 1:02d}T00:00:00Z", value=avg) for i in range(7)],
    )


def _catalog(accel_net: bool = False) -> SkuCatalog:
    cat = MagicMock(spec=SkuCatalog)
    cat.get.return_value = SkuSpec(
        vcpus=4,
        memory_gb=16.0,
        network_bandwidth_mbps=1000.0,
        accelerated_networking=accel_net,
    )
    return cat


class TestRszBsf001:
    """RSZ-BSF-001: non-B-series VM suitable for B-series."""

    def test_fires_for_eligible_low_cpu_d_series(self):
        """D-series VM with CPU avg below B-series baseline should get BSF recommendation."""
        vm = _vm(sku="Standard_D4s_v5", vcpus=4)
        # B4ms baseline = 40%; avg=20%, p95=30% → both below baseline and 2×baseline
        metrics = [_cpu(vm, avg=20.0, p95=30.0)]
        findings = burstable.detect([vm], metrics, [], _T, _catalog(accel_net=False))
        codes = [f.code for f in findings]
        assert "RSZ-BSF-001" in codes

    def test_fires_for_e_series(self):
        vm = _vm(sku="Standard_E4s_v5", vcpus=4)
        metrics = [_cpu(vm, avg=15.0, p95=25.0)]
        findings = burstable.detect([vm], metrics, [], _T, _catalog(accel_net=False))
        assert any(f.code == "RSZ-BSF-001" for f in findings)

    def test_fires_for_f_series(self):
        vm = _vm(sku="Standard_F4s_v2", vcpus=4)
        metrics = [_cpu(vm, avg=15.0, p95=25.0)]
        findings = burstable.detect([vm], metrics, [], _T, _catalog(accel_net=False))
        assert any(f.code == "RSZ-BSF-001" for f in findings)

    def test_suppressed_when_cpu_avg_above_baseline(self):
        """CPU avg above baseline means credits would deplete — no BSF."""
        vm = _vm(sku="Standard_D4s_v5", vcpus=4)
        # B4ms baseline = 40%; avg=50% > baseline
        metrics = [_cpu(vm, avg=50.0, p95=60.0)]
        findings = burstable.detect([vm], metrics, [], _T, _catalog(accel_net=False))
        assert all(f.code != "RSZ-BSF-001" for f in findings)

    def test_suppressed_when_p95_above_2x_baseline(self):
        """Spikes above 2× baseline would exhaust credits."""
        vm = _vm(sku="Standard_D4s_v5", vcpus=4)
        # B4ms baseline=40%; p95=85% > 2×40%=80%
        metrics = [_cpu(vm, avg=25.0, p95=85.0)]
        findings = burstable.detect([vm], metrics, [], _T, _catalog(accel_net=False))
        assert all(f.code != "RSZ-BSF-001" for f in findings)

    def test_suppressed_when_accelerated_networking_enabled(self):
        """B-series doesn't support AN; skip if current SKU uses AN."""
        vm = _vm(sku="Standard_D4s_v5", vcpus=4)
        metrics = [_cpu(vm, avg=20.0, p95=30.0)]
        findings = burstable.detect([vm], metrics, [], _T, _catalog(accel_net=True))
        assert all(f.code != "RSZ-BSF-001" for f in findings)

    def test_suppressed_for_non_eligible_family(self):
        """M-series or other families are not in the eligible set."""
        vm = _vm(sku="Standard_M8ms", vcpus=8)
        metrics = [_cpu(vm, avg=5.0, p95=10.0)]
        findings = burstable.detect([vm], metrics, [], _T, _catalog(accel_net=False))
        assert all(f.code != "RSZ-BSF-001" for f in findings)

    def test_suppressed_for_bseries_already(self):
        """Already on B-series — BSF should not fire (BSM might)."""
        vm = _vm(sku="Standard_B4ms", vcpus=4)
        metrics = [_cpu(vm, avg=20.0, p95=30.0)]
        findings = burstable.detect([vm], metrics, [], _T, _catalog(accel_net=False))
        assert all(f.code != "RSZ-BSF-001" for f in findings)

    def test_proposed_sku_mentions_bseries(self):
        vm = _vm(sku="Standard_D4s_v5", vcpus=4)
        metrics = [_cpu(vm, avg=20.0, p95=30.0)]
        findings = burstable.detect([vm], metrics, [], _T, _catalog(accel_net=False))
        bsf = next(f for f in findings if f.code == "RSZ-BSF-001")
        assert bsf.proposed and "B" in bsf.proposed


class TestRszBsm001:
    """RSZ-BSM-001: B-series VM misfit."""

    def test_fires_when_bseries_avg_exceeds_baseline(self):
        """B-series VM with avg CPU above baseline should get misfit recommendation."""
        vm = _vm(sku="Standard_B4ms", vcpus=4)
        # B4ms baseline = 40%; avg=60% > baseline
        metrics = [_cpu(vm, avg=60.0, p95=70.0)]
        findings = burstable.detect([vm], metrics, [], _T, _catalog())
        codes = [f.code for f in findings]
        assert "RSZ-BSM-001" in codes

    def test_suppressed_when_avg_below_baseline(self):
        """B-series VM comfortably within credit budget — no misfit."""
        vm = _vm(sku="Standard_B4ms", vcpus=4)
        metrics = [_cpu(vm, avg=20.0, p95=35.0)]
        findings = burstable.detect([vm], metrics, [], _T, _catalog())
        assert all(f.code != "RSZ-BSM-001" for f in findings)

    def test_suppressed_for_non_bseries(self):
        vm = _vm(sku="Standard_D4s_v5", vcpus=4)
        metrics = [_cpu(vm, avg=60.0, p95=70.0)]
        findings = burstable.detect([vm], metrics, [], _T, _catalog())
        assert all(f.code != "RSZ-BSM-001" for f in findings)
