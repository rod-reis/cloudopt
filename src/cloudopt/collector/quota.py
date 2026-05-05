"""Azure Compute quota collection.

For each (subscription, region) pair present in the VM inventory, fetches
compute quota and current usage from the Azure Compute Usages API.  Only
entries with a non-zero quota limit are included.
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
    vms_sub_regions: dict[str, set[str]],
    quota_alert_pct: float = 80.0,
) -> list[QuotaItem]:
    """Return compute quota utilisation for every (subscription, region) that has VMs.

    Args:
        credential:       Azure credential.
        subscriptions:    List of SubscriptionInfo objects for name lookup.
        vms_sub_regions:  Mapping of subscription_id → set of region strings
                          (derived from the collected VM inventory).
        quota_alert_pct:  Threshold (0–100) above which an entry is flagged.
    """
    sub_map: dict[str, str] = {s.subscription_id: s.subscription_name for s in subscriptions}

    # Build work list: (sub_id, region) pairs
    work: list[tuple[str, str]] = [
        (sub_id, region)
        for sub_id, regions in vms_sub_regions.items()
        for region in sorted(regions)
    ]

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
                        progress.advance(task)
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
