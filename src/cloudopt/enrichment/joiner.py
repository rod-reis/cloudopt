"""Join monitoring data points with Azure VM inventory by hostname.

Matching strategy (applied in priority order; first match wins):
  1. Exact:             hostname == vm.vm_name
  2. Case-insensitive:  hostname.lower() == vm.vm_name.lower()
  3. Short-name:        first label of hostname (before first ".") matches
                        first label of vm_name, both lowercased.
                        Handles FQDN vs short-name mismatches.

VMs with no matching hostname in the export are listed in
``EnrichmentSummary.unmatched_vm_names`` so the customer can fix the
mapping or provide a custom hostname-to-vmname mapping file.
"""

from __future__ import annotations

from cloudopt.enrichment.schema import (
    EnrichedVmMetrics,
    EnrichmentSummary,
    MonitoringDataPoint,
)
from cloudopt.models import VmInventory


def join_monitoring_data(
    data_points: list[MonitoringDataPoint],
    vms: list[VmInventory],
) -> tuple[list[EnrichedVmMetrics], EnrichmentSummary]:
    """Join *data_points* to *vms* by hostname.

    Returns:
        enriched:  one ``EnrichedVmMetrics`` per matched Azure VM
        summary:   match quality statistics
    """
    exact: dict[str, VmInventory] = {vm.vm_name: vm for vm in vms}
    lower: dict[str, VmInventory] = {vm.vm_name.lower(): vm for vm in vms}
    short: dict[str, VmInventory] = {
        vm.vm_name.split(".")[0].lower(): vm for vm in vms
    }

    # Group data points by the hostname they report
    by_host: dict[str, list[MonitoringDataPoint]] = {}
    for dp in data_points:
        by_host.setdefault(dp.hostname, []).append(dp)

    enriched: dict[str, EnrichedVmMetrics] = {}   # keyed by vm.vm_name
    unmatched_hostnames: list[str] = []

    for hostname, points in by_host.items():
        vm = _match_vm(hostname, exact, lower, short)
        if vm is None:
            unmatched_hostnames.append(hostname)
            continue
        if vm.vm_name not in enriched:
            enriched[vm.vm_name] = EnrichedVmMetrics(
                vm_name=vm.vm_name,
                hostname=hostname,
                source_tool=points[0].source_tool if points else "unknown",
            )
        enriched[vm.vm_name].data_points.extend(points)

    matched_names = set(enriched.keys())
    unmatched_vm_names = [
        vm.vm_name for vm in vms if vm.vm_name not in matched_names
    ]

    all_metrics: list[str] = sorted({
        dp.metric_name
        for evm in enriched.values()
        for dp in evm.data_points
    })
    source_tools: list[str] = sorted({
        dp.source_tool for points in by_host.values() for dp in points
    })
    schema_ver = data_points[0].schema_version if data_points else "1.0"

    summary = EnrichmentSummary(
        source_tools=source_tools,
        total_hostnames_in_export=len(by_host),
        matched_vm_count=len(enriched),
        unmatched_hostnames=sorted(unmatched_hostnames),
        unmatched_vm_names=sorted(unmatched_vm_names),
        schema_version=schema_ver,
        metrics_present=all_metrics,
    )
    return list(enriched.values()), summary


def _match_vm(
    hostname: str,
    exact: dict[str, VmInventory],
    lower: dict[str, VmInventory],
    short: dict[str, VmInventory],
) -> VmInventory | None:
    if hostname in exact:
        return exact[hostname]
    vm = lower.get(hostname.lower())
    if vm is not None:
        return vm
    vm = short.get(hostname.split(".")[0].lower())
    return vm
