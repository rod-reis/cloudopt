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
"""

from __future__ import annotations

from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from cloudopt.collector.auth import SubscriptionInfo
from cloudopt.models import QuotaItem

console = Console()


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
            work.append((sub_id, region))

    if not work:
        console.print(
            "[yellow]Warning:[/yellow] No (subscription, region) pairs to query for quota. "
            "Specify --regions or ensure the VM inventory is non-empty."
        )
        return []

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
