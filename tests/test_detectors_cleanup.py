"""Tests for detectors.cleanup (CLN-DSK-001, CLN-NIC-001, CLN-PIP-001, CLN-SNP-001)."""
from __future__ import annotations

import datetime

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


class TestClnDsk001ThirtyDayThreshold:
    """CLN-DSK-001 should enforce the 30-day min age for orphaned disks."""

    def _disk(self, time_created: str | None = None) -> AzureResource:
        return AzureResource(
            resource_id=f"/subscriptions/{SUB}/resourceGroups/rg/providers/microsoft.compute/disks/d1",
            name="d1",
            resource_type="microsoft.compute/disks",
            subscription_id=SUB,
            subscription_name="Test",
            resource_group="rg",
            location="eastus",
            managed_by=None,
            time_created=time_created,
        )

    def test_suppressed_when_disk_is_recent(self):
        """Disk created 5 days ago should not yet be flagged as orphaned."""
        recent = (
            datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(days=5)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = self._disk(time_created=recent)
        findings = cleanup.detect([], [], [], _T, _catalog(), resources=[r])
        assert all(f.code != "CLN-DSK-001" for f in findings)

    def test_fires_when_disk_is_old_enough(self):
        """Disk created 45 days ago should be flagged."""
        old = (
            datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(days=45)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = self._disk(time_created=old)
        findings = cleanup.detect([], [], [], _T, _catalog(), resources=[r])
        assert any(f.code == "CLN-DSK-001" for f in findings)

    def test_fires_with_age_note_when_time_created_missing(self):
        """When time_created is None, finding is still emitted but with unconfirmed-age note."""
        r = self._disk(time_created=None)
        findings = cleanup.detect([], [], [], _T, _catalog(), resources=[r])
        cln = [f for f in findings if f.code == "CLN-DSK-001"]
        assert cln, "should still emit finding when creation date is unknown"
        assert "unconfirmed" in cln[0].rationale.lower() or "unavailable" in cln[0].rationale.lower()

    def test_fires_at_exactly_30_days(self):
        """Disk created exactly 30 days ago should cross the threshold and be flagged."""
        exactly_30 = (
            datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(days=30)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = self._disk(time_created=exactly_30)
        findings = cleanup.detect([], [], [], _T, _catalog(), resources=[r])
        assert any(f.code == "CLN-DSK-001" for f in findings)
