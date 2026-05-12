"""Capacity Reservation Groups collector (SPEC §3.4).

Uses ``azure-mgmt-compute`` (already a project dependency) to list all
Capacity Reservation Groups visible to the credential across the in-scope
subscriptions, then fetches the individual reservations within each group.

No $ / cost fields are emitted — counts and zone metadata only (SPEC §1.2).
"""

from __future__ import annotations

import logging

from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from rich.console import Console

from cloudopt.collector.auth import SubscriptionInfo
from cloudopt.models import CapacityReservationGroup, CapacityReservationItem

console = Console()
_LOG = logging.getLogger(__name__)

# Expand flag so the CRG response includes the virtual-machine references.
_CRG_EXPAND = "virtualMachinesRef"


def collect_capacity_reservations(
    credential: DefaultAzureCredential,
    subscriptions: list[SubscriptionInfo],
) -> list[CapacityReservationGroup]:
    """Return all Capacity Reservation Groups for every in-scope subscription.

    Returns ``[]`` silently if:
    - ``subscriptions`` is empty.
    - The Compute API call fails (e.g. ``AuthorizationFailed``).
    """
    if not subscriptions:
        return []

    groups: list[CapacityReservationGroup] = []
    for sub in subscriptions:
        try:
            compute_client = ComputeManagementClient(credential, sub.subscription_id)
        except Exception as exc:
            _LOG.warning("CRG: could not initialise compute client — %s", exc)
            return []
        groups.extend(_list_groups(compute_client, sub))
    return groups


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _list_groups(
    compute_client: ComputeManagementClient,
    sub: SubscriptionInfo,
) -> list[CapacityReservationGroup]:
    """List all CRGs in a single subscription and populate their reservations."""
    result: list[CapacityReservationGroup] = []
    try:
        crgs = list(
            compute_client.capacity_reservation_groups.list_by_subscription(
                expand=_CRG_EXPAND
            )
        )
    except Exception as exc:
        _LOG.warning(
            "CRG: list_by_subscription failed for %s — %s",
            sub.subscription_id,
            exc,
        )
        return []

    for crg in crgs:
        try:
            group = _parse_crg(compute_client, crg, sub)
            result.append(group)
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("CRG: skipping malformed group %s — %s", getattr(crg, "name", "?"), exc)

    return result


def _parse_crg(
    compute_client: ComputeManagementClient,
    crg: object,
    sub: SubscriptionInfo,
) -> CapacityReservationGroup:
    """Parse a single CRG ARM object and its capacity reservations."""
    group_id: str = getattr(crg, "id", "") or ""
    name: str = getattr(crg, "name", "") or ""
    location: str = getattr(crg, "location", "") or ""
    zones_raw = getattr(crg, "zones", None)
    zones: list[str] = list(zones_raw) if isinstance(zones_raw, list) else []

    # Extract resource group from the resource ID
    resource_group = _rg_from_id(group_id)

    # Fetch individual capacity reservations within this group
    reservations = _list_reservations(compute_client, resource_group, name)

    return CapacityReservationGroup(
        group_id=group_id,
        group_name=name,
        subscription_id=sub.subscription_id,
        resource_group=resource_group,
        region=location,
        zones=zones,
        reservations=reservations,
    )


def _list_reservations(
    compute_client: ComputeManagementClient,
    resource_group: str,
    crg_name: str,
) -> list[CapacityReservationItem]:
    """Return the individual reservations within a CRG."""
    if not resource_group or not crg_name:
        return []
    try:
        crs = list(
            compute_client.capacity_reservations.list_by_capacity_reservation_group(
                resource_group, crg_name
            )
        )
    except Exception as exc:
        _LOG.debug("CRG: could not list reservations for %s/%s — %s", resource_group, crg_name, exc)
        return []

    items: list[CapacityReservationItem] = []
    for cr in crs:
        sku = getattr(cr, "sku", None)
        if sku is None:
            continue
        sku_name: str = getattr(sku, "name", "") or ""
        reserved: int = int(getattr(sku, "capacity", 0) or 0)
        vms_alloc = getattr(cr, "virtual_machines_allocated", None) or []
        used: int = len(list(vms_alloc))
        zones_raw = getattr(cr, "zones", None)
        zone = zones_raw[0] if isinstance(zones_raw, list) and zones_raw else None
        items.append(
            CapacityReservationItem(
                reservation_name=getattr(cr, "name", "") or "",
                sku_name=sku_name,
                reserved_count=reserved,
                used_count=used,
                zone=zone,
            )
        )
    return items


def _rg_from_id(resource_id: str) -> str:
    """Extract resource group name from an ARM resource ID."""
    parts = resource_id.lower().split("/")
    try:
        idx = parts.index("resourcegroups")
        # Return original-case version of the segment after "resourcegroups"
        original_parts = resource_id.split("/")
        return original_parts[idx + 1]
    except (ValueError, IndexError):
        return ""
