"""Tests for DCM-IDL-001 (idle running-VM detector)."""
from __future__ import annotations

from unittest.mock import MagicMock

from cloudopt.analyzer.detectors import decom
from cloudopt.analyzer.sku_catalog import SkuCatalog, SkuSpec
from cloudopt.models import CollectionThresholds, DailyDataPoint, VmInventory, VmMetrics

SUB = "a1b2c3d4-0000-0000-0000-000000000099"
_T = CollectionThresholds(lookback_days=7)


def _vm(name: str = "vm1", sku: str = "Standard_D4s_v5", vcpus: int = 4) -> VmInventory:
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


def _cpu_metric(vm: VmInventory, p95: float, daily_vals: list[float]) -> VmMetrics:
    """Build a CPU metric with the given P95 and time-series values."""
    pts = [
        DailyDataPoint(date=f"2026-04-{i + 1:02d}T{h:02d}:00:00Z", value=v)
        for i, v in enumerate(daily_vals)
        for h in range(24)
    ] if not daily_vals else [
        DailyDataPoint(date=f"2026-04-{i + 1:02d}T00:00:00Z", value=v)
        for i, v in enumerate(daily_vals)
    ]
    return VmMetrics(
        resource_id=vm.resource_id,
        metric_name="Percentage CPU",
        avg=sum(daily_vals) / len(daily_vals) if daily_vals else 0.0,
        p95=p95,
        max=max(daily_vals) if daily_vals else 0.0,
        time_series=pts,
    )


def _net_metric(vm: VmInventory, avg_bytes: float) -> VmMetrics:
    return VmMetrics(
        resource_id=vm.resource_id,
        metric_name="Network Out Total",
        avg=avg_bytes,
    )


def _catalog(bandwidth_mbps: float = 1000.0) -> SkuCatalog:
    cat = MagicMock(spec=SkuCatalog)
    cat.get.return_value = SkuSpec(
        vcpus=4,
        memory_gb=16.0,
        network_bandwidth_mbps=bandwidth_mbps,
        accelerated_networking=True,
    )
    cat.find_smaller_sku.return_value = None
    return cat


class TestDcmIdl001Fires:
    def test_fires_when_p95_below_3pct_and_network_low(self):
        """VM with P95 CPU < 3% and near-zero network should be flagged idle."""
        vm = _vm()
        # 7 daily values all near zero; p95 = 1.5%
        cpu = _cpu_metric(vm, p95=1.5, daily_vals=[1.0, 1.5, 0.5, 1.0, 2.0, 1.0, 1.5])
        net = _net_metric(vm, avg_bytes=100)  # negligible
        findings = decom.detect([vm], [cpu, net], [], _T, _catalog())
        codes = [f.code for f in findings]
        assert "DCM-IDL-001" in codes

    def test_fires_without_network_metric(self):
        """Idle detection should still fire when network metric is absent."""
        vm = _vm()
        cpu = _cpu_metric(vm, p95=1.0, daily_vals=[1.0] * 7)
        findings = decom.detect([vm], [cpu], [], _T, _catalog())
        codes = [f.code for f in findings]
        assert "DCM-IDL-001" in codes

    def test_rationale_mentions_lookback(self):
        vm = _vm()
        cpu = _cpu_metric(vm, p95=1.0, daily_vals=[1.0] * 7)
        findings = decom.detect([vm], [cpu], [], _T, _catalog())
        idl = next(f for f in findings if f.code == "DCM-IDL-001")
        assert "7-day" in idl.rationale or "7 day" in idl.rationale or "7" in idl.rationale


class TestDcmIdl001Suppressed:
    def test_suppressed_when_p95_above_3pct(self):
        vm = _vm()
        cpu = _cpu_metric(vm, p95=5.0, daily_vals=[3.0, 5.0, 6.0, 4.0, 5.0, 4.0, 4.0])
        findings = decom.detect([vm], [cpu], [], _T, _catalog())
        assert all(f.code != "DCM-IDL-001" for f in findings)

    def test_suppressed_when_no_metrics(self):
        vm = _vm()
        findings = decom.detect([vm], [], [], _T, _catalog())
        assert all(f.code != "DCM-IDL-001" for f in findings)

    def test_suppressed_for_stopped_vm(self):
        """DCM-IDL-001 must not fire when the VM is already stopped (DCM-STP-001 fires instead)."""
        vm = VmInventory(
            vm_name="vm1",
            subscription_id=SUB,
            subscription_name="Test",
            resource_group="rg",
            resource_id=f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
            vm_sku="Standard_D4s_v5",
            vcpus=4,
            memory_gb=16.0,
            region="eastus",
            os_type="Linux",
            power_state="powerstate/deallocated",
        )
        cpu = _cpu_metric(vm, p95=0.1, daily_vals=[0.1] * 7)
        findings = decom.detect([vm], [cpu], [], _T, _catalog())
        codes = [f.code for f in findings]
        assert "DCM-IDL-001" not in codes
        assert "DCM-STP-001" in codes

    def test_suppressed_when_network_high(self):
        """High network utilization should prevent idle classification."""
        vm = _vm()
        cpu = _cpu_metric(vm, p95=1.0, daily_vals=[1.0] * 7)
        # _network_util_pct expects bytes per PT1H interval (not bytes/sec).
        # At 80% of 1000 Mbps: 0.8 × 1000 × 125_000 × 3600 = 3.6 × 10^11 bytes/hr
        net = _net_metric(vm, avg_bytes=int(0.8 * 1000 * 125_000 * 3600))
        findings = decom.detect([vm], [cpu, net], [], _T, _catalog(bandwidth_mbps=1000.0))
        assert all(f.code != "DCM-IDL-001" for f in findings)
