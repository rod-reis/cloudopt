"""Async Azure Monitor metrics collection with throttling and checkpoint/resume.

Collects CPU, memory, disk, and network metrics for each VM (and VMSS aggregate).
Checkpoints every 500 VMs to allow resuming interrupted runs.
"""

from __future__ import annotations

import asyncio
import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from azure.identity import DefaultAzureCredential
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

from cloudopt.collector.throttle import ThrottleManager, with_retry
from cloudopt.models import DailyDataPoint, VmInventory, VmMetrics

console = Console()

# Checkpoint written every N VMs
_CHECKPOINT_INTERVAL = 500

# Azure Monitor metric names to collect.
# Each tuple: (azure_metric_name, internal_key, aggregation_csv)
# Aggregation MUST match what the metric supports — requesting an unsupported
# aggregation (e.g. Total on Percentage CPU) causes Azure Monitor to return
# 400 BadRequest, which silently drops the metric. See:
# https://learn.microsoft.com/azure/azure-monitor/essentials/metrics-supported#microsoftcomputevirtualmachines
_METRICS: list[tuple[str, str, str]] = [
    ("Percentage CPU",            "cpu_pct",             "Average,Minimum,Maximum"),
    ("Available Memory Bytes",    "available_memory_bytes", "Average,Minimum,Maximum"),
    ("Disk Read Bytes/sec",       "disk_read_bps",       "Average"),
    ("Disk Write Bytes/sec",      "disk_write_bps",      "Average"),
    ("Disk Read Operations/Sec",  "disk_read_iops",      "Average"),
    ("Disk Write Operations/Sec", "disk_write_iops",     "Average"),
    ("Network In Total",          "network_in_bytes",    "Total,Average,Minimum,Maximum"),
    ("Network Out Total",         "network_out_bytes",   "Total,Average,Minimum,Maximum"),
]


async def collect_metrics(
    credential: DefaultAzureCredential,
    vms: list[VmInventory],
    days: int,
    concurrency: int,
    checkpoint_path: Path,
    arm_rate: float = 20.0,
) -> list[VmMetrics]:
    """Collect Azure Monitor metrics for all VMs and return VmMetrics records.

    Already-collected VMs (from a previous checkpoint) are skipped.
    """
    from azure.mgmt.monitor.aio import MonitorManagementClient

    end_time = datetime.now(tz=timezone.utc)
    start_time = end_time - timedelta(days=days)
    timespan = f"{start_time.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}"

    # Load checkpoint
    completed_ids: set[str] = _load_checkpoint(checkpoint_path)
    results: list[VmMetrics] = []

    pending = [vm for vm in vms if vm.resource_id not in completed_ids]

    if completed_ids:
        console.print(
            f"[dim]Resuming: {len(completed_ids)} VM(s) already collected, "
            f"{len(pending)} remaining.[/dim]"
        )

    throttle = ThrottleManager(
        max_concurrency=concurrency,
        rate_per_second=arm_rate,
    )
    error_log: dict[str, int] = {}
    sample_errors: dict[str, str] = {}

    # Group VMs by subscription for sequential per-subscription processing
    by_sub: dict[str, list[VmInventory]] = {}
    for vm in pending:
        by_sub.setdefault(vm.subscription_id, []).append(vm)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task("Collecting VM metrics…", total=len(pending))

        processed = 0

        # Process subscriptions strictly one at a time to protect ARM rate limits.
        # Within each subscription, work is dispatched in batches of `concurrency`
        # so at most `concurrency` VM metric requests are in-flight simultaneously.
        for sub_id, sub_vms in by_sub.items():
            sub_name = sub_vms[0].subscription_name if sub_vms else sub_id[:8]
            progress.update(
                task_id,
                description=f"[{sub_name[:24]}] VM metrics…",
            )

            async with MonitorManagementClient(credential, sub_id) as client:

                async def collect_vm(vm: VmInventory, _client=client) -> list[VmMetrics]:
                    vm_metrics: list[VmMetrics] = []
                    for azure_name, _key, aggregation in _METRICS:
                        metric = await _fetch_metric(
                            client=_client,
                            resource_id=vm.resource_id,
                            metric_name=azure_name,
                            aggregation=aggregation,
                            timespan=timespan,
                            throttle=throttle,
                            subscription_id=vm.subscription_id,
                            error_log=error_log,
                            sample_errors=sample_errors,
                        )
                        if metric:
                            vm_metrics.append(metric)
                    return vm_metrics

                # Batch: process `concurrency` VMs at a time to cap task count
                for batch_start in range(0, len(sub_vms), concurrency):
                    batch = sub_vms[batch_start : batch_start + concurrency]
                    tasks = [asyncio.create_task(collect_vm(vm)) for vm in batch]
                    batch_metrics = await asyncio.gather(*tasks, return_exceptions=True)
                    for vm, vm_metrics in zip(batch, batch_metrics):
                        if isinstance(vm_metrics, list):
                            results.extend(vm_metrics)
                        completed_ids.add(vm.resource_id)
                        processed += 1
                        progress.advance(task_id)
                        if processed % _CHECKPOINT_INTERVAL == 0:
                            _save_checkpoint(checkpoint_path, completed_ids)

    _save_checkpoint(checkpoint_path, completed_ids)

    # Surface metric-fetch failures so empty Raw Metrics doesn't go unnoticed.
    if error_log:
        total_errors = sum(error_log.values())
        console.print(
            f"[yellow]Warning:[/yellow] {total_errors} metric fetch error(s) "
            f"across {len(error_log)} metric(s)."
        )
        for metric_name, count in sorted(error_log.items(), key=lambda x: -x[1]):
            sample = sample_errors.get(metric_name, "")
            console.print(f"  [dim]{metric_name}: {count} error(s) — {sample[:160]}[/dim]")

    return results


async def _fetch_metric(
    client: Any,
    resource_id: str,
    metric_name: str,
    aggregation: str,
    timespan: str,
    throttle: ThrottleManager,
    subscription_id: str,
    error_log: dict[str, int] | None = None,
    sample_errors: dict[str, str] | None = None,
) -> VmMetrics | None:
    """Fetch a single Azure Monitor metric for one resource."""
    try:
        async def call():
            return await client.metrics.list(
                resource_uri=resource_id,
                timespan=timespan,
                interval="P1D",
                metricnames=metric_name,
                aggregation=aggregation,
            )

        response = await with_retry(call, throttle, subscription_id)
    except Exception as exc:
        if error_log is not None:
            error_log[metric_name] = error_log.get(metric_name, 0) + 1
            if sample_errors is not None and metric_name not in sample_errors:
                sample_errors[metric_name] = f"{type(exc).__name__}: {exc}"
        return None

    if not response or not response.value:
        return None

    metric = response.value[0]
    if not metric.timeseries:
        return None

    time_series_data: list[DailyDataPoint] = []
    raw_values: list[float] = []
    max_vals: list[float] = []
    min_vals: list[float] = []

    for ts in metric.timeseries:
        for point in ts.data or []:
            val: float | None = point.average
            if val is None:
                val = point.total
            if val is not None:
                date_str = point.time_stamp.strftime("%Y-%m-%d") if point.time_stamp else ""
                time_series_data.append(DailyDataPoint(date=date_str, value=val))
                raw_values.append(val)
            # Capture actual peak values from the API response
            if point.maximum is not None:
                max_vals.append(point.maximum)
            if point.minimum is not None:
                min_vals.append(point.minimum)

    if not raw_values:
        return None

    raw_values_sorted = sorted(raw_values)
    n = len(raw_values_sorted)
    p50 = _percentile(raw_values_sorted, 50)
    p95 = _percentile(raw_values_sorted, 95)
    p99 = _percentile(raw_values_sorted, 99)

    return VmMetrics(
        resource_id=resource_id,
        metric_name=metric_name,
        avg=statistics.mean(raw_values),
        p50=p50,
        p95=p95,
        p99=p99,
        max=max(max_vals) if max_vals else max(raw_values),
        min=min(min_vals) if min_vals else min(raw_values),
        time_series=time_series_data,
    )


def _percentile(sorted_values: list[float], pct: int) -> float:
    """Return the p-th percentile of a sorted list."""
    if not sorted_values:
        return 0.0
    idx = (pct / 100) * (len(sorted_values) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def _load_checkpoint(path: Path) -> set[str]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(data.get("completed_ids", []))
        except Exception:
            return set()
    return set()


def _save_checkpoint(path: Path, completed_ids: set[str]) -> None:
    try:
        path.write_text(
            json.dumps({"completed_ids": sorted(completed_ids)}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass  # checkpoint failure is non-fatal
