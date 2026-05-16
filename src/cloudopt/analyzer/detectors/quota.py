"""QTA-OVR-001, QTA-WRN-001, QTA-CRI-001, QTA-CRG-001 detectors — quota signals.

Uses the canonical SPEC §2.5 thresholds from CollectionThresholds (70 / 85 / 20%).
The legacy ``generate_quota_recommendations()`` shim in ``recommendations.py``
preserves the old 75 / 85 / 15 / 25% tiers for backward compatibility.

Finding.vm_id is used as a resource identifier; for quota findings it holds
the quota pseudo-resource-ID (not a real VM resource ID).
"""

from __future__ import annotations

from cloudopt.analyzer.detectors._shared import _rec_kwargs
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.analyzer.taxonomy import Category, SubCategory
from cloudopt.models import (
    CollectionThresholds,
    Finding,
    QuotaItem,
    VmInventory,
    VmMetrics,
)

_XSUB_DONOR_MAX_PCT = 40.0  # subscriptions with < 40% util can donate


def detect(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    quota: list[QuotaItem],
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
) -> list[Finding]:
    """Emit QTA-* Findings from a list of QuotaItem records."""
    if not quota:
        return []

    # Pre-compute receivers by (region, resource_type) for cross-sub logic
    receivers_by_key: dict[tuple[str, str], set[str]] = {}
    for q in quota:
        if q.quota_limit > 0 and q.utilization_pct >= thresholds.quota_warning_pct:
            key = (q.region.lower(), q.resource_type)
            receivers_by_key.setdefault(key, set()).add(q.subscription_id)

    # Pre-compute donors by (region, resource_type)
    donors_by_key: dict[tuple[str, str], list[QuotaItem]] = {}
    for q in quota:
        if q.quota_limit > 0 and q.utilization_pct < _XSUB_DONOR_MAX_PCT:
            key = (q.region.lower(), q.resource_type)
            donors_by_key.setdefault(key, []).append(q)

    out: list[Finding] = []
    for q in quota:
        if q.quota_limit <= 0:
            continue
        f = _classify(q, receivers_by_key, donors_by_key, thresholds)
        if f is not None:
            out.append(f)
    return out


def _quota_resource_id(q: QuotaItem) -> str:
    return (
        f"/subscriptions/{q.subscription_id}/providers/Microsoft.Capacity"
        f"/locations/{q.region}/usages/{q.resource_type}"
    )


def _classify(
    q: QuotaItem,
    receivers_by_key: dict[tuple[str, str], set[str]],
    donors_by_key: dict[tuple[str, str], list[QuotaItem]],
    thresholds: CollectionThresholds,
) -> Finding | None:
    key = (q.region.lower(), q.resource_type)

    if q.utilization_pct >= thresholds.quota_critical_pct:
        return _critical_finding(q, key, donors_by_key)

    if q.utilization_pct >= thresholds.quota_warning_pct:
        return _warning_finding(q)

    if q.utilization_pct <= thresholds.quota_oversized_pct:
        receivers = receivers_by_key.get(key, set()) - {q.subscription_id}
        if not receivers:
            return None
        return _oversized_finding(q, len(receivers))

    return None


def _critical_finding(
    q: QuotaItem,
    key: tuple[str, str],
    donors_by_key: dict[tuple[str, str], list[QuotaItem]],
) -> Finding:
    donors = [
        d for d in donors_by_key.get(key, [])
        if d.subscription_id != q.subscription_id
    ]
    if donors:
        return _crg_finding(q, donors)
    return _cri_finding(q)


def _cri_finding(q: QuotaItem) -> Finding:
    return Finding(
        vm_id=_quota_resource_id(q),
        category=Category.QUOTA,
        subcategory=SubCategory.QUOTA_CRITICAL_INDIVIDUAL,
        code="QTA-CRI-001",
        current=f"{q.current_usage}/{q.quota_limit}",
        proposed=None,
        rationale=(
            f"{q.display_name} in {q.region} is at {q.utilization_pct:.1f}% "
            f"({q.current_usage}/{q.quota_limit}) — new deployments will start to fail. "
            "Request a quota increase immediately."
        ),
        **_rec_kwargs(category=Category.QUOTA),
    )


def _crg_finding(q: QuotaItem, donors: list[QuotaItem]) -> Finding:
    top = donors[:3]
    donor_summary = "; ".join(
        f"{d.subscription_name} ({d.utilization_pct:.0f}% used, "
        f"{d.quota_limit - d.current_usage} free)"
        for d in top
    )
    spare = sum(d.quota_limit - d.current_usage for d in top)
    return Finding(
        vm_id=_quota_resource_id(q),
        category=Category.QUOTA,
        subcategory=SubCategory.QUOTA_CRITICAL_GROUPABLE,
        code="QTA-CRG-001",
        current=f"{q.current_usage}/{q.quota_limit}",
        proposed=f"Consolidate via quota group (spare: {spare} units)",
        rationale=(
            f"{q.subscription_name} is at {q.utilization_pct:.1f}% of "
            f"{q.display_name} in {q.region}. Spare capacity exists in: "
            f"{donor_summary}. Consider workload redistribution or quota-group "
            "consolidation."
        ),
        **_rec_kwargs(category=Category.QUOTA),
    )


def _warning_finding(q: QuotaItem) -> Finding:
    return Finding(
        vm_id=_quota_resource_id(q),
        category=Category.QUOTA,
        subcategory=SubCategory.QUOTA_WARNING,
        code="QTA-WRN-001",
        current=f"{q.current_usage}/{q.quota_limit}",
        proposed=None,
        rationale=(
            f"{q.display_name} in {q.region} is at {q.utilization_pct:.1f}% "
            f"({q.current_usage}/{q.quota_limit}). Plan a quota increase before "
            "consumption crosses the critical threshold."
        ),
        **_rec_kwargs(category=Category.QUOTA),
    )


def _oversized_finding(q: QuotaItem, receiver_count: int) -> Finding:
    spare = max(0, q.quota_limit - q.current_usage * 2)
    return Finding(
        vm_id=_quota_resource_id(q),
        category=Category.QUOTA,
        subcategory=SubCategory.QUOTA_OVERSIZED,
        code="QTA-OVR-001",
        current=f"{q.current_usage}/{q.quota_limit}",
        proposed=f"Reduce by ~{spare} units",
        rationale=(
            f"{q.display_name} in {q.region} is only {q.utilization_pct:.1f}% used "
            f"({q.current_usage}/{q.quota_limit}). Quota is far larger than actual "
            f"consumption and {receiver_count} other subscription(s) on the same "
            "SKU need more capacity — reduce it to free regional headroom."
        ),
        **_rec_kwargs(category=Category.QUOTA),
    )
