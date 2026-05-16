"""Group managed-compute VMs by (parent_service, vmss, sku) for the Excel sheet.

SPEC §7.3: The managed-compute service sheets (AKS, AVD, Databricks, etc.)
show one row per unique (parent_service_id, parent_pool_name, vmss_id, vm_sku)
combination, aggregating metric statistics across all VMs in the group.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Optional

from cloudopt.enrichment.schema import GuestMetricRow
from cloudopt.models import ManagedComputeGroupRow, ParentServiceType, VmInventory, VmMetrics


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def group_managed_vms_by_sku(
    vms: list[VmInventory],
    metrics: dict[str, dict[str, VmMetrics]],
    guest: dict[str, GuestMetricRow] | None = None,
) -> list[ManagedComputeGroupRow]:
    """Aggregate VMs that belong to managed-compute services into group rows.

    Groups VMs by ``(parent_service_type, parent_service_id, parent_pool_name,
    vmss_id, vm_sku)`` then rolls up metric percentile averages.

    Includes STANDALONE_VMSS groups (VMSS Flex) so they appear on the
    *Perf by VMSS Group* sheet.  AKS/AVD/etc. groups appear on their
    respective managed-service sheets via the parent_service_type filter.

    Args:
        vms: Full VM inventory.  Bare standalone VMs are ignored.
        metrics: Mapping of resource_id → {metric_name → VmMetrics}.
        guest: Optional mapping of resource_id → GuestMetricRow.

    Returns:
        List of aggregated ``ManagedComputeGroupRow`` objects, one per group key.
    """
    guest = guest or {}
    buckets: dict[_GroupKey, list[VmInventory]] = defaultdict(list)

    for vm in vms:
        if vm.parent_service_type == ParentServiceType.STANDALONE:
            continue
        key = _GroupKey(
            parent_service_type=vm.parent_service_type,
            parent_service_id=vm.parent_service_id,
            parent_pool_name=vm.parent_pool_name,
            vmss_id=vm.vmss_id,
            vm_sku=vm.vm_sku,
        )
        buckets[key].append(vm)

    rows: list[ManagedComputeGroupRow] = []
    for key, group_vms in buckets.items():
        rows.append(_build_row(key, group_vms, metrics, guest))

    rows.sort(key=lambda r: (
        r.parent_service_type.value,
        r.parent_service_name or "",
        r.parent_pool_name or "",
        r.vmss_name or "",
        r.vm_sku,
    ))
    return rows


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class _GroupKey:
    """Hashable group-by key."""

    __slots__ = (
        "parent_service_type",
        "parent_service_id",
        "parent_pool_name",
        "vmss_id",
        "vm_sku",
    )

    def __init__(
        self,
        parent_service_type: ParentServiceType,
        parent_service_id: Optional[str],
        parent_pool_name: Optional[str],
        vmss_id: Optional[str],
        vm_sku: str,
    ) -> None:
        self.parent_service_type = parent_service_type
        self.parent_service_id = parent_service_id
        self.parent_pool_name = parent_pool_name
        self.vmss_id = vmss_id
        self.vm_sku = vm_sku

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _GroupKey):
            return NotImplemented
        return (
            self.parent_service_type == other.parent_service_type
            and self.parent_service_id == other.parent_service_id
            and self.parent_pool_name == other.parent_pool_name
            and self.vmss_id == other.vmss_id
            and self.vm_sku == other.vm_sku
        )

    def __hash__(self) -> int:
        return hash((
            self.parent_service_type,
            self.parent_service_id,
            self.parent_pool_name,
            self.vmss_id,
            self.vm_sku,
        ))


def _last_segment(resource_id: Optional[str]) -> Optional[str]:
    if not resource_id:
        return None
    parts = resource_id.strip("/").split("/")
    return parts[-1] if parts else None


def _avg_metric(values: list[Optional[float]]) -> Optional[float]:
    non_null = [v for v in values if v is not None]
    return round(mean(non_null), 2) if non_null else None


def _build_row(
    key: _GroupKey,
    group_vms: list[VmInventory],
    metrics: dict[str, dict[str, VmMetrics]],
    guest: dict[str, GuestMetricRow],
) -> ManagedComputeGroupRow:
    """Build a single ManagedComputeGroupRow from a list of VMs in the same group."""
    first = group_vms[0]
    instance_count = len(group_vms)

    def _stat(metric_name: str, stat: str) -> Optional[float]:
        vals = [
            getattr(metrics.get(v.resource_id, {}).get(metric_name), stat, None)
            for v in group_vms
        ]
        return _avg_metric([v for v in vals if v is not None])

    def _mem_avg_pct() -> Optional[float]:
        """Convert Available Memory Bytes avg → % used, averaged across group."""
        vals: list[float] = []
        for v in group_vms:
            m = metrics.get(v.resource_id, {}).get("Available Memory Bytes")
            if m and m.avg is not None and v.memory_gb:
                avail_gb = m.avg / (1024 ** 3)
                used_pct = 100.0 * (1.0 - avail_gb / v.memory_gb)
                vals.append(max(0.0, min(100.0, used_pct)))
        return round(mean(vals), 2) if vals else None

    # Aggregate platform metrics
    cpu_avg = _stat("Percentage CPU", "avg")
    cpu_p95 = _stat("Percentage CPU", "p95")
    cpu_p99 = _stat("Percentage CPU", "p99")
    cpu_max = _stat("Percentage CPU", "max")
    cpu_min = _stat("Percentage CPU", "min")
    mem_avg = _mem_avg_pct()

    # Aggregate guest metrics across all VMs in the group
    merged_guest = _merge_guest_metrics(
        [guest[v.resource_id] for v in group_vms if v.resource_id in guest]
    )

    zones_set: set[str] = set()
    for vm in group_vms:
        if vm.availability_zone:
            zones_set.add(vm.availability_zone)

    # Sources used — collect from all metrics in the group
    sources: set[str] = set()
    days_list: list[int] = []
    for vm in group_vms:
        vm_met = metrics.get(vm.resource_id, {})
        for m in vm_met.values():
            if hasattr(m, "source") and m.source:
                sources.add(m.source)
            if hasattr(m, "days_observed") and m.days_observed:
                days_list.append(m.days_observed)

    return ManagedComputeGroupRow(
        parent_service_type=key.parent_service_type,
        parent_service_name=first.parent_service_name,
        parent_service_id=key.parent_service_id,
        parent_pool_name=key.parent_pool_name,
        vmss_name=first.vmss_name or _last_segment(key.vmss_id),
        vmss_id=key.vmss_id,
        vm_sku=key.vm_sku,
        instance_count=instance_count,
        total_instance_count=instance_count,
        subscription_name=first.subscription_name,
        subscription_id=first.subscription_id,
        resource_group=first.resource_group,
        region=first.region,
        os_type=first.os_type,
        os_image=first.image_offer or first.os_version,
        zones=", ".join(sorted(zones_set)) or None,
        vcpus=first.vcpus,
        memory_gb=first.memory_gb,
        tags=None,
        avg_cpu_pct=cpu_avg,
        p95_cpu_pct=cpu_p95,
        p99_cpu_pct=cpu_p99,
        max_cpu_pct=cpu_max,
        min_cpu_pct=cpu_min,
        avg_mem_pct=mem_avg,
        guest_metrics=merged_guest.model_dump(exclude_none=True) if merged_guest else {},
        has_os_data=merged_guest.has_any_data if merged_guest else False,
        sources_used=", ".join(sorted(sources)) or None,
        days_observed=max(days_list) if days_list else None,
        coverage_pct=None,
    )


def _merge_guest_metrics(rows: list[GuestMetricRow]) -> GuestMetricRow:
    """Average each guest metric field across the group."""
    if not rows:
        return GuestMetricRow()

    field_values: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        for field_name in GuestMetricRow.model_fields:
            val = getattr(row, field_name)
            if val is not None:
                field_values[field_name].append(val)

    averaged = {
        fn: round(mean(vals), 4)
        for fn, vals in field_values.items()
        if vals
    }
    return GuestMetricRow(**averaged)
