"""Unified CLOUDOPT recommendation engine.

Workload-level (parent-resource) aggregation + a single unified telemetry
evaluation pass that maps signals onto six umbrella categories:

    A. QUOTA_OPTIMIZATION       — quota tiers
    B. SKU_SWAP                 — same size, different family
    C. RESIZING                 — same family, smaller / larger size
    D. RESOURCE_CLEANUP         — deallocated / idle VMs to decommission
    E. MODERNIZATION            — legacy → modern, IaaS → PaaS
    F. REGION_EXPANSION         — re-distribute workloads across subs / regions

Each emitted ``VmRecommendation`` carries the umbrella in ``category`` and
the granular signal in ``subcategory`` (e.g. ``underutilized``,
``memory-bound``, ``quota-critical``).  Notes default to "Architect/Engineer to review".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from cloudopt.analyzer.sku_catalog import SkuCatalog
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

_PAAS_DISK_IOPS_THRESHOLD = 50.0

QUOTA_CRITICAL_PCT = 85.0
QUOTA_WARNING_PCT = 75.0
QUOTA_OVERPROVISIONED_PCT = 15.0
QUOTA_REVIEW_PCT = 25.0

_XSUB_DONOR_MAX_PCT = 40.0
_XSUB_RECEIVER_MIN_PCT = QUOTA_WARNING_PCT

_MEM_BOUND_MEM_PCT = 70.0
_MEM_BOUND_CPU_PCT = 25.0
_CPU_BOUND_CPU_PCT = 70.0
_CPU_BOUND_MEM_PCT = 25.0


# --- Workload grouping ------------------------------------------------------


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
        return len(self.members) > 1 or self.parent_type != "Microsoft.Compute/virtualMachines"

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


def _detect_parent(vm: VmInventory) -> tuple[str, str, str]:
    """Return (parent_id, parent_type, parent_name) for a VM."""
    sub = vm.subscription_id
    rg = vm.resource_group

    if vm.vmss_name:
        return (
            f"/subscriptions/{sub}/resourceGroups/{rg}/providers/"
            f"Microsoft.Compute/virtualMachineScaleSets/{vm.vmss_name}",
            "Microsoft.Compute/virtualMachineScaleSets",
            vm.vmss_name,
        )
    if vm.availability_set_name:
        return (
            f"/subscriptions/{sub}/resourceGroups/{rg}/providers/"
            f"Microsoft.Compute/availabilitySets/{vm.availability_set_name}",
            "Microsoft.Compute/availabilitySets",
            vm.availability_set_name,
        )
    if rg.lower().startswith("databricks-rg-"):
        ws_name = rg[len("databricks-rg-"):].split("-")[0] or rg
        return (
            f"/subscriptions/{sub}/resourceGroups/{rg}/providers/"
            f"Microsoft.Databricks/workspaces/{ws_name}",
            "Microsoft.Databricks/workspaces",
            ws_name,
        )
    if vm.image_sku and "avd" in vm.image_sku.lower():
        return (
            f"/subscriptions/{sub}/resourceGroups/{rg}/providers/"
            f"Microsoft.DesktopVirtualization/hostpools/{rg}",
            "Microsoft.DesktopVirtualization/hostpools",
            rg,
        )
    return (vm.resource_id, "Microsoft.Compute/virtualMachines", vm.vm_name)


def _build_workload_groups(vms: list[VmInventory]) -> list[_WorkloadGroup]:
    groups: dict[str, _WorkloadGroup] = {}
    for vm in vms:
        pid, ptype, pname = _detect_parent(vm)
        g = groups.get(pid)
        if g is None:
            g = _WorkloadGroup(
                parent_id=pid,
                parent_type=ptype,
                parent_name=pname,
                region=vm.region,
                subscription_id=vm.subscription_id,
                subscription_name=vm.subscription_name,
                resource_group=vm.resource_group,
            )
            groups[pid] = g
        g.members.append(vm)
    return list(groups.values())


# --- Top-level entry --------------------------------------------------------


def generate_recommendations(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    thresholds: CollectionThresholds,
    sku_catalog: SkuCatalog,
) -> list[VmRecommendation]:
    metrics_by_vm = _group_metrics(metrics)
    workloads = _build_workload_groups(vms)
    out: list[VmRecommendation] = []
    for group in workloads:
        out.extend(_evaluate_workload(group, metrics_by_vm, thresholds, sku_catalog))
    return out


# --- Per-workload unified evaluation ----------------------------------------


def _evaluate_workload(
    group: _WorkloadGroup,
    metrics_by_vm: dict[str, dict[str, VmMetrics]],
    thresholds: CollectionThresholds,
    sku_catalog: SkuCatalog,
) -> list[VmRecommendation]:
    out: list[VmRecommendation] = []

    # D. RESOURCE_CLEANUP — deallocated VMs flagged before the metric early-return
    deallocated = [
        m for m in group.members
        if (m.power_state or "").lower() == "powerstate/deallocated"
    ]
    if deallocated:
        out.append(
            _make_rec(
                group=group,
                priority=Pri.HIGH,
                title="Review deallocated VM(s) for decommissioning",
                umbrella=Cat.RESOURCE_CLEANUP,
                subcategory=Cat.DECOMMISSION_CANDIDATE,
                current_sku=group.representative_sku,
                recommended_sku=None,
                reason=(
                    f"{len(deallocated)} of {len(group.members)} VM(s) in this workload "
                    "are in a deallocated (stopped) state. Review whether these can be "
                    "permanently decommissioned to free compute quota and reduce "
                    "management overhead."
                ),
                optimization="Reclaim quota; eliminate ongoing management overhead",
            )
        )

    cpu_avgs: list[float] = []
    cpu_p95s: list[float] = []
    mem_pcts: list[float] = []
    disk_iops_totals: list[float] = []

    for vm in group.members:
        vm_met = metrics_by_vm.get(vm.resource_id, {})
        cpu_avg = _stat(vm_met, "Percentage CPU", "avg")
        cpu_p95 = _stat(vm_met, "Percentage CPU", "p95")
        mem_avail = _stat(vm_met, "Available Memory Bytes", "avg")
        disk_r = _stat(vm_met, "Disk Read Operations/Sec", "avg") or 0.0
        disk_w = _stat(vm_met, "Disk Write Operations/Sec", "avg") or 0.0

        if cpu_avg is not None:
            cpu_avgs.append(cpu_avg)
        if cpu_p95 is not None:
            cpu_p95s.append(cpu_p95)
        mem_pct = _mem_utilization_pct(mem_avail, vm.memory_gb)
        if mem_pct is not None:
            mem_pcts.append(mem_pct)
        disk_iops_totals.append(disk_r + disk_w)

    if not cpu_avgs and not cpu_p95s and not mem_pcts:
        return out  # May include RESOURCE_CLEANUP recs for fully-deallocated groups

    cpu_avg = _mean(cpu_avgs)
    cpu_p95 = max(cpu_p95s) if cpu_p95s else None
    mem_pct_avg = _mean(mem_pcts)
    total_disk_iops = _mean(disk_iops_totals) or 0.0

    representative_vm = group.members[0]
    sku = group.representative_sku
    vcpus = group.representative_vcpus
    memory_gb = group.representative_memory_gb

    size_recommendation_emitted = False

    # C. RESIZING — underutilized
    if (
        cpu_avg is not None
        and mem_pct_avg is not None
        and cpu_avg < thresholds.underutilized_cpu_avg
        and mem_pct_avg < thresholds.underutilized_memory_avg
    ):
        recommended = sku_catalog.find_smaller_sku(
            subscription_id=group.subscription_id,
            region=group.region,
            current_sku=sku,
            required_vcpus=max(1, int(vcpus * (cpu_avg / 100) * thresholds.headroom_multiplier)),
            required_memory_gb=max(
                0.5, memory_gb * (mem_pct_avg / 100) * thresholds.headroom_multiplier
            ),
        )
        if recommended and _is_legacy_sku(recommended):
            recommended = None
        savings = _savings_pct(vcpus, recommended, sku_catalog, representative_vm)
        out.append(
            _make_rec(
                group=group,
                priority=Pri.HIGH,
                title="Right-size or decommission underutilized workload",
                umbrella=Cat.RESIZING,
                subcategory=Cat.UNDERUTILIZED,
                current_sku=sku,
                recommended_sku=recommended,
                reason=(
                    f"Avg CPU {cpu_avg:.1f}% < {thresholds.underutilized_cpu_avg}% threshold; "
                    f"avg memory utilization {mem_pct_avg:.1f}% < "
                    f"{thresholds.underutilized_memory_avg}% threshold. "
                    "Consider a smaller SKU or decommissioning."
                ),
                optimization=_savings_label(savings),
                savings_pct=savings,
            )
        )
        size_recommendation_emitted = True

    # C. RESIZING — oversized (P95 below threshold)
    if (
        not size_recommendation_emitted
        and cpu_p95 is not None
        and cpu_p95 < thresholds.oversize_cpu_p95
    ):
        required_vcpus = max(1, int(vcpus * (cpu_p95 / 100) * thresholds.headroom_multiplier))
        required_mem = max(
            0.5, memory_gb * ((mem_pct_avg or 50.0) / 100) * thresholds.headroom_multiplier
        )
        recommended = sku_catalog.find_smaller_sku(
            subscription_id=group.subscription_id,
            region=group.region,
            current_sku=sku,
            required_vcpus=required_vcpus,
            required_memory_gb=required_mem,
        )
        if recommended and _is_legacy_sku(recommended):
            recommended = None
        if recommended:
            savings = _savings_pct(vcpus, recommended, sku_catalog, representative_vm)
            out.append(
                _make_rec(
                    group=group,
                    priority=Pri.MEDIUM,
                    title="Right-size oversized workload",
                    umbrella=Cat.RESIZING,
                    subcategory=Cat.RIGHT_SIZE,
                    current_sku=sku,
                    recommended_sku=recommended,
                    reason=(
                        f"P95 CPU {cpu_p95:.1f}% < {thresholds.oversize_cpu_p95}% oversized threshold. "
                        f"Required capacity with {thresholds.headroom_multiplier}x headroom: "
                        f"{required_vcpus} vCPUs / {required_mem:.1f} GB memory."
                    ),
                    optimization=_savings_label(savings),
                    savings_pct=savings,
                )
            )
            size_recommendation_emitted = True

    # B. SKU_SWAP — memory- vs compute-bound
    if not size_recommendation_emitted and cpu_avg is not None and mem_pct_avg is not None:
        swap = _suggest_family_swap(sku, cpu_avg, mem_pct_avg)
        if swap is not None:
            target_family, sub_signal = swap
            out.append(
                _make_rec(
                    group=group,
                    priority=Pri.MEDIUM,
                    title=f"Swap to a {target_family}-series SKU",
                    umbrella=Cat.SKU_SWAP,
                    subcategory=sub_signal,
                    current_sku=sku,
                    recommended_sku=None,
                    recommended_resource_type=(
                        f"Standard_{target_family}* (same vCPU class, "
                        f"{target_family}-series)"
                    ),
                    reason=(
                        f"Avg CPU {cpu_avg:.1f}% / memory {mem_pct_avg:.1f}% indicates a "
                        f"{sub_signal.replace('-', ' ')} workload. "
                        f"The {target_family}-series is a better fit."
                    ),
                    optimization="Better cost/performance fit for workload profile",
                )
            )
            size_recommendation_emitted = True

    # E. MODERNIZATION — current SKU on a legacy family
    if _is_legacy_sku(sku):
        modern_target = _modern_replacement(sku)
        out.append(
            _make_rec(
                group=group,
                priority=Pri.HIGH,
                title="Modernise to a current-generation VM family",
                umbrella=Cat.MODERNIZATION,
                subcategory=Cat.LEGACY_FAMILY,
                current_sku=sku,
                recommended_sku=None,
                recommended_resource_type=modern_target,
                reason=(
                    f"{sku} belongs to a previous-generation family. Newer "
                    "generations deliver better price-performance and longer support."
                ),
                optimization="Modern silicon, better $/perf, longer support window",
            )
        )

    # E. MODERNIZATION — PaaS candidate
    if (
        cpu_avg is not None
        and cpu_avg < thresholds.paas_candidate_cpu_avg
        and total_disk_iops < _PAAS_DISK_IOPS_THRESHOLD
    ):
        out.append(
            _make_rec(
                group=group,
                priority=Pri.MEDIUM,
                title="Migrate workload to a PaaS service",
                umbrella=Cat.MODERNIZATION,
                subcategory=Cat.PAAS_CANDIDATE,
                current_sku=sku,
                recommended_sku=None,
                recommended_resource_type="App Service / Container Apps / Azure SQL",
                reason=(
                    f"Very low avg CPU {cpu_avg:.1f}% (< {thresholds.paas_candidate_cpu_avg}%) "
                    f"and low disk IOPS ({total_disk_iops:.0f} combined). "
                    "Review whether this workload could move to App Service, Azure SQL, or a container."
                ),
                optimization="Eliminate IaaS overhead",
            )
        )

    return out


# --- A. QUOTA_OPTIMIZATION --------------------------------------------------


def generate_quota_recommendations(
    quota_items: list[QuotaItem],
) -> list[VmRecommendation]:
    # Pre-compute, per resource_type, the set of subscriptions that *need*
    # more quota (>= warning).  Over-provisioned / review recs are only
    # surfaced when at least one OTHER subscription on the same SKU could
    # absorb the freed quota \u2014 otherwise rebalancing has no business value.
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
            title = "Quota at critical utilization \u2014 request increase immediately"
            reason = (
                f"{q.display_name} in {q.region} is at {q.utilization_pct:.1f}% "
                f"({q.current_usage}/{q.quota_limit}) \u2014 new deployments will start to fail."
            )
            optimization = "Avoid deployment failures"
        elif q.utilization_pct >= QUOTA_WARNING_PCT:
            priority = Pri.HIGH
            subcategory = Cat.QUOTA_WARNING
            title = "Quota approaching limit \u2014 plan a quota increase"
            reason = (
                f"{q.display_name} in {q.region} is at {q.utilization_pct:.1f}% "
                f"({q.current_usage}/{q.quota_limit}). Plan a quota increase before "
                "consumption crosses the critical threshold."
            )
            optimization = "Headroom for upcoming workloads"
        elif q.utilization_pct <= QUOTA_OVERPROVISIONED_PCT:
            # Only recommend trimming if another subscription on the same
            # SKU is starved \u2014 otherwise nobody benefits from the freed quota.
            receivers = receivers_by_rt.get(q.resource_type, set()) - {q.subscription_id}
            if not receivers:
                continue
            priority = Pri.HIGH
            subcategory = Cat.QUOTA_OVERPROVISIONED
            title = "Quota massively over-provisioned \u2014 request reduction"
            spare = max(0, q.quota_limit - q.current_usage * 2)
            reason = (
                f"{q.display_name} in {q.region} is only {q.utilization_pct:.1f}% used "
                f"({q.current_usage}/{q.quota_limit}). Quota is far larger than actual "
                f"consumption and {len(receivers)} other subscription(s) on the same "
                "SKU need more capacity \u2014 reduce it to free regional headroom."
            )
            optimization = f"Release ~{spare} units back to the region"
        elif q.utilization_pct <= QUOTA_REVIEW_PCT:
            receivers = receivers_by_rt.get(q.resource_type, set()) - {q.subscription_id}
            if not receivers:
                continue
            priority = Pri.MEDIUM
            subcategory = Cat.QUOTA_REVIEW
            title = "Quota over-provisioned \u2014 review for reduction"
            reason = (
                f"{q.display_name} in {q.region} is only {q.utilization_pct:.1f}% used "
                f"({q.current_usage}/{q.quota_limit}). {len(receivers)} other subscription(s) "
                "on the same SKU need more capacity \u2014 consider trimming to free regional headroom."
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


# --- F. REGION_EXPANSION ----------------------------------------------------


def generate_cross_subscription_transfer_recommendations(
    quota_items: list[QuotaItem],
) -> list[VmRecommendation]:
    out: list[VmRecommendation] = []

    by_pair: dict[tuple[str, str], list[QuotaItem]] = {}
    for q in quota_items:
        if q.quota_limit <= 0:
            continue
        by_pair.setdefault((q.region.lower(), q.resource_type), []).append(q)

    # Same-region cross-sub
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
                f"{d.subscription_name} ({d.utilization_pct:.0f}% used, "
                f"{d.quota_limit - d.current_usage} free)"
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
                        f"{receiver.subscription_name} is at {receiver.utilization_pct:.1f}% of "
                        f"{receiver.display_name} in {region}. Spare capacity exists in: {donor_summary}. "
                        "Consider moving workloads (or new deployments) to the donor subscription(s) "
                        "to balance regional capacity."
                    ),
                    estimated_optimization=f"~{spare} units of head-room available",
                    notes=ARCHITECT_REVIEW_NOTE,
                )
            )

    # Cross-region
    for (region, resource_type), items in by_pair.items():
        receivers = [q for q in items if q.utilization_pct >= _XSUB_RECEIVER_MIN_PCT]
        same_region_donors = any(q.utilization_pct < _XSUB_DONOR_MAX_PCT for q in items)
        if not receivers or same_region_donors:
            continue

        cross_region_donors: list[QuotaItem] = []
        for (other_region, other_rt), other_items in by_pair.items():
            if other_rt != resource_type or other_region == region:
                continue
            cross_region_donors.extend(
                q for q in other_items if q.utilization_pct < _XSUB_DONOR_MAX_PCT
            )
        if not cross_region_donors:
            continue
        cross_region_donors.sort(key=lambda d: d.quota_limit - d.current_usage, reverse=True)

        for receiver in receivers:
            top = cross_region_donors[:3]
            donor_summary = "; ".join(
                f"{d.subscription_name}/{d.region} ({d.utilization_pct:.0f}% used, "
                f"{d.quota_limit - d.current_usage} free)"
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
                        f"{receiver.subscription_name} is at {receiver.utilization_pct:.1f}% of "
                        f"{receiver.display_name} in {region} and no same-region head-room exists. "
                        f"Capacity is available in: {donor_summary}. Consider placing Non-Prod / DR "
                        "workloads in those regions, or expanding the workload footprint."
                    ),
                    estimated_optimization=f"~{spare} units of head-room available cross-region",
                    notes=ARCHITECT_REVIEW_NOTE,
                )
            )

    return out


# --- Sorting helper ---------------------------------------------------------


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


# --- Legacy SKU detection ---------------------------------------------------


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


# --- SKU swap heuristic -----------------------------------------------------


def _suggest_family_swap(sku: str, cpu_pct: float, mem_pct: float) -> Optional[tuple[str, str]]:
    if not sku.lower().startswith("standard_d"):
        return None
    if mem_pct >= _MEM_BOUND_MEM_PCT and cpu_pct < _MEM_BOUND_CPU_PCT:
        return ("E", Cat.MEMORY_BOUND)
    if cpu_pct >= _CPU_BOUND_CPU_PCT and mem_pct < _CPU_BOUND_MEM_PCT:
        return ("F", Cat.COMPUTE_BOUND)
    return None


# --- Helpers ---------------------------------------------------------------


def _make_rec(
    *,
    group: _WorkloadGroup,
    priority: str,
    title: str,
    umbrella: str,
    subcategory: str,
    current_sku: str,
    recommended_sku: Optional[str],
    reason: str,
    optimization: str,
    recommended_resource_type: str = "",
    savings_pct: Optional[float] = None,
) -> VmRecommendation:
    if group.is_aggregated and group.parent_type != "Microsoft.Compute/virtualMachines":
        rec_resource_id = group.parent_id
    else:
        rec_resource_id = group.members[0].resource_id

    current_rt = group.parent_type or "Microsoft.Compute/virtualMachines"
    if not recommended_resource_type:
        recommended_resource_type = current_rt if recommended_sku else ""

    is_multi = len(group.members) > 1
    return VmRecommendation(
        priority=priority,
        recommendation=(f"[{group.parent_name}] {title}" if is_multi else title),
        category=umbrella,
        subcategory=subcategory,
        resource_id=rec_resource_id,
        parent_resource_id=group.parent_id,
        parent_resource_type=group.parent_type,
        parent_resource_name=group.parent_name,
        member_resource_ids=[m.resource_id for m in group.members],
        member_count=len(group.members),
        current_sku=current_sku,
        recommended_sku=recommended_sku,
        current_resource_type=current_rt,
        recommended_resource_type=recommended_resource_type,
        reason=(
            f"[{len(group.members)} VMs in {group.parent_name}] " + reason
            if is_multi
            else reason
        ),
        estimated_optimization=optimization,
        estimated_savings_pct=savings_pct,
        notes=ARCHITECT_REVIEW_NOTE,
    )


def _group_metrics(metrics: list[VmMetrics]) -> dict[str, dict[str, VmMetrics]]:
    result: dict[str, dict[str, VmMetrics]] = {}
    for m in metrics:
        result.setdefault(m.resource_id, {})[m.metric_name] = m
    return result


def _stat(vm_met: dict[str, VmMetrics], metric_name: str, stat: str) -> Optional[float]:
    m = vm_met.get(metric_name)
    if m is None:
        return None
    return getattr(m, stat, None)


def _mem_utilization_pct(avail_bytes: Optional[float], total_gb: float) -> Optional[float]:
    if avail_bytes is None or total_gb <= 0:
        return None
    avail_gb = avail_bytes / (1024 ** 3)
    used_pct = (1 - avail_gb / total_gb) * 100
    return max(0.0, min(100.0, used_pct))


def _mean(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _savings_pct(
    current_vcpus: int,
    recommended_sku: Optional[str],
    sku_catalog: SkuCatalog,
    vm: VmInventory,
) -> Optional[float]:
    if not recommended_sku or current_vcpus == 0:
        return None
    spec = sku_catalog.get(vm.subscription_id, vm.region, recommended_sku)
    if not spec or spec.vcpus == 0:
        return None
    pct = (1 - spec.vcpus / current_vcpus) * 100
    return round(max(0.0, pct), 1)


def _savings_label(pct: Optional[float]) -> str:
    if pct is None or pct <= 0:
        return "Capacity / cost reduction (qualitative)"
    return f"~{pct:.0f}% vCPU reduction"

