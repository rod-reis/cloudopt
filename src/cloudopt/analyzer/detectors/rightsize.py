"""RSZ-DWN-001 detector — right-size down (underutilized and oversized).

Ports the RESIZING/underutilized and RESIZING/right-size (oversized) rules
from ``recommendations.py`` (SPEC §11.2.2) and extends them with:

- Outbound network utilization signal: a downsize is suppressed when the VM
  is network-bound, even if CPU/memory are low.
- User-facing workload classification: bursty workloads use tighter P95
  thresholds (40%) while steady non-user-facing workloads use relaxed ones
  (80%), mirroring CloudFit Logic 2.
- Lookback-aware rationale text: references the actual analysis window.
- VMSS instance count recommendations: for scale-set groups, a reduced
  instance count is recommended before a SKU change.
"""

from __future__ import annotations

import math
from typing import Optional

from cloudopt.analyzer.detectors._shared import (
    _WorkloadGroup,
    _best_enriched,
    _build_workload_groups,
    _get_mem_pct,
    _group_metrics,
    _is_legacy_sku,
    _is_user_facing,
    _mean,
    _network_util_pct,
    _rec_kwargs,
    _stat,
)
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.analyzer.taxonomy import Category, SubCategory
from cloudopt.enrichment.schema import EnrichedVmMetrics
from cloudopt.models import (
    CollectionThresholds,
    Finding,
    QuotaItem,
    VmInventory,
    VmMetrics,
)

# Network utilization thresholds for the new-SKU projection (mirrors CloudFit)
_NETWORK_THRESHOLD_USER_FACING = 40.0      # P95 net util % on recommended SKU
_NETWORK_THRESHOLD_NON_USER_FACING = 80.0

# P95 CPU thresholds on the *recommended* SKU for user-facing vs non-user-facing
_CPU_P95_THRESHOLD_USER_FACING = 40.0
_CPU_P95_THRESHOLD_NON_USER_FACING = 80.0

# Network utilization above which a downsize is suppressed (current SKU)
_NETWORK_SUPPRESS_THRESHOLD = 40.0


def detect(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    quota: list[QuotaItem],
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
    *,
    enriched_map: Optional[dict[str, EnrichedVmMetrics]] = None,
) -> list[Finding]:
    """Emit RSZ-DWN-001 Findings for underutilized and oversized workloads."""
    metrics_by_vm = _group_metrics(metrics)
    workloads = _build_workload_groups(vms)
    out: list[Finding] = []
    for group in workloads:
        out.extend(_evaluate(group, metrics_by_vm, thresholds, catalog, enriched_map=enriched_map))
    return out


def _evaluate(
    group: _WorkloadGroup,
    metrics_by_vm: dict[str, dict[str, VmMetrics]],
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
    *,
    enriched_map: Optional[dict[str, EnrichedVmMetrics]] = None,
) -> list[Finding]:
    out: list[Finding] = []
    group_enriched = _best_enriched(group.members, enriched_map)

    cpu_avgs: list[float] = []
    cpu_p95s: list[float] = []
    mem_pcts: list[float] = []
    net_utils: list[float] = []
    user_facing_votes: list[bool] = []

    for vm in group.members:
        vm_met = metrics_by_vm.get(vm.resource_id, {})
        cpu_avg = _stat(vm_met, "Percentage CPU", "avg")
        cpu_p95 = _stat(vm_met, "Percentage CPU", "p95")
        vm_enriched = enriched_map.get(vm.resource_id) if enriched_map else None
        mem_pct, _ = _get_mem_pct(vm, vm_met, vm_enriched)

        sku_spec = catalog.get(vm.subscription_id, vm.region, vm.vm_sku)
        bw = sku_spec.network_bandwidth_mbps if sku_spec else 0.0
        net_avg = _stat(vm_met, "Network Out Total", "avg")
        net_util = _network_util_pct(net_avg, bw)

        if cpu_avg is not None:
            cpu_avgs.append(cpu_avg)
        if cpu_p95 is not None:
            cpu_p95s.append(cpu_p95)
        if mem_pct is not None:
            mem_pcts.append(mem_pct)
        if net_util is not None:
            net_utils.append(net_util)
        user_facing_votes.append(_is_user_facing(vm_met))

    if not cpu_avgs and not cpu_p95s and not mem_pcts:
        return out

    cpu_avg = _mean(cpu_avgs)
    cpu_p95 = max(cpu_p95s) if cpu_p95s else None
    mem_pct_avg = _mean(mem_pcts)
    net_util_avg = _mean(net_utils)
    user_facing = any(user_facing_votes)

    # Select the appropriate P95 threshold based on workload classification
    cpu_p95_threshold = (
        _CPU_P95_THRESHOLD_USER_FACING if user_facing else _CPU_P95_THRESHOLD_NON_USER_FACING
    )

    # Suppress downsize when the workload is network-bound on the current SKU
    if net_util_avg is not None and net_util_avg >= _NETWORK_SUPPRESS_THRESHOLD:
        return out

    sku = group.representative_sku
    vcpus = group.representative_vcpus
    memory_gb = group.representative_memory_gb
    representative_vm = group.members[0]
    lookback = thresholds.lookback_days

    size_rec_emitted = False

    # --- VMSS instance count recommendation (prioritized over SKU change) ---
    if group.is_aggregated and group.parent_type == "Microsoft.Compute/virtualMachineScaleSets":
        inst_finding = _vmss_instance_recommendation(
            group, cpu_avgs, mem_pcts, user_facing, thresholds, lookback, group_enriched
        )
        if inst_finding is not None:
            out.append(inst_finding)
            size_rec_emitted = True

    # Rule A: underutilized — both CPU and memory below thresholds
    if (
        not size_rec_emitted
        and cpu_avg is not None
        and mem_pct_avg is not None
        and cpu_avg < thresholds.underutilized_cpu_avg
        and mem_pct_avg < thresholds.underutilized_memory_avg
    ):
        recommended = _find_smaller(
            catalog, representative_vm, sku, vcpus, memory_gb,
            cpu_factor=cpu_avg / 100,
            mem_factor=mem_pct_avg / 100,
            thresholds=thresholds,
        )
        if recommended:
            net_note = (
                f" Outbound network {net_util_avg:.1f}% of SKU bandwidth."
                if net_util_avg is not None
                else ""
            )
            wf_note = " (user-facing workload)" if user_facing else " (non-user-facing workload)"
            rationale = (
                f"Avg CPU {cpu_avg:.1f}% < {thresholds.underutilized_cpu_avg}% threshold; "
                f"avg memory utilization {mem_pct_avg:.1f}% < "
                f"{thresholds.underutilized_memory_avg}% threshold "
                f"over {lookback} days{wf_note}.{net_note} "
                "Consider a smaller SKU or decommissioning."
            )
            out.append(_make_rsz_finding(group, sku, recommended, rationale, signal="underutilized", enriched=group_enriched))
            size_rec_emitted = True

    # Rule B: oversized — P95 CPU below threshold (only if underutilized not emitted)
    if (
        not size_rec_emitted
        and cpu_p95 is not None
        and cpu_p95 < cpu_p95_threshold
    ):
        recommended = _find_smaller(
            catalog, representative_vm, sku, vcpus, memory_gb,
            cpu_factor=cpu_p95 / 100,
            mem_factor=(mem_pct_avg or 50.0) / 100,
            thresholds=thresholds,
        )
        if recommended:
            wf_note = " (user-facing workload)" if user_facing else " (non-user-facing workload)"
            net_note = (
                f" Outbound network {net_util_avg:.1f}% of SKU bandwidth."
                if net_util_avg is not None
                else ""
            )
            rationale = (
                f"P95 CPU {cpu_p95:.1f}% < {cpu_p95_threshold:.0f}% oversized threshold "
                f"over {lookback} days{wf_note}.{net_note} "
                f"Required capacity with {thresholds.headroom_multiplier}x headroom: "
                f"{max(1, int(vcpus * (cpu_p95 / 100) * thresholds.headroom_multiplier))} vCPUs / "
                f"{max(0.5, memory_gb * ((mem_pct_avg or 50.0) / 100) * thresholds.headroom_multiplier):.1f}"
                " GB memory."
            )
            out.append(_make_rsz_finding(group, sku, recommended, rationale, signal="oversized", enriched=group_enriched))

    return out


def _vmss_instance_recommendation(
    group: _WorkloadGroup,
    cpu_avgs: list[float],
    mem_pcts: list[float],
    user_facing: bool,
    thresholds: CollectionThresholds,
    lookback: int,
    enriched: Optional[EnrichedVmMetrics],
) -> Optional[Finding]:
    """Return a VMSS instance-count reduction finding, or None.

    Estimates the minimum number of instances needed so that the consolidated
    CPU load stays within the P95 target threshold with the configured headroom.
    Only recommends if at least one instance can be removed.
    """
    if not cpu_avgs:
        return None
    current_count = len(cpu_avgs)
    if current_count <= 1:
        return None

    total_cpu_avg = sum(cpu_avgs)  # summed load across all instances
    target_pct = (
        _CPU_P95_THRESHOLD_USER_FACING if user_facing else _CPU_P95_THRESHOLD_NON_USER_FACING
    )
    # Minimum instances to keep total load below target with headroom
    min_instances = math.ceil(
        (total_cpu_avg * thresholds.headroom_multiplier) / target_pct
    )
    min_instances = max(1, min_instances)

    if min_instances >= current_count:
        return None

    vm_id = group.parent_id
    sku = group.representative_sku
    wf_note = " (user-facing)" if user_facing else " (non-user-facing)"
    rationale = (
        f"VMSS has {current_count} instances with avg CPU "
        f"{total_cpu_avg / current_count:.1f}% per instance over {lookback} days{wf_note}. "
        f"Consolidated load ({total_cpu_avg:.1f}% total) fits within {min_instances} instance(s) "
        f"at ≤ {target_pct:.0f}% CPU with {thresholds.headroom_multiplier}x headroom. "
        "Consider scaling in to reduce instance count."
    )
    kwargs = _rec_kwargs(enriched=enriched, category=Category.RIGHTSIZE)
    kwargs["deltas"] = {"signal": "vmss-instance-count", "proposed_instance_count": min_instances}
    return Finding(
        vm_id=vm_id,
        category=Category.RIGHTSIZE,
        subcategory=SubCategory.DOWNSIZE,
        code="RSZ-DWN-001",
        current=f"{sku} × {current_count}",
        proposed=f"{sku} × {min_instances}",
        rationale=rationale,
        **kwargs,
    )


def _find_smaller(
    catalog: SkuCatalog,
    vm: VmInventory,
    current_sku: str,
    vcpus: int,
    memory_gb: float,
    cpu_factor: float,
    mem_factor: float,
    thresholds: CollectionThresholds,
) -> Optional[str]:
    """Return a smaller SKU or None; drop the candidate if it is itself legacy."""
    req_vcpus = max(1, int(vcpus * cpu_factor * thresholds.headroom_multiplier))
    req_mem = max(0.5, memory_gb * mem_factor * thresholds.headroom_multiplier)
    candidate = catalog.find_smaller_sku(
        subscription_id=vm.subscription_id,
        region=vm.region,
        current_sku=current_sku,
        required_vcpus=req_vcpus,
        required_memory_gb=req_mem,
    )
    if candidate and _is_legacy_sku(candidate):
        return None
    return candidate


def _make_rsz_finding(
    group: _WorkloadGroup,
    current_sku: str,
    proposed_sku: Optional[str],
    rationale: str,
    signal: str = "",
    enriched: Optional[EnrichedVmMetrics] = None,
) -> Finding:
    vm_id = (
        group.parent_id
        if group.is_aggregated
        else group.members[0].resource_id
    )
    kwargs = _rec_kwargs(enriched=enriched, category=Category.RIGHTSIZE)
    if signal:
        kwargs["deltas"] = {"signal": signal}
    return Finding(
        vm_id=vm_id,
        category=Category.RIGHTSIZE,
        subcategory=SubCategory.DOWNSIZE,
        code="RSZ-DWN-001",
        current=current_sku or None,
        proposed=proposed_sku,
        rationale=rationale,
        **kwargs,
    )
