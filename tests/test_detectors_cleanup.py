"""Tests for detectors.cleanup (CLN-DSK-001, CLN-NIC-001, CLN-PIP-001, CLN-SNP-001)."""
from __future__ import annotations

from cloudopt.analyzer.detectors import cleanup
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.models import AzureResource, CollectionThresholds
from unittest.mock import MagicMock

SUB = "a1b2c3d4-0000-0000-0000-000000000004"
_T = CollectionThresholds()


def _resource(
    rid: str = "/subscriptions/{sub}/resourceGroups/rg/providers/",
    rtype: str = "microsoft.compute/disks",
    managed_by: str | None = None,
) -> AzureResource:
    return AzureResource(
        resource_id=f"/subscriptions/{SUB}/resourceGroups/rg/providers/{rtype}/res1",
        name="res1",
        resource_type=rtype,
        subscription_id=SUB,
        subscription_name="Test",
        resource_group="rg",
        location="eastus",
        managed_by=managed_by,
    )


def _catalog() -> SkuCatalog:
    cat = MagicMock(spec=SkuCatalog)
    return cat


class TestClnDsk001:
    def test_unattached_disk_emits_finding(self):
        r = _resource(rtype="microsoft.compute/disks", managed_by=None)
        findings = cleanup.detect([], [], [], _T, _catalog(), resources=[r])
        assert any(f.code == "CLN-DSK-001" for f in findings)

    def test_attached_disk_no_finding(self):
        r = _resource(rtype="microsoft.compute/disks", managed_by="/subscriptions/x/vm1")
        findings = cleanup.detect([], [], [], _T, _catalog(), resources=[r])
        assert all(f.code != "CLN-DSK-001" for f in findings)


class TestClnNic001:
    def test_unattached_nic_emits_finding(self):
        r = _resource(rtype="microsoft.network/networkinterfaces", managed_by=None)
        findings = cleanup.detect([], [], [], _T, _catalog(), resources=[r])
        assert any(f.code == "CLN-NIC-001" for f in findings)

    def test_attached_nic_no_finding(self):
        r = _resource(rtype="microsoft.network/networkinterfaces", managed_by="/subscriptions/x/vm1")
        findings = cleanup.detect([], [], [], _T, _catalog(), resources=[r])
        assert all(f.code != "CLN-NIC-001" for f in findings)


class TestClnPip001:
    def test_unassociated_pip_emits_finding(self):
        r = _resource(rtype="microsoft.network/publicipaddresses", managed_by=None)
        findings = cleanup.detect([], [], [], _T, _catalog(), resources=[r])
        assert any(f.code == "CLN-PIP-001" for f in findings)


class TestClnSnp001:
    def test_snapshot_always_emits_finding(self):
        r = _resource(rtype="microsoft.compute/snapshots")
        findings = cleanup.detect([], [], [], _T, _catalog(), resources=[r])
        assert any(f.code == "CLN-SNP-001" for f in findings)


class TestClnEmpty:
    def test_returns_empty_when_resources_is_none(self):
        findings = cleanup.detect([], [], [], _T, _catalog(), resources=None)
        assert findings == []

    def test_returns_empty_when_resources_is_empty_list(self):
        findings = cleanup.detect([], [], [], _T, _catalog(), resources=[])
        assert findings == []
