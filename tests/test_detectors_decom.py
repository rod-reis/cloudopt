"""Tests for detectors.decom (DCM-STP-001, DCM-DLC-001, DCM-ENV-001)."""
from __future__ import annotations

from cloudopt.analyzer.detectors import decom
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.models import CollectionThresholds, VmInventory
from unittest.mock import MagicMock

SUB = "a1b2c3d4-0000-0000-0000-000000000003"
_T = CollectionThresholds()


def _vm(
    name: str = "vm1",
    sku: str = "Standard_D4s_v5",
    vcpus: int = 4,
    power_state: str | None = None,
    environment: str | None = None,
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
        power_state=power_state,
        environment=environment,
    )


def _catalog() -> SkuCatalog:
    cat = MagicMock(spec=SkuCatalog)
    cat.find_smaller_sku.return_value = None
    return cat


class TestDcmStp001:
    def test_deallocated_emits_finding(self):
        vm = _vm(power_state="powerstate/deallocated")
        findings = decom.detect([vm], [], [], _T, _catalog())
        assert any(f.code == "DCM-STP-001" for f in findings)

    def test_stopped_emits_finding(self):
        vm = _vm(power_state="powerstate/stopped")
        findings = decom.detect([vm], [], [], _T, _catalog())
        assert any(f.code == "DCM-STP-001" for f in findings)

    def test_running_vm_no_finding(self):
        vm = _vm(power_state="powerstate/running")
        findings = decom.detect([vm], [], [], _T, _catalog())
        assert all(f.code != "DCM-STP-001" for f in findings)

    def test_no_power_state_no_finding(self):
        vm = _vm(power_state=None)
        findings = decom.detect([vm], [], [], _T, _catalog())
        assert all(f.code != "DCM-STP-001" for f in findings)


class TestDcmDlc001:
    def test_emits_when_flag_on_dev_env_large_vm(self):
        vm = _vm(sku="Standard_D16s_v5", vcpus=16, environment="dev")
        findings = decom.detect([vm], [], [], _T, _catalog(), enable_dlc=True)
        assert any(f.code == "DCM-DLC-001" for f in findings)

    def test_suppressed_when_flag_off(self):
        vm = _vm(sku="Standard_D16s_v5", vcpus=16, environment="dev")
        findings = decom.detect([vm], [], [], _T, _catalog(), enable_dlc=False)
        assert all(f.code != "DCM-DLC-001" for f in findings)

    def test_suppressed_when_not_dev_env(self):
        vm = _vm(sku="Standard_D16s_v5", vcpus=16, environment="prod")
        findings = decom.detect([vm], [], [], _T, _catalog(), enable_dlc=True)
        assert all(f.code != "DCM-DLC-001" for f in findings)

    def test_suppressed_when_vcpus_below_threshold(self):
        vm = _vm(sku="Standard_D8s_v5", vcpus=8, environment="dev")
        findings = decom.detect([vm], [], [], _T, _catalog(), enable_dlc=True)
        assert all(f.code != "DCM-DLC-001" for f in findings)


class TestDcmEnv001:
    def test_emits_when_flag_on_and_no_environment(self):
        vm = _vm(environment=None)
        findings = decom.detect([vm], [], [], _T, _catalog(), enable_env_check=True)
        assert any(f.code == "DCM-ENV-001" for f in findings)

    def test_suppressed_when_flag_off(self):
        vm = _vm(environment=None)
        findings = decom.detect([vm], [], [], _T, _catalog(), enable_env_check=False)
        assert all(f.code != "DCM-ENV-001" for f in findings)

    def test_suppressed_when_environment_is_set(self):
        vm = _vm(environment="prod")
        findings = decom.detect([vm], [], [], _T, _catalog(), enable_env_check=True)
        assert all(f.code != "DCM-ENV-001" for f in findings)
