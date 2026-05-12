"""Tests for detectors.quota (QTA-OVR-001, QTA-WRN-001, QTA-CRI-001, QTA-CRG-001)."""
from __future__ import annotations

from cloudopt.analyzer.detectors import quota
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.models import CollectionThresholds, QuotaItem
from unittest.mock import MagicMock

SUB1 = "a1b2c3d4-0000-0000-0000-000000000005"
SUB2 = "a1b2c3d4-0000-0000-0000-000000000006"
_T = CollectionThresholds()


def _q(
    sub: str = SUB1,
    util_pct: float = 50.0,
    limit: int = 100,
    usage: int = 50,
    region: str = "eastus",
    rtype: str = "Standard_D_Family_vCPUs",
) -> QuotaItem:
    return QuotaItem(
        subscription_id=sub,
        subscription_name="Test",
        region=region,
        resource_type=rtype,
        display_name=rtype,
        current_usage=usage,
        quota_limit=limit,
        utilization_pct=util_pct,
        alert=util_pct >= 80.0,
    )


def _catalog() -> SkuCatalog:
    return MagicMock(spec=SkuCatalog)


class TestQtaCri001:
    def test_emits_cri_when_above_critical_threshold(self):
        q = _q(util_pct=90.0, usage=90)
        findings = quota.detect([], [], [q], _T, _catalog())
        assert any(f.code == "QTA-CRI-001" for f in findings)

    def test_no_cri_when_below_threshold(self):
        q = _q(util_pct=80.0, usage=80)
        findings = quota.detect([], [], [q], _T, _catalog())
        assert all(f.code != "QTA-CRI-001" for f in findings)


class TestQtaCrg001:
    def test_emits_crg_when_donor_exists_in_same_region(self):
        # receiver in critical state
        receiver = _q(sub=SUB1, util_pct=90.0, usage=90, region="eastus")
        # donor in same region with low utilization
        donor = _q(sub=SUB2, util_pct=30.0, usage=30, limit=100, region="eastus")
        findings = quota.detect([], [], [receiver, donor], _T, _catalog())
        assert any(f.code == "QTA-CRG-001" for f in findings)

    def test_no_crg_when_no_donor(self):
        # single critical sub with no donors
        q = _q(sub=SUB1, util_pct=90.0, usage=90)
        findings = quota.detect([], [], [q], _T, _catalog())
        assert all(f.code != "QTA-CRG-001" for f in findings)


class TestQtaWrn001:
    def test_emits_wrn_between_warning_and_critical(self):
        # 70 <= util < 85
        q = _q(util_pct=75.0, usage=75)
        findings = quota.detect([], [], [q], _T, _catalog())
        assert any(f.code == "QTA-WRN-001" for f in findings)

    def test_no_wrn_above_critical(self):
        q = _q(util_pct=90.0, usage=90)
        findings = quota.detect([], [], [q], _T, _catalog())
        assert all(f.code != "QTA-WRN-001" for f in findings)


class TestQtaOvr001:
    def test_emits_ovr_when_low_utilization_and_receiver_exists(self):
        # low-util sub (donor candidate)
        low = _q(sub=SUB1, util_pct=10.0, usage=10, region="eastus")
        # receiver in same region
        high = _q(sub=SUB2, util_pct=75.0, usage=75, region="eastus")
        findings = quota.detect([], [], [low, high], _T, _catalog())
        assert any(f.code == "QTA-OVR-001" for f in findings)

    def test_no_ovr_without_receiver(self):
        low = _q(sub=SUB1, util_pct=10.0, usage=10)
        findings = quota.detect([], [], [low], _T, _catalog())
        assert all(f.code != "QTA-OVR-001" for f in findings)


class TestQtaEmpty:
    def test_empty_quota_returns_empty_list(self):
        findings = quota.detect([], [], [], _T, _catalog())
        assert findings == []

    def test_zero_limit_skipped(self):
        q = _q(util_pct=90.0, limit=0, usage=0)
        findings = quota.detect([], [], [q], _T, _catalog())
        assert findings == []
