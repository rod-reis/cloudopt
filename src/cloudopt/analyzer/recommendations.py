"""Thin shim over the Step-2 detector pipeline.

This module preserves the public API from the pre-Step-2 monolith while
delegating all logic to ``cloudopt.analyzer.detectors``.

Public interface (deprecated -- call detectors.run_all() directly):
    generate_recommendations()
    generate_quota_recommendations()
    generate_cross_subscription_transfer_recommendations()
    sort_recommendations()

Legacy helpers (re-exported for backward compat):
    _is_legacy_sku(), _modern_replacement(), _suggest_family_swap()

Legacy constants:
    QUOTA_CRITICAL_PCT, QUOTA_WARNING_PCT, QUOTA_OVERPROVISIONED_PCT,
    QUOTA_REVIEW_PCT, _XSUB_DONOR_MAX_PCT, _XSUB_RECEIVER_MIN_PCT
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from typing import Optional

from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.enrichment.schema import EnrichedVmMetrics, MonitoringConfidence
from cloudopt.models import (
    ARCHITECT_REVIEW_NOTE,
    CollectionThresholds,
    QuotaItem,
    RecommendationCategory as Cat,
    RecommendationPriority as Pri,
    VmInventory,
    VmMetrics,
    VmRecommendation,
)

# ---------------------------------------------------------------------------
# Backward-compat constants
# ---------------------------------------------------------------------------

QUOTA_CRITICAL_PCT = 85.0
QUOTA_WARNING_PCT = 75.0           # old value; detector uses 70.0 per SPEC
QUOTA_OVERPROVISIONED_PCT = 15.0   # old value; detector uses 20.0 per SPEC
QUOTA_REVIEW_PCT = 25.0            # no SPEC equivalent -- kept for call-site compat

_XSUB_DONOR_MAX_PCT = 40.0
_XSUB_RECEIVER_MIN_PCT = QUOTA_WARNING_PCT

_MEM_BOUND_MEM_PCT = 70.0
_MEM_BOUND_CPU_PCT = 25.0
_CPU_BOUND_CPU_PCT = 70.0
_CPU_BOUND_MEM_PCT = 25.0


# ---------------------------------------------------------------------------
# Legacy workload-group dataclass (kept for backward compat)
# ---------------------------------------------------------------------------


@dataclass
class _WorkloadGroup:
    parent_id: str
    parent_type: str
    parent_name: str
    region: str
    subscription_id: str
    subscription_name: str
    resource_group: str
    members: list[VmInventory] = field(default_factory=list)

    @property
    def is_aggregated(self) -> bool:
        return (
            len(self.members) > 1
            or self.parent_type != "Microsoft.Compute/virtualMachines"
        )

    @property
    def representative_sku(self) -> str:
        skus = [m.vm_sku for m in self.members if m.vm_sku]
        if not skus:
            return ""
        return max(set(skus), key=skus.count)

    @property
    def representative_vcpus(self) -> int:
        vcpus = [m.vcpus for m in self.members if m.vcpus]
        return max(vcpus) if vcpus else 0

    @property
    def representative_memory_gb(self) -> float:
        mem = [m.memory_gb for m in self.members if m.memory_gb]
        return max(mem) if mem else 0.0


# ---------------------------------------------------------------------------
# Legacy SKU-detection helpers
# ---------------------------------------------------------------------------

_GP_LEGACY_RE = re.compile(r"^Standard_(?:A|D|B|DC)\w*?_v[123]$", re.IGNORECASE)
_OTHER_LEGACY_RE = re.compile(r"^Standard_(?:E|F|G|H|L|M|N)\w*?_v[12]$", re.IGNORECASE)
_NO_VERSION_RE = re.compile(r"^Standard_(?:A|D|F|G)\d", re.IGNORECASE)


def _is_legacy_sku(sku: str) -> bool:
    if not sku:
        return False
    if _GP_LEGACY_RE.match(sku):
        return True
    if _OTHER_LEGACY_RE.match(sku):
        return True
    if _NO_VERSION_RE.match(sku) and "_v" not in sku.lower():
        return True
    return False


def _modern_replacement(sku: str) -> str:
    s = sku.lower()
    if "standard_d" in s or s.startswith("standard_b"):
        return "Standard_Dv5 / Dsv5 / Ddsv5 family"
    if "standard_e" in s:
        return "Standard_Ev5 / Esv5 / Edsv5 family"
    if "standard_f" in s:
        return "Standard_Fsv2 family"
    if "standard_a" in s:
        return "Standard_Dv5 / Dsv5 family (general purpose)"
    return "current-generation family in same workload class"


def _suggest_family_swap(
    sku: str, cpu_pct: float, mem_pct: float
) -> Optional[tuple[str, str]]:
    if not sku.lower().startswith("standard_d"):
        return None
    if mem_pct >= _MEM_BOUND_MEM_PCT and cpu_pct < _MEM_BOUND_CPU_PCT:
        return ("E", Cat.MEMORY_BOUND)
    if cpu_pct >= _CPU_BOUND_CPU_PCT and mem_pct < _CPU_BOUND_MEM_PCT:
        return ("F", Cat.COMPUTE_BOUND)
    return None


# ---------------------------------------------------------------------------
# Finding -> VmRecommendation translator
# ---------------------------------------------------------------------------

_SUBCATEGORY_MAP: dict[tuple, tuple[str, str, str]] = {
    ("rightsize", "underutilized"): (Cat.RESIZING, Cat.UNDERUTILIZED, Pri.HIGH),
    ("rightsize", "oversized"):     (Cat.RESIZING, Cat.RIGHT_SIZE, Pri.MEDIUM),
    ("swap", "family", "memory-bound"):  (Cat.SKU_SWAP, Cat.MEMORY_BOUND, Pri.MEDIUM),
    ("swap", "family", "compute-bound"): (Cat.SKU_SWAP, Cat.COMPUTE_BOUND, Pri.MEDIUM),
    ("swap", "lifecycle"):               (Cat.MODERNIZATION, Cat.LEGACY_FAMILY, Pri.HIGH),
    ("swap", "architecture"):            (Cat.MODERNIZATION, "arm64-candidate", Pri.MEDIUM),
    ("decom", "stopped-allocated"):      (Cat.RESOURCE_CLEANUP, Cat.DECOMMISSION_CANDIDATE, Pri.HIGH),
    ("decom", "lower-env-overprovisioned"): (Cat.RESOURCE_CLEANUP, "lower-env-oversized", Pri.MEDIUM),
    ("decom", "deallocated-stale"):      (Cat.RESOURCE_CLEANUP, "env-tag-missing", Pri.MEDIUM),
    ("cleanup", "unattached-disk"):      (Cat.RESOURCE_CLEANUP, "unattached-disk", Pri.HIGH),
    ("cleanup", "unattached-nic"):       (Cat.RESOURCE_CLEANUP, "unattached-nic", Pri.HIGH),
    ("cleanup", "unassociated-public-ip"): (Cat.RESOURCE_CLEANUP, "unassociated-pip", Pri.HIGH),
    ("cleanup", "unused-snapshot"):      (Cat.RESOURCE_CLEANUP, "stale-snapshot", Pri.HIGH),
    ("quota", "critical-individual"):    (Cat.QUOTA_OPTIMIZATION, Cat.QUOTA_CRITICAL, Pri.CRITICAL),
    ("quota", "warning"):                (Cat.QUOTA_OPTIMIZATION, Cat.QUOTA_WARNING, Pri.HIGH),
    ("quota", "oversized"):              (Cat.QUOTA_OPTIMIZATION, Cat.QUOTA_OVERPROVISIONED, Pri.HIGH),
    ("quota", "critical-groupable"):     (Cat.REGION_EXPANSION, Cat.CROSS_SUB_TRANSFER, Pri.HIGH),
}


def _finding_to_vm_recommendation(f) -> VmRecommendation:
    cat_val = f.category.value.lower()
    sub_val = f.subcategory.value.lower()
    signal = f.deltas.get("signal", "")

    if cat_val == "rightsize":
        key: tuple = ("rightsize", signal or "oversized")
    elif cat_val == "swap" and sub_val == "family":
        key = ("swap", "family", signal or "memory-bound")
    else:
        key = (cat_val, sub_val)

    umbrella, sub_str, priority = _SUBCATEGORY_MAP.get(
        key, (Cat.RESIZING, "unknown", Pri.MEDIUM)
    )

    return VmRecommendation(
        priority=priority,
        recommendation=f.code,
        category=umbrella,
        subcategory=sub_str,
        resource_id=f.vm_id,
        parent_resource_id=f.vm_id,
        parent_resource_type="Microsoft.Compute/virtualMachines",
        parent_resource_name="",
        member_resource_ids=[f.vm_id],
        member_count=1,
        current_sku=f.current or "",
        recommended_sku=f.proposed,
        current_resource_type="",
        recommended_resource_type="",
        reason=f.rationale,
        estimated_optimization="",
        estimated_savings_pct=None,
        notes=ARCHITECT_REVIEW_NOTE,
        confidence=MonitoringConfidence.PLATFORM_ONLY,
        evidence=f.evidence_sources,
    )


# ---------------------------------------------------------------------------
# Public API -- deprecated wrappers
# ---------------------------------------------------------------------------


def generate_recommendations(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    thresholds: CollectionThresholds,
    sku_catalog: SkuCatalog,
    *,
    enriched_metrics: Optional[list[EnrichedVmMetrics]] = None,
) -> list[VmRecommendation]:
    """Deprecated: use detectors.run_all() directly."""
    warnings.warn(
        "generate_recommendations() is deprecated; use detectors.run_all() directly.",
        DeprecationWarning,
        stacklevel=2,
    )
    from cloudopt.analyzer import detectors  # noqa: PLC0415

    findings = detectors.run_all(vms, metrics, [], thresholds, sku_catalog)
    return [_finding_to_vm_recommendation(f) for f in findings]


def generate_quota_recommendations(
    quota_items: list[QuotaItem],
) -> list[VmRecommendation]:
    """Generate quota-tier recommendations (OLD threshold values preserved)."""
    receivers_by_rt: dict[str, set[str]] = {}
    for q in quota_items:
        if q.quota_limit > 0 and q.utilization_pct >= QUOTA_WARNING_PCT:
            receivers_by_rt.setdefault(q.resource_type, set()).add(q.subscription_id)

    out: list[VmRecommendation] = []
    for q in quota_items:
        if q.quota_limit <= 0:
            continue

        if q.utilization_pct >= QUOTA_CRITICAL_PCT:
            priority = Pri.CRITICAL
            subcategory = Cat.QUOTA_CRITICAL
            title = "Quota at critical utilization -- request increase immediately"
            reason = (
                f"{q.display_name} in {q.region} is at {q.utilization_pct:.1f}%"
                f" ({q.current_usage}/{q.quota_limit}) -- new deployments will start to fail."
            )
            optimization = "Avoid deployment failures"
        elif q.utilization_pct >= QUOTA_WARNING_PCT:
            priority = Pri.HIGH
            subcategory = Cat.QUOTA_WARNING
            title = "Quota approaching limit -- plan a quota increase"
            reason = (
                f"{q.display_name} in {q.region} is at {q.utilization_pct:.1f}%"
                f" ({q.current_usage}/{q.quota_limit}). Plan a quota increase before"
                " consumption crosses the critical threshold."
            )
            optimization = "Headroom for upcoming workloads"
        elif q.utilization_pct <= QUOTA_OVERPROVISIONED_PCT:
            receivers = receivers_by_rt.get(q.resource_type, set()) - {q.subscription_id}
            if not receivers:
                continue
            priority = Pri.HIGH
            subcategory = Cat.QUOTA_OVERPROVISIONED
            title = "Quota massively over-provisioned -- request reduction"
            spare = max(0, q.quota_limit - q.current_usage * 2)
            reason = (
                f"{q.display_name} in {q.region} is only {q.utilization_pct:.1f}% used"
                f" ({q.current_usage}/{q.quota_limit}). Quota is far larger than actual"
                f" consumption and {len(receivers)} other subscription(s) on the same"
                " SKU need more capacity -- reduce it to free regional headroom."
            )
            optimization = f"Release ~{spare} units back to the region"
        elif q.utilization_pct <= QUOTA_REVIEW_PCT:
            receivers = receivers_by_rt.get(q.resource_type, set()) - {q.subscription_id}
            if not receivers:
                continue
            priority = Pri.MEDIUM
            subcategory = Cat.QUOTA_REVIEW
            title = "Quota over-provisioned -- review for reduction"
            reason = (
                f"{q.display_name} in {q.region} is only {q.utilization_pct:.1f}% used"
                f" ({q.current_usage}/{q.quota_limit}). {len(receivers)} other subscription(s)"
                " on the same SKU need more capacity -- consider trimming to free regional headroom."
            )
            optimization = "Free unused regional capacity"
        else:
            continue

        out.append(
            VmRecommendation(
                priority=priority,
                recommendation=title,
                category=Cat.QUOTA_OPTIMIZATION,
                subcategory=subcategory,
                resource_id=(
                    f"/subscriptions/{q.subscription_id}/providers/Microsoft.Capacity"
                    f"/locations/{q.region}/usages/{q.resource_type}"
                ),
                current_resource_type=q.resource_type,
                recommended_resource_type=q.resource_type,
                current_sku=f"{q.current_usage}/{q.quota_limit}",
                recommended_sku=None,
                reason=reason,
                estimated_optimization=optimization,
                notes=ARCHITECT_REVIEW_NOTE,
            )
        )

    return out


def generate_cross_subscription_transfer_recommendations(
    quota_items: list[QuotaItem],
) -> list[VmRecommendation]:
    """Generate cross-subscription / cross-region transfer suggestions."""
    out: list[VmRecommendation] = []

    by_pair: dict[tuple[str, str], list[QuotaItem]] = {}
    for q in quota_items:
        if q.quota_limit <= 0:
            continue
        by_pair.setdefault((q.region.lower(), q.resource_type), []).append(q)

    for (region, resource_type), items in by_pair.items():
        if len(items) < 2:
            continue
        donors = [q for q in items if q.utilization_pct < _XSUB_DONOR_MAX_PCT]
        receivers = [q for q in items if q.utilization_pct >= _XSUB_RECEIVER_MIN_PCT]
        if not donors or not receivers:
            continue
        donors.sort(key=lambda d: d.quota_limit - d.current_usage, reverse=True)

        for receiver in receivers:
            real_donors = [d for d in donors if d.subscription_id != receiver.subscription_id]
            if not real_donors:
                continue
            top = real_donors[:3]
            donor_summary = "; ".join(
                f"{d.subscription_name} ({d.utilization_pct:.0f}% used,"
                f" {d.quota_limit - d.current_usage} free)"
                for d in top
            )
            spare = sum(d.quota_limit - d.current_usage for d in top)
            out.append(
                VmRecommendation(
                    priority=Pri.HIGH,
                    recommendation="Re-distribute workload across subscriptions",
                    category=Cat.REGION_EXPANSION,
                    subcategory=Cat.CROSS_SUB_TRANSFER,
                    resource_id=(
                        f"/subscriptions/{receiver.subscription_id}/providers/"
                        f"Microsoft.Capacity/locations/{receiver.region}/usages/"
                        f"{receiver.resource_type}"
                    ),
                    current_resource_type=receiver.resource_type,
                    recommended_resource_type=receiver.resource_type,
                    current_sku=f"{receiver.current_usage}/{receiver.quota_limit}",
                    recommended_sku=None,
                    reason=(
                        f"{receiver.subscription_name} is at {receiver.utilization_pct:.1f}% of"
                        f" {receiver.display_name} in {region}. Spare capacity exists in:"
                        f" {donor_summary}. Consider moving workloads (or new deployments) to"
                        " the donor subscription(s) to balance regional capacity."
                    ),
                    estimated_optimization=f"~{spare} units of head-room available",
                    notes=ARCHITECT_REVIEW_NOTE,
                )
            )

    for (region, resource_type), items in by_pair.items():
        receivers = [q for q in items if q.utilization_pct >= _XSUB_RECEIVER_MIN_PCT]
        same_region_donors = any(q.utilization_pct < _XSUB_DONOR_MAX_PCT for q in items)
        if not receivers or same_region_donors:
            continue

        cross_donors: list[QuotaItem] = []
        for (other_region, other_rt), other_items in by_pair.items():
            if other_rt != resource_type or other_region == region:
                continue
            cross_donors.extend(
                q for q in other_items if q.utilization_pct < _XSUB_DONOR_MAX_PCT
            )
        if not cross_donors:
            continue
        cross_donors.sort(key=lambda d: d.quota_limit - d.current_usage, reverse=True)

        for receiver in receivers:
            top = cross_donors[:3]
            donor_summary = "; ".join(
                f"{d.subscription_name}/{d.region} ({d.utilization_pct:.0f}% used,"
                f" {d.quota_limit - d.current_usage} free)"
                for d in top
            )
            spare = sum(d.quota_limit - d.current_usage for d in top)
            out.append(
                VmRecommendation(
                    priority=Pri.HIGH,
                    recommendation="Expand into another region (Non-Prod / DR / new region)",
                    category=Cat.REGION_EXPANSION,
                    subcategory=Cat.CROSS_REGION_TRANSFER,
                    resource_id=(
                        f"/subscriptions/{receiver.subscription_id}/providers/"
                        f"Microsoft.Capacity/locations/{receiver.region}/usages/"
                        f"{receiver.resource_type}"
                    ),
                    current_resource_type=receiver.resource_type,
                    recommended_resource_type=receiver.resource_type,
                    current_sku=f"{receiver.current_usage}/{receiver.quota_limit}",
                    recommended_sku=None,
                    reason=(
                        f"{receiver.subscription_name} is at {receiver.utilization_pct:.1f}% of"
                        f" {receiver.display_name} in {region} and no same-region head-room"
                        f" exists. Capacity is available in: {donor_summary}. Consider placing"
                        " Non-Prod / DR workloads in those regions, or expanding the workload footprint."
                    ),
                    estimated_optimization=f"~{spare} units of head-room available cross-region",
                    notes=ARCHITECT_REVIEW_NOTE,
                )
            )

    return out


# ---------------------------------------------------------------------------
# Sorting helper
# ---------------------------------------------------------------------------

_PRIORITY_RANK = {
    Pri.CRITICAL: 0,
    Pri.HIGH: 1,
    Pri.MEDIUM: 2,
    Pri.LOW: 3,
}


def sort_recommendations(recs: list[VmRecommendation]) -> list[VmRecommendation]:
    return sorted(
        recs,
        key=lambda r: (
            _PRIORITY_RANK.get(r.priority, 99),
            r.category,
            r.subcategory,
            r.resource_id,
        ),
    )
