"""Tests for detectors.disk_pv2 (SWP-DST-002 — Premium SSD v1 → v2)."""
from __future__ import annotations

from cloudopt.analyzer.detectors import disk_pv2
from cloudopt.analyzer.taxonomy import Category, Confidence, FindingType, SubCategory
from cloudopt.models import DiskInventory

SUB = "a1b2c3d4-0000-0000-0000-000000000002"
_VM_ID = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1"


def _disk(
    name: str = "disk1",
    sku_name: str | None = "Premium_LRS",
    os_type: str | None = None,
    disk_state: str | None = "Attached",
    managed_by: str | None = _VM_ID,
    **kw,
) -> DiskInventory:
    return DiskInventory(
        resource_id=f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/disks/{name}",
        disk_name=name,
        subscription_id=SUB,
        subscription_name="Test",
        resource_group="rg",
        location="eastus",
        sku_name=sku_name,
        performance_tier=kw.pop("performance_tier", "P30"),
        disk_size_gb=kw.pop("disk_size_gb", 1024),
        disk_iops_read_write=kw.pop("disk_iops_read_write", 5000),
        disk_mbps_read_write=kw.pop("disk_mbps_read_write", 200),
        disk_state=disk_state,
        os_type=os_type,
        managed_by=managed_by,
        **kw,
    )


class TestDiskPv2Detect:
    def test_attached_premium_v1_data_disk_emits_finding(self):
        findings = disk_pv2.detect([_disk()])
        assert len(findings) == 1
        f = findings[0]
        assert f.code == "SWP-DST-002"
        assert f.category is Category.SWAP
        assert f.subcategory is SubCategory.DISK_TIER
        assert f.finding_type is FindingType.RECOMMENDATION
        assert f.current == "Premium_LRS"
        assert f.proposed == "PremiumV2_LRS"
        assert f.vm_id == _disk().resource_id

    def test_finding_is_medium_with_blockers(self):
        f = disk_pv2.detect([_disk()])[0]
        assert f.confidence is Confidence.MEDIUM
        assert f.blockers_to_high  # non-empty, required by validator
        assert f.customer_inputs_needed  # migration prerequisites present

    def test_rationale_is_efficiency_framed_not_cost(self):
        f = disk_pv2.detect([_disk()])[0]
        text = (f.rationale + " ".join(f.blockers_to_high)).lower()
        for banned in ("cost", "save", "saving", "cheaper", "spend", "bill", "$"):
            assert banned not in text

    def test_os_disk_not_flagged(self):
        assert disk_pv2.detect([_disk(os_type="Linux")]) == []

    def test_standard_disk_not_flagged(self):
        assert disk_pv2.detect([_disk(sku_name="Standard_LRS")]) == []

    def test_premium_v2_disk_not_flagged(self):
        assert disk_pv2.detect([_disk(sku_name="PremiumV2_LRS")]) == []

    def test_unattached_disk_not_flagged(self):
        assert disk_pv2.detect([_disk(disk_state="Unattached", managed_by=None)]) == []

    def test_empty_and_none_inputs(self):
        assert disk_pv2.detect([]) == []
        assert disk_pv2.detect(None) == []

    def test_shared_disk_adds_multi_attach_input(self):
        disk = _disk(managed_by_extended=[_VM_ID, _VM_ID + "-2"])
        f = disk_pv2.detect([disk])[0]
        assert any("shared" in s.lower() for s in f.customer_inputs_needed)

    def test_mixed_fleet_only_flags_candidates(self):
        disks = [
            _disk(name="ok-pv1"),
            _disk(name="os", os_type="Windows"),
            _disk(name="std", sku_name="StandardSSD_LRS"),
            _disk(name="unattached", disk_state="Unattached", managed_by=None),
        ]
        findings = disk_pv2.detect(disks)
        assert {f.vm_id for f in findings} == {_disk(name="ok-pv1").resource_id}
