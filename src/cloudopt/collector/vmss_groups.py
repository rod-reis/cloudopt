"""Collect VMSS Uniform resources + CPU/memory metrics and return ManagedComputeGroupRow list.

VMSS Uniform VMs appear as resources of type
``microsoft.compute/virtualmachinescalesets``.  Unlike Flex VMSS (whose member
VMs surface individually in ARG), Uniform VMs are aggregated at the VMSS level
by Azure Monitor and do not appear in the regular VM inventory query.

This module:
1. Queries ARG for all Uniform VMSS in scope.
2. Fetches ``Percentage CPU`` and ``Available Memory Bytes`` metrics per VMSS
   from Azure Monitor in a single call (sync).
3. Returns one ``ManagedComputeGroupRow`` per VMSS with CPU aggregates and
   memory utilisation percentage derived from available bytes + SKU total RAM.
"""
from __future__ import annotations

import logging
import statistics
from typing import Any

from azure.identity import DefaultAzureCredential
from azure.mgmt.monitor import MonitorManagementClient
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from cloudopt.collector.auth import SubscriptionInfo
from cloudopt.models import ManagedComputeGroupRow, ParentServiceType
from cloudopt.scope import ScopeFilter, kql_location_clause, kql_resource_group_clause

# SkuCatalog is imported lazily to avoid circular imports; typing-only here.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from cloudopt.analyzer.sku_catalog import SkuCatalog

console = Console()
_LOG = logging.getLogger(__name__)

_MAX_SUBS_PER_QUERY = 200

# Azure Monitor metrics to collect at the VMSS level
_CPU_METRIC = "Percentage CPU"
_MEM_METRIC = "Available Memory Bytes"
_AGGREGATION = "Average,Minimum,Maximum"

# ARG query for Uniform VMSS
_VMSS_QUERY_BASE = "Resources\n| where type =~ 'microsoft.compute/virtualmachinescalesets'\n| where properties.orchestrationMode =~ 'Uniform'"
_VMSS_QUERY_TAIL = """
| project
    id,
    name,
    subscriptionId,
    resourceGroup,
    location,
    skuName     = tostring(sku.name),
    capacity    = tolong(sku.capacity),
    osType      = tostring(properties.virtualMachineProfile.storageProfile.osDisk.osType),
    zones       = tostring(iif(array_length(zones) > 0, strcat_array(zones, ','), ''))
| order by name asc
"""


def _build_query(scope: ScopeFilter | None) -> str:
    clauses = ""
    if scope is not None:
        clauses = kql_location_clause(scope) + kql_resource_group_clause(scope)
    return _VMSS_QUERY_BASE + clauses + _VMSS_QUERY_TAIL


def collect_vmss_groups(
    credential: DefaultAzureCredential,
    subscriptions: list[SubscriptionInfo],
    scope: ScopeFilter | None,
    days: int = 30,
    sku_catalog: "SkuCatalog | None" = None,
) -> list[ManagedComputeGroupRow]:
    """Return one ``ManagedComputeGroupRow`` per Uniform VMSS in scope.

    CPU metrics and Available Memory Bytes are fetched from Azure Monitor for
    each VMSS resource in a single metrics API call.  Memory utilisation is
    derived as ``(1 - avg_available_bytes / total_bytes) * 100`` using the
    per-node total RAM from the SKU catalog; it is left ``None`` when the SKU
    catalog is absent or the metric is unavailable.

    Args:
        sku_catalog: Optional :class:`~cloudopt.analyzer.sku_catalog.SkuCatalog`
            used to populate *vcpus* and *memory_gb* from the live Azure SKU API.
            When *None*, vcpus and memory_gb default to 0 and avg_mem_pct is None.
    """
    if not subscriptions:
        return []

    sub_map: dict[str, str] = {s.subscription_id: s.subscription_name for s in subscriptions}
    sub_ids = list(sub_map.keys())
    query_text = _build_query(scope)

    arg_client = ResourceGraphClient(credential)
    batches = [
        sub_ids[i: i + _MAX_SUBS_PER_QUERY]
        for i in range(0, len(sub_ids), _MAX_SUBS_PER_QUERY)
    ]

    raw_vmss: list[dict[str, Any]] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Collecting VMSS Uniform inventory…", total=len(batches))
        for batch in batches:
            skip_token: str | None = None
            while True:
                options = QueryRequestOptions(
                    result_format="objectArray", skip_token=skip_token
                )
                request = QueryRequest(
                    subscriptions=batch, query=query_text, options=options
                )
                try:
                    response = arg_client.resources(request)
                except Exception as exc:
                    console.print(
                        f"[yellow]Warning:[/yellow] VMSS ARG query failed: {exc}"
                    )
                    break

                rows: list[dict[str, Any]] = response.data or []
                raw_vmss.extend(rows)

                skip_token = None
                if hasattr(response, "skip_token") and response.skip_token:
                    skip_token = response.skip_token
                if skip_token is None:
                    break

            progress.advance(task)

    if not raw_vmss:
        return []

    console.print(f"[dim]VMSS Uniform: {len(raw_vmss)} scale set(s) found.[/dim]")

    # Group VMSS by subscription for batched Monitor queries
    sub_to_vmss: dict[str, list[dict[str, Any]]] = {}
    for row in raw_vmss:
        sub_id = str(row.get("subscriptionId", ""))
        sub_to_vmss.setdefault(sub_id, []).append(row)

    results: list[ManagedComputeGroupRow] = []
    import datetime
    from datetime import timezone

    end_time = datetime.datetime.now(tz=timezone.utc)
    start_time = end_time - datetime.timedelta(days=days)
    timespan = (
        f"{start_time.strftime('%Y-%m-%dT%H:%M:%SZ')}/"
        f"{end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )

    for sub_id, vmss_list in sub_to_vmss.items():
        sub_name = sub_map.get(sub_id, sub_id)
        try:
            monitor = MonitorManagementClient(credential, sub_id)
        except Exception as exc:
            _LOG.warning("VMSS metrics: MonitorManagementClient failed for %s — %s", sub_id, exc)
            for row in vmss_list:
                results.append(_build_row(row, sub_name, None, sku_catalog))
            continue

        for row in vmss_list:
            vmss_id: str = str(row.get("id", ""))
            cpu_metrics = _fetch_platform_metrics(monitor, vmss_id, timespan)
            results.append(_build_row(row, sub_name, cpu_metrics, sku_catalog))

    return results


def _fetch_platform_metrics(
    monitor: MonitorManagementClient,
    vmss_id: str,
    timespan: str,
) -> dict[str, float] | None:
    """Fetch Percentage CPU and Available Memory Bytes for a VMSS.

    Both metrics are requested in a single Azure Monitor call.
    Returns a dict with keys: avg, p95, p99, max, min (CPU %) plus
    avg_available_mem_bytes — or None on failure.
    """
    try:
        response = monitor.metrics.list(
            resource_uri=vmss_id,
            timespan=timespan,
            interval="P1D",
            metricnames=f"{_CPU_METRIC},{_MEM_METRIC}",
            aggregation=_AGGREGATION,
        )
    except Exception as exc:
        _LOG.debug("VMSS platform metrics failed for %s: %s", vmss_id, exc)
        return None

    if not response or not response.value:
        return None

    cpu_metric = next(
        (m for m in response.value if m.name and m.name.value and
         m.name.value.lower() == _CPU_METRIC.lower()),
        None,
    )
    mem_metric = next(
        (m for m in response.value if m.name and m.name.value and
         m.name.value.lower() == _MEM_METRIC.lower()),
        None,
    )

    # ── CPU aggregates ──────────────────────────────────────────────────────
    avg_vals: list[float] = []
    max_vals: list[float] = []
    min_vals: list[float] = []

    if cpu_metric and cpu_metric.timeseries:
        for ts in cpu_metric.timeseries:
            for point in ts.data or []:
                if point.average is not None:
                    avg_vals.append(point.average)
                if point.maximum is not None:
                    max_vals.append(point.maximum)
                if point.minimum is not None:
                    min_vals.append(point.minimum)

    if not avg_vals:
        return None

    sorted_avgs = sorted(avg_vals)

    def _pct(data: list[float], p: float) -> float:
        if not data:
            return 0.0
        idx = (len(data) - 1) * p / 100.0
        lo, hi = int(idx), min(int(idx) + 1, len(data) - 1)
        return data[lo] + (data[hi] - data[lo]) * (idx - lo)

    result: dict[str, float] = {
        "avg": round(statistics.mean(avg_vals), 2),
        "p95": round(_pct(sorted_avgs, 95), 2),
        "p99": round(_pct(sorted_avgs, 99), 2),
        "max": round(max(max_vals) if max_vals else max(avg_vals), 2),
        "min": round(min(min_vals) if min_vals else min(avg_vals), 2),
    }

    # ── Memory: Available Memory Bytes (average across instances, per day) ──
    mem_avgs: list[float] = []
    if mem_metric and mem_metric.timeseries:
        for ts in mem_metric.timeseries:
            for point in ts.data or []:
                if point.average is not None:
                    mem_avgs.append(point.average)
    if mem_avgs:
        result["avg_available_mem_bytes"] = statistics.mean(mem_avgs)

    return result


def _compute_mem_pct(
    metrics: dict[str, float] | None,
    memory_gb: float,
) -> float | None:
    """Convert avg_available_mem_bytes → used memory %.

    Uses Available Memory Bytes (avg per instance per day) and the per-node
    total RAM from the SKU catalog.  Returns None when either value is absent.
    """
    if not metrics:
        return None
    avg_avail = metrics.get("avg_available_mem_bytes")
    if avg_avail is None or memory_gb <= 0:
        return None
    total_bytes = memory_gb * 1024.0 ** 3
    pct = (1.0 - avg_avail / total_bytes) * 100.0
    return round(max(0.0, min(100.0, pct)), 2)


def _build_row(
    row: dict[str, Any],
    sub_name: str,
    cpu: dict[str, float] | None,
    sku_catalog: "SkuCatalog | None" = None,
) -> ManagedComputeGroupRow:
    """Build a ManagedComputeGroupRow from an ARG VMSS row + optional CPU stats."""
    vmss_id: str = str(row.get("id", ""))
    capacity = int(row.get("capacity") or 0)
    sku_name = str(row.get("skuName", "") or "")
    sub_id = str(row.get("subscriptionId", ""))
    region = str(row.get("location", ""))

    vcpus: int = 0
    memory_gb: float = 0.0
    if sku_catalog is not None and sku_name:
        sku_spec = sku_catalog.get(sub_id, region, sku_name)
        if sku_spec is not None:
            vcpus = sku_spec.vcpus
            memory_gb = sku_spec.memory_gb

    return ManagedComputeGroupRow(
        parent_service_type=ParentServiceType.STANDALONE_VMSS,
        parent_service_id=vmss_id or None,
        parent_service_name=str(row.get("name", "")) or None,
        parent_pool_name=None,
        vmss_id=vmss_id or None,
        vmss_name=str(row.get("name", "")) or None,
        vm_sku=sku_name,
        instance_count=capacity,
        total_instance_count=capacity,
        subscription_id=sub_id,
        subscription_name=sub_name,
        resource_group=str(row.get("resourceGroup", "")),
        region=region,
        os_type=str(row.get("osType", "") or "") or None,
        zones=str(row.get("zones", "") or "") or None,
        vcpus=vcpus,
        memory_gb=memory_gb,
        avg_cpu_pct=cpu.get("avg") if cpu else None,
        p95_cpu_pct=cpu.get("p95") if cpu else None,
        p99_cpu_pct=cpu.get("p99") if cpu else None,
        max_cpu_pct=cpu.get("max") if cpu else None,
        min_cpu_pct=cpu.get("min") if cpu else None,
        avg_mem_pct=_compute_mem_pct(cpu, memory_gb),
    )
