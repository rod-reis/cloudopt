"""VM inventory collection via Azure Resource Graph.

Uses a single cross-subscription KQL query (O(1) API calls) instead of
iterating management APIs per resource group. Supports up to 200 subscriptions
per query — batches automatically for larger estates.
"""

from __future__ import annotations

from typing import Any, cast

from azure.identity import DefaultAzureCredential
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from cloudopt.collector.auth import SubscriptionInfo
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.models import VmInventory
from cloudopt.scope import (
    ScopeFilter,
    kql_location_clause,
    kql_resource_group_clause,
)

console = Console()

# Resource Graph API max subscriptions per query
_MAX_SUBS_PER_QUERY = 200


def _scope_clauses(scope: ScopeFilter | None) -> str:
    """Region + resource-group KQL pipe fragments for an inventory query."""
    if scope is None:
        return ""
    return kql_location_clause(scope) + kql_resource_group_clause(scope)


# KQL query — captures the full ``properties`` blob plus a few projected
# convenience columns for downstream parsing.  The full properties payload is
# stored as ``raw_properties`` on the VmInventory model so every field
# available in the Azure Resource Graph ``resources`` table is preserved.
_VM_QUERY_BASE = """
Resources
| where type =~ 'microsoft.compute/virtualmachines'"""

_VM_QUERY_TAIL = """
| extend
    vmssName         = tostring(properties.virtualMachineScaleSet.id),
    avSetName        = tostring(properties.availabilitySet.id),
    osType           = tostring(properties.storageProfile.osDisk.osType),
    vmSku            = tostring(properties.hardwareProfile.vmSize),
    zone             = tostring(iff(isnull(zones) or array_length(zones) == 0, '', tostring(zones[0]))),
    nicCount         = array_length(properties.networkProfile.networkInterfaces),
    osDiskSizeGbVm   = toint(properties.storageProfile.osDisk.diskSizeGB),
    osDiskManagedId  = tolower(tostring(properties.storageProfile.osDisk.managedDisk.id)),
    powerState       = tostring(properties.extended.instanceView.powerState.code),
    imgPublisher    = tostring(properties.storageProfile.imageReference.publisher),
    imgOffer        = tostring(properties.storageProfile.imageReference.offer),
    imgSku          = tostring(properties.storageProfile.imageReference.sku),
    imgVersion      = tostring(properties.storageProfile.imageReference.exactVersion)
| extend
    dataDisks = properties.storageProfile.dataDisks
| join kind=leftouter (
    Resources
    | where type =~ 'microsoft.compute/disks'
    | project managedDiskId = tolower(id), managedDiskSizeGb = toint(properties.diskSizeGB)
) on $left.osDiskManagedId == $right.managedDiskId
| extend osDiskSizeGb = coalesce(osDiskSizeGbVm, managedDiskSizeGb, 0)
| project
    id,
    name,
    subscriptionId,
    resourceGroup,
    location,
    vmSku,
    osType,
    zone,
    nicCount,
    osDiskSizeGb,
    dataDisks,
    vmssName,
    avSetName,
    tags,
    powerState,
    imgPublisher,
    imgOffer,
    imgSku,
    imgVersion,
    properties
"""


def _build_vm_query(scope: ScopeFilter | None) -> str:
    return _VM_QUERY_BASE + _scope_clauses(scope) + _VM_QUERY_TAIL


def collect_inventory(
    credential: DefaultAzureCredential,
    subscriptions: list[SubscriptionInfo],
    sku_catalog: SkuCatalog,
    scope: ScopeFilter | None = None,
) -> list[VmInventory]:
    """Query Azure Resource Graph for all VMs across the given subscriptions.

    Region and resource-group filters from ``scope`` are applied directly in
    KQL (server-side).  Tag filters are applied in-memory after the query so
    that the tag values themselves are never persisted by us — they live only
    long enough to decide whether each VM is in scope.
    """
    client = ResourceGraphClient(credential)
    sub_map: dict[str, str] = {s.subscription_id: s.subscription_name for s in subscriptions}
    sub_ids = list(sub_map.keys())

    raw_rows: list[dict[str, Any]] = []

    # Batch subscriptions to stay within Resource Graph limits
    batches = [
        sub_ids[i : i + _MAX_SUBS_PER_QUERY]
        for i in range(0, len(sub_ids), _MAX_SUBS_PER_QUERY)
    ]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Querying Resource Graph…", total=len(batches))

        for batch in batches:
            skip_token: str | None = None
            while True:
                options = QueryRequestOptions(result_format="objectArray")
                if skip_token:
                    options.skip_token = skip_token

                request = QueryRequest(
                    subscriptions=batch,
                    query=_build_vm_query(scope),
                    options=options,
                )
                response = client.resources(request)
                raw_rows.extend(cast(list[dict[str, Any]], response.data or []))

                skip_token = getattr(response, "skip_token", None)
                if not skip_token:
                    break

            progress.advance(task)

    vms: list[VmInventory] = []
    for row in raw_rows:
        vm = _row_to_vm(row, sub_map, sku_catalog)
        if vm is None:
            continue
        # In-memory tag filter (tags themselves are NOT persisted from here)
        if scope is not None and scope.has_tag_filter:
            if not scope.in_scope_tags(row.get("tags")):
                continue
        vms.append(vm)

    return vms


def _row_to_vm(
    row: dict[str, Any],
    sub_map: dict[str, str],
    sku_catalog: SkuCatalog,
) -> VmInventory | None:
    """Convert a Resource Graph result row into a VmInventory model."""
    sub_id: str = row.get("subscriptionId", "")
    region: str = (row.get("location") or "").lower()
    vm_sku: str = row.get("vmSku") or ""

    spec = sku_catalog.get(sub_id, region, vm_sku)

    # Parse data disk sizes
    data_disks = row.get("dataDisks") or []
    disk_sizes: list[float] = []
    if isinstance(data_disks, list):
        for disk in data_disks:
            if isinstance(disk, dict):
                size = disk.get("diskSizeGB")
                if size is not None:
                    try:
                        disk_sizes.append(float(size))
                    except (ValueError, TypeError):
                        pass
    disk_count = len(disk_sizes)

    os_disk_size = row.get("osDiskSizeGb")
    try:
        os_disk_gb = float(os_disk_size) if os_disk_size else 0.0
    except (ValueError, TypeError):
        os_disk_gb = 0.0
    if os_disk_gb:
        disk_sizes.insert(0, os_disk_gb)

    # Extract bare names from resource IDs for VMSS and AvSet
    vmss_raw: str = row.get("vmssName") or ""
    avset_raw: str = row.get("avSetName") or ""

    def _last_segment(resource_id: str) -> str | None:
        parts = resource_id.strip("/").split("/")
        return parts[-1] if parts else None

    try:
        return VmInventory(
            resource_id=row.get("id", ""),
            subscription_id=sub_id,
            subscription_name=sub_map.get(sub_id, sub_id),
            resource_group=row.get("resourceGroup", ""),
            vm_name=row.get("name", ""),
            vm_sku=vm_sku,
            vcpus=spec.vcpus if spec else 0,
            memory_gb=spec.memory_gb if spec else 0.0,
            region=region,
            os_type=row.get("osType") or "Unknown",
            os_version=row.get("imgVersion") or None,
            availability_zone=row.get("zone") or None,
            power_state=row.get("powerState") or None,
            image_publisher=row.get("imgPublisher") or None,
            image_offer=row.get("imgOffer") or None,
            image_sku=row.get("imgSku") or None,
            image_version=row.get("imgVersion") or None,
            nic_count=int(row.get("nicCount") or 0),
            disk_count=disk_count,
            disk_sizes_gb=disk_sizes,
            vmss_name=_last_segment(vmss_raw) if vmss_raw else None,
            vmss_id=vmss_raw if vmss_raw else None,
            availability_set_name=_last_segment(avset_raw) if avset_raw else None,
            availability_set_id=avset_raw if avset_raw else None,
            raw_properties=cast(dict, row.get("properties")) if isinstance(row.get("properties"), dict) else {},
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pre-execution resource counting
# ---------------------------------------------------------------------------

def _build_count_query(scope: ScopeFilter | None) -> str:
    base = """
Resources
| where type in~ ('microsoft.compute/virtualmachines', 'microsoft.insights/components')"""
    tail = """
| summarize count() by type, subscriptionId
"""
    return base + _scope_clauses(scope) + tail


def count_resources_by_type(
    credential: DefaultAzureCredential,
    subscriptions: list[SubscriptionInfo],
    scope: ScopeFilter | None = None,
) -> dict[str, dict[str, int]]:
    """Count VMs and App Insights components per subscription via Resource Graph.

    Returns: {subscription_id: {"vms": n, "appinsights": n}}
    A fast, lightweight query used for the pre-execution summary only.
    """
    client = ResourceGraphClient(credential)
    sub_ids = [s.subscription_id for s in subscriptions]

    counts: dict[str, dict[str, int]] = {
        s.subscription_id: {"vms": 0, "appinsights": 0} for s in subscriptions
    }

    for i in range(0, len(sub_ids), _MAX_SUBS_PER_QUERY):
        batch = sub_ids[i : i + _MAX_SUBS_PER_QUERY]
        req = QueryRequest(
            subscriptions=batch,
            query=_build_count_query(scope),
            options=QueryRequestOptions(result_format="objectArray"),
        )
        try:
            resp = client.resources(req)
            for row in cast(list[dict[str, Any]], resp.data or []):
                sub_id = row.get("subscriptionId", "")
                rtype = (row.get("type") or "").lower()
                count = int(row.get("count_", 0))
                if sub_id not in counts:
                    counts[sub_id] = {"vms": 0, "appinsights": 0}
                if "virtualmachines" in rtype:
                    counts[sub_id]["vms"] = count
                elif "insights/components" in rtype:
                    counts[sub_id]["appinsights"] = count
        except Exception:
            pass  # non-fatal; summary will show 0

    return counts
