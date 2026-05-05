"""Tests for data models and subscription ID masking."""
import pytest
from cloudopt.models import (
    CollectionThresholds,
    VmInventory,
    VmRecommendation,
    VmMetrics,
    DailyDataPoint,
    mask_subscription_id,
    mask_subscription_ids_in_string,
)


# ---------------------------------------------------------------------------
# mask_subscription_id
# ---------------------------------------------------------------------------

class TestMaskSubscriptionId:
    def test_standard_guid_keeps_first_8_chars(self):
        guid = "a1b2c3d4-ef56-7890-abcd-ef1234567890"
        result = mask_subscription_id(guid)
        assert result == "a1b2c3d4-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

    def test_preserves_case_of_first_segment(self):
        guid = "AABBCCDD-ef56-7890-abcd-ef1234567890"
        result = mask_subscription_id(guid)
        assert result.startswith("AABBCCDD")

    def test_short_guid_returns_as_is(self):
        """Non-standard GUIDs (too short) should not crash."""
        result = mask_subscription_id("short")
        assert isinstance(result, str)

    def test_empty_string(self):
        result = mask_subscription_id("")
        assert result == ""


class TestMaskSubscriptionIdsInString:
    def test_replaces_guid_in_resource_id(self):
        resource_id = (
            "/subscriptions/a1b2c3d4-ef56-7890-abcd-ef1234567890"
            "/resourceGroups/my-rg/providers/Microsoft.Compute/virtualMachines/my-vm"
        )
        result = mask_subscription_ids_in_string(resource_id)
        assert "a1b2c3d4-xxxx-xxxx-xxxx-xxxxxxxxxxxx" in result
        assert "my-rg" in result
        assert "my-vm" in result

    def test_replaces_multiple_guids(self):
        text = "a1b2c3d4-0000-0000-0000-000000000001 and b2c3d4e5-0000-0000-0000-000000000002"
        result = mask_subscription_ids_in_string(text)
        assert "a1b2c3d4-xxxx" in result
        assert "b2c3d4e5-xxxx" in result

    def test_string_without_guids_unchanged(self):
        text = "no guids here"
        assert mask_subscription_ids_in_string(text) == text


# ---------------------------------------------------------------------------
# CollectionThresholds
# ---------------------------------------------------------------------------

class TestCollectionThresholds:
    def test_defaults(self):
        t = CollectionThresholds()
        assert t.underutilized_cpu_avg == pytest.approx(15.0)
        assert t.underutilized_memory_avg == pytest.approx(20.0)
        assert t.oversize_cpu_p95 == pytest.approx(40.0)
        assert t.headroom_multiplier == pytest.approx(1.2)
        assert t.paas_candidate_cpu_avg == pytest.approx(10.0)

    def test_custom_values(self):
        t = CollectionThresholds(underutilized_cpu_avg=25.0)
        assert t.underutilized_cpu_avg == pytest.approx(25.0)
        assert t.underutilized_memory_avg == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# VmInventory
# ---------------------------------------------------------------------------

class TestVmInventory:
    def _make_vm(self, **kwargs):
        defaults = dict(
            vm_name="test-vm",
            subscription_id="a1b2c3d4-ef56-7890-abcd-ef1234567890",
            subscription_name="Test Sub",
            resource_group="test-rg",
            resource_id="/subscriptions/a1b2c3d4-ef56-7890-abcd-ef1234567890/resourceGroups/test-rg/providers/Microsoft.Compute/virtualMachines/test-vm",
            vm_sku="Standard_D2s_v3",
            vcpus=2,
            memory_gb=8.0,
            region="eastus",
            os_type="Linux",
        )
        defaults.update(kwargs)
        return VmInventory(**defaults)

    def test_creation_with_defaults(self):
        vm = self._make_vm()
        assert vm.vm_name == "test-vm"
        assert vm.workload is None
        assert vm.application is None
        assert vm.environment is None
        assert vm.criticality is None
        assert vm.owner is None
        assert vm.custom is None

    def test_csa_editable_fields(self):
        vm = self._make_vm(
            workload="SAP",
            application="Finance App",
            environment="Production",
            criticality="High",
            owner="team-infra",
            custom_notes="Maintenance window: Sunday 2am",
        )
        assert vm.workload == "SAP"
        assert vm.environment == "Production"

    def test_masked_subscription_id(self):
        vm = self._make_vm()
        assert vm.masked_subscription_id() == "a1b2c3d4-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

    def test_masked_resource_id_masks_guid_segment(self):
        vm = self._make_vm()
        masked = vm.masked_resource_id()
        assert "a1b2c3d4-xxxx-xxxx-xxxx-xxxxxxxxxxxx" in masked
        assert "test-rg" in masked
        assert "test-vm" in masked

    def test_optional_fields_default_to_none(self):
        vm = self._make_vm()
        assert vm.vmss_name is None
        assert vm.availability_set_name is None
        assert vm.availability_zone is None


# ---------------------------------------------------------------------------
# VmRecommendation
# ---------------------------------------------------------------------------

class TestVmRecommendation:
    def _make_rec(self, **kwargs):
        defaults = dict(
            resource_id="/subscriptions/a1b2c3d4-ef56-7890-abcd-ef1234567890/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm",
            current_sku="Standard_D4s_v3",
            category="underutilized",
            reason="Low CPU and memory utilisation",
        )
        defaults.update(kwargs)
        return VmRecommendation(**defaults)

    def test_creation_defaults(self):
        rec = self._make_rec()
        assert rec.manual_override is None
        assert rec.recommended_sku is None
        assert rec.estimated_savings_pct is None

    def test_override_enum(self):
        rec = self._make_rec(manual_override="accept")
        assert rec.manual_override == "accept"

    def test_masked_resource_id(self):
        rec = self._make_rec()
        masked = rec.masked_resource_id()
        assert "a1b2c3d4-xxxx" in masked
        assert "vm" in masked


# ---------------------------------------------------------------------------
# VmMetrics
# ---------------------------------------------------------------------------

class TestVmMetrics:
    def test_creation(self):
        m = VmMetrics(
            resource_id="/subscriptions/a1b2c3d4-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm",
            metric_name="Percentage CPU",
            avg=25.0,
            p50=23.0,
            p95=55.0,
            max=88.0,
            min=2.0,
            time_series=[DailyDataPoint(date="2026-04-01T00:00:00Z", value=25.0)],
        )
        assert m.avg == pytest.approx(25.0)
        assert len(m.time_series) == 1
        assert m.time_series[0].date == "2026-04-01T00:00:00Z"

    def test_empty_time_series(self):
        m = VmMetrics(
            resource_id="/subscriptions/a1b2c3d4-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm",
            metric_name="Percentage CPU",
            time_series=[],
        )
        assert m.time_series == []
        assert m.avg is None
