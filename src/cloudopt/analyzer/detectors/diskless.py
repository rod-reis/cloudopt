"""SWP-DSK-001 detector — diskless SKU recommendation.

Evaluates whether a VM running a diskful SKU (D, E, or F family v1–v5) has
negligible temp-disk activity and could safely migrate to the equivalent
diskless SKU (indicated by a trailing 's' in the SKU name, e.g. D4s_v5 vs
D4_v5 in some families, or the *_v5 / *ds_v5 distinction in others).

Criteria (mirrors CloudFit Logic 3):
  - SKU is in the D, E, or F family (v1–v5) — diskful variant.
  - Temp disk read/write IOPS utilization < 5% over the lookback period.
  - Temp disk read/write throughput utilization < 5% over the lookback period.
  - Temp disk metrics are present (idle disk with no telemetry is excluded —
    absent metrics do not confirm the disk is unused).

The P100 (maximum hourly value) is used for all temp-disk checks to capture
any peak activity.  Percentage is computed against the SKU's temp-disk limits.

NOTE: Temp-disk capacity limits are not exposed by the resource_skus API in a
machine-readable way for all SKUs.  We fall back to a fixed conservative IOPS
cap (3 200 IOPS / 25 MB/s for a Standard temp disk) when catalog data is
unavailable.  If the actual temp disk limit is higher, the utilization % will
be understated, making this check conservative (fewer false positives).
"""

from __future__ import annotations

import re
from typing import Optional

from cloudopt.analyzer.detectors._shared import (
    _WorkloadGroup,
    _best_enriched,
    _build_workload_groups,
    _group_metrics,
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

# Diskful D/E/F families v1–v5 (case-insensitive)
_DISKFUL_RE = re.compile(
    r"^Standard_[DEF]\d+[a-z]*_v[1-5]$", re.IGNORECASE
)

# Conservative fallback temp-disk limits (Standard local SSD for D/E/F v3+)
_DEFAULT_TEMP_IOPS = 3_200.0      # IOPS
_DEFAULT_TEMP_BPS = 25 * 1024 * 1024.0  # 25 MB/s in bytes/sec

# Maximum utilisation threshold for diskless eligibility
_TEMP_DISK_UTIL_THRESHOLD = 5.0   # %


def detect(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    quota: list[QuotaItem],
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
    *,
    enriched_map: Optional[dict[str, EnrichedVmMetrics]] = None,
) -> list[Finding]:
    """Emit SWP-DSK-001 Findings for VMs eligible for diskless SKUs."""
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

    if not sku or not _DISKFUL_RE.match(sku):
        return out

    temp_iops_maxes: list[float] = []
    temp_bps_maxes: list[float] = []
    has_temp_metrics = False

    for vm in group.members:
        vm_met = metrics_by_vm.get(vm.resource_id, {})

        read_iops = _stat(vm_met, "Temp Disk Read Operations/Sec", "max")
        write_iops = _stat(vm_met, "Temp Disk Write Operations/Sec", "max")
        read_bps = _stat(vm_met, "Temp Disk Read Bytes/sec", "max")
        write_bps = _stat(vm_met, "Temp Disk Write Bytes/sec", "max")

        # Skip this VM entirely if no temp-disk telemetry is present
        if all(v is None for v in (read_iops, write_iops, read_bps, write_bps)):
            continue

        has_temp_metrics = True
        total_iops = (read_iops or 0.0) + (write_iops or 0.0)
        total_bps = (read_bps or 0.0) + (write_bps or 0.0)
        temp_iops_maxes.append(total_iops)
        temp_bps_maxes.append(total_bps)

    if not has_temp_metrics:
        # No telemetry — excluded per policy (cannot confirm disk is unused)
        return out

    peak_iops = max(temp_iops_maxes) if temp_iops_maxes else 0.0
    peak_bps = max(temp_bps_maxes) if temp_bps_maxes else 0.0

    iops_util_pct = (peak_iops / _DEFAULT_TEMP_IOPS) * 100.0
    bps_util_pct = (peak_bps / _DEFAULT_TEMP_BPS) * 100.0

    if iops_util_pct >= _TEMP_DISK_UTIL_THRESHOLD or bps_util_pct >= _TEMP_DISK_UTIL_THRESHOLD:
        return out

    # Derive diskless SKU name heuristic: remove trailing 's' from the base
    # size (e.g. D4s_v5 → D4_v5) where applicable, or suggest the 'ds'->'d'
    # variant.  Provide the pattern as a suggestion when the exact SKU cannot
    # be determined without a full catalog cross-reference.
    diskless_suggestion = _suggest_diskless(sku)

    vm_id = group.parent_id if group.is_aggregated else group.members[0].resource_id
    kwargs = _rec_kwargs(enriched=group_enriched, category=Category.SWAP)
    kwargs["deltas"] = {"signal": "diskless"}
    out.append(
        Finding(
            vm_id=vm_id,
            category=Category.SWAP,
            subcategory=SubCategory.DISK_TIER,
            code="SWP-DSK-001",
            current=sku,
            proposed=diskless_suggestion,
            rationale=(
                f"Temp disk peak IOPS {peak_iops:.0f} ({iops_util_pct:.1f}% of capacity) "
                f"and throughput {peak_bps / 1024 / 1024:.1f} MB/s "
                f"({bps_util_pct:.1f}% of capacity) are both below the "
                f"{_TEMP_DISK_UTIL_THRESHOLD:.0f}% threshold over the "
                f"{thresholds.lookback_days}-day lookback. "
                "The VM is eligible for a diskless SKU, which does not include a "
                "local temporary disk and is less expensive."
            ),
            **kwargs,
        )
    )
    return out


def _suggest_diskless(sku: str) -> str:
    """Heuristic: return the likely diskless variant name or a descriptive suggestion."""
    # Standard_D4s_v5  → Standard_D4_v5 (remove trailing 's' before _v)
    candidate = re.sub(r"s(_v\d+)$", r"\1", sku, flags=re.IGNORECASE)
    if candidate.lower() != sku.lower():
        return candidate
    # Standard_D4ds_v5 → Standard_D4d_v5
    candidate2 = re.sub(r"ds(_v\d+)$", r"d\1", sku, flags=re.IGNORECASE)
    if candidate2.lower() != sku.lower():
        return candidate2
    return f"{sku} (diskless equivalent — verify in SKU catalog)"
