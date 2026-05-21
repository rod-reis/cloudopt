"""QTA-OPS-001 detector — Capacity Operations Hygiene.

Emits exactly ONE Finding per subscription (keyed by subscription_id) when
one or more of five sub-checks fail.  The finding bundles all missing signals
so remediation is targeted and the blast radius stays low.

Sub-checks:
  A. Quota usage alert       — active metric alert on quota.percentUsage ≥ 80%
  B. Allocation-failure alert — active activity-log alert on AllocationFailed /
                                SkuNotAvailable / ZonalAllocationFailed
  C. QuotaExceeded alert     — active activity-log alert on QuotaExceeded
  D. CRR under-utilization alert — only when CRGs exist in scope; active alert
                                   on Microsoft.Compute/capacityReservationGroups
  E. Service Health alert     — active Service Health alert subscription for
                                Compute

Confidence: HIGH (ARM alert enumeration is authoritative — no metric sampling
needed).  Readiness: READY when any sub-check fails.
"""

from __future__ import annotations

import json
from typing import Optional

from cloudopt.analyzer.taxonomy import Category, Confidence, FindingType, Readiness, SubCategory
from cloudopt.models import (
    CapacityAlert,
    CapacityAlertType,
    CapacityReservationGroup,
    CollectionThresholds,
    Finding,
    QuotaItem,
    VmInventory,
    VmMetrics,
)

# ---------------------------------------------------------------------------
# Sub-check signal constants
# ---------------------------------------------------------------------------

# Sub-check A: quota usage alert — metric names that indicate a quota threshold alert
_QUOTA_METRIC_SIGNALS: frozenset[str] = frozenset({
    "quota.percentusage",
    "usages",
    "currentusage",
    "currentvalue",
    "quota",
})

# Sub-check B: allocation failure signals
_ALLOC_FAILURE_SIGNALS: frozenset[str] = frozenset({
    "allocationfailed",
    "skunotavailable",
    "zonalallocationfailed",
    "allocatefailed",
    "microsoft.compute/virtualmachines/write",  # broad compute write failure
})

# Sub-check C: quota-exceeded signals
_QUOTA_EXCEEDED_SIGNALS: frozenset[str] = frozenset({
    "quotaexceeded",
    "operationnotallowed",
})

# Sub-check D: CRR scope types
_CRR_RESOURCE_TYPE = "microsoft.compute/capacityreservationgroups"

# Sub-check E: service health keywords
_SERVICE_HEALTH_COMPUTE_KEYWORDS: frozenset[str] = frozenset({
    "compute",
    "virtualmachines",
    "microsoft.compute",
})


# ---------------------------------------------------------------------------
# Sub-check helpers
# ---------------------------------------------------------------------------


def _signal_matches(alert_signals: list[str], targets: frozenset[str]) -> bool:
    for s in alert_signals:
        if s.lower().rstrip("/") in targets:
            return True
        for t in targets:
            if t in s.lower():
                return True
    return False


def _scope_matches_subscription(scopes: list[str], sub_id: str) -> bool:
    """Return True if any scope in the alert covers the given subscription."""
    if not scopes:
        return True  # subscription-level alert with no explicit scope list
    sub_id_lower = sub_id.lower()
    for scope in scopes:
        if sub_id_lower in scope.lower():
            return True
    return False


def _scope_contains_crr(scopes: list[str]) -> bool:
    for scope in scopes:
        if _CRR_RESOURCE_TYPE in scope.lower():
            return True
    return False


def _service_health_covers_compute(alert: CapacityAlert) -> bool:
    """Return True if a service-health alert covers the Compute category."""
    combined = " ".join(alert.signals + alert.scopes).lower()
    for kw in _SERVICE_HEALTH_COMPUTE_KEYWORDS:
        if kw in combined:
            return True
    # If signals are empty the alert may be a catch-all service-health rule
    if not alert.signals and alert.alert_type is CapacityAlertType.SERVICE_HEALTH_ALERT:
        return True
    return False


# ---------------------------------------------------------------------------
# Per-subscription sub-check evaluation
# ---------------------------------------------------------------------------


class _SubCheck:
    def __init__(self, label: str, why_missing: str):
        self.label = label
        self.passed = False
        self.why = why_missing  # description shown when the check fails

    def to_dict(self) -> dict:
        return {"label": self.label, "pass": self.passed, "why": self.why if not self.passed else ""}


def _evaluate_subscription(
    sub_id: str,
    sub_name: str,
    alerts: list[CapacityAlert],
    has_crgs: bool,
) -> Optional[Finding]:
    """Evaluate the five sub-checks for one subscription.

    Returns a Finding if any sub-check fails, or None if all pass.
    """
    active_metric = [a for a in alerts if a.enabled and a.alert_type is CapacityAlertType.METRIC_ALERT]
    active_activity = [a for a in alerts if a.enabled and a.alert_type is CapacityAlertType.ACTIVITY_LOG_ALERT]
    active_svc_health = [a for a in alerts if a.enabled and a.alert_type is CapacityAlertType.SERVICE_HEALTH_ALERT]
    active_sqr = [a for a in alerts if a.enabled and a.alert_type is CapacityAlertType.SCHEDULED_QUERY_RULE]

    checks: list[_SubCheck] = []

    # Sub-check A — quota usage metric alert (≥80%)
    ck_a = _SubCheck(
        "A: Quota usage alert",
        "No active metric alert found for compute quota usage ≥ 80%. "
        "Create an Azure Monitor metric alert on 'quota.percentUsage' for "
        "Microsoft.Compute vCPU quotas with threshold 80%.",
    )
    for a in active_metric + active_sqr:
        if (
            _scope_matches_subscription(a.scopes, sub_id)
            and _signal_matches(a.signals, _QUOTA_METRIC_SIGNALS)
        ):
            ck_a.passed = True
            break
    checks.append(ck_a)

    # Sub-check B — allocation-failure activity-log alert
    ck_b = _SubCheck(
        "B: Allocation/SkuNotAvailable failure alert",
        "No active activity-log alert found for AllocationFailed, SkuNotAvailable, "
        "or ZonalAllocationFailed. Create an Activity Log alert on these operation "
        "results to detect capacity blocks before they impact deployments.",
    )
    for a in active_activity:
        if (
            _scope_matches_subscription(a.scopes, sub_id)
            and _signal_matches(a.signals, _ALLOC_FAILURE_SIGNALS)
        ):
            ck_b.passed = True
            break
    checks.append(ck_b)

    # Sub-check C — QuotaExceeded deployment-failure alert
    ck_c = _SubCheck(
        "C: QuotaExceeded deployment-failure alert",
        "No active activity-log alert found for QuotaExceeded. Create an "
        "Activity Log alert on 'QuotaExceeded' or 'OperationNotAllowed' "
        "to detect quota-blocked deployments.",
    )
    for a in active_activity:
        if (
            _scope_matches_subscription(a.scopes, sub_id)
            and _signal_matches(a.signals, _QUOTA_EXCEEDED_SIGNALS)
        ):
            ck_c.passed = True
            break
    checks.append(ck_c)

    # Sub-check D — CRR under-utilization alert (only when CRGs exist)
    if has_crgs:
        ck_d = _SubCheck(
            "D: CRR under-utilization alert",
            "Capacity Reservation Groups are in use but no alert monitors "
            "CRG utilization (usedCount / reservedCount). Create a metric "
            "alert on Microsoft.Compute/capacityReservationGroups to flag "
            "when utilization drops below 50%.",
        )
        for a in active_metric:
            if (
                _scope_matches_subscription(a.scopes, sub_id)
                and _scope_contains_crr(a.scopes + a.signals)
            ):
                ck_d.passed = True
                break
        checks.append(ck_d)

    # Sub-check E — Service Health alert for Compute
    ck_e = _SubCheck(
        "E: Service Health alert (Compute)",
        "No active Service Health alert found for the Compute service. "
        "Create an Azure Monitor Service Health alert scoped to "
        "Microsoft.Compute to receive retirement and impairment notices.",
    )
    for a in active_svc_health:
        if (
            _scope_matches_subscription(a.scopes, sub_id)
            and _service_health_covers_compute(a)
        ):
            ck_e.passed = True
            break
    checks.append(ck_e)

    # If all pass — no finding
    failing = [c for c in checks if not c.passed]
    if not failing:
        return None

    # Build rationale
    rationale_parts = [
        f"Subscription '{sub_name}' is missing {len(failing)} of "
        f"{len(checks)} proactive capacity monitoring signals:"
    ]
    for c in failing:
        rationale_parts.append(f"  • {c.label}: {c.why}")
    rationale = "\n".join(rationale_parts)

    return Finding(
        vm_id=f"/subscriptions/{sub_id}",
        category=Category.QUOTA,
        subcategory=SubCategory.QUOTA_OPS_HYGIENE,
        code="QTA-OPS-001",
        finding_type=FindingType.RECOMMENDATION,
        current=None,
        proposed=None,
        confidence=Confidence.HIGH,
        confidence_score=90,
        readiness=Readiness.READY,
        deltas={"subchecks": [c.to_dict() for c in checks]},
        evidence_sources=["microsoft.insights/metricalerts", "microsoft.insights/activitylogalerts"],
        rationale=rationale,
        blockers_to_high=[],  # HIGH by construction — ARM alert enumeration is authoritative
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def detect(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    quota_items: list[QuotaItem],
    thresholds: CollectionThresholds,
    *,
    capacity_alerts: Optional[list[CapacityAlert]] = None,
    crg_items: Optional[list[CapacityReservationGroup]] = None,
) -> list[Finding]:
    """Emit at most one QTA-OPS-001 Finding per subscription.

    Emits a finding for every subscription that has at least one failing
    sub-check.  Returns an empty list when all sub-checks pass or when
    ``capacity_alerts`` is None / empty.
    """
    if capacity_alerts is None:
        return []

    # Build per-subscription data
    sub_names: dict[str, str] = {}
    for vm in vms:
        sub_names.setdefault(vm.subscription_id, vm.subscription_name)
    for qi in quota_items:
        sub_names.setdefault(qi.subscription_id, getattr(qi, "subscription_name", qi.subscription_id))

    # Gather all subscription IDs in scope
    all_subs: set[str] = set(sub_names.keys())
    # Also add any subscriptions that appear only in alerts
    for a in capacity_alerts:
        if a.subscription_id:
            all_subs.add(a.subscription_id)

    # Check which subscriptions have CRGs
    subs_with_crgs: set[str] = set()
    if crg_items:
        for crg in crg_items:
            subs_with_crgs.add(crg.subscription_id)

    # Group alerts by subscription
    alerts_by_sub: dict[str, list[CapacityAlert]] = {}
    for a in capacity_alerts:
        sub = a.subscription_id
        if not sub:
            # subscription-level scope: broadcast to all in-scope subs
            for s in all_subs:
                alerts_by_sub.setdefault(s, []).append(a)
        else:
            alerts_by_sub.setdefault(sub, []).append(a)

    out: list[Finding] = []
    for sub_id in sorted(all_subs):
        sub_name = sub_names.get(sub_id, sub_id)
        sub_alerts = alerts_by_sub.get(sub_id, [])
        has_crgs = sub_id in subs_with_crgs
        finding = _evaluate_subscription(sub_id, sub_name, sub_alerts, has_crgs)
        if finding:
            out.append(finding)

    return out
