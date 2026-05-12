"""RSZ-DWN-001 detector — right-size down (underutilized and oversized).

Ports the RESIZING/underutilized and RESIZING/right-size (oversized) rules
from ``recommendations.py`` verbatim (SPEC §11.2.2).  No threshold or
heuristic changes are made in this step.

Deferred (no existing logic to port):
  RSZ-UPS-001, RSZ-BSF-001, RSZ-BSM-001, RSZ-DSK-001
"""

from __future__ import annotations

from typing import Optional

from cloudopt.analyzer.detectors._shared import (
    _WorkloadGroup,
    _build_workload_groups,
    _get_mem_pct,
    _group_metrics,
    _is_legacy_sku,
    _mean,
    _rec_kwargs,
    _stat,
)
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.analyzer.taxonomy import Category, SubCategory
from cloudopt.models import (
    CollectionThresholds,
    Finding,
    QuotaItem,
    VmInventory,
    VmMetrics,
)


def detect(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    quota: list[QuotaItem],
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
) -> list[Finding]:
    """Emit RSZ-DWN-001 Findings for underutilized and oversized workloads."""
    metrics_by_vm = _group_metrics(metrics)
    workloads = _build_workload_groups(vms)
    out: list[Finding] = []
    for group in workloads:
        out.extend(_evaluate(group, metrics_by_vm, thresholds, catalog))
    return out


def _evaluate(
    group: _WorkloadGroup,
    metrics_by_vm: dict[str, dict[str, VmMetrics]],
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
) -> list[Finding]:
    out: list[Finding] = []

    cpu_avgs: list[float] = []
    cpu_p95s: list[float] = []
    mem_pcts: list[float] = []

    for vm in group.members:
        vm_met = metrics_by_vm.get(vm.resource_id, {})
        cpu_avg = _stat(vm_met, "Percentage CPU", "avg")
        cpu_p95 = _stat(vm_met, "Percentage CPU", "p95")
        mem_pct, _ = _get_mem_pct(vm, vm_met, None)

        if cpu_avg is not None:
            cpu_avgs.append(cpu_avg)
        if cpu_p95 is not None:
            cpu_p95s.append(cpu_p95)
        if mem_pct is not None:
            mem_pcts.append(mem_pct)

    if not cpu_avgs and not cpu_p95s and not mem_pcts:
        return out

    cpu_avg = _mean(cpu_avgs)
    cpu_p95 = max(cpu_p95s) if cpu_p95s else None
    mem_pct_avg = _mean(mem_pcts)

    sku = group.representative_sku
    vcpus = group.representative_vcpus
    memory_gb = group.representative_memory_gb
    representative_vm = group.members[0]

    size_rec_emitted = False

    # Rule A: underutilized — both CPU and memory below thresholds
    if (
        cpu_avg is not None
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
            rationale = (
                f"Avg CPU {cpu_avg:.1f}% < {thresholds.underutilized_cpu_avg}% threshold; "
                f"avg memory utilization {mem_pct_avg:.1f}% < "
                f"{thresholds.underutilized_memory_avg}% threshold. "
                "Consider a smaller SKU or decommissioning."
            )
            out.append(_make_rsz_finding(group, sku, recommended, rationale, signal="underutilized"))
            size_rec_emitted = True

    # Rule B: oversized — P95 CPU below threshold (only if underutilized not emitted)
    if (
        not size_rec_emitted
        and cpu_p95 is not None
        and cpu_p95 < thresholds.oversize_cpu_p95
    ):
        recommended = _find_smaller(
            catalog, representative_vm, sku, vcpus, memory_gb,
            cpu_factor=cpu_p95 / 100,
            mem_factor=(mem_pct_avg or 50.0) / 100,
            thresholds=thresholds,
        )
        if recommended:
            rationale = (
                f"P95 CPU {cpu_p95:.1f}% < {thresholds.oversize_cpu_p95}% oversized "
                f"threshold. Required capacity with {thresholds.headroom_multiplier}x "
                f"headroom: {max(1, int(vcpus * (cpu_p95 / 100) * thresholds.headroom_multiplier))} "
                f"vCPUs / "
                f"{max(0.5, memory_gb * ((mem_pct_avg or 50.0) / 100) * thresholds.headroom_multiplier):.1f}"
                " GB memory."
            )
            out.append(_make_rsz_finding(group, sku, recommended, rationale, signal="oversized"))

    return out


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
) -> Finding:
    vm_id = (
        group.parent_id
        if group.is_aggregated
        else group.members[0].resource_id
    )
    kwargs = _rec_kwargs()
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
