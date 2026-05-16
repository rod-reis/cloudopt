"""Azure Compute quota collection.

For each (subscription, region) pair derived from the active scope, fetches
compute quota and current usage from the Azure Compute Usages API.  Only
vCPU-related entries with a non-zero quota limit are included.

Quota is a subscription + region concept — no resource group or tag filtering
is applied here.  Region selection follows this priority order:

1. ``scope.locations`` — explicit region filter provided by the user.
2. Per-subscription VM regions — derived from the collected VM inventory.
3. Global VM region union — fallback for subscriptions that have no VMs in
   the current inventory but are explicitly in scope.

Allocation failure counts are collected from Azure Monitor Activity Logs and
joined on to each QuotaItem by (subscription_id, region, vm_family).
"""

from __future__ import annotations

import datetime
import json

from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.monitor import MonitorManagementClient
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from cloudopt.collector.auth import SubscriptionInfo
from cloudopt.models import QuotaItem

console = Console()

# Allocation failures older than this many days are ignored.
_ALLOCATION_FAILURE_WINDOW_DAYS = 30

# PAYG (pay-as-you-go) default vCPU quota applied to most VM families on a new
# subscription.  Raised? = Yes when a subscription's actual limit exceeds this.
_UNIVERSAL_PAYG_DEFAULT_VCPUS = 10

# Geography meta-locations returned by the ARM locations API that do not
# correspond to deployable Azure regions.  The Compute Usages API rejects
# them with NoRegisteredProviderFound, so we skip them during quota collection.
_NON_COMPUTE_LOCATIONS: frozenset[str] = frozenset({
    "global",
    "unitedstates",
    "europe",
    "asia",
    "australia",
    "brazil",
    "canada",
    "china",
    "france",
    "germany",
    "india",
    "japan",
    "korea",
    "norway",
    "southafrica",
    "switzerland",
    "uae",
})


def collect_quota(
    credential: DefaultAzureCredential,
    subscriptions: list[SubscriptionInfo],
    scope: "ScopeFilter",  # noqa: F821  (imported below to avoid circularity)
    vms_sub_regions: dict[str, set[str]] | None = None,
    quota_alert_pct: float = 80.0,
) -> list[QuotaItem]:
    """Return compute quota utilization for every (subscription, region) in scope.

    All subscriptions in the list are queried — not only those that contain
    VM inventory — so that empty subscriptions are still covered.

    Args:
        credential:       Azure credential.
        subscriptions:    Full list of in-scope SubscriptionInfo objects.
        scope:            Active :class:`~cloudopt.scope.ScopeFilter`; used for
                          region filtering only (no RG or tag filtering applied).
        vms_sub_regions:  Optional mapping of subscription_id → set of region
                          strings derived from the VM inventory.  Used as a
                          fallback when ``scope.locations`` is not set.
        quota_alert_pct:  Threshold (0–100) above which an entry is flagged.
    """
    _vm_regions: dict[str, set[str]] = vms_sub_regions or {}

    # Global fallback: union of all VM regions across every subscription.
    _all_vm_regions: set[str] = set().union(*_vm_regions.values()) if _vm_regions else set()

    sub_map: dict[str, str] = {s.subscription_id: s.subscription_name for s in subscriptions}

    # Build work list: (sub_id, region) pairs.
    # Quota is subscription + region only — no RG or tag filtering.
    work: list[tuple[str, str]] = []
    for sub_id in sub_map:
        if scope.locations:
            regions_for_sub = set(scope.locations)
        else:
            # Use VM-derived regions for this sub; fall back to the global
            # union for subscriptions that have no VMs in the current run.
            regions_for_sub = _vm_regions.get(sub_id) or _all_vm_regions

        for region in sorted(regions_for_sub):
            # The Compute Usages API does not accept geography meta-locations
            # (e.g. 'global', 'unitedstates', 'europe') — skip them to avoid
            # spurious NoRegisteredProviderFound errors.  These strings appear
            # in the ARM locations list as data-residency groupings and can
            # surface in VM inventory when Arc-enabled or cross-region resources
            # are present.
            if region.lower() in _NON_COMPUTE_LOCATIONS:
                continue
            work.append((sub_id, region))

    if not work:
        console.print(
            "[yellow]Warning:[/yellow] No (subscription, region) pairs to query for quota. "
            "Specify --regions or ensure the VM inventory is non-empty."
        )
        return []

    # ── Collect allocation failures from Activity Logs (best-effort) ─────
    failure_counts = _collect_allocation_failures(credential, list(sub_map.keys()))

    results: list[QuotaItem] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Collecting quota…", total=len(work))

        for sub_id, region in work:
            sub_name = sub_map.get(sub_id, sub_id)
            try:
                compute_client = ComputeManagementClient(credential, sub_id)
                for usage in compute_client.usage.list(location=region):
                    limit: int = int(usage.limit or 0)
                    current: int = int(usage.current_value or 0)
                    if limit == 0:
                        continue
                    resource_type: str = (usage.name.value or "")
                    display_name: str = (usage.name.localized_value or resource_type)
                    # Only collect vCPU-style quota entries.  Other compute
                    # quotas (availability sets, regional VM count, etc.) are
                    # not actionable for capacity rebalancing.
                    if "vcpu" not in display_name.lower():
                        continue
                    pct = round(current / limit * 100, 1)
                    failures = failure_counts.get((sub_id, region, resource_type), 0)
                    results.append(
                        QuotaItem(
                            subscription_id=sub_id,
                            subscription_name=sub_name,
                            region=region,
                            resource_type=resource_type,
                            display_name=display_name,
                            current_usage=current,
                            quota_limit=limit,
                            utilization_pct=pct,
                            alert=(pct >= quota_alert_pct),
                            allocation_failures_30d=failures,
                            subscription_default=_UNIVERSAL_PAYG_DEFAULT_VCPUS,
                            # peak_usage_pct_30d: not available from the Compute
                            # Usages API (point-in-time only); leave as None.
                        )
                    )
            except Exception as exc:
                console.print(
                    f"[yellow]Warning:[/yellow] quota unavailable for "
                    f"{sub_name} / {region}: {exc}"
                )
            finally:
                progress.advance(task)

    return results


def _collect_allocation_failures(
    credential: DefaultAzureCredential,
    subscription_ids: list[str],
) -> dict[tuple[str, str, str], int]:
    """Query Activity Logs for AllocationFailed events in the last 30 days.

    Returns a mapping of (subscription_id, location, resource_type) →
    failure count.  ``resource_type`` is a best-effort VM-family derivation
    from the error message; falls back to ``"all"`` when it cannot be parsed.

    Failures are detected by looking for Activity Log entries where:
    - operationName contains ``Microsoft.Compute/virtualMachines/write`` or
      ``Microsoft.Compute/virtualMachineScaleSets/write``
    - status == "Failed"
    - statusMessage JSON contains the string ``"AllocationFailed"``
    """
    since = (
        datetime.datetime.utcnow() - datetime.timedelta(days=_ALLOCATION_FAILURE_WINDOW_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Activity Log filter — we narrow to failed Compute write operations.
    # Each subscription is queried independently; failures per sub are rare
    # so the volume is low.
    activity_filter = (
        f"eventTimestamp ge '{since}' "
        "and resourceProvider eq 'Microsoft.Compute' "
        "and status eq 'Failed'"
    )

    counts: dict[tuple[str, str, str], int] = {}

    for sub_id in subscription_ids:
        try:
            monitor_client = MonitorManagementClient(credential, sub_id)
            for event in monitor_client.activity_logs.list(filter=activity_filter):
                op_name: str = (
                    getattr(event.operation_name, "value", None) or ""
                ).lower()
                if "virtualmachine" not in op_name:
                    continue
                # Parse statusMessage to confirm AllocationFailed
                status_msg = getattr(event, "properties", None) or {}
                if isinstance(status_msg, dict):
                    raw = status_msg.get("statusMessage", "") or ""
                else:
                    raw = ""
                if "allocationfailed" not in raw.lower():
                    continue

                location: str = getattr(event, "resource_provider_name", None) and ""
                location = str(getattr(event, "resource_id", "") or "")
                # Extract location from the resource ID path:
                # /subscriptions/{sub}/resourceGroups/{rg}/providers/...
                location = _extract_location_from_event(event)
                resource_type = _parse_vm_family(raw)
                key = (sub_id, location, resource_type)
                counts[key] = counts.get(key, 0) + 1
        except Exception as exc:
            # Activity Log queries are best-effort; never block quota collection
            console.print(
                f"[dim]Activity Log query failed for {sub_id[:8]}…: {exc}[/dim]"
            )

    return counts


def _extract_location_from_event(event: object) -> str:
    """Extract the ARM region from an Activity Log event's resource ID."""
    # The Activity Log event has a ``resource_id`` but no ``location`` field.
    # We fall back to an empty string when it cannot be determined.
    resource_id: str = str(getattr(event, "resource_id", "") or "")
    # Best-effort: check event.resource_group_name + event.subscription_id
    # Location is not reliably in Activity Logs; return empty for now.
    return ""


def _parse_vm_family(status_message: str) -> str:
    """Extract the VM family name from an AllocationFailed statusMessage JSON.

    The statusMessage field from a failed Compute write contains a JSON blob
    with a ``details`` array whose ``message`` mentions the SKU/family.
    We return ``"all"`` when parsing fails.
    """
    try:
        obj = json.loads(status_message)
        details = obj.get("details") or obj.get("error", {}).get("details", [])
        for detail in details:
            msg: str = str(detail.get("message", ""))
            # Look for patterns like "Standard_D2s_v3" or a family keyword
            if "standard_" in msg.lower():
                import re
                m = re.search(r"Standard_\w+", msg, re.IGNORECASE)
                if m:
                    return m.group(0)
    except Exception:
        pass
    return "all"


def sub_regions_from_vms(vms: list) -> dict[str, set[str]]:
    """Build a subscription_id → {region, ...} mapping from a VM list."""
    mapping: dict[str, set[str]] = {}
    for vm in vms:
        mapping.setdefault(vm.subscription_id, set()).add(vm.region)
    return mapping


# ---------------------------------------------------------------------------
# TYPE_CHECKING import to satisfy the ScopeFilter annotation without
# introducing a circular import at runtime.
# ---------------------------------------------------------------------------
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    from cloudopt.scope import ScopeFilter  # noqa: F401



def collect_quota(
    credential: DefaultAzureCredential,
    subscriptions: list[SubscriptionInfo],
    scope: "ScopeFilter",  # noqa: F821  (imported below to avoid circularity)
    vms_sub_regions: dict[str, set[str]] | None = None,
    quota_alert_pct: float = 80.0,
) -> list[QuotaItem]:
    """Return compute quota utilization for every (subscription, region) in scope.

    All subscriptions in the list are queried — not only those that contain
    VM inventory — so that empty subscriptions are still covered.

    Args:
        credential:       Azure credential.
        subscriptions:    Full list of in-scope SubscriptionInfo objects.
        scope:            Active :class:`~cloudopt.scope.ScopeFilter`; used for
                          region filtering only (no RG or tag filtering applied).
        vms_sub_regions:  Optional mapping of subscription_id → set of region
                          strings derived from the VM inventory.  Used as a
                          fallback when ``scope.locations`` is not set.
        quota_alert_pct:  Threshold (0–100) above which an entry is flagged.
    """
    _vm_regions: dict[str, set[str]] = vms_sub_regions or {}

    # Global fallback: union of all VM regions across every subscription.
    _all_vm_regions: set[str] = set().union(*_vm_regions.values()) if _vm_regions else set()

    sub_map: dict[str, str] = {s.subscription_id: s.subscription_name for s in subscriptions}

    # Build work list: (sub_id, region) pairs.
    # Quota is subscription + region only — no RG or tag filtering.
    work: list[tuple[str, str]] = []
    for sub_id in sub_map:
        if scope.locations:
            regions_for_sub = set(scope.locations)
        else:
            # Use VM-derived regions for this sub; fall back to the global
            # union for subscriptions that have no VMs in the current run.
            regions_for_sub = _vm_regions.get(sub_id) or _all_vm_regions

        for region in sorted(regions_for_sub):
            # Skip geography meta-locations that the Compute Usages API rejects.
            if region.lower() in _NON_COMPUTE_LOCATIONS:
                continue
            work.append((sub_id, region))

    if not work:
        console.print(
            "[yellow]Warning:[/yellow] No (subscription, region) pairs to query for quota. "
            "Specify --regions or ensure the VM inventory is non-empty."
        )
        return []

    # Re-organise the flat work list into subscription → [regions] so that
    # we create one ComputeManagementClient per subscription.  This avoids
    # redundant credential lookups (and the resulting SDK log noise) when
    # the same subscription has multiple regions to query.
    sub_regions_work: dict[str, list[str]] = {}
    for sub_id, region in work:
        sub_regions_work.setdefault(sub_id, []).append(region)

    results: list[QuotaItem] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Collecting quota…", total=len(work))

        for sub_id, regions in sub_regions_work.items():
            sub_name = sub_map.get(sub_id, sub_id)
            compute_client = ComputeManagementClient(credential, sub_id)
            # Set when a credential / auth error is detected on the first
            # region — all remaining regions of this subscription will have
            # the same problem, so we skip them to avoid duplicate log noise.
            sub_auth_failed = False

            for region in regions:
                if sub_auth_failed:
                    progress.advance(task)
                    continue
                try:
                    for usage in compute_client.usage.list(location=region):
                        limit: int = int(usage.limit or 0)
                        current: int = int(usage.current_value or 0)
                        if limit == 0:
                            continue
                        resource_type: str = (usage.name.value or "")
                        display_name: str = (usage.name.localized_value or resource_type)
                        # Only collect vCPU-style quota entries.  Other compute
                        # quotas (availability sets, regional VM count, etc.) are
                        # not actionable for capacity rebalancing.
                        if "vcpu" not in display_name.lower():
                            continue
                        pct = round(current / limit * 100, 1)
                        results.append(
                            QuotaItem(
                                subscription_id=sub_id,
                                subscription_name=sub_name,
                                region=region,
                                resource_type=resource_type,
                                display_name=display_name,
                                current_usage=current,
                                quota_limit=limit,
                                utilization_pct=pct,
                                alert=(pct >= quota_alert_pct),
                                subscription_default=_UNIVERSAL_PAYG_DEFAULT_VCPUS,
                            )
                        )
                except Exception as exc:
                    exc_str = str(exc).lower()
                    # Auth/credential errors affect the entire subscription,
                    # not just this region — emit one consolidated warning and
                    # skip remaining regions to suppress repeated SDK log lines.
                    if any(kw in exc_str for kw in ("credential", "azure cli", "authentication", "token")):
                        console.print(
                            f"[yellow]Warning:[/yellow] quota unavailable for "
                            f"{sub_name} (all regions): {exc}"
                        )
                        sub_auth_failed = True
                    else:
                        console.print(
                            f"[yellow]Warning:[/yellow] quota unavailable for "
                            f"{sub_name} / {region}: {exc}"
                        )
                finally:
                    progress.advance(task)

    return results


def sub_regions_from_vms(vms: list) -> dict[str, set[str]]:
    """Build a subscription_id → {region, ...} mapping from a VM list."""
    mapping: dict[str, set[str]] = {}
    for vm in vms:
        mapping.setdefault(vm.subscription_id, set()).add(vm.region)
    return mapping


# ---------------------------------------------------------------------------
# TYPE_CHECKING import to satisfy the ScopeFilter annotation without
# introducing a circular import at runtime.
# ---------------------------------------------------------------------------
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    from cloudopt.scope import ScopeFilter  # noqa: F401
