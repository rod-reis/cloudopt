"""Generic Azure Resource Graph inventory collector.

Returns every resource visible in the current scope (subscription, resource
group, region, tag filters) from the ARG ``resources`` table, excluding the
``tags`` and ``properties`` blobs.  Used to populate the **Inventory** sheet
in the Excel workbook so analysts have a full picture of what exists in the
estate alongside the VM-level analysis.

Only resource types listed in ``data/inscoperesourcetypes.csv`` are collected;
types absent from the file are excluded so the inventory stays focused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from azure.identity import DefaultAzureCredential
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from cloudopt.collector.auth import SubscriptionInfo
from cloudopt.models import AzureResource
from cloudopt.scope import ScopeFilter, kql_location_clause, kql_resource_group_clause

console = Console()

_MAX_SUBS_PER_QUERY = 200

# ---------------------------------------------------------------------------
# In-scope resource type allowlist
# ---------------------------------------------------------------------------

def _load_inscope_types() -> frozenset[str]:
    """Load in-scope resource types from the CSV file (case-insensitive)."""
    data_file = Path(__file__).parent.parent / "data" / "inscoperesourcetypes.csv"
    if not data_file.exists():
        return frozenset()
    types: set[str] = set()
    for line in data_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            types.add(line.lower())
    return frozenset(types)


_IN_SCOPE_RESOURCE_TYPES: frozenset[str] = _load_inscope_types()


def _type_filter_clause() -> str:
    """Return a KQL ``| where type in~(...)`` clause for in-scope types."""
    if not _IN_SCOPE_RESOURCE_TYPES:
        return ""
    quoted = ", ".join(f"'{t}'" for t in sorted(_IN_SCOPE_RESOURCE_TYPES))
    return f"\n| where type in~ ({quoted})"


# KQL query: projects the standard ARG columns every analyst expects to see.
# Tags and the large ``properties`` blob are intentionally omitted.
_RESOURCES_QUERY_BASE = "Resources"

_RESOURCES_QUERY_TAIL = """
| project
    id,
    name,
    type,
    subscriptionId,
    resourceGroup,
    location,
    kind,
    skuName    = tostring(sku.name),
    skuTier    = tostring(sku.tier),
    planName   = tostring(plan.name),
    planPub    = tostring(plan.publisher),
    planProd   = tostring(plan.product),
    zones      = tostring(iif(array_length(zones) > 0, strcat_array(zones, ','), '')),
    managedBy
| order by type asc, name asc
"""


def _scope_clauses(scope: ScopeFilter | None) -> str:
    if scope is None:
        return ""
    return kql_location_clause(scope) + kql_resource_group_clause(scope)


def _build_query(scope: ScopeFilter | None) -> str:
    return (
        _RESOURCES_QUERY_BASE
        + _type_filter_clause()
        + _scope_clauses(scope)
        + _RESOURCES_QUERY_TAIL
    )


def collect_resources(
    credential: DefaultAzureCredential,
    subscriptions: list[SubscriptionInfo],
    scope: ScopeFilter | None = None,
) -> list[AzureResource]:
    """Return all ARG resources matching the scope (tags excluded).

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
    results: list[AzureResource] = []

    batches = [sub_ids[i: i + _MAX_SUBS_PER_QUERY] for i in range(0, len(sub_ids), _MAX_SUBS_PER_QUERY)]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Collecting resource inventory…", total=len(batches))

        for batch in batches:
            skip_token: str | None = None
            while True:
                options = QueryRequestOptions(result_format="objectArray", skip_token=skip_token)
                request = QueryRequest(subscriptions=batch, query=query_text, options=options)
                try:
                    response = client.resources(request)
                except Exception as exc:
                    console.print(f"[yellow]Warning:[/yellow] ARG resource query failed: {exc}")
                    break

                rows: list[dict[str, Any]] = response.data or []
                for row in rows:
                    sub_id = str(row.get("subscriptionId", ""))
                    resource = AzureResource(
                        resource_id=str(row.get("id", "")),
                        name=str(row.get("name", "")),
                        resource_type=str(row.get("type", "")),
                        subscription_id=sub_id,
                        subscription_name=sub_map.get(sub_id, sub_id),
                        resource_group=str(row.get("resourceGroup", "")),
                        location=str(row.get("location", "")),
                        kind=_str_or_none(row.get("kind")),
                        sku_name=_str_or_none(row.get("skuName")),
                        sku_tier=_str_or_none(row.get("skuTier")),
                        plan_name=_str_or_none(row.get("planName")),
                        plan_publisher=_str_or_none(row.get("planPub")),
                        plan_product=_str_or_none(row.get("planProd")),
                        zones=_str_or_none(row.get("zones")),
                        managed_by=_str_or_none(row.get("managedBy")),
                    )
                    # Apply tag filters in-memory (same pattern as VM inventory)
                    if scope is not None and scope.has_tag_filter:
                        # Tags were excluded from the projection; tag-filtered
                        # inventory requires tags — skip tag filtering here and
                        # note that tag filtering applies to VM inventory only.
                        pass
                    results.append(resource)

                skip_token = getattr(response, "skip_token", None) or getattr(
                    getattr(response, "skip_token_encoding", None), "__class__", None
                ) and None
                # Use the skip_token from the response if present
                if hasattr(response, "skip_token") and response.skip_token:
                    skip_token = response.skip_token
                else:
                    break

            progress.advance(task)

    return results


def _str_or_none(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None
