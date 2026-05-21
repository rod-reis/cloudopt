"""RSZ-DSK-001 detector — disk rightsize (over/under-provisioned IOPS).

Fires when OS-agent enrichment data shows that observed disk IOPS are
significantly below the capacity implied by the VM's total provisioned disk
storage, suggesting the disks are over-provisioned for actual workload demand.

A platform-only path (using "Disk Read/Write Operations/Sec" averages) is
also checked as a lower-confidence signal when enrichment data is absent.

Triggers:
  OS-agent (HIGH confidence path):
    os.disk.read_iops P95 + os.disk.write_iops P95 < 50 IOPS combined
    AND vm.disk_count >= 1

  Platform-only (MEDIUM confidence path):
    avg("Disk Read Operations/Sec") + avg("Disk Write Operations/Sec") < 50
    AND vm.disk_count >= 1 AND sum(vm.disk_sizes_gb) > 256 GB

Both paths represent a conservative detection to avoid false positives.
The finding prompts workload owners to review whether provisioned disk
capacity and IOPS tier match actual usage.

Note: Without per-disk tier metadata (not available in the current
VmInventory model), exact provisioned IOPS limits cannot be computed.
The finding is therefore a MEDIUM-confidence recommendation to review,
not a prescriptive "downsize to X".
"""

from __future__ import annotations

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
from cloudopt.enrichment.schema import EnrichedVmMetrics, MonitoringConfidence
from cloudopt.models import (
    CollectionThresholds,
    Finding,
    QuotaItem,
    VmInventory,
    VmMetrics,
)

# Combined read+write IOPS below which disks are considered under-utilized
_LOW_IOPS_THRESHOLD = 50.0

# Minimum total provisioned disk size (GB) to make the platform-only check
# meaningful — small VMs with tiny disks are not candidates for downsizing
_MIN_DISK_SIZE_GB = 256.0


def detect(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    quota: list[QuotaItem],
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
    *,
    enriched_map: Optional[dict[str, EnrichedVmMetrics]] = None,
) -> list[Finding]:
    """Emit RSZ-DSK-001 Findings for VMs with under-utilized disk IOPS."""
    metrics_by_vm = _group_metrics(metrics)
    workloads = _build_workload_groups(vms)
    out: list[Finding] = []
    for group in workloads:
        result = _evaluate(group, metrics_by_vm, thresholds, enriched_map=enriched_map)
        if result is not None:
            out.append(result)
    return out


def _evaluate(
    group: _WorkloadGroup,
    metrics_by_vm: dict[str, dict[str, VmMetrics]],
    thresholds: CollectionThresholds,
    *,
    enriched_map: Optional[dict[str, EnrichedVmMetrics]] = None,
) -> Optional[Finding]:
    group_enriched = _best_enriched(group.members, enriched_map)
    vm_id = group.parent_id if group.is_aggregated else group.members[0].resource_id
    sku = group.representative_sku

    # --- OS-agent path (preferred) ---
    if (
        group_enriched is not None
        and group_enriched.confidence_tier != MonitoringConfidence.PLATFORM_ONLY
    ):
        read_dp = group_enriched.get("os.disk.read_iops") or group_enriched.get("os.disk.iops_read")
        write_dp = group_enriched.get("os.disk.write_iops") or group_enriched.get("os.disk.iops_write")

        if read_dp is not None and write_dp is not None:
            read_p95 = read_dp.p95_value if read_dp.p95_value is not None else read_dp.avg_value
            write_p95 = write_dp.p95_value if write_dp.p95_value is not None else write_dp.avg_value

            if read_p95 is not None and write_p95 is not None:
                total_p95 = read_p95 + write_p95
                has_disks = any(m.disk_count >= 1 for m in group.members)

                if total_p95 < _LOW_IOPS_THRESHOLD and has_disks:
                    kwargs = _rec_kwargs(enriched=group_enriched, category=Category.RIGHTSIZE)
                    kwargs["deltas"] = {
                        "signal": "low-disk-iops-os",
                        "combined_iops_p95": round(total_p95, 1),
                    }
                    return Finding(
                        vm_id=vm_id,
                        category=Category.RIGHTSIZE,
                        subcategory=SubCategory.DISK_RIGHTSIZE,
                        code="RSZ-DSK-001",
                        current=sku or None,
                        proposed=None,
                        rationale=(
                            f"OS-agent data shows combined disk IOPS P95 {total_p95:.1f} "
                            f"(read {read_p95:.1f} + write {write_p95:.1f}) is below the "
                            f"{_LOW_IOPS_THRESHOLD:.0f} IOPS threshold over the "
                            f"{thresholds.lookback_days}-day lookback. "
                            "Review whether the provisioned disk capacity and tier match "
                            "actual IOPS demand — smaller or lower-tier disks may suffice."
                        ),
                        **kwargs,
                    )

    # --- Platform-only path (lower confidence, requires large disks) ---
    read_avgs: list[float] = []
    write_avgs: list[float] = []
    total_disk_gb: float = 0.0

    for vm in group.members:
        vm_met = metrics_by_vm.get(vm.resource_id, {})
        r = _stat(vm_met, "Disk Read Operations/Sec", "avg")
        w = _stat(vm_met, "Disk Write Operations/Sec", "avg")
        if r is not None:
            read_avgs.append(r)
        if w is not None:
            write_avgs.append(w)
        total_disk_gb += sum(vm.disk_sizes_gb) if vm.disk_sizes_gb else 0.0

    if not read_avgs and not write_avgs:
        return None
    if total_disk_gb < _MIN_DISK_SIZE_GB:
        return None

    avg_read = sum(read_avgs) / len(read_avgs) if read_avgs else 0.0
    avg_write = sum(write_avgs) / len(write_avgs) if write_avgs else 0.0
    avg_total = avg_read + avg_write

    if avg_total >= _LOW_IOPS_THRESHOLD:
        return None

    has_disks = any(m.disk_count >= 1 for m in group.members)
    if not has_disks:
        return None

    kwargs = _rec_kwargs(enriched=None, category=Category.RIGHTSIZE)
    kwargs["deltas"] = {
        "signal": "low-disk-iops-platform",
        "combined_iops_avg": round(avg_total, 1),
        "total_disk_gb": round(total_disk_gb, 0),
    }

    return Finding(
        vm_id=vm_id,
        category=Category.RIGHTSIZE,
        subcategory=SubCategory.DISK_RIGHTSIZE,
        code="RSZ-DSK-001",
        current=sku or None,
        proposed=None,
        rationale=(
            f"Average disk IOPS {avg_total:.1f} ops/sec (read {avg_read:.1f} + "
            f"write {avg_write:.1f}) is below the {_LOW_IOPS_THRESHOLD:.0f} IOPS "
            f"threshold with {total_disk_gb:.0f} GB total provisioned disk "
            f"over the {thresholds.lookback_days}-day lookback. "
            "Review whether the provisioned disk capacity and IOPS tier match actual demand. "
            "Supply OS-agent disk metrics (os.disk.read_iops / os.disk.write_iops) via a "
            "monitoring export to confirm this finding at HIGH confidence."
        ),
        **kwargs,
    )
