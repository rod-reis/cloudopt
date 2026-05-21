"""Phase 5 — QTA-OPS-001 Capacity Operations Hygiene tests (SPEC §5.x).

Tests cover:
  - All 5 sub-checks pass → no finding
  - Single failing sub-check → one finding per subscription
  - Multiple failing sub-checks → bundled in one finding
  - Exactly one finding per subscription (not one per sub-check)
  - Sub-check D only evaluated when CRGs exist
  - detector.detect() returns empty when capacity_alerts is None
"""
from __future__ import annotations

import pytest

from cloudopt.analyzer.detectors.ops_hygiene import detect, _evaluate_subscription
from cloudopt.models import (
    CapacityAlert,
    CapacityAlertType,
    CapacityReservationGroup,
    CapacityReservationItem,
    CollectionThresholds,
    VmInventory,
    VmMetrics,
)

SUB_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SUB_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SUB_NAME = "Test Subscription"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _vm(sub_id: str = SUB_A, sub_name: str = SUB_NAME) -> VmInventory:
    return VmInventory(
        vm_name="vm1",
        subscription_id=sub_id,
        subscription_name=sub_name,
        resource_group="rg1",
        resource_id=f"/subscriptions/{sub_id}/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1",
        vm_sku="Standard_D4s_v5",
        vcpus=4,
        memory_gb=16.0,
        region="eastus",
        os_type="Linux",
    )


def _metric_alert(sub_id: str, signals: list[str], enabled: bool = True) -> CapacityAlert:
    return CapacityAlert(
        resource_id=f"/subscriptions/{sub_id}/resourceGroups/rg/providers/Microsoft.Insights/metricAlerts/al1",
        subscription_id=sub_id,
        alert_type=CapacityAlertType.METRIC_ALERT,
        name="quota-alert",
        enabled=enabled,
        signals=signals,
        scopes=[f"/subscriptions/{sub_id}"],
    )


def _activity_alert(sub_id: str, signals: list[str], enabled: bool = True) -> CapacityAlert:
    return CapacityAlert(
        resource_id=f"/subscriptions/{sub_id}/resourceGroups/rg/providers/Microsoft.Insights/activityLogAlerts/al2",
        subscription_id=sub_id,
        alert_type=CapacityAlertType.ACTIVITY_LOG_ALERT,
        name="alloc-failed-alert",
        enabled=enabled,
        signals=signals,
        scopes=[f"/subscriptions/{sub_id}"],
    )


def _svc_health_alert(sub_id: str, signals: list[str] | None = None, enabled: bool = True) -> CapacityAlert:
    return CapacityAlert(
        resource_id=f"/subscriptions/{sub_id}/resourceGroups/rg/providers/Microsoft.Insights/activityLogAlerts/svc",
        subscription_id=sub_id,
        alert_type=CapacityAlertType.SERVICE_HEALTH_ALERT,
        name="svc-health-compute",
        enabled=enabled,
        signals=signals or ["compute"],
        scopes=[f"/subscriptions/{sub_id}"],
    )


def _crg(sub_id: str) -> CapacityReservationGroup:
    return CapacityReservationGroup(
        group_id=f"/subscriptions/{sub_id}/resourceGroups/rg/providers/Microsoft.Compute/capacityReservationGroups/crg1",
        group_name="crg1",
        subscription_id=sub_id,
        resource_group="rg",
        region="eastus",
        reservations=[
            CapacityReservationItem(
                reservation_name="res1",
                sku_name="Standard_D4s_v5",
                reserved_count=4,
                used_count=2,
            )
        ],
    )


def _full_alerts(sub_id: str) -> list[CapacityAlert]:
    """Return a complete set of passing alerts for a subscription."""
    return [
        _metric_alert(sub_id, ["quota.percentUsage"]),
        _activity_alert(sub_id, ["AllocationFailed"]),
        _activity_alert(sub_id, ["QuotaExceeded"]),
        _svc_health_alert(sub_id, ["compute"]),
    ]


# ---------------------------------------------------------------------------
# _evaluate_subscription unit tests
# ---------------------------------------------------------------------------


class TestEvaluateSubscription:
    def test_all_pass_returns_none(self):
        alerts = _full_alerts(SUB_A)
        result = _evaluate_subscription(SUB_A, SUB_NAME, alerts, has_crgs=False)
        assert result is None

    def test_no_alerts_all_fail(self):
        result = _evaluate_subscription(SUB_A, SUB_NAME, [], has_crgs=False)
        assert result is not None
        assert result.code == "QTA-OPS-001"

    def test_missing_quota_alert_fails_check_a(self):
        alerts = [
            _activity_alert(SUB_A, ["AllocationFailed"]),
            _activity_alert(SUB_A, ["QuotaExceeded"]),
            _svc_health_alert(SUB_A, ["compute"]),
        ]
        result = _evaluate_subscription(SUB_A, SUB_NAME, alerts, has_crgs=False)
        assert result is not None
        subchecks = result.deltas["subchecks"]
        a_check = next(c for c in subchecks if c["label"].startswith("A:"))
        assert a_check["pass"] is False

    def test_missing_alloc_alert_fails_check_b(self):
        alerts = [
            _metric_alert(SUB_A, ["quota.percentUsage"]),
            _activity_alert(SUB_A, ["QuotaExceeded"]),
            _svc_health_alert(SUB_A, ["compute"]),
        ]
        result = _evaluate_subscription(SUB_A, SUB_NAME, alerts, has_crgs=False)
        assert result is not None
        subchecks = result.deltas["subchecks"]
        b_check = next(c for c in subchecks if c["label"].startswith("B:"))
        assert b_check["pass"] is False

    def test_missing_quota_exceeded_fails_check_c(self):
        alerts = [
            _metric_alert(SUB_A, ["quota.percentUsage"]),
            _activity_alert(SUB_A, ["AllocationFailed"]),
            _svc_health_alert(SUB_A, ["compute"]),
        ]
        result = _evaluate_subscription(SUB_A, SUB_NAME, alerts, has_crgs=False)
        assert result is not None
        subchecks = result.deltas["subchecks"]
        c_check = next(c for c in subchecks if c["label"].startswith("C:"))
        assert c_check["pass"] is False

    def test_svc_health_alert_passes_check_e(self):
        alerts = [
            _metric_alert(SUB_A, ["quota.percentUsage"]),
            _activity_alert(SUB_A, ["AllocationFailed"]),
            _activity_alert(SUB_A, ["QuotaExceeded"]),
            _svc_health_alert(SUB_A, ["compute"]),
        ]
        result = _evaluate_subscription(SUB_A, SUB_NAME, alerts, has_crgs=False)
        assert result is None  # all pass

    def test_missing_svc_health_fails_check_e(self):
        alerts = [
            _metric_alert(SUB_A, ["quota.percentUsage"]),
            _activity_alert(SUB_A, ["AllocationFailed"]),
            _activity_alert(SUB_A, ["QuotaExceeded"]),
            # no service health alert
        ]
        result = _evaluate_subscription(SUB_A, SUB_NAME, alerts, has_crgs=False)
        assert result is not None
        subchecks = result.deltas["subchecks"]
        e_check = next(c for c in subchecks if c["label"].startswith("E:"))
        assert e_check["pass"] is False

    def test_disabled_alert_does_not_satisfy_check(self):
        alerts = [
            _metric_alert(SUB_A, ["quota.percentUsage"], enabled=False),  # disabled
            _activity_alert(SUB_A, ["AllocationFailed"]),
            _activity_alert(SUB_A, ["QuotaExceeded"]),
            _svc_health_alert(SUB_A, ["compute"]),
        ]
        result = _evaluate_subscription(SUB_A, SUB_NAME, alerts, has_crgs=False)
        assert result is not None
        subchecks = result.deltas["subchecks"]
        a_check = next(c for c in subchecks if c["label"].startswith("A:"))
        assert a_check["pass"] is False

    def test_subcheck_d_not_present_when_no_crgs(self):
        result = _evaluate_subscription(SUB_A, SUB_NAME, [], has_crgs=False)
        assert result is not None
        subchecks = result.deltas["subchecks"]
        d_checks = [c for c in subchecks if c["label"].startswith("D:")]
        assert d_checks == []

    def test_subcheck_d_present_when_crgs_exist(self):
        result = _evaluate_subscription(SUB_A, SUB_NAME, [], has_crgs=True)
        assert result is not None
        subchecks = result.deltas["subchecks"]
        d_checks = [c for c in subchecks if c["label"].startswith("D:")]
        assert len(d_checks) == 1

    def test_finding_is_high_confidence_ready(self):
        result = _evaluate_subscription(SUB_A, SUB_NAME, [], has_crgs=False)
        assert result is not None
        from cloudopt.analyzer.taxonomy import Confidence, Readiness
        assert result.confidence == Confidence.HIGH
        assert result.readiness == Readiness.READY
        assert result.confidence_score == 90

    def test_all_pass_with_alloc_failure_keyword_variant(self):
        """SkuNotAvailable variant should also satisfy sub-check B."""
        alerts = [
            _metric_alert(SUB_A, ["quota.percentUsage"]),
            _activity_alert(SUB_A, ["SkuNotAvailable"]),
            _activity_alert(SUB_A, ["QuotaExceeded"]),
            _svc_health_alert(SUB_A, ["compute"]),
        ]
        result = _evaluate_subscription(SUB_A, SUB_NAME, alerts, has_crgs=False)
        assert result is None

    def test_rationale_lists_failing_subchecks(self):
        result = _evaluate_subscription(SUB_A, SUB_NAME, [], has_crgs=False)
        assert result is not None
        # Should mention checks A, B, C, E
        for check_label in ("A:", "B:", "C:", "E:"):
            assert check_label in result.rationale


# ---------------------------------------------------------------------------
# detect() integration tests
# ---------------------------------------------------------------------------


class TestDetect:
    def test_returns_empty_when_alerts_is_none(self):
        vms = [_vm()]
        result = detect(vms, [], [], CollectionThresholds(), capacity_alerts=None)
        assert result == []

    def test_empty_alerts_list_emits_finding(self):
        """Empty list means the collector ran and found no alerts — all sub-checks fail."""
        vms = [_vm()]
        result = detect(vms, [], [], CollectionThresholds(), capacity_alerts=[])
        assert len(result) == 1

    def test_one_finding_per_subscription(self):
        vms = [_vm(SUB_A), _vm(SUB_B, "Sub B")]
        result = detect(vms, [], [], CollectionThresholds(), capacity_alerts=[])
        # Empty alerts: both subs have failing checks
        assert len(result) == 2
        subs = {f.vm_id for f in result}
        assert f"/subscriptions/{SUB_A}" in subs
        assert f"/subscriptions/{SUB_B}" in subs

    def test_no_finding_when_all_pass(self):
        vms = [_vm(SUB_A)]
        alerts = _full_alerts(SUB_A)
        result = detect(vms, [], [], CollectionThresholds(), capacity_alerts=alerts)
        assert result == []

    def test_crg_subcheck_d_triggered(self):
        vms = [_vm(SUB_A)]
        crg = _crg(SUB_A)
        # Provide all other alerts but no CRR alert
        alerts = _full_alerts(SUB_A)
        result = detect(
            vms, [], [], CollectionThresholds(),
            capacity_alerts=alerts,
            crg_items=[crg],
        )
        assert len(result) == 1
        subchecks = result[0].deltas["subchecks"]
        d_checks = [c for c in subchecks if c["label"].startswith("D:")]
        assert len(d_checks) == 1
        assert d_checks[0]["pass"] is False

    def test_no_crg_subcheck_d_absent(self):
        vms = [_vm(SUB_A)]
        alerts = _full_alerts(SUB_A)
        result = detect(vms, [], [], CollectionThresholds(), capacity_alerts=alerts, crg_items=[])
        assert result == []  # all pass (no CRGs → D not evaluated)

    def test_finding_code_is_qta_ops_001(self):
        vms = [_vm(SUB_A)]
        result = detect(vms, [], [], CollectionThresholds(), capacity_alerts=[])
        assert len(result) == 1
        assert result[0].code == "QTA-OPS-001"
