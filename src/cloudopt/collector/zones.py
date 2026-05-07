"""Subscription availability-zone mapping collector.

Calls the ARM REST API at api-version=2022-12-01 directly to retrieve
the physical-to-logical zone mapping for every location that supports
Availability Zones.  One row is emitted per (subscription, location,
logical zone) triple.

Why direct REST instead of SubscriptionClient.subscriptions.list_locations():
  azure-mgmt-subscription==3.1.1 (the latest published release) uses an
  API version predating 2022-12-01.  Its generated Location model contains
  only six fields (id, subscriptionId, name, displayName, latitude,
  longitude) and does NOT include availabilityZoneMappings, so that
  attribute is always None regardless of what the API returns.  The
  2022-12-01 REST endpoint does include availabilityZoneMappings and must
  be called directly using a bearer token obtained from the credential.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from azure.identity import DefaultAzureCredential
from rich.console import Console

from cloudopt.collector.auth import SubscriptionInfo
from cloudopt.models import SubscriptionZoneMapping

console = Console()

_ARM_API_VERSION = "2022-12-01"
_ARM_ENDPOINT = "https://management.azure.com"
_ARM_SCOPE = "https://management.azure.com/.default"


def _list_locations_raw(token: str, subscription_id: str) -> list[dict[str, Any]]:
    """Return all location objects for a subscription via the ARM REST API.

    Follows ``nextLink`` pagination if present.  Raises :class:`RuntimeError`
    on HTTP errors so the caller can log a warning and continue.
    """
    all_locations: list[dict[str, Any]] = []
    next_url: str | None = (
        f"{_ARM_ENDPOINT}/subscriptions/{subscription_id}/locations"
        f"?api-version={_ARM_API_VERSION}"
    )
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    while next_url:
        req = urllib.request.Request(next_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"ARM HTTP {exc.code}: {detail}") from exc

        data: dict[str, Any] = json.loads(body)
        all_locations.extend(data.get("value", []))
        next_url = data.get("nextLink")

    return all_locations


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
    result: list[SubscriptionZoneMapping] = []

    for sub in subscriptions:
        try:
            # DefaultAzureCredential caches tokens internally and refreshes
            # when they expire, so calling get_token() per subscription is safe.
            token = credential.get_token(_ARM_SCOPE).token
            locations = _list_locations_raw(token, sub.subscription_id)

            for loc in locations:
                az_mappings = loc.get("availabilityZoneMappings") or []
                for mapping in az_mappings:
                    physical = mapping.get("physicalZone", "")
                    # physicalZone looks like "eastus-az1" or "centralus-1".
                    # Extract only the trailing digit(s) for the Physical Zone
                    # column; keep the full value in physical_zone_name.
                    _m = re.search(r"\d+$", physical)
                    physical_zone_num = _m.group() if _m else physical
                    result.append(
                        SubscriptionZoneMapping(
                            tenant_id=sub.tenant_id,
                            subscription_id=sub.subscription_id,
                            subscription_name=sub.subscription_name,
                            location=loc.get("name", ""),
                            logical_zone=mapping.get("logicalZone", ""),
                            physical_zone=physical_zone_num,
                            physical_zone_name=physical,
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            console.print(
                f"[yellow]Warning:[/yellow] Could not retrieve zone mappings for "
                f"subscription {sub.subscription_id!r}: {exc}"
            )

    return result
