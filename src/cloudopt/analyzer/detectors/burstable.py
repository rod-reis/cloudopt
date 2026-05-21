"""RSZ-BSF-001 / RSZ-BSM-001 detectors — burstable SKU fit and misfit.

RSZ-BSF-001: A non-B-series VM whose workload profile is suitable for a
  B-series burstable SKU (lower cost, variable-performance).

  Criteria (mirroring CloudFit Logic 4):
    - Current SKU is in the D, E, or F family (general purpose / compute /
      memory optimised) — not already B-series.
    - Average CPU utilization < B-series baseline for the same vCPU count.
    - P95 CPU utilization < 2 × B-series baseline (spikes are credit-safe).
    - Current SKU does NOT have AcceleratedNetworking enabled (B-series does
      not support AN; recommending would degrade networking).
    - 7-day (or lookback) credit model stays net-positive (avg below baseline).

RSZ-BSM-001: A B-series VM whose workload is consistently exceeding the
  burstable credit budget, indicating a poor fit for the B-series model.
"""

from __future__ import annotations

from typing import Optional

from cloudopt.analyzer.detectors._shared import (
    _WorkloadGroup,
    _best_enriched,
    _bseries_baseline_pct,
    _bseries_credits_sufficient,
    _build_workload_groups,
    _group_metrics,
    _is_bseries_sku,
    _mean,
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

# SKU families eligible for a burstable recommendation (D, E, F)
_ELIGIBLE_FAMILIES = ("standard_d", "standard_e", "standard_f")


def detect(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    quota: list[QuotaItem],
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
    *,
    enriched_map: Optional[dict[str, EnrichedVmMetrics]] = None,
) -> list[Finding]:
    """Emit RSZ-BSF-001 and RSZ-BSM-001 Findings."""
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

    sku = group.representative_sku
    vcpus = group.representative_vcpus
    if not sku or not vcpus:
        return out

    cpu_avgs: list[float] = []
    cpu_p95s: list[float] = []
    has_accel_net: bool = False

    for vm in group.members:
        vm_met = metrics_by_vm.get(vm.resource_id, {})
        cpu_avg = _stat(vm_met, "Percentage CPU", "avg")
        cpu_p95 = _stat(vm_met, "Percentage CPU", "p95")
        if cpu_avg is not None:
            cpu_avgs.append(cpu_avg)
        if cpu_p95 is not None:
            cpu_p95s.append(cpu_p95)

        sku_spec = catalog.get(vm.subscription_id, vm.region, vm.vm_sku)
        if sku_spec and sku_spec.accelerated_networking:
            has_accel_net = True

    if not cpu_avgs:
        return out

    cpu_avg = _mean(cpu_avgs) or 0.0
    cpu_p95 = max(cpu_p95s) if cpu_p95s else 0.0
    baseline = _bseries_baseline_pct(vcpus)
    lookback = thresholds.lookback_days

    # RSZ-BSF-001: non-B-series eligible for B-series
    if not _is_bseries_sku(sku):
        sku_lower = sku.lower()
        eligible = any(sku_lower.startswith(f) for f in _ELIGIBLE_FAMILIES)
        if (
            eligible
            and not has_accel_net
            and cpu_avg < baseline
            and cpu_p95 < 2 * baseline
            and _bseries_credits_sufficient(cpu_avg, vcpus, lookback)
        ):
            proposed = f"Standard_B{vcpus}ms (or equivalent B-series with {vcpus} vCPUs)"
            kwargs = _rec_kwargs(enriched=group_enriched, category=Category.RIGHTSIZE)
            kwargs["deltas"] = {"signal": "burstable-fit"}
            out.append(
                Finding(
                    vm_id=group.parent_id if group.is_aggregated else group.members[0].resource_id,
                    category=Category.RIGHTSIZE,
                    subcategory=SubCategory.BURSTABLE_FIT,
                    code="RSZ-BSF-001",
                    current=sku,
                    proposed=proposed,
                    rationale=(
                        f"Avg CPU {cpu_avg:.1f}% is below the B-series baseline of "
                        f"{baseline:.0f}% for {vcpus} vCPUs over {lookback} days; "
                        f"P95 CPU {cpu_p95:.1f}% < {2 * baseline:.0f}% (2× baseline). "
                        "AcceleratedNetworking is not enabled. "
                        "The B-series credit model is sufficient to support this workload "
                        "at lower cost."
                    ),
                    **kwargs,
                )
            )

    # RSZ-BSM-001: already B-series but CPU consistently exceeds baseline
    else:
        if cpu_avg >= baseline:
            kwargs = _rec_kwargs(enriched=group_enriched, category=Category.RIGHTSIZE)
            kwargs["deltas"] = {"signal": "burstable-misfit"}
            out.append(
                Finding(
                    vm_id=group.parent_id if group.is_aggregated else group.members[0].resource_id,
                    category=Category.RIGHTSIZE,
                    subcategory=SubCategory.BURSTABLE_MISFIT,
                    code="RSZ-BSM-001",
                    current=sku,
                    proposed=None,
                    rationale=(
                        f"VM is on B-series ({sku}) but avg CPU {cpu_avg:.1f}% meets or "
                        f"exceeds the burstable baseline of {baseline:.0f}% over {lookback} days. "
                        "This workload is depleting burst credits and would benefit from a "
                        "general-purpose SKU in the D, E, or F series."
                    ),
                    **kwargs,
                )
            )

    return out
