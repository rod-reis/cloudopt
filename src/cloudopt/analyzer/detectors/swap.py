"""SWP-FAM-001, SWP-LFC-001 detectors — SKU swap signals.

Ports the SKU_SWAP and MODERNIZATION/legacy-family rules from
``recommendations.py`` verbatim (SPEC §11.2.2).

Mapping from old umbrella codes:
  SKU_SWAP / memory-bound     → SWP-FAM-001
  SKU_SWAP / compute-bound    → SWP-FAM-001
  MODERNIZATION / legacy-family → SWP-LFC-001 (replaces old MODERNIZATION umbrella)

Deferred (no existing logic to port):
  SWP-GEN-001, SWP-DST-001
"""

from __future__ import annotations

from typing import Optional

from cloudopt.analyzer.detectors._shared import (
    _WorkloadGroup,
    _best_enriched,
    _build_workload_groups,
    _get_mem_pct,
    _group_metrics,
    _is_legacy_sku,
    _mean,
    _modern_replacement,
    _rec_kwargs,
    _stat,
    _suggest_family_swap,
)
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.analyzer.taxonomy import Category, Confidence, Readiness, SubCategory
from cloudopt.enrichment.schema import EnrichedVmMetrics
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
    *,
    enriched_map: Optional[dict[str, EnrichedVmMetrics]] = None,
) -> list[Finding]:
    """Emit SWP-FAM-001 and SWP-LFC-001 Findings."""
    metrics_by_vm = _group_metrics(metrics)
    workloads = _build_workload_groups(vms)
    out: list[Finding] = []
    for group in workloads:
        out.extend(_evaluate(group, metrics_by_vm, thresholds, enriched_map=enriched_map))
    return out


def _evaluate(
    group: _WorkloadGroup,
    metrics_by_vm: dict[str, dict[str, VmMetrics]],
    thresholds: CollectionThresholds,
    *,
    enriched_map: Optional[dict[str, EnrichedVmMetrics]] = None,
) -> list[Finding]:
    out: list[Finding] = []
    group_enriched = _best_enriched(group.members, enriched_map)

    cpu_avgs: list[float] = []
    cpu_p95s: list[float] = []
    mem_pcts: list[float] = []

    for vm in group.members:
        vm_met = metrics_by_vm.get(vm.resource_id, {})
        cpu_avg = _stat(vm_met, "Percentage CPU", "avg")
        cpu_p95 = _stat(vm_met, "Percentage CPU", "p95")
        vm_enriched = enriched_map.get(vm.resource_id) if enriched_map else None
        mem_pct, _ = _get_mem_pct(vm, vm_met, vm_enriched)

        if cpu_avg is not None:
            cpu_avgs.append(cpu_avg)
        if cpu_p95 is not None:
            cpu_p95s.append(cpu_p95)
        if mem_pct is not None:
            mem_pcts.append(mem_pct)

    cpu_avg = _mean(cpu_avgs)
    cpu_p95 = max(cpu_p95s) if cpu_p95s else None
    mem_pct_avg = _mean(mem_pcts)

    sku = group.representative_sku
    vm_id = (
        group.parent_id if group.is_aggregated else group.members[0].resource_id
    )

    # SWP-FAM-001: only when no size rec would fire (port existing logic 1:1)
    would_downsize = _would_downsize(cpu_avg, cpu_p95, mem_pct_avg, thresholds)
    if (
        not would_downsize
        and cpu_avg is not None
        and mem_pct_avg is not None
    ):
        swap = _suggest_family_swap(sku, cpu_avg, mem_pct_avg)
        if swap is not None:
            target_family, signal_label = swap
            kwargs = _rec_kwargs(enriched=group_enriched, category=Category.SWAP)
            kwargs["deltas"] = {"signal": signal_label}
            out.append(
                Finding(
                    vm_id=vm_id,
                    category=Category.SWAP,
                    subcategory=SubCategory.FAMILY,
                    code="SWP-FAM-001",
                    current=sku or None,
                    proposed=f"Standard_{target_family}* (same vCPU class)",
                    rationale=(
                        f"Avg CPU {cpu_avg:.1f}% / memory {mem_pct_avg:.1f}% indicates a "
                        f"{signal_label} workload. The {target_family}-series is a better fit."
                    ),
                    **kwargs,
                )
            )

    # SWP-LFC-001: legacy SKU check — always independent of size rec
    # Legacy-generation is an ARM fact (authoritative), so confidence is HIGH
    # regardless of whether guest monitoring data is available.
    if sku and _is_legacy_sku(sku):
        modern_target = _modern_replacement(sku)
        lfc_kwargs = _rec_kwargs(enriched=group_enriched, category=Category.SWAP)
        lfc_kwargs["confidence"] = Confidence.HIGH
        lfc_kwargs["readiness"] = Readiness.READY
        if not group_enriched:
            lfc_kwargs["blockers_to_high"] = [
                "No Guest OS monitoring export supplied. Provide a canonical CSV export "
                "from Datadog, Splunk, Dynatrace, or another supported tool to unlock "
                "HIGH confidence about the right new SKU recommended."
            ]
        else:
            lfc_kwargs["blockers_to_high"] = []
        out.append(
            Finding(
                vm_id=vm_id,
                category=Category.SWAP,
                subcategory=SubCategory.LIFECYCLE,
                code="SWP-LFC-001",
                current=sku,
                proposed=modern_target,
                rationale=(
                    f"{sku} belongs to a previous-generation family. Newer generations "
                    "deliver better price-performance and longer support."
                ),
                **lfc_kwargs,
            )
        )

    return out


def _would_downsize(
    cpu_avg: Optional[float],
    cpu_p95: Optional[float],
    mem_pct_avg: Optional[float],
    thresholds: CollectionThresholds,
) -> bool:
    """Return True only when the rightsize underutilized rule (Rule A) would fire."""
    if cpu_avg is not None and mem_pct_avg is not None:
        if (
            cpu_avg < thresholds.underutilized_cpu_avg
            and mem_pct_avg < thresholds.underutilized_memory_avg
        ):
            return True
    return False
