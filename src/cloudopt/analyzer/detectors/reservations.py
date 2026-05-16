"""CRR-UNU-001, CRR-UNF-001 detectors.

SPEC §2.6 — Capacity Reservation Groups.

No $ / cost fields in any Finding.  Counts and percentages only
(SPEC §1.2, §2.6, §13).

``Finding.vm_id`` holds the CRG resource ID
(same convention as quota.py which uses quota pseudo-IDs).
"""

from __future__ import annotations

from cloudopt.analyzer.taxonomy import Category, SubCategory
from cloudopt.models import (
    CapacityReservationGroup,
    Finding,
)

# --------------------------------------------------------------------------
# Duration blocker message used for CRR findings (snapshot can't verify
# the "≥ 30 days" requirement from SPEC §2.6).
# --------------------------------------------------------------------------
_CRR_DURATION_BLOCKER = (
    "Duration \u2265 30 days unverified from single-snapshot collection"
)


def detect(
    capacity_reservations: list[CapacityReservationGroup],
) -> list[Finding]:
    """Run CRR §2.6 detectors and return combined Finding list.

    Args:
        capacity_reservations: CRG records.
    """
    out: list[Finding] = []

    for crg in capacity_reservations:
        f = _check_unused(crg)
        if f is not None:
            out.append(f)
        f = _check_underfilled(crg)
        if f is not None:
            out.append(f)

    return out


# ---------------------------------------------------------------------------
# CRR-UNU-001
# ---------------------------------------------------------------------------

def _check_unused(crg: CapacityReservationGroup) -> Finding | None:
    if crg.used_count_total > 0:
        return None
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

