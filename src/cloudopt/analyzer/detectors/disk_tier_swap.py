"""SWP-DST-001 detector — disk tier swap (Premium SSD → Standard SSD).

Fires when a VM uses Premium Storage managed disks but observed aggregate
disk IOPS are well below what justifies the premium tier, indicating that
Standard SSD would likely suffice.

Signal source:
  Platform metrics: "Disk Read Operations/Sec" and "Disk Write Operations/Sec"
  (both collected as Average aggregation).

Disk type inference:
  Primary: ``vm.raw_properties.storageProfile`` (verbatim ARG blob).
  Fallback: SKU name 's' suffix (Premium Storage capable SKU).

IOPS threshold:
  Conservative default — avg total IOPS < 100 ops/sec across the lookback
  window.  This is well below even the smallest Premium SSD P4 tier limit
  (120 IOPS), making false positives rare.

Confidence: MEDIUM (platform metrics only; actual disk tier confirmed from
raw_properties when available, inferred from SKU name otherwise).
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

# Average total IOPS (read + write) below which a tier swap is considered
_LOW_IOPS_THRESHOLD = 100.0

# SKU name pattern indicating Premium Storage support ('s' suffix before _v or at end)
_PREMIUM_SKU_RE = re.compile(r"s(_v\d+)?$", re.IGNORECASE)


def detect(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    quota: list[QuotaItem],
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
    *,
    enriched_map: Optional[dict[str, EnrichedVmMetrics]] = None,
) -> list[Finding]:
    """Emit SWP-DST-001 Findings for VMs with low disk IOPS on Premium Storage."""
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
    sku = group.representative_sku
    if not sku:
        return None

    # Collect aggregate IOPS across all group members
    total_read_avgs: list[float] = []
    total_write_avgs: list[float] = []
    premium_votes: list[bool] = []

    for vm in group.members:
        vm_met = metrics_by_vm.get(vm.resource_id, {})
        read_avg = _stat(vm_met, "Disk Read Operations/Sec", "avg")
        write_avg = _stat(vm_met, "Disk Write Operations/Sec", "avg")

        if read_avg is not None:
            total_read_avgs.append(read_avg)
        if write_avg is not None:
            total_write_avgs.append(write_avg)

        premium_votes.append(_uses_premium_storage(vm))

    if not total_read_avgs and not total_write_avgs:
        return None  # no disk metrics available

    avg_read = sum(total_read_avgs) / len(total_read_avgs) if total_read_avgs else 0.0
    avg_write = sum(total_write_avgs) / len(total_write_avgs) if total_write_avgs else 0.0
    avg_total_iops = avg_read + avg_write

    if avg_total_iops >= _LOW_IOPS_THRESHOLD:
        return None  # IOPS too high — Premium may be needed

    # Only flag if Premium Storage is in use
    uses_premium = any(premium_votes)
    if not uses_premium:
        return None

    group_enriched = _best_enriched(group.members, enriched_map)
    vm_id = group.parent_id if group.is_aggregated else group.members[0].resource_id

    # Determine tier source for rationale clarity
    tier_source = "confirmed from VM storage profile" if _raw_props_confirm_premium(
        group.members[0]
    ) else "inferred from Premium Storage-capable SKU"

    kwargs = _rec_kwargs(enriched=group_enriched, category=Category.SWAP)
    kwargs["deltas"] = {
        "signal": "low-iops-premium",
        "avg_total_iops": round(avg_total_iops, 1),
    }

    return Finding(
        vm_id=vm_id,
        category=Category.SWAP,
        subcategory=SubCategory.DISK_TIER,
        code="SWP-DST-001",
        current="Premium_LRS",
        proposed="StandardSSD_LRS",
        rationale=(
            f"Average disk IOPS {avg_total_iops:.1f} ops/sec (read {avg_read:.1f} + "
            f"write {avg_write:.1f}) is below the {_LOW_IOPS_THRESHOLD:.0f} IOPS threshold "
            f"over the {thresholds.lookback_days}-day lookback. "
            f"Premium Storage is in use ({tier_source}). "
            "Standard SSD provides up to 6000 IOPS at lower cost — review whether the "
            "workload requires premium tier latency/throughput guarantees before downtiering."
        ),
        **kwargs,
    )


def _uses_premium_storage(vm: VmInventory) -> bool:
    """Return True when the VM is confirmed or likely using Premium Storage disks."""
    if _raw_props_confirm_premium(vm):
        return True
    # Fallback: SKU name ending in 's' before _v or at end indicates Premium Storage capable
    return bool(_PREMIUM_SKU_RE.search(vm.vm_sku))


def _raw_props_confirm_premium(vm: VmInventory) -> bool:
    """Return True when raw_properties confirms at least one Premium_LRS disk."""
    try:
        sp = vm.raw_properties.get("storageProfile", {})
        for disk in [sp.get("osDisk", {})] + sp.get("dataDisks", []):
            t = (disk.get("managedDisk") or {}).get("storageAccountType", "")
            if t.lower() == "premium_lrs":
                return True
    except Exception:
        pass
    return False
