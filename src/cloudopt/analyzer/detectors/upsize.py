"""RSZ-UPS-001 detector — right-size up (sustained CPU and memory pressure).

Fires only when OS-level monitoring data is available (os-aware or workload-aware
enrichment confidence tier).  Azure Monitor host-level metrics alone are not
sufficient to drive an upsize recommendation — the proxy ``Available Memory Bytes``
counter is too noisy on its own to confirm real saturation.

Trigger (both conditions must hold over the full lookback window):
  os.cpu.used_percent  P95 >= 85 %
  os.memory.used_percent P95 >= 85 %

When P95 is absent from the enrichment export, avg_value is used as a
conservative fallback.

The finding is suppressed entirely when no enrichment data is present —
this is intentional.  An upsize flag without OS-agent evidence would be
noise and could encourage unnecessary resizing.
"""

from __future__ import annotations

from typing import Optional

from cloudopt.analyzer.detectors._shared import (
    _WorkloadGroup,
    _best_enriched,
    _build_workload_groups,
    _group_metrics,
    _rec_kwargs,
)
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.analyzer.taxonomy import Category, Confidence, Readiness, SubCategory
from cloudopt.enrichment.schema import EnrichedVmMetrics, MonitoringConfidence
from cloudopt.models import (
    CollectionThresholds,
    Finding,
    QuotaItem,
    VmInventory,
    VmMetrics,
)

# Both CPU and memory P95 must be at or above this threshold
_PRESSURE_THRESHOLD_PCT = 85.0


def detect(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    quota: list[QuotaItem],
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
    *,
    enriched_map: Optional[dict[str, EnrichedVmMetrics]] = None,
) -> list[Finding]:
    """Emit RSZ-UPS-001 Findings for VMs with sustained CPU and memory pressure."""
    if not enriched_map:
        return []
    workloads = _build_workload_groups(vms)
    out: list[Finding] = []
    for group in workloads:
        result = _evaluate(group, thresholds, catalog, enriched_map)
        if result is not None:
            out.append(result)
    return out


def _evaluate(
    group: _WorkloadGroup,
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
    enriched_map: dict[str, EnrichedVmMetrics],
) -> Optional[Finding]:
    group_enriched = _best_enriched(group.members, enriched_map)
    if group_enriched is None:
        return None

    # Require OS-level data — platform-only confidence is insufficient
    if group_enriched.confidence_tier == MonitoringConfidence.PLATFORM_ONLY:
        return None

    # Try SPEC §7.4 canonical name first, then the legacy alias
    cpu_dp = group_enriched.get("os.cpu.used_percent") or group_enriched.get("os.cpu.percent")
    mem_dp = group_enriched.get("os.memory.used_percent")

    if cpu_dp is None or mem_dp is None:
        return None

    # Prefer P95; fall back to avg when the export doesn't include percentiles
    cpu_p95 = cpu_dp.p95_value if cpu_dp.p95_value is not None else cpu_dp.avg_value
    mem_p95 = mem_dp.p95_value if mem_dp.p95_value is not None else mem_dp.avg_value

    if cpu_p95 is None or mem_p95 is None:
        return None

    if cpu_p95 < _PRESSURE_THRESHOLD_PCT or mem_p95 < _PRESSURE_THRESHOLD_PCT:
        return None

    sku = group.representative_sku
    representative_vm = group.members[0]

    larger_sku = catalog.find_larger_sku(
        subscription_id=representative_vm.subscription_id,
        region=representative_vm.region,
        current_sku=sku,
    )

    vm_id = group.parent_id if group.is_aggregated else group.members[0].resource_id

    # OS-agent data drives HIGH confidence for upsize
    kwargs = _rec_kwargs(enriched=group_enriched, category=Category.RIGHTSIZE)
    kwargs["confidence"] = Confidence.HIGH
    kwargs["readiness"] = Readiness.READY
    kwargs["blockers_to_high"] = []
    kwargs["deltas"] = {
        "signal": "pressure",
        "cpu_p95": round(cpu_p95, 1),
        "mem_p95": round(mem_p95, 1),
    }

    return Finding(
        vm_id=vm_id,
        category=Category.RIGHTSIZE,
        subcategory=SubCategory.UPSIZE,
        code="RSZ-UPS-001",
        current=sku or None,
        proposed=larger_sku,
        rationale=(
            f"OS-agent data shows CPU P95 {cpu_p95:.1f}% and memory P95 {mem_p95:.1f}% "
            f"— both above the {_PRESSURE_THRESHOLD_PCT:.0f}% pressure threshold — "
            f"over the {thresholds.lookback_days}-day lookback. "
            "The current SKU may be undersized; scaling up preserves performance headroom "
            "and reduces risk of resource contention. "
            "Review with the workload owner before actioning."
        ),
        **kwargs,
    )
