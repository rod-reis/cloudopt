"""Tests for the recommendation engine — rules 1–3 (availability rule removed)."""
from cloudopt.models import (
    CollectionThresholds,
    VmInventory,
    VmMetrics,
)
from cloudopt.analyzer.recommendations import generate_recommendations
from cloudopt.analyzer.sku_catalog import SkuCatalog, SkuSpec
from unittest.mock import MagicMock

# Subcategory constants (granular signals).  In the CLOUDOPT model the
# umbrella ``category`` field holds one of the 5 top-level groups and the
# ``subcategory`` field holds the granular signal that fired the rule.
UNDERUTILIZED   = "underutilized"
RIGHT_SIZE       = "right-size"
PAAS_CANDIDATE   = "PaaS-candidate"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SUB_ID = "a1b2c3d4-0000-0000-0000-000000000000"


def _vm(name="vm", sku="Standard_D4s_v5", vcpus=4, memory_gb=16.0,
        zone=None, avset=None, vmss=None):
    return VmInventory(
        vm_name=name,
        subscription_id=SUB_ID,
        subscription_name="Test Sub",
        resource_group="rg",
        resource_id=f"/subscriptions/{SUB_ID}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/{name}",
        vm_sku=sku,
        vcpus=vcpus,
        memory_gb=memory_gb,
        region="eastus",
        os_type="Linux",
        availability_zone=zone,
        availability_set_name=avset,
        vmss_name=vmss,
    )


def _metric(vm: VmInventory, metric_name: str, avg=None, p95=None):
    return VmMetrics(
        resource_id=vm.resource_id,
        metric_name=metric_name,
        avg=avg,
        p50=avg,
        p95=p95 if p95 is not None else avg,
        max=avg,
        min=avg,
        time_series=[],
    )


def _metrics_for(vm: VmInventory, cpu_avg=None, mem_avg=None, disk_iops=None, cpu_p95=None):
    m = []
    if cpu_avg is not None:
        m.append(_metric(vm, "Percentage CPU", avg=cpu_avg, p95=cpu_p95 or cpu_avg))
    if mem_avg is not None:
        m.append(_metric(vm, "Available Memory Bytes", avg=mem_avg))
    if disk_iops is not None:
        m.append(_metric(vm, "Disk Read Operations/Sec", avg=disk_iops))
        m.append(_metric(vm, "Disk Write Operations/Sec", avg=0.0))
    return m


def _catalog_with(smaller_sku="Standard_D2s_v5", vcpus=2, memory_gb=8.0):
    catalog = MagicMock(spec=SkuCatalog)
    def get_impl(sub, region, sku):
        if sku == "Standard_D4s_v5":
            return SkuSpec(vcpus=4, memory_gb=16.0)
        if sku == smaller_sku:
            return SkuSpec(vcpus=vcpus, memory_gb=memory_gb)
        return None
    catalog.get.side_effect = get_impl
    catalog.find_smaller_sku.return_value = smaller_sku  # string SKU name
    return catalog


# ---------------------------------------------------------------------------
# Rule 1 — Underutilized
# ---------------------------------------------------------------------------

class TestUnderutilizedRule:
    def test_flags_when_cpu_and_mem_below_threshold(self):
        vm = _vm()
        # Available Memory Bytes metric: 14 GB available out of 16 GB → ~12.5% used < 20% threshold
        metrics = _metrics_for(vm, cpu_avg=5.0, mem_avg=14_000_000_000)  # both below default 15/20
        catalog = _catalog_with()
        recs = generate_recommendations([vm], metrics, CollectionThresholds(), catalog)
        subs = [r.subcategory for r in recs]
        assert UNDERUTILIZED in subs

    def test_no_flag_when_cpu_above_threshold(self):
        vm = _vm()
        metrics = _metrics_for(vm, cpu_avg=20.0, mem_avg=10.0)  # cpu above 15
        catalog = _catalog_with()
        recs = generate_recommendations([vm], metrics, CollectionThresholds(), catalog)
        subs = [r.subcategory for r in recs]
        assert UNDERUTILIZED not in subs

    def test_no_flag_when_mem_above_threshold(self):
        vm = _vm()
        metrics = _metrics_for(vm, cpu_avg=5.0, mem_avg=25.0)  # mem above 20
        catalog = _catalog_with()
        recs = generate_recommendations([vm], metrics, CollectionThresholds(), catalog)
        subs = [r.subcategory for r in recs]
        assert UNDERUTILIZED not in subs


# ---------------------------------------------------------------------------
# Rule 2 — Right-size / Oversized
# ---------------------------------------------------------------------------

class TestRightSizeRule:
    def test_flags_when_p95_below_oversize_threshold(self):
        vm = _vm()
        # P95 CPU = 35 < default 40 → oversized
        metrics = _metrics_for(vm, cpu_avg=30.0, cpu_p95=35.0)
        catalog = _catalog_with()
        recs = generate_recommendations([vm], metrics, CollectionThresholds(), catalog)
        subs = [r.subcategory for r in recs]
        assert RIGHT_SIZE in subs

    def test_no_flag_when_p95_above_threshold(self):
        vm = _vm()
        metrics = _metrics_for(vm, cpu_avg=50.0, cpu_p95=65.0)  # p95 above 40
        catalog = _catalog_with()
        recs = generate_recommendations([vm], metrics, CollectionThresholds(), catalog)
        subs = [r.subcategory for r in recs]
        assert RIGHT_SIZE not in subs

    def test_recommended_sku_populated(self):
        vm = _vm()
        metrics = _metrics_for(vm, cpu_avg=20.0, cpu_p95=30.0)
        catalog = _catalog_with()
        recs = generate_recommendations([vm], metrics, CollectionThresholds(), catalog)
        size_recs = [r for r in recs if r.subcategory == RIGHT_SIZE]
        assert size_recs
        assert size_recs[0].recommended_sku == "Standard_D2s_v5"


# ---------------------------------------------------------------------------
# Rule 3 — PaaS candidate
# ---------------------------------------------------------------------------

class TestPaasRule:
    def test_flags_low_cpu_and_low_iops(self):
        vm = _vm()
        # avg CPU < 10 (default), disk IOPS < 50
        metrics = _metrics_for(vm, cpu_avg=5.0, disk_iops=20.0)
        catalog = MagicMock(spec=SkuCatalog)
        catalog.find_smaller_sku.return_value = None  # prevent right-size from triggering
        recs = generate_recommendations([vm], metrics, CollectionThresholds(), catalog)
        subs = [r.subcategory for r in recs]
        assert PAAS_CANDIDATE not in subs  # PaaS detection removed per SPEC §13

    def test_no_flag_high_cpu(self):
        vm = _vm()
        metrics = _metrics_for(vm, cpu_avg=15.0, disk_iops=20.0)
        catalog = MagicMock(spec=SkuCatalog)
        catalog.find_smaller_sku.return_value = None
        recs = generate_recommendations([vm], metrics, CollectionThresholds(), catalog)
        subs = [r.subcategory for r in recs]
        assert PAAS_CANDIDATE not in subs

    def test_no_flag_high_iops(self):
        vm = _vm()
        metrics = _metrics_for(vm, cpu_avg=5.0, disk_iops=200.0)
        catalog = MagicMock(spec=SkuCatalog)
        catalog.find_smaller_sku.return_value = None
        recs = generate_recommendations([vm], metrics, CollectionThresholds(), catalog)
        subs = [r.subcategory for r in recs]
        assert PAAS_CANDIDATE not in subs


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_no_metrics_returns_empty(self):
        vm = _vm(zone=None, avset=None)
        catalog = MagicMock(spec=SkuCatalog)
        recs = generate_recommendations([vm], [], CollectionThresholds(), catalog)
        # Metric-dependent rules (underutilized, right-size) must not fire with no metrics.
        # Metadata-only rules (e.g. SWP-ARC-001) may still fire.
        size_subs = {r.subcategory for r in recs if r.subcategory in (UNDERUTILIZED, RIGHT_SIZE)}
        assert not size_subs, f"Metric-driven rules must not fire with no metrics, got: {size_subs}"

    def test_empty_vm_list(self):
        recs = generate_recommendations([], [], CollectionThresholds(), MagicMock())
        assert recs == []

    def test_only_one_size_recommendation_per_vm(self):
        """A VM should not get both underutilized AND right-size recommendations."""
        vm = _vm()
        # Triggers both underutilized (cpu 5<15, mem 10<20) and right-size (p95 35<40)
        metrics = _metrics_for(vm, cpu_avg=5.0, mem_avg=10.0, cpu_p95=35.0)
        catalog = _catalog_with()
        recs = generate_recommendations([vm], metrics, CollectionThresholds(), catalog)
        size_subs = [r.subcategory for r in recs if r.subcategory in (
            UNDERUTILIZED, RIGHT_SIZE
        )]
        assert len(size_subs) == 1, "Only one size recommendation per VM expected"
