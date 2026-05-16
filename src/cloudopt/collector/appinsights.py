"""Application Insights inventory discovery and metrics collection.

Metrics collected
-----------------
Standard (via Azure Monitor Metrics API):
  Availability  : availabilityResults/availabilityPercentage
  Requests      : requests/count, requests/duration, requests/failed
  Exceptions    : exceptions/count, exceptions/server
  Performance   : performanceCounters/processCpuPercentage,
                  performanceCounters/processPrivateBytes,
                  performanceCounters/memoryAvailableBytes,
                  performanceCounters/processorCpuPercentage,
                  performanceCounters/processIOBytesPerSecond

JVM (via Log Analytics customMetrics table, workspace-based components only):
  Memory        : jvm/memory/heap/used, jvm/memory/heap/committed,
                  jvm/memory/heap/max, jvm/memory/nonheap/used
  GC            : jvm/gc/pause, jvm/gc/count
  Threads       : jvm/threads/count

Processing is sequential per subscription, with a bounded batch window of
``concurrency`` components at a time to protect ARM API rate limits.
"""

from __future__ import annotations

import asyncio
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from azure.identity import DefaultAzureCredential
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

from cloudopt.collector.auth import SubscriptionInfo
from cloudopt.collector.throttle import ThrottleManager, with_retry
from cloudopt.models import AppInsightsInventory, AppInsightsMetrics, DailyDataPoint
from cloudopt.scope import (
    ScopeFilter,
    kql_location_clause,
    kql_resource_group_clause,
)

console = Console()

_MAX_SUBS_PER_QUERY = 200

# ---------------------------------------------------------------------------
# Metric catalogue
# ---------------------------------------------------------------------------

# (azure_metric_name, display_name, category, unit, primary_aggregation)
_AI_METRICS: list[tuple[str, str, str, str, str]] = [
    (
        "availabilityResults/availabilityPercentage",
        "Availability %",
        "availability",
        "Percent",
        "Average",
    ),
    ("requests/count",    "Request Count",          "requests",    "Count",        "Total"),
    ("requests/duration", "Request Duration (ms)",  "requests",    "Milliseconds", "Average"),
    ("requests/failed",   "Failed Requests",        "requests",    "Count",        "Total"),
    ("exceptions/count",  "Exception Count",        "exceptions",  "Count",        "Total"),
    ("exceptions/server", "Server Exceptions",      "exceptions",  "Count",        "Total"),
    (
        "performanceCounters/processCpuPercentage",
        "Process CPU %",
        "performance",
        "Percent",
        "Average",
    ),
    (
        "performanceCounters/processPrivateBytes",
        "Process Private Bytes",
        "performance",
        "Bytes",
        "Average",
    ),
    (
        "performanceCounters/memoryAvailableBytes",
        "Available Memory Bytes",
        "performance",
        "Bytes",
        "Average",
    ),
    (
        "performanceCounters/processorCpuPercentage",
        "Processor CPU %",
        "performance",
        "Percent",
        "Average",
    ),
    (
        "performanceCounters/processIOBytesPerSecond",
        "Process IO Bytes/sec",
        "performance",
        "Bytes",
        "Average",
    ),
]

# JVM custom metrics queried from Log Analytics customMetrics table.
# (la_metric_name, display_name, category, unit)
_JVM_LA_METRICS: list[tuple[str, str, str, str]] = [
    ("jvm/memory/heap/used",      "JVM Heap Used (bytes)",       "jvm_memory",  "Bytes"),
    ("jvm/memory/heap/committed", "JVM Heap Committed (bytes)",  "jvm_memory",  "Bytes"),
    ("jvm/memory/heap/max",       "JVM Heap Max (bytes)",        "jvm_memory",  "Bytes"),
    ("jvm/memory/nonheap/used",   "JVM Non-Heap Used (bytes)",   "jvm_memory",  "Bytes"),
    ("jvm/gc/pause",              "JVM GC Pause (ms)",           "jvm_gc",      "Milliseconds"),
    ("jvm/gc/count",              "JVM GC Count",                "jvm_gc",      "Count"),
    ("jvm/threads/count",         "JVM Thread Count",            "jvm_threads", "Count"),
]

# Resource Graph query to discover all App Insights components
_AI_QUERY_BASE = """
Resources
| where type =~ 'microsoft.insights/components'"""

_AI_QUERY_TAIL = """
| project
    id,
    name,
    subscriptionId,
    resourceGroup,
    location,
    kind,
    tags,
    appType       = tostring(properties.Application_Type),
    workspaceId   = tostring(properties.WorkspaceResourceId),
    ingestionMode = tostring(properties.IngestionMode)
"""


def _region_clause(regions: list[str] | None) -> str:
    """Return a KQL pipe fragment filtering by location, or empty string."""
    if not regions:
        return ""
    safe = [r.strip().lower() for r in regions if r.strip()]
    if not safe:
        return ""
    quoted = ", ".join(f"'{r}'" for r in safe)
    return f"\n| where location in~ ({quoted})"


def _build_ai_query(scope: ScopeFilter | None) -> str:
    if scope is None:
        return _AI_QUERY_BASE + _AI_QUERY_TAIL
    return (
        _AI_QUERY_BASE
        + kql_location_clause(scope)
        + kql_resource_group_clause(scope)
        + _AI_QUERY_TAIL
    )

# Resource Graph query to resolve workspace customer IDs (used for LA queries)
_WS_CUSTOMER_ID_QUERY = """
Resources
| where type =~ 'microsoft.operationalinsights/workspaces'
| where tolower(id) in ({ids})
| project id = tolower(id), customerId = tostring(properties.customerId)
"""


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def collect_appinsights_inventory(
    credential: DefaultAzureCredential,
    subscriptions: list[SubscriptionInfo],
    scope: ScopeFilter | None = None,
) -> list[AppInsightsInventory]:
    """Discover all Application Insights components across subscriptions."""
    rg_client = ResourceGraphClient(credential)
    sub_ids = [s.subscription_id for s in subscriptions]
    sub_name_map = {s.subscription_id: s.subscription_name for s in subscriptions}

    components: list[AppInsightsInventory] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task("Discovering Application Insights components…", total=None)

        for i in range(0, len(sub_ids), _MAX_SUBS_PER_QUERY):
            batch = sub_ids[i : i + _MAX_SUBS_PER_QUERY]
            skip_token: str | None = None

            while True:
                opts = QueryRequestOptions(result_format="objectArray", top=1000)
                if skip_token:
                    opts.skip_token = skip_token

                req = QueryRequest(subscriptions=batch, query=_build_ai_query(scope), options=opts)
                resp = rg_client.resources(req)

                for row in cast(list[dict[str, Any]], resp.data or []):
                    sub_id = row.get("subscriptionId", "")
                    workspace_id: str | None = row.get("workspaceId") or None
                    if not workspace_id:
                        workspace_id = None

                    tags = row.get("tags") or {}
                    # In-memory tag filter (tags themselves are NOT persisted)
                    if scope is not None and scope.has_tag_filter:
                        if not scope.in_scope_tags(tags):
                            continue

                    components.append(
                        AppInsightsInventory(
                            resource_id=row.get("id", ""),
                            subscription_id=sub_id,
                            subscription_name=sub_name_map.get(sub_id, sub_id),
                            resource_group=row.get("resourceGroup", ""),
                            component_name=row.get("name", ""),
                            kind=row.get("kind", ""),
                            application_type=row.get("appType", ""),
                            workspace_resource_id=workspace_id,
                            region=row.get("location", ""),
                            tags={},  # never persist customer tag values
                        )
                    )

                skip_token = getattr(resp, "skip_token", None)
                if not skip_token:
                    break

    return components


# ---------------------------------------------------------------------------
# Metrics collection
# ---------------------------------------------------------------------------

async def collect_appinsights_metrics(
    credential: DefaultAzureCredential,
    components: list[AppInsightsInventory],
    days: int,
    concurrency: int,
    arm_rate: float = 20.0,
) -> list[AppInsightsMetrics]:
    """Collect App Insights metrics for all components, sequential per subscription.

    Standard metrics are fetched via Azure Monitor Metrics API.
    JVM metrics are fetched from Log Analytics (workspace-based components only).
    """
    from azure.mgmt.monitor.aio import MonitorManagementClient

    if not components:
        return []

    end_time = datetime.now(tz=timezone.utc)
    start_time = end_time - timedelta(days=days)
    timespan = (
        f"{start_time.strftime('%Y-%m-%dT%H:%M:%SZ')}/"
        f"{end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )

    results: list[AppInsightsMetrics] = []

    # Group by subscription for sequential processing
    by_sub: dict[str, list[AppInsightsInventory]] = {}
    for comp in components:
        by_sub.setdefault(comp.subscription_id, []).append(comp)

    throttle = ThrottleManager(
        max_concurrency=concurrency,
        rate_per_second=arm_rate,
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task(
            "Collecting App Insights metrics…", total=len(components)
        )

        # Process subscriptions one at a time
        for sub_id, sub_components in by_sub.items():
            sub_name = sub_components[0].subscription_name if sub_components else sub_id[:8]
            progress.update(
                task_id,
                description=f"[{sub_name[:24]}] App Insights…",
            )

            async with MonitorManagementClient(credential, sub_id) as monitor_client:  # type: ignore[arg-type]
                # Process in batches of `concurrency` to cap in-flight tasks
                for batch_start in range(0, len(sub_components), concurrency):
                    batch = sub_components[batch_start : batch_start + concurrency]
                    tasks = [
                        asyncio.create_task(
                            _collect_component_metrics(
                                client=monitor_client,
                                component=comp,
                                timespan=timespan,
                                throttle=throttle,
                            )
                        )
                        for comp in batch
                    ]
                    batch_results = await asyncio.gather(*tasks, return_exceptions=True)
                    for item in batch_results:
                        if isinstance(item, list):
                            results.extend(item)
                        progress.advance(task_id)

    # Collect JVM metrics from Log Analytics for workspace-based components
    workspace_components = [c for c in components if c.workspace_resource_id]
    if workspace_components:
        console.print(
            f"[dim]  Querying JVM metrics from {len(workspace_components)} "
            f"workspace-linked component(s)…[/dim]"
        )
        jvm_metrics = await _collect_jvm_metrics(
            credential=credential,
            components=workspace_components,
            days=days,
            concurrency=concurrency,
        )
        results.extend(jvm_metrics)

    return results


async def _collect_component_metrics(
    client: Any,
    component: AppInsightsInventory,
    timespan: str,
    throttle: ThrottleManager,
) -> list[AppInsightsMetrics]:
    """Fetch all standard metrics for one App Insights component."""
    component_metrics: list[AppInsightsMetrics] = []

    for azure_name, display_name, category, unit, aggregation in _AI_METRICS:
        metric = await _fetch_ai_metric(
            client=client,
            resource_id=component.resource_id,
            metric_name=azure_name,
            display_name=display_name,
            category=category,
            unit=unit,
            aggregation=aggregation,
            timespan=timespan,
            throttle=throttle,
            subscription_id=component.subscription_id,
        )
        if metric is not None:
            component_metrics.append(metric)

    return component_metrics


async def _fetch_ai_metric(
    client: Any,
    resource_id: str,
    metric_name: str,
    display_name: str,
    category: str,
    unit: str,
    aggregation: str,
    timespan: str,
    throttle: ThrottleManager,
    subscription_id: str,
) -> AppInsightsMetrics | None:
    """Fetch a single Azure Monitor metric for one App Insights component."""
    try:
        async def call():
            return await client.metrics.list(
                resource_uri=resource_id,
                timespan=timespan,
                interval="P1D",
                metricnames=metric_name,
                aggregation=f"{aggregation},Minimum,Maximum",
            )

        response = await with_retry(call, throttle, subscription_id)
    except Exception:
        return None

    if not response or not response.value:
        return None

    metric_obj = response.value[0]
    if not metric_obj.timeseries:
        return None

    time_series_data: list[DailyDataPoint] = []
    raw_values: list[float] = []

    for ts in metric_obj.timeseries:
        for point in ts.data or []:
            val: float | None = (
                point.total if aggregation.lower() == "total" else point.average
            )
            if val is None:
                val = point.total if point.total is not None else point.average
            if val is not None:
                date_str = (
                    point.time_stamp.strftime("%Y-%m-%d") if point.time_stamp else ""
                )
                time_series_data.append(DailyDataPoint(date=date_str, value=val))
                raw_values.append(val)

    if not raw_values:
        return None

    raw_sorted = sorted(raw_values)
    return AppInsightsMetrics(
        resource_id=resource_id,
        metric_name=metric_name,
        display_name=display_name,
        category=category,
        unit=unit,
        avg=statistics.mean(raw_values),
        p50=_percentile(raw_sorted, 50),
        p95=_percentile(raw_sorted, 95),
        max=max(raw_values),
        min=min(raw_values),
        time_series=time_series_data,
    )


# ---------------------------------------------------------------------------
# JVM metrics via Log Analytics
# ---------------------------------------------------------------------------

async def _collect_jvm_metrics(
    credential: DefaultAzureCredential,
    components: list[AppInsightsInventory],
    days: int,
    concurrency: int,
) -> list[AppInsightsMetrics]:
    """Query JVM custom metrics from linked Log Analytics workspaces.

    Requires ``azure-monitor-query`` package.  Gracefully skips if unavailable.
    """
    try:
        from azure.monitor.query.aio import LogsQueryClient
        from azure.monitor.query import LogsQueryStatus
    except ImportError:
        console.print(
            "[dim]  azure-monitor-query not installed — skipping JVM Log Analytics metrics.[/dim]"
        )
        return []

    results: list[AppInsightsMetrics] = []

    # Resolve workspace customer IDs (GUIDs) via Resource Graph
    # The workspace_resource_id is a full ARM resource ID; LA query API needs the GUID.
    ws_resource_ids = list({
        (c.workspace_resource_id or "").lower()
        for c in components
        if c.workspace_resource_id
    })
    if not ws_resource_ids:
        return []

    # Look up customerId for each workspace via Resource Graph
    rg_client = ResourceGraphClient(credential)
    sub_ids = list({c.subscription_id for c in components})
    ws_customer_id_map: dict[str, str] = {}  # lower(resource_id) → customer_id GUID

    id_literals = ", ".join(f'"{rid}"' for rid in ws_resource_ids)
    kql = _WS_CUSTOMER_ID_QUERY.format(ids=id_literals)

    try:
        for i in range(0, len(sub_ids), _MAX_SUBS_PER_QUERY):
            batch = sub_ids[i : i + _MAX_SUBS_PER_QUERY]
            req = QueryRequest(
                subscriptions=batch,
                query=kql,
                options=QueryRequestOptions(result_format="objectArray"),
            )
            resp = rg_client.resources(req)
            for row in cast(list[dict[str, Any]], resp.data or []):
                rid = (row.get("id") or "").lower()
                cid = row.get("customerId", "")
                if rid and cid:
                    ws_customer_id_map[rid] = cid
    except Exception as exc:
        console.print(f"[dim]  Failed to resolve workspace IDs: {exc}[/dim]")
        return []

    # Map components to their workspace customer ID
    ws_to_components: dict[str, list[AppInsightsInventory]] = {}
    for comp in components:
        rid = (comp.workspace_resource_id or "").lower()
        cid = ws_customer_id_map.get(rid)
        if cid:
            ws_to_components.setdefault(cid, []).append(comp)

    if not ws_to_components:
        console.print("[dim]  No workspace customer IDs resolved — skipping JVM metrics.[/dim]")
        return []

    semaphore = asyncio.Semaphore(concurrency)
    metric_names_kql = ", ".join(f'"{m[0]}"' for m in _JVM_LA_METRICS)
    # Workspace-based App Insights stores custom metrics in the `AppMetrics`
    # table of the linked Log Analytics workspace, NOT in `customMetrics`
    # (which only exists for legacy classic AI components).
    # Schema: Name, Sum, ItemCount, Min, Max, TimeGenerated.
    # See https://learn.microsoft.com/azure/azure-monitor/app/convert-classic-resource
    la_query = (
        f"AppMetrics\n"
        f"| where Name in ({metric_names_kql})\n"
        f"| where TimeGenerated >= ago({days}d)\n"
        f"| extend avg_point = iff(ItemCount > 0, Sum / ItemCount, todouble(Sum))\n"
        f"| summarize\n"
        f"    avg_val = avg(avg_point),\n"
        f"    max_val = max(Max),\n"
        f"    min_val = min(Min)\n"
        f"    by Name, bin(TimeGenerated, 1d)\n"
        f"| order by Name asc, TimeGenerated asc"
    )

    async def query_workspace(
        customer_id: str, comps: list[AppInsightsInventory]
    ) -> list[AppInsightsMetrics]:
        async with semaphore:
            try:
                async with LogsQueryClient(credential) as la_client:  # type: ignore[arg-type]
                    response = await la_client.query_workspace(
                        workspace_id=customer_id,
                        query=la_query,
                        timespan=timedelta(days=days),
                    )
                if response.status != LogsQueryStatus.SUCCESS:
                    return []

                # Parse rows: [name, timestamp, avg_val, max_val, min_val]
                metric_data: dict[str, list[tuple[Any, float, float, float]]] = {}
                for table in response.tables:
                    for row in table.rows:
                        name = row[0]
                        ts = row[1]
                        avg_v = row[2]
                        max_v = row[3]
                        min_v = row[4]
                        if avg_v is not None:
                            metric_data.setdefault(name, []).append(
                                (ts, float(avg_v), float(max_v or avg_v), float(min_v or avg_v))
                            )

                ws_results: list[AppInsightsMetrics] = []
                for comp in comps:
                    for la_name, disp_name, category, unit in _JVM_LA_METRICS:
                        data_points = metric_data.get(la_name, [])
                        if not data_points:
                            continue
                        raw_avg = [v[1] for v in data_points]
                        ts_data = [
                            DailyDataPoint(
                                date=(
                                    t.strftime("%Y-%m-%d")
                                    if hasattr(t, "strftime")
                                    else str(t)[:10]
                                ),
                                value=v,
                            )
                            for t, v, _, _ in data_points
                        ]
                        raw_sorted = sorted(raw_avg)
                        ws_results.append(
                            AppInsightsMetrics(
                                resource_id=comp.resource_id,
                                metric_name=la_name,
                                display_name=disp_name,
                                category=category,
                                unit=unit,
                                avg=statistics.mean(raw_avg),
                                p50=_percentile(raw_sorted, 50),
                                p95=_percentile(raw_sorted, 95),
                                max=max(v[2] for v in data_points),
                                min=min(v[3] for v in data_points),
                                time_series=ts_data,
                            )
                        )
                return ws_results
            except Exception as exc:
                console.print(
                    f"[yellow]  ⚠ JVM metrics skipped[/yellow] for workspace "
                    f"{customer_id[:8]}… "
                    f"The Log Analytics API uses audience "
                    f"'https://api.loganalytics.io' (distinct from ARM), so "
                    f"DefaultAzureCredential must hold a token for that audience. "
                    f"Assign the 'Log Analytics Reader' role on the workspace to "
                    f"the identity running this tool, then re-run. "
                    f"({type(exc).__name__}: {exc})"
                )
                return []

    tasks = [
        asyncio.create_task(query_workspace(cid, comps))
        for cid, comps in ws_to_components.items()
    ]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)
    for item in all_results:
        if isinstance(item, list):
            results.extend(item)

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _percentile(sorted_values: list[float], pct: int) -> float:
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    k = (n - 1) * pct / 100
    f = int(k)
    c = min(f + 1, n - 1)
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


# ---------------------------------------------------------------------------
# Metric catalogue accessors (used by CLI summary)
# ---------------------------------------------------------------------------

def standard_metric_display_names() -> list[str]:
    return [display for _, display, _, _, _ in _AI_METRICS]


def jvm_metric_display_names() -> list[str]:
    return [display for _, display, _, _ in _JVM_LA_METRICS]
