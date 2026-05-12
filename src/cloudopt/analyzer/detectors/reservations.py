"""RSV-UND-001, RSV-EXP-001, RSV-UNC-001, CRR-UNU-001, CRR-UNF-001 detectors.

SPEC §2.6 — Reservations & Capacity Reservations.

No $ / cost fields in any Finding.  Counts, percentages, and dates only
(SPEC §1.2, §2.6, §13).

``Finding.vm_id`` holds the reservation order ID or CRG resource ID for
non-VM findings (same convention as quota.py which uses quota pseudo-IDs).
"""

from __future__ import annotations

import datetime

from cloudopt.analyzer.detectors._shared import _candidate_kwargs, _rec_kwargs
from cloudopt.analyzer.taxonomy import Category, SubCategory
from cloudopt.models import (
    CapacityReservationGroup,
    CollectionThresholds,
    Finding,
    ReservationOrder,
    VmInventory,
    VmMetrics,
)

# --------------------------------------------------------------------------
# Duration blocker message used for CRR findings (snapshot can't verify
# the "≥ 30 days" requirement from SPEC §2.6).
# --------------------------------------------------------------------------
_CRR_DURATION_BLOCKER = (
    "Duration \u2265 30 days unverified from single-snapshot collection"
)


def detect(
    reservations: list[ReservationOrder],
    capacity_reservations: list[CapacityReservationGroup],
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    thresholds: CollectionThresholds,
    *,
    _today: datetime.date | None = None,
) -> list[Finding]:
    """Run all §2.6 detectors and return combined Finding list.

    Args:
        reservations:          RI / Savings Plan order records.
        capacity_reservations: CRG records.
        vms:                   VM inventory (for RSV-UNC-001).
        metrics:               Platform metrics (for RSV-UNC-001).
        thresholds:            Detection thresholds.
        _today:                Override for "today" (test injection only).
    """
    today = _today or datetime.date.today()
    out: list[Finding] = []

    # Pre-build CPU p95 index keyed by resource_id
    cpu_p95_by_vm = _build_cpu_index(metrics)

    # Pre-build covered (sku_lower, region_lower, sub_id) set for RSV-UNC-001
    covered = _covered_tuples(reservations)

    for r in reservations:
        f = _check_underutilized(r, thresholds)
        if f is not None:
            out.append(f)
        f = _check_expiring(r, thresholds, today=today)
        if f is not None:
            out.append(f)

    for vm in vms:
        f = _check_uncovered(vm, cpu_p95_by_vm.get(vm.resource_id), covered, thresholds)
        if f is not None:
            out.append(f)

    for crg in capacity_reservations:
        f = _check_unused(crg)
        if f is not None:
            out.append(f)
        f = _check_underfilled(crg)
        if f is not None:
            out.append(f)

    return out


# ---------------------------------------------------------------------------
# RSV-UND-001
# ---------------------------------------------------------------------------

def _check_underutilized(
    r: ReservationOrder,
    thresholds: CollectionThresholds,
) -> Finding | None:
    if r.utilization_pct is None:
        return None
    if r.utilization_pct >= thresholds.rsvp_underutilized_pct:
        return None
    kwargs = _rec_kwargs()
    kwargs["deltas"] = {
        "utilization_pct": r.utilization_pct,
        "reserved_count": r.reserved_count,
    }
    return Finding(
        vm_id=r.order_id,
        category=Category.RSVP,
        subcategory=SubCategory.RSVP_UNDERUTILIZED,
        code="RSV-UND-001",
        current=f"{r.utilization_pct:.0f}% utilised of {r.reserved_count} reserved",
        proposed=None,
        rationale=(
            f"RI/SP '{r.display_name}' averaged {r.utilization_pct:.0f}% utilisation"
            f" over the last 30 days (threshold: {thresholds.rsvp_underutilized_pct:.0f}%)."
        ),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# RSV-EXP-001
# ---------------------------------------------------------------------------

def _check_expiring(
    r: ReservationOrder,
    thresholds: CollectionThresholds,
    *,
    today: datetime.date,
) -> Finding | None:
    if not r.expiry_date:
        return None
    try:
        expiry = datetime.date.fromisoformat(r.expiry_date[:10])
    except ValueError:
        return None
    days_to_expiry = (expiry - today).days
    if days_to_expiry > thresholds.rsvp_expiring_days:
        return None
    kwargs = _rec_kwargs()
    kwargs["deltas"] = {
        "days_to_expiry": days_to_expiry,
        "reserved_count": r.reserved_count,
    }
    return Finding(
        vm_id=r.order_id,
        category=Category.RSVP,
        subcategory=SubCategory.RSVP_EXPIRING,
        code="RSV-EXP-001",
        current=f"expires {r.expiry_date} ({days_to_expiry} days)",
        proposed=None,
        rationale=(
            f"RI/SP '{r.display_name}' expires on {r.expiry_date}"
            f" ({days_to_expiry} days from now)."
        ),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# RSV-UNC-001
# ---------------------------------------------------------------------------

_STOPPED_STATES = {"deallocated", "stopped"}


def _check_uncovered(
    vm: VmInventory,
    cpu_p95: float | None,
    covered: set[tuple[str, str, str]],
    thresholds: CollectionThresholds,
) -> Finding | None:
    """Emit RSV-UNC-001 when a steady-state VM has no RI/SP coverage."""
    # Skip stopped / deallocated VMs
    power = (vm.power_state or "").lower()
    if any(s in power for s in _STOPPED_STATES):
        return None

    # Skip VMs with insufficient CPU data or below the steady-state threshold
    if cpu_p95 is None:
        return None
    if cpu_p95 <= thresholds.rsvp_uncovered_cpu_p95_pct:
        return None

    # Check coverage: (sku_lower, region_lower, sub_id)
    key = (vm.vm_sku.lower(), vm.region.lower(), vm.subscription_id)
    if key in covered:
        return None

    kwargs = _candidate_kwargs()
    kwargs["deltas"] = {"cpu_p95": cpu_p95}
    return Finding(
        vm_id=vm.resource_id,
        category=Category.RSVP,
        subcategory=SubCategory.RSVP_UNCOVERED_STEADY,
        code="RSV-UNC-001",
        current=f"{vm.vm_sku} on-demand, p95 CPU {cpu_p95:.1f}%",
        proposed=None,
        rationale=(
            f"VM {vm.vm_name} appears steady-state (p95 CPU {cpu_p95:.1f}%"
            f" > {thresholds.rsvp_uncovered_cpu_p95_pct:.0f}%) but has no"
            " RI/SP coverage matching its SKU and region."
        ),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# CRR-UNU-001
# ---------------------------------------------------------------------------

def _check_unused(crg: CapacityReservationGroup) -> Finding | None:
    if crg.used_count_total > 0:
        return None
    kwargs = _rec_kwargs()
    kwargs["confidence"].__class__  # keep linter happy; confidence is set below
    from cloudopt.analyzer.taxonomy import Confidence, Readiness
    from cloudopt.models import FindingType as FT

    return Finding(
        vm_id=crg.group_id,
        category=Category.CRR,
        subcategory=SubCategory.CRR_UNUSED,
        code="CRR-UNU-001",
        finding_type=FT.RECOMMENDATION,
        current=f"0 of {crg.reserved_count_total} reserved slots used",
        proposed=None,
        deltas={
            "reserved_count": crg.reserved_count_total,
            "used_count": 0,
        },
        evidence_sources=["platform"],
        confidence=Confidence.LOW,
        readiness=Readiness.INSUFFICIENT,
        blockers_to_high=[_CRR_DURATION_BLOCKER],
        customer_inputs_needed=[],
        rationale=(
            f"CRG '{crg.group_name}' in {crg.region} has"
            f" {crg.reserved_count_total} reserved slot(s) but 0 VMs allocated."
        ),
    )


# ---------------------------------------------------------------------------
# CRR-UNF-001
# ---------------------------------------------------------------------------

def _check_underfilled(crg: CapacityReservationGroup) -> Finding | None:
    if crg.reserved_count_total <= crg.used_count_total:
        return None
    from cloudopt.analyzer.taxonomy import Confidence, Readiness
    from cloudopt.models import FindingType as FT

    fill_pct = crg.fill_rate_pct or 0.0
    return Finding(
        vm_id=crg.group_id,
        category=Category.CRR,
        subcategory=SubCategory.CRR_UNDERFILLED,
        code="CRR-UNF-001",
        finding_type=FT.RECOMMENDATION,
        current=(
            f"{crg.used_count_total} of {crg.reserved_count_total}"
            f" reserved slots used ({fill_pct:.0f}%)"
        ),
        proposed=None,
        deltas={
            "reserved_count": crg.reserved_count_total,
            "used_count": crg.used_count_total,
            "fill_rate_pct": fill_pct,
        },
        evidence_sources=["platform"],
        confidence=Confidence.LOW,
        readiness=Readiness.INSUFFICIENT,
        blockers_to_high=[_CRR_DURATION_BLOCKER],
        customer_inputs_needed=[],
        rationale=(
            f"CRG '{crg.group_name}' in {crg.region} has"
            f" {crg.reserved_count_total} reserved slot(s) but only"
            f" {crg.used_count_total} are currently allocated ({fill_pct:.0f}% fill rate)."
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_cpu_index(metrics: list[VmMetrics]) -> dict[str, float]:
    """Return mapping resource_id → p95 CPU value."""
    index: dict[str, float] = {}
    for m in metrics:
        if "cpu" in m.metric_name.lower() and m.p95 is not None:
            # Keep the highest p95 if multiple CPU metrics exist
            existing = index.get(m.resource_id)
            if existing is None or m.p95 > existing:
                index[m.resource_id] = m.p95
    return index


def _covered_tuples(
    reservations: list[ReservationOrder],
) -> set[tuple[str, str, str]]:
    """Return a set of (sku_lower, region_lower, sub_id) tuples covered by RIs/SPs.

    For 'Shared' scope reservations, every subscription in ``applied_scope_ids``
    is considered covered.  For 'Single' scope, only the specific subscription.
    """
    covered: set[tuple[str, str, str]] = set()
    for r in reservations:
        sku = r.sku_name.lower()
        region = r.region.lower()
        for sub_id in r.applied_scope_ids:
            covered.add((sku, region, sub_id))
    return covered
