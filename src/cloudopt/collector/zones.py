"""Subscription availability-zone mapping collector.

Uses SubscriptionClient.subscriptions.list_locations() to retrieve the
physical-to-logical zone mapping for every location that supports
Availability Zones.  One row is emitted per (subscription, location,
logical zone) triple.
"""

from __future__ import annotations

from azure.identity import DefaultAzureCredential
from azure.mgmt.subscription import SubscriptionClient
from rich.console import Console

from cloudopt.collector.auth import SubscriptionInfo
from cloudopt.models import SubscriptionZoneMapping

console = Console()


def collect_zone_mappings(
    credential: DefaultAzureCredential,
    subscriptions: list[SubscriptionInfo],
) -> list[SubscriptionZoneMapping]:
    """Return one :class:`SubscriptionZoneMapping` row per (subscription,
    location, logical zone) for every location that has AZ mappings.

    Failures for individual subscriptions are logged as warnings rather
    than raising exceptions so that a single inaccessible subscription
    does not abort the whole collection run.
    """
    client = SubscriptionClient(credential)
    result: list[SubscriptionZoneMapping] = []

    for sub in subscriptions:
        try:
            locations = client.subscriptions.list_locations(
                sub.subscription_id,
                include_extended_locations=False,
            )
            for loc in locations:
                if not loc.availability_zone_mappings:
                    continue
                for mapping in loc.availability_zone_mappings:
                    physical = mapping.physical_zone or ""
                    result.append(
                        SubscriptionZoneMapping(
                            tenant_id=sub.tenant_id,
                            subscription_id=sub.subscription_id,
                            subscription_name=sub.subscription_name,
                            location=loc.name or "",
                            logical_zone=mapping.logical_zone or "",
                            physical_zone=physical,
                            physical_zone_name=physical,
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            console.print(
                f"[yellow]Warning:[/yellow] Could not retrieve zone mappings for "
                f"subscription {sub.subscription_id!r}: {exc}"
            )

    return result
