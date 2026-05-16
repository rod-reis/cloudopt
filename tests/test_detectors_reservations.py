"""TDD tests for SPEC §2.6 capacity-reservation detectors.

Covers:
  CRR-UNU-001  crg.unused
  CRR-UNF-001  crg.underfilled
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cloudopt.analyzer.detectors import reservations as det
from cloudopt.analyzer.detectors import run_all
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.analyzer.taxonomy import Category, SubCategory
from cloudopt.models import (
    CapacityReservationGroup,
    CapacityReservationItem,
    CollectionThresholds,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_THRESHOLDS = CollectionThresholds()


def _crg(
    *,
    group_id: str = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Compute/capacityReservationGroups/crg1",
    group_name: str = "crg1",
    subscription_id: str = "00000000-0000-0000-0000-000000000001",
    resource_group: str = "rg",
    region: str = "eastus",
    zones: list[str] | None = None,
    reserved_count: int = 5,
    used_count: int = 0,
) -> CapacityReservationGroup:
    item = CapacityReservationItem(
        reservation_name="cr1",
        sku_name="Standard_D4s_v5",
        reserved_count=reserved_count,
        used_count=used_count,
    )
    return CapacityReservationGroup(
        group_id=group_id,
        group_name=group_name,
        subscription_id=subscription_id,
        resource_group=resource_group,
        region=region,
        zones=zones or [],
        reservations=[item],
    )


# ---------------------------------------------------------------------------
# CRR-UNU-001 — unused CRG
# ---------------------------------------------------------------------------

class TestCrrUnused:
    def test_fires_when_all_used_zero(self):
        crg = _crg(reserved_count=5, used_count=0)
        findings = det.detect(capacity_reservations=[crg])
        assert any(f.code == "CRR-UNU-001" for f in findings)

    def test_no_fire_when_has_vm(self):
        crg = _crg(reserved_count=5, used_count=2)
        findings = det.detect(capacity_reservations=[crg])
        assert not any(f.code == "CRR-UNU-001" for f in findings)

    def test_finding_category(self):
        crg = _crg(reserved_count=4, used_count=0)
        findings = det.detect(capacity_reservations=[crg])
        f = next(f for f in findings if f.code == "CRR-UNU-001")
        assert f.category is Category.CRR
        assert f.subcategory is SubCategory.CRR_UNUSED
        assert "$" not in str(f.deltas)


# ---------------------------------------------------------------------------
# CRR-UNF-001 — underfilled CRG
# ---------------------------------------------------------------------------

class TestCrrUnderfilled:
    def test_fires_when_reserved_gt_used(self):
        crg = _crg(reserved_count=5, used_count=3)
        findings = det.detect(capacity_reservations=[crg])
        assert any(f.code == "CRR-UNF-001" for f in findings)

    def test_no_fire_when_fully_used(self):
        crg = _crg(reserved_count=5, used_count=5)
        findings = det.detect(capacity_reservations=[crg])
        assert not any(f.code == "CRR-UNF-001" for f in findings)

    def test_finding_category(self):
        crg = _crg(reserved_count=5, used_count=1)
        findings = det.detect(capacity_reservations=[crg])
        f = next(f for f in findings if f.code == "CRR-UNF-001")
        assert f.category is Category.CRR
        assert f.subcategory is SubCategory.CRR_UNDERFILLED
        assert "$" not in str(f.deltas)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_no_findings_empty_inputs(self):
        findings = det.detect(capacity_reservations=[])
        assert findings == []

    def test_run_all_includes_crr_findings(self):
        catalog = SkuCatalog(MagicMock())
        with patch.object(catalog, "find_smaller_sku", return_value=None):
            findings = run_all(
                vms=[],
                metrics=[],
                quota_items=[],
                thresholds=_THRESHOLDS,
                catalog=catalog,
                crg_items=[_crg(reserved_count=5, used_count=0)],
            )
        assert any(f.code == "CRR-UNU-001" for f in findings)


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestCrgModel:
    def test_total_counts(self):
        crg = CapacityReservationGroup(
            group_id="/subscriptions/00000000/resourceGroups/rg/providers/Microsoft.Compute/capacityReservationGroups/g",
            group_name="g",
            subscription_id="00000000-0000-0000-0000-000000000001",
            resource_group="rg",
            region="eastus",
            reservations=[
                CapacityReservationItem(reservation_name="r1", sku_name="Standard_D4s_v5", reserved_count=3, used_count=2),
                CapacityReservationItem(reservation_name="r2", sku_name="Standard_D4s_v5", reserved_count=2, used_count=1),
            ],
        )
        assert crg.reserved_count_total == 5
        assert crg.used_count_total == 3
