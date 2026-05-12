"""Smoke tests for detectors.run_all() and the generate_recommendations() shim."""
from __future__ import annotations

import warnings
from unittest.mock import MagicMock

from cloudopt.analyzer import detectors
from cloudopt.analyzer.recommendations import generate_recommendations
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.models import CollectionThresholds, VmInventory, VmMetrics

SUB = "a1b2c3d4-0000-0000-0000-000000000007"


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


def _metrics(vm: VmInventory) -> list[VmMetrics]:
    return [
        VmMetrics(resource_id=vm.resource_id, metric_name="Percentage CPU", avg=5.0, p95=6.0),
        VmMetrics(resource_id=vm.resource_id, metric_name="Available Memory Bytes", avg=14_000_000_000),
    ]


def _catalog_smaller() -> SkuCatalog:
    cat = MagicMock(spec=SkuCatalog)
    cat.find_smaller_sku.return_value = "Standard_D2s_v5"
    return cat


class TestRunAll:
    def test_underutilized_vm_produces_at_least_one_finding(self):
        vm = _vm()
        findings = detectors.run_all([vm], _metrics(vm), [], CollectionThresholds(), _catalog_smaller())
        assert findings, "Expected ≥1 Finding for underutilized VM"

    def test_findings_have_valid_codes(self):
        vm = _vm()
        findings = detectors.run_all([vm], _metrics(vm), [], CollectionThresholds(), _catalog_smaller())
        for f in findings:
            assert f.code, f"Finding has empty code: {f}"

    def test_empty_inputs_return_empty_list(self):
        cat = MagicMock(spec=SkuCatalog)
        cat.find_smaller_sku.return_value = None
        findings = detectors.run_all([], [], [], CollectionThresholds(), cat)
        assert findings == []


class TestGenerateRecommendationsShim:
    def test_emits_deprecation_warning(self):
        vm = _vm()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            generate_recommendations([vm], _metrics(vm), CollectionThresholds(), _catalog_smaller())
        assert any(issubclass(x.category, DeprecationWarning) for x in w)

    def test_underutilized_vm_produces_at_least_one_recommendation(self):
        vm = _vm()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            recs = generate_recommendations([vm], _metrics(vm), CollectionThresholds(), _catalog_smaller())
        assert recs, "Expected ≥1 VmRecommendation for underutilized VM"

    def test_subcategory_is_underutilized(self):
        vm = _vm()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            recs = generate_recommendations([vm], _metrics(vm), CollectionThresholds(), _catalog_smaller())
        subs = [r.subcategory for r in recs]
        assert "underutilized" in subs
