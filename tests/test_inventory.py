"""Tests for the Resource Graph inventory collector."""
import pytest
from unittest.mock import MagicMock
from cloudopt.collector.inventory import _row_to_vm
from cloudopt.analyzer.sku_catalog import SkuCatalog, SkuSpec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_sku_catalog():
    catalog = MagicMock(spec=SkuCatalog)
    catalog.get.return_value = SkuSpec(vcpus=2, memory_gb=8.0)
    return catalog


def _make_row(**overrides):
    """Return a minimal Resource Graph row dict matching actual KQL output keys."""
    base = {
        "id": "/subscriptions/a1b2c3d4-0000-0000-0000-000000000000/resourceGroups/test-rg/providers/Microsoft.Compute/virtualMachines/test-vm",
        "name": "test-vm",
        "subscriptionId": "a1b2c3d4-0000-0000-0000-000000000000",
        "resourceGroup": "test-rg",
        "location": "eastus",
        "vmSku": "Standard_D2s_v3",
        "osType": "Linux",
        "zone": "1",
        "nicCount": 1,
        "osDiskSizeGb": 128,
        "dataDisks": [],
        "vmssName": None,
        "avSetName": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _row_to_vm
# ---------------------------------------------------------------------------

class TestRowToVm:
    def test_basic_conversion(self, mock_sku_catalog):
        row = _make_row()
        # _row_to_vm takes sub_map dict {sub_id: sub_name}, not a plain string
        sub_map = {"a1b2c3d4-0000-0000-0000-000000000000": "Test Sub"}
        vm = _row_to_vm(row, sub_map, mock_sku_catalog)

        assert vm.vm_name == "test-vm"
        assert vm.subscription_id == "a1b2c3d4-0000-0000-0000-000000000000"
        assert vm.subscription_name == "Test Sub"
        assert vm.resource_group == "test-rg"
        assert vm.vm_sku == "Standard_D2s_v3"
        assert vm.region == "eastus"
        assert vm.os_type == "Linux"
        assert vm.vcpus == 2
        assert vm.memory_gb == pytest.approx(8.0)

    def test_availability_zone_parsed(self, mock_sku_catalog):
        row = _make_row(zone="2")
        sub_map = {"a1b2c3d4-0000-0000-0000-000000000000": "Sub"}
        vm = _row_to_vm(row, sub_map, mock_sku_catalog)
        assert vm.availability_zone == "2"

    def test_no_zone_is_none(self, mock_sku_catalog):
        row = _make_row(zone=None)
        sub_map = {"a1b2c3d4-0000-0000-0000-000000000000": "Sub"}
        vm = _row_to_vm(row, sub_map, mock_sku_catalog)
        assert vm.availability_zone is None

    def test_vmss_name_captured(self, mock_sku_catalog):
        row = _make_row(vmssName="my-vmss")
        sub_map = {"a1b2c3d4-0000-0000-0000-000000000000": "Sub"}
        vm = _row_to_vm(row, sub_map, mock_sku_catalog)
        assert vm.vmss_name == "my-vmss"

    def test_availability_set_captured(self, mock_sku_catalog):
        row = _make_row(avSetName="my-avset")
        sub_map = {"a1b2c3d4-0000-0000-0000-000000000000": "Sub"}
        vm = _row_to_vm(row, sub_map, mock_sku_catalog)
        assert vm.availability_set_name == "my-avset"

    def test_missing_sku_falls_back_to_zero(self, mock_sku_catalog):
        mock_sku_catalog.get.return_value = None
        row = _make_row(vmSku="Standard_Unknown_v99")
        sub_map = {"a1b2c3d4-0000-0000-0000-000000000000": "Sub"}
        vm = _row_to_vm(row, sub_map, mock_sku_catalog)
        assert vm.vcpus == 0
        assert vm.memory_gb == pytest.approx(0.0)

    def test_resource_id_preserved(self, mock_sku_catalog):
        row = _make_row()
        sub_map = {"a1b2c3d4-0000-0000-0000-000000000000": "Sub"}
        vm = _row_to_vm(row, sub_map, mock_sku_catalog)
        assert "test-vm" in vm.resource_id
        assert "test-rg" in vm.resource_id

    def test_tags_ignored_gracefully(self, mock_sku_catalog):
        """Tags dict on the row should not break parsing."""
        row = _make_row(tags={"env": "prod", "owner": "team"})
        sub_map = {"a1b2c3d4-0000-0000-0000-000000000000": "Sub"}
        vm = _row_to_vm(row, sub_map, mock_sku_catalog)
        assert vm.vm_name == "test-vm"
