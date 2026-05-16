"""Collect last successful VM deallocate/stop timestamp from Azure Activity Log.

Each stopped/deallocated VM is queried **individually** using a
``resourceId eq '<arm-resource-id>'`` filter so the API returns only the handful
of events for that specific resource — instead of scanning the entire
subscription's Administrative event history (which can be millions of records
and take hours to paginate).

Queries run in parallel via a ``ThreadPoolExecutor`` (one
``MonitorManagementClient`` is created per subscription and shared across
worker threads for that subscription).

VMs with no qualifying event found within the 90-day lookback are NOT included
in the result (``days_stopped`` remains ``None`` in ``VmInventory``).
"""
from __future__ import annotations

import datetime
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timezone

from azure.identity import DefaultAzureCredential
from azure.mgmt.monitor import MonitorManagementClient
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

from cloudopt.collector.auth import SubscriptionInfo
from cloudopt.models import VmInventory

_LOG = logging.getLogger(__name__)
_console = Console()

_LOOKBACK_DAYS = 90
_MAX_WORKERS = 10  # parallel Activity Log queries per subscription

_DEALLOCATE_OPS: frozenset[str] = frozenset(
    {
        "microsoft.compute/virtualmachines/deallocate/action",
        "microsoft.compute/virtualmachines/poweroff/action",
    }
)


def _query_single_vm(
    client: MonitorManagementClient,
    resource_id: str,
    filter_expr: str,
) -> tuple[str, datetime.datetime | None]:
    """Query Activity Log for one VM. Returns (resource_id_lower, last_op_ts | None)."""
    rid_lower = resource_id.lower()
    latest: datetime.datetime | None = None
    try:
        for event in client.activity_logs.list(filter=filter_expr):
            op = (
                getattr(getattr(event, "operation_name", None), "value", "") or ""
            ).lower()
            if op not in _DEALLOCATE_OPS:
                continue
            status_val = (
                getattr(getattr(event, "status", None), "value", "") or ""
            ).lower()
            if status_val != "succeeded":
                continue
            ts = getattr(event, "event_timestamp", None)
            if ts is None:
                continue
            if not isinstance(ts, datetime.datetime):
                ts = datetime.datetime.fromisoformat(str(ts))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if latest is None or ts > latest:
                latest = ts
    except Exception as exc:
        _LOG.warning("StopHistory: activity_logs.list failed for %s — %s", rid_lower, exc)
    _LOG.debug("StopHistory: %s → %s", rid_lower, latest)
    return rid_lower, latest


def collect_stop_history(
    credential: DefaultAzureCredential,
    subscriptions: list[SubscriptionInfo],
    stopped_vms: list[VmInventory],
) -> dict[str, int]:
    """Return ``{resource_id_lower → days_since_last_deallocate}`` for stopped VMs.

    Queries are scoped per-VM via ``resourceId eq '<arm-id>'`` so each API call
    returns at most a handful of events.  Queries run in parallel per
    subscription using a thread pool.

    Only VMs in *stopped_vms* are queried.  VMs with no qualifying event in
    the 90-day lookback are omitted from the result.
    """
    if not stopped_vms:
        return {}

    end = datetime.datetime.now(tz=timezone.utc)
    start = end - datetime.timedelta(days=_LOOKBACK_DAYS)
    ts_filter = (
        f"eventTimestamp ge '{start.isoformat()}' and "
        f"eventTimestamp le '{end.isoformat()}'"
    )

    # Group stopped VMs by subscription so we create one client per subscription.
    sub_to_vms: dict[str, list[VmInventory]] = {}
    for vm in stopped_vms:
        sub_to_vms.setdefault(vm.subscription_id, []).append(vm)

    last_event: dict[str, datetime.datetime] = {}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=_console,
        transient=True,
    ) as progress:
        task = progress.add_task(
            "Querying Activity Log per VM…", total=len(stopped_vms)
        )

        for sub in subscriptions:
            sub_vms = sub_to_vms.get(sub.subscription_id)
            if not sub_vms:
                continue

            try:
                client = MonitorManagementClient(credential, sub.subscription_id)
            except Exception as exc:
                _LOG.warning(
                    "StopHistory: MonitorManagementClient failed for %s — %s",
                    sub.subscription_id, exc,
                )
                progress.advance(task, len(sub_vms))
                continue

            _executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS)
            futures = {
                _executor.submit(
                    _query_single_vm,
                    client,
                    vm.resource_id,
                    f"{ts_filter} and resourceId eq '{vm.resource_id}'",
                ): vm
                for vm in sub_vms
            }
            with _executor:
                for future in as_completed(futures):
                    rid_lower, ts = future.result()
                    if ts is not None:
                        if rid_lower not in last_event or ts > last_event[rid_lower]:
                            last_event[rid_lower] = ts
                    progress.advance(task)

    now = datetime.datetime.now(tz=timezone.utc)
    return {rid: max(0, (now - ts).days) for rid, ts in last_event.items()}
