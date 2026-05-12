"""Reservation Orders + utilization collector (SPEC §3.4).

Queries:
  1. ``Microsoft.Capacity/reservationOrders`` via Azure Resource Graph for
     inventory (order ID, SKU, term, expiry, applied scope).
  2. ``Microsoft.Consumption/reservationDetails`` for utilization counts
     (last 30 days).  Skipped silently if the identity lacks
     ``Microsoft.Consumption/*/read`` or the SDK is unavailable.

No $ / cost fields are ever read or emitted — counts, percentages, and
dates only (SPEC §1.2).
"""

from __future__ import annotations

import logging
from typing import Any

from azure.identity import DefaultAzureCredential
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions
from rich.console import Console

from cloudopt.collector.auth import SubscriptionInfo
from cloudopt.models import ReservationOrder

try:
    from azure.mgmt.consumption import ConsumptionManagementClient

    _CONSUMPTION_AVAILABLE = True
except ImportError:
    ConsumptionManagementClient = None  # type: ignore[assignment,misc]
    _CONSUMPTION_AVAILABLE = False

console = Console()
_LOG = logging.getLogger(__name__)

# ARG query — projection of the reservation-orders resource type.
# appliedScopeType and appliedScopes live inside ``properties``.
_RSV_QUERY = """
Resources
| where type =~ 'microsoft.capacity/reservationorders'
| project
    id,
    displayName    = tostring(properties.displayName),
    term           = tostring(properties.term),
    expiryDate     = tostring(properties.expiryDate),
    skuName        = tostring(properties.reservations[0].sku.name),
    location       = tostring(properties.reservations[0].location),
    reservedCount  = toint(properties.reservations[0].reservedQuantity),
    appliedScopeType = tostring(properties.appliedScopeType),
    appliedScopes  = properties.appliedScopes
"""

# Consumption API look-back (days)
_UTIL_WINDOW_DAYS = 30


def collect_reservations(
    credential: DefaultAzureCredential,
    subscriptions: list[SubscriptionInfo],
) -> list[ReservationOrder]:
    """Return RI / Savings Plan orders visible to the credential.

    Returns ``[]`` silently if:
    - ``subscriptions`` is empty.
    - The Azure Resource Graph call fails (e.g. ``AuthorizationFailed``).

    Utilization is joined from the Consumption API when available; the field
    is ``None`` when the API is inaccessible.
    """
    if not subscriptions:
        return []

    try:
        arg_client = ResourceGraphClient(credential)
    except Exception as exc:
        _LOG.warning("Reservations: could not initialise ARG client — %s", exc)
        return []

    sub_ids = [s.subscription_id for s in subscriptions]
    orders: list[ReservationOrder] = _query_arg(arg_client, sub_ids)

    if not orders:
        return []

    _join_utilization(credential, sub_ids, orders)
    return orders


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _query_arg(
    arg_client: ResourceGraphClient,
    sub_ids: list[str],
) -> list[ReservationOrder]:
    """Query ARG for reservation orders and return parsed ReservationOrder list."""
    orders: list[ReservationOrder] = []
    try:
        resp = arg_client.resources(
            QueryRequest(
                subscriptions=sub_ids,
                query=_RSV_QUERY,
                options=QueryRequestOptions(result_format="objectArray"),
            )
        )
    except Exception as exc:
        _LOG.warning("Reservations: ARG query failed — %s", exc)
        return []

    rows: list[dict[str, Any]] = resp.data or []
    for row in rows:
        try:
            orders.append(_row_to_order(row))
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("Reservations: skipping malformed row — %s", exc)
    return orders


def _row_to_order(row: dict[str, Any]) -> ReservationOrder:
    """Parse a single ARG row into a ReservationOrder."""
    applied_scopes: list[str] = []
    scopes_raw = row.get("appliedScopes") or []
    if isinstance(scopes_raw, list):
        applied_scopes = [str(s) for s in scopes_raw]
    elif isinstance(scopes_raw, str) and scopes_raw:
        applied_scopes = [scopes_raw]

    return ReservationOrder(
        order_id=str(row.get("id", "")),
        display_name=str(row.get("displayName", "")),
        term=str(row.get("term", "")),
        expiry_date=str(row.get("expiryDate", ""))[:10],  # trim to YYYY-MM-DD
        sku_name=str(row.get("skuName", "")),
        region=str(row.get("location", "")),
        reserved_count=int(row.get("reservedCount") or 0),
        applied_scope_type=str(row.get("appliedScopeType", "Shared")),
        applied_scope_ids=applied_scopes,
        utilization_pct=None,
    )


def _join_utilization(
    credential: DefaultAzureCredential,
    sub_ids: list[str],
    orders: list[ReservationOrder],
) -> None:
    """Populate ``utilization_pct`` on each order using the Consumption API.

    Mutates *orders* in place.  Silently skips if the API is unavailable or
    the identity lacks permissions.
    """
    if not _CONSUMPTION_AVAILABLE or ConsumptionManagementClient is None:
        return

    # Build index: order_id fragment → order (order IDs may contain full path)
    order_index: dict[str, ReservationOrder] = {}
    for o in orders:
        # Extract the GUID from the full resource ID path
        parts = o.order_id.rstrip("/").split("/")
        order_index[parts[-1]] = o

    import datetime

    end = datetime.datetime.now(datetime.timezone.utc).date()
    start = end - datetime.timedelta(days=_UTIL_WINDOW_DAYS)
    scope = f"/subscriptions/{sub_ids[0]}" if sub_ids else "/providers/Microsoft.Capacity"

    try:
        client = ConsumptionManagementClient(credential, sub_ids[0])
        details = list(
            client.reservations_details.list(
                scope=scope,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
            )
        )
    except Exception as exc:
        _LOG.debug("Reservations: Consumption API unavailable — %s", exc)
        return

    # Aggregate utilization per order: average utilized_percentage across records
    util_sum: dict[str, float] = {}
    util_cnt: dict[str, int] = {}
    for detail in details:
        oid = getattr(detail, "reservation_order_id", None)
        pct = getattr(detail, "utilized_percentage", None)
        if oid and pct is not None:
            util_sum[oid] = util_sum.get(oid, 0.0) + float(pct)
            util_cnt[oid] = util_cnt.get(oid, 0) + 1

    for oid, total in util_sum.items():
        order = order_index.get(oid)
        if order is not None:
            order.utilization_pct = round(total / util_cnt[oid], 1)
