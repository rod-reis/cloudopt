"""Managed-disk inventory collector (Azure Resource Graph).

Returns every managed disk visible in the current scope from the ARG
``microsoft.compute/disks`` table, promoting the performance-relevant
properties to first-class columns and retaining the full ``properties``
payload verbatim.  Used to populate the **Disk Inventory** sheet and to
drive the Pv1 → Pv2 modernization recommendation (SWP-DST-002).

Read-only: the tool never mutates Azure state.
"""

from __future__ import annotations

from typing import Any

from azure.identity import DefaultAzureCredential
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from cloudopt.collector.auth import SubscriptionInfo
from cloudopt.models import DiskInventory
from cloudopt.scope import ScopeFilter, kql_location_clause, kql_resource_group_clause

console = Console()

_MAX_SUBS_PER_QUERY = 200

# KQL: project every disk property useful for efficiency analysis plus the
# full ``properties`` blob (carried verbatim into ``raw_properties``).  Tags
# are intentionally omitted from persistence.
_DISKS_QUERY_BASE = "Resources\n| where type =~ 'microsoft.compute/disks'"

_DISKS_QUERY_TAIL = """
| project
    id,
    name,
    subscriptionId,
    resourceGroup,
    location,
    skuName        = tostring(sku.name),
    skuTier        = tostring(sku.tier),
    perfTier       = tostring(properties.tier),
    diskSizeGb     = toint(properties.diskSizeGB),
    iopsRW         = toint(properties.diskIOPSReadWrite),
    mbpsRW         = toint(properties.diskMBpsReadWrite),
    iopsRO         = toint(properties.diskIOPSReadOnly),
    mbpsRO         = toint(properties.diskMBpsReadOnly),
    burstingEnabled = tobool(properties.burstingEnabled),
    diskState      = tostring(properties.diskState),
    osType         = tostring(properties.osType),
    managedBy,
    managedByExtended,
    zones          = tostring(iif(array_length(zones) > 0, strcat_array(zones, ','), '')),
    encryptionType = tostring(properties.encryption.type),
    networkAccessPolicy = tostring(properties.networkAccessPolicy),
    publicNetworkAccess = tostring(properties.publicNetworkAccess),
    diskControllerTypes = tostring(properties.supportedCapabilities.diskControllerTypes),
    hyperVGeneration = tostring(properties.hyperVGeneration),
    timeCreated    = tostring(properties.timeCreated),
    properties
| order by skuName asc, name asc
"""


def _scope_clauses(scope: ScopeFilter | None) -> str:
    if scope is None:
        return ""
    return kql_location_clause(scope) + kql_resource_group_clause(scope)


def _build_query(scope: ScopeFilter | None) -> str:
    return _DISKS_QUERY_BASE + _scope_clauses(scope) + _DISKS_QUERY_TAIL


def _str_or_none(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _int_or_none(val: Any) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _bool_or_none(val: Any) -> bool | None:
    if isinstance(val, bool):
        return val
    if val is None:
        return None
    s = str(val).strip().lower()
    if s in ("true", "1"):
        return True
    if s in ("false", "0"):
        return False
    return None


def _extended_attachers(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(v) for v in val if v]
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    return []


def collect_disks(
    credential: DefaultAzureCredential,
    subscriptions: list[SubscriptionInfo],
    scope: ScopeFilter | None = None,
) -> list[DiskInventory]:
    """Return all managed disks matching the scope (tags excluded).

    Args:
        credential:    Azure credential.
        subscriptions: In-scope subscriptions.
        scope:         Active :class:`~cloudopt.scope.ScopeFilter`; used for
                       region and resource-group filtering.
    """
    if not subscriptions:
        return []

    sub_map: dict[str, str] = {s.subscription_id: s.subscription_name for s in subscriptions}
    sub_ids = list(sub_map.keys())
    query_text = _build_query(scope)

    client = ResourceGraphClient(credential)
    results: list[DiskInventory] = []

    batches = [sub_ids[i: i + _MAX_SUBS_PER_QUERY] for i in range(0, len(sub_ids), _MAX_SUBS_PER_QUERY)]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Collecting disk inventory…", total=len(batches))

        for batch in batches:
            skip_token: str | None = None
            while True:
                options = QueryRequestOptions(result_format="objectArray", skip_token=skip_token)
                request = QueryRequest(subscriptions=batch, query=query_text, options=options)
                try:
                    response = client.resources(request)
                except Exception as exc:
                    console.print(f"[yellow]Warning:[/yellow] ARG disk query failed: {exc}")
                    break

                rows: list[dict[str, Any]] = response.data or []
                for row in rows:
                    sub_id = str(row.get("subscriptionId", ""))
                    raw_props = row.get("properties")
                    disk = DiskInventory(
                        resource_id=str(row.get("id", "")),
                        disk_name=str(row.get("name", "")),
                        subscription_id=sub_id,
                        subscription_name=sub_map.get(sub_id, sub_id),
                        resource_group=str(row.get("resourceGroup", "")),
                        location=str(row.get("location", "")),
                        sku_name=_str_or_none(row.get("skuName")),
                        sku_tier=_str_or_none(row.get("skuTier")),
                        performance_tier=_str_or_none(row.get("perfTier")),
                        disk_size_gb=_int_or_none(row.get("diskSizeGb")),
                        disk_iops_read_write=_int_or_none(row.get("iopsRW")),
                        disk_mbps_read_write=_int_or_none(row.get("mbpsRW")),
                        disk_iops_read_only=_int_or_none(row.get("iopsRO")),
                        disk_mbps_read_only=_int_or_none(row.get("mbpsRO")),
                        bursting_enabled=_bool_or_none(row.get("burstingEnabled")),
                        disk_state=_str_or_none(row.get("diskState")),
                        os_type=_str_or_none(row.get("osType")),
                        managed_by=_str_or_none(row.get("managedBy")),
                        managed_by_extended=_extended_attachers(row.get("managedByExtended")),
                        zones=_str_or_none(row.get("zones")),
                        encryption_type=_str_or_none(row.get("encryptionType")),
                        network_access_policy=_str_or_none(row.get("networkAccessPolicy")),
                        public_network_access=_str_or_none(row.get("publicNetworkAccess")),
                        disk_controller_types=_str_or_none(row.get("diskControllerTypes")),
                        hyper_v_generation=_str_or_none(row.get("hyperVGeneration")),
                        time_created=_str_or_none(row.get("timeCreated")),
                        raw_properties=raw_props if isinstance(raw_props, dict) else {},
                    )
                    results.append(disk)

                if hasattr(response, "skip_token") and response.skip_token:
                    skip_token = response.skip_token
                else:
                    break

            progress.advance(task)

    return results
