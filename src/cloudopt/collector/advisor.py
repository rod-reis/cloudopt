"""Azure Advisor — SKU-change recommendations collector.

Uses Azure Resource Graph (``advisorresources`` table) so the whole estate
is queried with a single API call regardless of subscription count.

We only keep recommendations whose intent is to change a resource SKU —
typically right-sizing, shutdown, or upgrade-of-version recommendations
under the Cost or Performance category.  The filter is applied both
server-side (KQL) and client-side (model construction) for safety.
"""

from __future__ import annotations

from typing import Any, cast

from azure.identity import DefaultAzureCredential
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from cloudopt.collector.auth import SubscriptionInfo
from cloudopt.models import AdvisorRecommendation
from cloudopt.scope import ScopeFilter

console = Console()

_MAX_SUBS_PER_QUERY = 200

# KQL — pulls Advisor recommendations from the cross-subscription graph.
# Filters:
#   * type == microsoft.advisor/recommendations
#   * category in ('Cost', 'Performance')
#   * shortDescription mentions resize / SKU / right-size / upgrade, OR
#     extendedProperties contains a target/recommended SKU field.
#
# Note: scope filters (location, resourceGroup) are applied CLIENT-SIDE
# because Advisor recommendation rows in the ``advisorresources`` table do
# not preserve those columns through the projection (they live inside
# ``properties.resourceMetadata`` and are extracted from the impacted
# resource ID).  Pushing ``| where location ...`` after the projection
# fails with ``Operator_FailedToResolveEntity``.
_QUERY = """
advisorresources
| where type =~ 'microsoft.advisor/recommendations'
| extend props        = properties
| extend category     = tostring(props.category)
| extend impact       = tostring(props.impact)
| extend shortP       = tostring(props.shortDescription.problem)
| extend shortS       = tostring(props.shortDescription.solution)
| extend impField     = tostring(props.impactedField)
| extend impValue     = tostring(props.impactedValue)
| extend ext          = props.extendedProperties
| extend currentSku   = tostring(coalesce(ext.currentSku, ext.CurrentSku, ext.fromSku, ext.FromSku))
| extend targetSku    = tostring(coalesce(ext.targetSku, ext.TargetSku, ext.toSku, ext.ToSku, ext.recommendedSku, ext.RecommendedSku))
| extend annualSavings = todouble(coalesce(ext.annualSavingsAmount, ext.AnnualSavingsAmount, ext.savingsAmount))
| extend lastUpdated  = tostring(props.lastUpdated)
| where category in~ ('Cost', 'Performance')
| where (
        shortP matches regex @'(?i)resize|right.?size|under.?utiliz|over.?provision|upgrade|sku'
     or shortS matches regex @'(?i)resize|right.?size|under.?utiliz|sku|upgrade'
     or isnotempty(targetSku)
  )
| project
    recommendationId = id,
    subscriptionId,
    resourceGroup,
    impactedResourceId   = impValue,
    impactedResourceType = impField,
    category,
    impact,
    shortDescription     = shortP,
    currentSku,
    targetSku,
    annualSavings,
    lastUpdated
"""


def _location_from_resource_id(resource_id: str) -> str:
    """Best-effort extraction of region from an ARM resource ID.

    Advisor recommendation rows don't expose ``location`` as a top-level
    column, but resource IDs often contain ``/locations/<region>/``.
    Returns lowercase region or empty string.
    """
    if not resource_id:
        return ""
    parts = resource_id.lower().split("/")
    for i, segment in enumerate(parts):
        if segment == "locations" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _build_query(scope: ScopeFilter | None) -> str:
    """Return the KQL for the cross-subscription Advisor pull.

    Scope filters (location, resource group) are intentionally applied
    client-side: pushing them after the projection fails because the
    columns aren't carried through.
    """
    return _QUERY


def collect_advisor_sku_recommendations(
    credential: DefaultAzureCredential,
    subscriptions: list[SubscriptionInfo],
    scope: ScopeFilter | None = None,
) -> list[AdvisorRecommendation]:
    """Return all SKU-change Advisor recommendations across the given subs."""
    if not subscriptions:
        return []

    client = ResourceGraphClient(credential)
    sub_map = {s.subscription_id: s.subscription_name for s in subscriptions}
    sub_ids = list(sub_map.keys())

    rows: list[dict[str, Any]] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Querying Advisor recommendations…", total=None)

        for i in range(0, len(sub_ids), _MAX_SUBS_PER_QUERY):
            batch = sub_ids[i : i + _MAX_SUBS_PER_QUERY]
            skip_token: str | None = None
            while True:
                opts = QueryRequestOptions(result_format="objectArray")
                if skip_token:
                    opts.skip_token = skip_token
                req = QueryRequest(
                    subscriptions=batch,
                    query=_build_query(scope),
                    options=opts,
                )
                try:
                    resp = client.resources(req)
                except Exception as exc:
                    console.print(
                        f"[yellow]Warning:[/yellow] Advisor query failed: {exc}"
                    )
                    return []
                rows.extend(cast(list[dict[str, Any]], resp.data or []))
                skip_token = getattr(resp, "skip_token", None)
                if not skip_token:
                    break

    recs: list[AdvisorRecommendation] = []
    locations_filter = set(scope.locations) if (scope and scope.locations) else None
    rg_filter: set[tuple[str, str]] | None = None
    if scope and scope.resource_groups:
        rg_filter = {(rg.subscription_id, rg.name) for rg in scope.resource_groups}

    for row in rows:
        sub_id = row.get("subscriptionId", "") or ""
        impacted = row.get("impactedResourceId", "") or ""
        impacted_name = impacted.rsplit("/", 1)[-1] if impacted else ""
        rg_name = (row.get("resourceGroup", "") or "").lower()

        # Client-side scope filters — see _build_query for rationale.
        if locations_filter is not None:
            region = _location_from_resource_id(impacted)
            if region and region not in locations_filter:
                continue
            # If we couldn't parse a region, keep the row (fail-open) so we
            # don't drop legitimately scoped recommendations.
        if rg_filter is not None:
            if (sub_id.lower(), rg_name) not in rg_filter:
                continue

        try:
            recs.append(
                AdvisorRecommendation(
                    recommendation_id=row.get("recommendationId", ""),
                    subscription_id=sub_id,
                    subscription_name=sub_map.get(sub_id, sub_id),
                    resource_group=row.get("resourceGroup", "") or "",
                    impacted_resource_id=impacted,
                    impacted_resource_name=impacted_name,
                    impacted_resource_type=row.get("impactedResourceType", "") or "",
                    category=row.get("category", "") or "",
                    impact=row.get("impact", "") or "",
                    short_description=row.get("shortDescription", "") or "",
                    current_sku=row.get("currentSku", "") or "",
                    recommended_sku=row.get("targetSku", "") or "",
                    annual_savings_usd=(
                        float(row["annualSavings"])
                        if row.get("annualSavings") not in (None, "")
                        else None
                    ),
                    last_updated=row.get("lastUpdated", "") or "",
                )
            )
        except Exception:
            continue

    return recs
