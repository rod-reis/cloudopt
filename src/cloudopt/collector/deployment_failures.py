"""Deployment Failures collector (SPEC §3.5).

Queries the Azure Monitor Activity Log for the last 90 days, filtering on
``category=Administrative`` and ``level in {Error, Critical}`` for the three
target resource types:

  - ``microsoft.compute/virtualmachines``
  - ``microsoft.compute/virtualmachinescalesets``
  - ``microsoft.containerservice/managedclusters``

Each matching entry is bucketed into one of four error classes:

  ``allocation`` — AllocationFailed, ZonalAllocationFailed,
                   OverconstrainedAllocationRequest, SkuNotAvailable.
  ``quota``      — QuotaExceeded, OperationNotAllowed + "quota" substring.
  ``image``      — ImagePullBackOff, ImageNotFound, RegistryAuth.
  ``other``      — everything else.

No $ / cost fields are emitted (SPEC §1.2).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from azure.identity import DefaultAzureCredential
from azure.mgmt.monitor import MonitorManagementClient

from cloudopt.collector.auth import SubscriptionInfo
from cloudopt.models import DeploymentFailureEntry

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Target resource types (lowercase for case-insensitive matching)
# ---------------------------------------------------------------------------

_TARGET_TYPES: frozenset[str] = frozenset({
    "microsoft.compute/virtualmachines",
    "microsoft.compute/virtualmachinescalesets",
    "microsoft.containerservice/managedclusters",
})

# ---------------------------------------------------------------------------
# Error-class bucketing keywords
# ---------------------------------------------------------------------------

_ALLOCATION_KEYWORDS = frozenset({
    "allocationfailed",
    "zonalallocationfailed",
    "overconstrainedallocationrequest",
    "skunotavailable",
})

_QUOTA_KEYWORDS = frozenset({
    "quotaexceeded",
})

_IMAGE_KEYWORDS = frozenset({
    "imagepullbackoff",
    "imagenotfound",
    "registryauth",
})


def collect_deployment_failures(
    credential: DefaultAzureCredential,
    subscriptions: list[SubscriptionInfo],
) -> list[DeploymentFailureEntry]:
    """Return Activity Log deployment failure entries for all in-scope subscriptions.

    Returns ``[]`` silently if:
    - ``subscriptions`` is empty.
    - The Monitor API call fails (e.g. ``AuthorizationFailed``).
    """
    if not subscriptions:
        return []

    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=90)

    results: list[DeploymentFailureEntry] = []
    for sub in subscriptions:
        try:
            monitor_client = MonitorManagementClient(credential, sub.subscription_id)
        except Exception as exc:
            _LOG.warning("DeploymentFailures: could not init monitor client — %s", exc)
            return []
        results.extend(_query_subscription(monitor_client, sub, start, end))
    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _query_subscription(
    monitor_client: MonitorManagementClient,
    sub: SubscriptionInfo,
    start: datetime,
    end: datetime,
) -> list[DeploymentFailureEntry]:
    """Query the Activity Log for one subscription and return bucketed entries."""
    filter_expr = (
        f"eventTimestamp ge '{start.isoformat()}' and "
        f"eventTimestamp le '{end.isoformat()}' and "
        "category eq 'Administrative' and "
        "(level eq 'Error' or level eq 'Critical')"
    )
    try:
        events = list(monitor_client.activity_logs.list(filter=filter_expr))
    except Exception as exc:
        _LOG.warning(
            "DeploymentFailures: activity_logs.list failed for %s — %s",
            sub.subscription_id,
            exc,
        )
        return []

    entries: list[DeploymentFailureEntry] = []
    for event in events:
        entry = _parse_event(event, sub)
        if entry is not None:
            entries.append(entry)
    return entries


def _parse_event(event: object, sub: SubscriptionInfo) -> DeploymentFailureEntry | None:
    """Parse a single Activity Log event into a DeploymentFailureEntry.

    Returns ``None`` if the resource type is not in the target set.
    """
    resource_id: str = getattr(event, "resource_id", "") or ""
    resource_type = _extract_resource_type(resource_id)

    if resource_type not in _TARGET_TYPES:
        return None

    resource_name = _extract_resource_name(resource_id)
    resource_group = _extract_resource_group(resource_id)

    op_name_obj = getattr(event, "operation_name", None)
    operation_name: str = (
        getattr(op_name_obj, "value", "") or ""
        if op_name_obj is not None
        else ""
    )

    status_obj = getattr(event, "status", None)
    status_message: str = (
        getattr(status_obj, "localizedValue", "") or ""
        if status_obj is not None
        else ""
    )

    timestamp_raw = getattr(event, "event_timestamp", None)
    timestamp: str = (
        timestamp_raw.isoformat()
        if isinstance(timestamp_raw, datetime)
        else str(timestamp_raw or "")
    )

    # Region: not always present in activity log entries
    region = _extract_region(resource_id)

    error_class = _classify_error(status_message)

    return DeploymentFailureEntry(
        resource_id=resource_id,
        resource_name=resource_name,
        resource_type=resource_type,
        subscription_id=sub.subscription_id,
        resource_group=resource_group,
        region=region,
        error_class=error_class,
        operation_name=operation_name,
        status_message=status_message,
        timestamp=timestamp,
    )


def _classify_error(message: str) -> str:
    """Bucket an error message into one of four error classes."""
    lower = message.lower()

    for kw in _ALLOCATION_KEYWORDS:
        if kw in lower:
            return "allocation"

    for kw in _QUOTA_KEYWORDS:
        if kw in lower:
            return "quota"

    # OperationNotAllowed + quota substring → quota
    if "operationnotallowed" in lower and "quota" in lower:
        return "quota"

    for kw in _IMAGE_KEYWORDS:
        if kw in lower:
            return "image"

    return "other"


def _extract_resource_type(resource_id: str) -> str:
    """Extract lowercased provider/type from an ARM resource ID.

    E.g. '.../Microsoft.Compute/virtualMachines/vm1' → 'microsoft.compute/virtualmachines'
    """
    parts = resource_id.lower().split("/")
    # Find 'providers' marker
    try:
        idx = parts.index("providers")
        if idx + 2 < len(parts):
            return f"{parts[idx + 1]}/{parts[idx + 2]}"
    except ValueError:
        pass
    return ""


def _extract_resource_name(resource_id: str) -> str:
    """Extract the leaf resource name from an ARM resource ID."""
    parts = [p for p in resource_id.split("/") if p]
    return parts[-1] if parts else ""


def _extract_resource_group(resource_id: str) -> str:
    """Extract resource group from an ARM resource ID."""
    parts = resource_id.lower().split("/")
    try:
        idx = parts.index("resourcegroups")
        original = resource_id.split("/")
        return original[idx + 1] if idx + 1 < len(original) else ""
    except ValueError:
        return ""


def _extract_region(resource_id: str) -> str:
    """Region is not in the resource ID; return empty string.

    The Activity Log event has a ``location`` attribute on some versions of the
    SDK but it is not reliable.  Return empty so the model validates cleanly.
    """
    return ""
