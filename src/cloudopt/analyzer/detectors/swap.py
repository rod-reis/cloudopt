"""SWP-FAM-001, SWP-GEN-001, SWP-LFC-001, SWP-ARC-001 detectors — SKU swap signals.

Ports the SKU_SWAP and MODERNIZATION/legacy-family rules from
``recommendations.py`` verbatim (SPEC §11.2.2) and adds:

  SWP-GEN-001 — same shape, newer CPU generation (ARM catalog fact, HIGH confidence)
  SWP-ARC-001 — x64 → ARM64 eligibility candidate (DISCOVERY only)

Mapping from old umbrella codes:
  SKU_SWAP / memory-bound     → SWP-FAM-001
  SKU_SWAP / compute-bound    → SWP-FAM-001
  MODERNIZATION / legacy-family → SWP-LFC-001 (replaces old MODERNIZATION umbrella)
"""

from __future__ import annotations

from typing import Optional

from cloudopt.analyzer.detectors._shared import (
    _WorkloadGroup,
    _best_enriched,
    _build_workload_groups,
    _candidate_kwargs,
    _get_mem_pct,
    _group_metrics,
    _is_legacy_sku,
    _mean,
    _modern_replacement,
    _rec_kwargs,
    _stat,
    _suggest_family_swap,
)
from cloudopt.analyzer.sku_catalog import (
    SkuCatalog,
    _is_arm64_sku,
    _sku_generation_version,
)
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
    """Emit SWP-FAM-001, SWP-GEN-001, SWP-LFC-001, and SWP-ARC-001 Findings."""
    metrics_by_vm = _group_metrics(metrics)
    workloads = _build_workload_groups(vms)
    out: list[Finding] = []
    for group in workloads:
        out.extend(_evaluate(group, metrics_by_vm, thresholds, enriched_map=enriched_map))
        out.extend(_evaluate_generation_swap(group, catalog, enriched_map=enriched_map))
        out.extend(_evaluate_arm64_candidate(group, catalog, enriched_map=enriched_map))
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


def _evaluate_generation_swap(
    group: _WorkloadGroup,
    catalog: SkuCatalog,
    *,
    enriched_map: Optional[dict[str, EnrichedVmMetrics]] = None,
) -> list[Finding]:
    """Emit SWP-GEN-001 when a newer generation of the same SKU exists in the region.

    Generation swap is an ARM catalog fact (no performance thresholds required)
    so confidence is always HIGH.  The finding is suppressed when the current
    SKU has no _vN generation suffix (e.g. B-series) or the catalog has no
    newer match.
    """
    sku = group.representative_sku
    if not sku:
        return []

    representative_vm = group.members[0]
    result = catalog.find_newer_generation_sku(
        subscription_id=representative_vm.subscription_id,
        region=representative_vm.region,
        current_sku=sku,
    )
    if result is None:
        return []

    newer_sku, newer_gen = result
    current_gen = _sku_generation_version(sku)
    group_enriched = _best_enriched(group.members, enriched_map)
    vm_id = group.parent_id if group.is_aggregated else group.members[0].resource_id

    kwargs = _rec_kwargs(enriched=group_enriched, category=Category.SWAP)
    kwargs["confidence"] = Confidence.HIGH
    kwargs["readiness"] = Readiness.READY
    kwargs["blockers_to_high"] = []
    kwargs["deltas"] = {"generation_gap": newer_gen - current_gen}

    return [
        Finding(
            vm_id=vm_id,
            category=Category.SWAP,
            subcategory=SubCategory.GENERATION,
            code="SWP-GEN-001",
            current=sku,
            proposed=newer_sku,
            rationale=(
                f"{sku} is generation v{current_gen}; {newer_sku} (v{newer_gen}) "
                "is available in this region with the same vCPU and memory shape. "
                "Newer generations deliver improved CPU micro-architecture, default "
                "Accelerated Networking, and better throughput-per-core without any "
                "workload change required."
            ),
            **kwargs,
        )
    ]


def _evaluate_arm64_candidate(
    group: _WorkloadGroup,
    catalog: SkuCatalog,
    *,
    enriched_map: Optional[dict[str, EnrichedVmMetrics]] = None,
) -> list[Finding]:
    """Emit SWP-ARC-001 CANDIDATE when an ARM64 equivalent SKU exists for a Linux x64 VM.

    This is a DISCOVERY-only candidate — the tool never auto-prescribes an
    architecture change.  Suppressed for Windows VMs and VMs already running
    ARM64 SKUs.
    """
    sku = group.representative_sku
    if not sku:
        return []

    # Skip VMs already on ARM64
    if _is_arm64_sku(sku):
        return []

    # Only Linux VMs — ARM64 workloads require binary-compatible OS and apps
    for vm in group.members:
        if vm.os_type and "windows" in vm.os_type.lower():
            return []

    representative_vm = group.members[0]
    arm64_sku = catalog.find_arm64_equivalent_sku(
        subscription_id=representative_vm.subscription_id,
        region=representative_vm.region,
        current_sku=sku,
    )
    if arm64_sku is None:
        return []

    group_enriched = _best_enriched(group.members, enriched_map)
    vm_id = group.parent_id if group.is_aggregated else group.members[0].resource_id

    kwargs = _candidate_kwargs(enriched=group_enriched, category=Category.SWAP)
    kwargs["customer_inputs_needed"] = [
        "Confirm all workload binaries are ARM64-compatible.",
        "Confirm OS image (kernel + distribution) is available for ARM64.",
        "Validate third-party software / agents support ARM64.",
    ]

    return [
        Finding(
            vm_id=vm_id,
            category=Category.SWAP,
            subcategory=SubCategory.ARCHITECTURE,
            code="SWP-ARC-001",
            current=sku,
            proposed=arm64_sku,
            rationale=(
                f"An ARM64 equivalent ({arm64_sku}) with the same vCPU and memory shape "
                "exists in this region. ARM64 (Ampere Altra) SKUs can offer improved "
                "throughput-per-core for compatible Linux workloads. "
                "Binary and OS compatibility must be validated before migrating — "
                "this is a discovery flag, not an automated recommendation."
            ),
            **kwargs,
        )
    ]
