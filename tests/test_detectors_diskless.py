"""Tests for detectors.diskless (SWP-DSK-001)."""
from __future__ import annotations

from unittest.mock import MagicMock

from cloudopt.analyzer.detectors import diskless
from cloudopt.analyzer.sku_catalog import SkuCatalog, SkuSpec
from cloudopt.models import CollectionThresholds, DailyDataPoint, VmInventory, VmMetrics

SUB = "a1b2c3d4-0000-0000-0000-000000000097"
_T = CollectionThresholds(lookback_days=7)

_TEMP_READ_OPS = "Temp Disk Read Operations/Sec"
_TEMP_WRITE_OPS = "Temp Disk Write Operations/Sec"
_TEMP_READ_BW = "Temp Disk Read Bytes/sec"
_TEMP_WRITE_BW = "Temp Disk Write Bytes/sec"


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


def _tdisk_metrics(vm: VmInventory, read_ops: float = 10.0, write_ops: float = 10.0,
                   read_bw: float = 1024.0, write_bw: float = 1024.0) -> list[VmMetrics]:
    """Create temp disk metrics with the given peak values."""
    pts = [DailyDataPoint(date=f"2026-04-{i + 1:02d}T00:00:00Z", value=v) for i, v in enumerate([read_ops] * 7)]
    return [
        VmMetrics(resource_id=vm.resource_id, metric_name=_TEMP_READ_OPS,
                  avg=read_ops, max=read_ops, time_series=pts),
        VmMetrics(resource_id=vm.resource_id, metric_name=_TEMP_WRITE_OPS,
                  avg=write_ops, max=write_ops,
                  time_series=[DailyDataPoint(date=f"2026-04-{i + 1:02d}T00:00:00Z", value=write_ops) for i in range(7)]),
        VmMetrics(resource_id=vm.resource_id, metric_name=_TEMP_READ_BW,
                  avg=read_bw, max=read_bw,
                  time_series=[DailyDataPoint(date=f"2026-04-{i + 1:02d}T00:00:00Z", value=read_bw) for i in range(7)]),
        VmMetrics(resource_id=vm.resource_id, metric_name=_TEMP_WRITE_BW,
                  avg=write_bw, max=write_bw,
                  time_series=[DailyDataPoint(date=f"2026-04-{i + 1:02d}T00:00:00Z", value=write_bw) for i in range(7)]),
    ]


def _catalog() -> SkuCatalog:
    cat = MagicMock(spec=SkuCatalog)
    cat.get.return_value = SkuSpec(
        vcpus=4,
        memory_gb=16.0,
        network_bandwidth_mbps=1000.0,
        accelerated_networking=True,
    )
    return cat


class TestSwpDsk001Fires:
    def test_fires_for_idle_temp_disk_d_series(self):
        """D-series VM with temp disk metrics near zero should get diskless recommendation."""
        vm = _vm(sku="Standard_D4s_v5", vcpus=4)
        # Tiny IOPS and bandwidth — well below 5% of conservative fallback
        metrics = _tdisk_metrics(vm, read_ops=1.0, write_ops=1.0, read_bw=512.0, write_bw=512.0)
        findings = diskless.detect([vm], metrics, [], _T, _catalog())
        codes = [f.code for f in findings]
        assert "SWP-DSK-001" in codes

    def test_fires_for_e_series(self):
        vm = _vm(sku="Standard_E4s_v5", vcpus=4)
        metrics = _tdisk_metrics(vm, read_ops=1.0, write_ops=1.0, read_bw=512.0, write_bw=512.0)
        findings = diskless.detect([vm], metrics, [], _T, _catalog())
        assert any(f.code == "SWP-DSK-001" for f in findings)

    def test_fires_for_f_series(self):
        vm = _vm(sku="Standard_F4s_v2", vcpus=4)
        metrics = _tdisk_metrics(vm, read_ops=1.0, write_ops=1.0, read_bw=512.0, write_bw=512.0)
        findings = diskless.detect([vm], metrics, [], _T, _catalog())
        assert any(f.code == "SWP-DSK-001" for f in findings)

    def test_proposed_sku_contains_as(self):
        """Diskless D-series v3+ is denoted by 'as' or 's_v[n]' — the 'd' suffix is dropped."""
        vm = _vm(sku="Standard_D4s_v5", vcpus=4)
        metrics = _tdisk_metrics(vm, read_ops=1.0, write_ops=1.0, read_bw=512.0, write_bw=512.0)
        findings = diskless.detect([vm], metrics, [], _T, _catalog())
        swp = next(f for f in findings if f.code == "SWP-DSK-001")
        assert swp.proposed is not None


class TestSwpDsk001Suppressed:
    def test_suppressed_when_iops_high(self):
        """VM actively using temp disk IOPS should not get diskless recommendation."""
        vm = _vm(sku="Standard_D4s_v5", vcpus=4)
        # 3000 IOPS on a fallback cap of 3200 = ~93.75% → above 5%
        metrics = _tdisk_metrics(vm, read_ops=1500.0, write_ops=1500.0, read_bw=1.0, write_bw=1.0)
        findings = diskless.detect([vm], metrics, [], _T, _catalog())
        assert all(f.code != "SWP-DSK-001" for f in findings)

    def test_suppressed_when_bandwidth_high(self):
        vm = _vm(sku="Standard_D4s_v5", vcpus=4)
        # 24 MB/s on 25 MB/s cap = 96% → above 5%
        metrics = _tdisk_metrics(vm, read_ops=1.0, write_ops=1.0,
                                 read_bw=12 * 1024 * 1024, write_bw=12 * 1024 * 1024)
        findings = diskless.detect([vm], metrics, [], _T, _catalog())
        assert all(f.code != "SWP-DSK-001" for f in findings)

    def test_suppressed_when_no_temp_disk_metrics(self):
        """Missing temp disk telemetry → conservative; do not recommend."""
        vm = _vm(sku="Standard_D4s_v5", vcpus=4)
        findings = diskless.detect([vm], [], [], _T, _catalog())
        assert all(f.code != "SWP-DSK-001" for f in findings)

    def test_suppressed_for_ineligible_family(self):
        """N-series or other GPU families are not D/E/F — no diskless recommendation."""
        vm = _vm(sku="Standard_NC4as_T4_v3", vcpus=4)
        metrics = _tdisk_metrics(vm, read_ops=1.0, write_ops=1.0, read_bw=1.0, write_bw=1.0)
        findings = diskless.detect([vm], metrics, [], _T, _catalog())
        assert all(f.code != "SWP-DSK-001" for f in findings)

    def test_suppressed_for_non_d_family(self):
        """Non-D/E/F family SKUs are not eligible for diskless recommendations."""
        vm = _vm(sku="Standard_L8s_v3", vcpus=8)  # Lsv3 = storage-optimized, not D/E/F
        metrics = _tdisk_metrics(vm, read_ops=1.0, write_ops=1.0, read_bw=1.0, write_bw=1.0)
        findings = diskless.detect([vm], metrics, [], _T, _catalog())
        assert all(f.code != "SWP-DSK-001" for f in findings)
