"""TDD tests for SPEC §2.6 reservation / capacity-reservation detectors.

Covers:
  RSV-UND-001  rsvp.underutilized
  RSV-EXP-001  rsvp.expiring
  RSV-UNC-001  rsvp.uncovered-steady
  CRR-UNU-001  crg.unused
  CRR-UNF-001  crg.underfilled
"""

from __future__ import annotations

import datetime

import pytest

from cloudopt.analyzer.detectors import reservations as det
from cloudopt.analyzer.taxonomy import Category, FindingType, SubCategory
from cloudopt.models import (
    CapacityReservationGroup,
    CapacityReservationItem,
    CollectionThresholds,
    ReservationOrder,
    VmInventory,
    VmMetrics,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_THRESHOLDS = CollectionThresholds()

_TODAY = datetime.date(2025, 6, 1)


def _order(
    *,
    order_id: str = "/providers/Microsoft.Capacity/reservationOrders/ord-1",
    display_name: str = "Prod-Reservation",
    term: str = "P1Y",
    expiry_date: str = "2026-06-01",
    sku_name: str = "Standard_D4s_v5",
    region: str = "eastus",
    reserved_count: int = 10,
    applied_scope_type: str = "Shared",
    applied_scope_ids: list[str] | None = None,
    utilization_pct: float | None = 75.0,
) -> ReservationOrder:
    return ReservationOrder(
        order_id=order_id,
        display_name=display_name,
        term=term,
        expiry_date=expiry_date,
        sku_name=sku_name,
        region=region,
        reserved_count=reserved_count,
        applied_scope_type=applied_scope_type,
        applied_scope_ids=applied_scope_ids or ["00000000-0000-0000-0000-000000000001"],
        utilization_pct=utilization_pct,
    )


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


def _vm(
    *,
    resource_id: str = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
    subscription_id: str = "00000000-0000-0000-0000-000000000001",
    vm_sku: str = "Standard_D4s_v5",
    region: str = "eastus",
    power_state: str = "PowerState/running",
) -> VmInventory:
    return VmInventory(
        resource_id=resource_id,
        subscription_id=subscription_id,
        subscription_name="Prod",
        resource_group="rg",
        vm_name="vm1",
        vm_sku=vm_sku,
        vcpus=4,
        memory_gb=16.0,
        region=region,
        os_type="Linux",
        power_state=power_state,
    )


def _metrics_with_cpu(
    *,
    resource_id: str = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
    cpu_p95: float = 25.0,
) -> VmMetrics:
    return VmMetrics(
        resource_id=resource_id,
        metric_name="Percentage CPU",
        p95=cpu_p95,
        avg=15.0,
    )


# ---------------------------------------------------------------------------
# RSV-UND-001 — underutilized
# ---------------------------------------------------------------------------

class TestRsvUnderutilized:
    def test_fires_below_threshold(self):
        findings = det.detect(
            reservations=[_order(utilization_pct=70.0)],
            capacity_reservations=[],
            vms=[],
            metrics=[],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        codes = [f.code for f in findings]
        assert "RSV-UND-001" in codes

    def test_no_fire_at_threshold(self):
        findings = det.detect(
            reservations=[_order(utilization_pct=80.0)],
            capacity_reservations=[],
            vms=[],
            metrics=[],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        assert not any(f.code == "RSV-UND-001" for f in findings)

    def test_no_fire_when_utilization_unavailable(self):
        findings = det.detect(
            reservations=[_order(utilization_pct=None)],
            capacity_reservations=[],
            vms=[],
            metrics=[],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        assert not any(f.code == "RSV-UND-001" for f in findings)

    def test_finding_is_recommendation(self):
        findings = det.detect(
            reservations=[_order(utilization_pct=50.0)],
            capacity_reservations=[],
            vms=[],
            metrics=[],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        f = next(f for f in findings if f.code == "RSV-UND-001")
        assert f.finding_type is FindingType.RECOMMENDATION
        assert f.category is Category.RSVP
        assert f.subcategory is SubCategory.RSVP_UNDERUTILIZED

    def test_deltas_contain_utilization_no_dollar(self):
        findings = det.detect(
            reservations=[_order(utilization_pct=60.0)],
            capacity_reservations=[],
            vms=[],
            metrics=[],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        f = next(f for f in findings if f.code == "RSV-UND-001")
        # No $ in deltas
        assert "$" not in str(f.deltas)
        assert "utilization_pct" in f.deltas


# ---------------------------------------------------------------------------
# RSV-EXP-001 — expiring
# ---------------------------------------------------------------------------

class TestRsvExpiring:
    def test_fires_within_60_days(self):
        expiry = (_TODAY + datetime.timedelta(days=30)).isoformat()
        findings = det.detect(
            reservations=[_order(expiry_date=expiry, utilization_pct=None)],
            capacity_reservations=[],
            vms=[],
            metrics=[],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        assert any(f.code == "RSV-EXP-001" for f in findings)

    def test_fires_at_exactly_60_days(self):
        expiry = (_TODAY + datetime.timedelta(days=60)).isoformat()
        findings = det.detect(
            reservations=[_order(expiry_date=expiry, utilization_pct=None)],
            capacity_reservations=[],
            vms=[],
            metrics=[],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        assert any(f.code == "RSV-EXP-001" for f in findings)

    def test_no_fire_beyond_60_days(self):
        expiry = (_TODAY + datetime.timedelta(days=61)).isoformat()
        findings = det.detect(
            reservations=[_order(expiry_date=expiry, utilization_pct=None)],
            capacity_reservations=[],
            vms=[],
            metrics=[],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        assert not any(f.code == "RSV-EXP-001" for f in findings)

    def test_finding_category(self):
        expiry = (_TODAY + datetime.timedelta(days=10)).isoformat()
        findings = det.detect(
            reservations=[_order(expiry_date=expiry, utilization_pct=None)],
            capacity_reservations=[],
            vms=[],
            metrics=[],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        f = next(f for f in findings if f.code == "RSV-EXP-001")
        assert f.category is Category.RSVP
        assert f.subcategory is SubCategory.RSVP_EXPIRING
        # No $ in current field
        assert "$" not in (f.current or "")


# ---------------------------------------------------------------------------
# RSV-UNC-001 — uncovered-steady
# ---------------------------------------------------------------------------

class TestRsvUncovered:
    def test_fires_for_steady_uncovered_vm(self):
        vm = _vm()
        m = _metrics_with_cpu(resource_id=vm.resource_id, cpu_p95=30.0)
        findings = det.detect(
            reservations=[],  # no reservations → no coverage
            capacity_reservations=[],
            vms=[vm],
            metrics=[m],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        assert any(f.code == "RSV-UNC-001" for f in findings)

    def test_no_fire_when_covered(self):
        vm = _vm(subscription_id="00000000-0000-0000-0000-000000000001")
        m = _metrics_with_cpu(resource_id=vm.resource_id, cpu_p95=30.0)
        # Reservation covers the same SKU/region/subscription
        r = _order(
            sku_name="Standard_D4s_v5",
            region="eastus",
            applied_scope_type="Shared",
            applied_scope_ids=["00000000-0000-0000-0000-000000000001"],
            utilization_pct=None,
        )
        findings = det.detect(
            reservations=[r],
            capacity_reservations=[],
            vms=[vm],
            metrics=[m],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        assert not any(f.code == "RSV-UNC-001" for f in findings)

    def test_no_fire_when_cpu_p95_below_threshold(self):
        vm = _vm()
        m = _metrics_with_cpu(resource_id=vm.resource_id, cpu_p95=15.0)
        findings = det.detect(
            reservations=[],
            capacity_reservations=[],
            vms=[vm],
            metrics=[m],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        assert not any(f.code == "RSV-UNC-001" for f in findings)

    def test_no_fire_when_vm_stopped(self):
        vm = _vm(power_state="PowerState/stopped")
        m = _metrics_with_cpu(resource_id=vm.resource_id, cpu_p95=30.0)
        findings = det.detect(
            reservations=[],
            capacity_reservations=[],
            vms=[vm],
            metrics=[m],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        assert not any(f.code == "RSV-UNC-001" for f in findings)

    def test_finding_is_candidate(self):
        vm = _vm()
        m = _metrics_with_cpu(resource_id=vm.resource_id, cpu_p95=30.0)
        findings = det.detect(
            reservations=[],
            capacity_reservations=[],
            vms=[vm],
            metrics=[m],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        f = next(f for f in findings if f.code == "RSV-UNC-001")
        assert f.finding_type is FindingType.CANDIDATE
        assert f.category is Category.RSVP
        assert f.subcategory is SubCategory.RSVP_UNCOVERED_STEADY


# ---------------------------------------------------------------------------
# CRR-UNU-001 — unused CRG
# ---------------------------------------------------------------------------

class TestCrrUnused:
    def test_fires_when_all_used_zero(self):
        crg = _crg(reserved_count=5, used_count=0)
        findings = det.detect(
            reservations=[],
            capacity_reservations=[crg],
            vms=[],
            metrics=[],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        assert any(f.code == "CRR-UNU-001" for f in findings)

    def test_no_fire_when_has_vm(self):
        crg = _crg(reserved_count=5, used_count=2)
        findings = det.detect(
            reservations=[],
            capacity_reservations=[crg],
            vms=[],
            metrics=[],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        assert not any(f.code == "CRR-UNU-001" for f in findings)

    def test_finding_category(self):
        crg = _crg(reserved_count=4, used_count=0)
        findings = det.detect(
            reservations=[],
            capacity_reservations=[crg],
            vms=[],
            metrics=[],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
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
        findings = det.detect(
            reservations=[],
            capacity_reservations=[crg],
            vms=[],
            metrics=[],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        assert any(f.code == "CRR-UNF-001" for f in findings)

    def test_no_fire_when_fully_used(self):
        crg = _crg(reserved_count=5, used_count=5)
        findings = det.detect(
            reservations=[],
            capacity_reservations=[crg],
            vms=[],
            metrics=[],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        assert not any(f.code == "CRR-UNF-001" for f in findings)

    def test_finding_category(self):
        crg = _crg(reserved_count=5, used_count=1)
        findings = det.detect(
            reservations=[],
            capacity_reservations=[crg],
            vms=[],
            metrics=[],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        f = next(f for f in findings if f.code == "CRR-UNF-001")
        assert f.category is Category.CRR
        assert f.subcategory is SubCategory.CRR_UNDERFILLED
        assert "$" not in str(f.deltas)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_no_findings_empty_inputs(self):
        findings = det.detect(
            reservations=[],
            capacity_reservations=[],
            vms=[],
            metrics=[],
            thresholds=_THRESHOLDS,
            _today=_TODAY,
        )
        assert findings == []

    def test_run_all_includes_rsvp_findings(self):
        from unittest.mock import MagicMock, patch
        from cloudopt.analyzer.detectors import run_all
        from cloudopt.analyzer.sku_catalog import SkuCatalog

        vm = _vm()
        m = _metrics_with_cpu(resource_id=vm.resource_id, cpu_p95=30.0)
        catalog = SkuCatalog(MagicMock())
        with patch.object(catalog, "find_smaller_sku", return_value=None):
            findings = run_all(
                vms=[vm],
                metrics=[m],
                quota_items=[],
                thresholds=_THRESHOLDS,
                catalog=catalog,
                rsvp_orders=[],
                crg_items=[_crg(reserved_count=5, used_count=0)],
            )
        assert any(f.code == "CRR-UNU-001" for f in findings)


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestReservationModel:
    def test_masks_scope_ids(self):
        r = ReservationOrder(
            order_id="/providers/Microsoft.Capacity/reservationOrders/ord-1",
            display_name="Prod",
            term="P1Y",
            expiry_date="2026-01-01",
            sku_name="Standard_D4s_v5",
            region="eastus",
            reserved_count=5,
            applied_scope_type="Single",
            applied_scope_ids=["a1b2c3d4-1234-5678-abcd-000000000001"],
            utilization_pct=90.0,
        )
        masked = r.masked_applied_scope_ids()
        assert all("xxxx" in m for m in masked)
        assert all("-1234-" not in m for m in masked)


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
