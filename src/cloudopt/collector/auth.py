"""Azure authentication helpers using DefaultAzureCredential."""

from __future__ import annotations

from dataclasses import dataclass

from azure.identity import DefaultAzureCredential
from azure.mgmt.subscription import SubscriptionClient
from rich.console import Console

console = Console()


@dataclass(frozen=True)
class SubscriptionInfo:
    subscription_id: str
    subscription_name: str
    tenant_id: str = ""


def build_credential(tenant_id: str | None = None) -> DefaultAzureCredential:
    """Return a DefaultAzureCredential, optionally pinned to a single tenant.

    When ``tenant_id`` is provided every credential source that accepts a
    tenant override (Azure CLI, PowerShell, Interactive, Environment) is
    constrained to it.  Other sources fall back to their default tenant.
    """
    if tenant_id:
        return DefaultAzureCredential(
            shared_cache_tenant_id=tenant_id,
            visual_studio_code_tenant_id=tenant_id,
            interactive_browser_tenant_id=tenant_id,
            workload_identity_tenant_id=tenant_id,
        )
    return DefaultAzureCredential()


def list_subscriptions(
    credential: DefaultAzureCredential,
    filter_ids: list[str] | None = None,
    tenant_id: str | None = None,
) -> list[SubscriptionInfo]:
    """Return accessible subscriptions, optionally filtered.

    Filtering is applied in this order: Tenant -> Subscriptions.

    ``filter_ids`` may contain bare GUIDs or full ``/subscriptions/<guid>``
    paths; matching is case-insensitive on the GUID.  Raises typer.Exit if
    no subscriptions remain after filtering.
    """
    import typer

    client = SubscriptionClient(credential)
    subs: list[SubscriptionInfo] = []

    # Normalise filter list to lowercased GUIDs
    wanted: set[str] | None = None
    if filter_ids:
        from cloudopt.scope import parse_subscription_id
        wanted = set()
        for raw in filter_ids:
            try:
                wanted.add(parse_subscription_id(raw))
            except ValueError:
                console.print(
                    f"[yellow]Warning:[/yellow] ignoring invalid subscription id "
                    f"{raw!r}"
                )

    tid = (tenant_id or "").strip().lower() or None

    for sub in client.subscriptions.list():
        if sub.state and sub.state.lower() != "enabled":
            continue
        sub_guid = (sub.subscription_id or "").lower()
        sub_tenant = (sub.tenant_id or "").lower() if hasattr(sub, "tenant_id") else ""
        if tid and sub_tenant and sub_tenant != tid:
            continue
        if wanted is not None and sub_guid not in wanted:
            continue
        subs.append(
            SubscriptionInfo(
                subscription_id=sub.subscription_id or "",
                subscription_name=sub.display_name or sub.subscription_id or "",
                tenant_id=sub_tenant,
            )
        )

    if not subs:
        console.print(
            "[red]Error:[/red] No accessible Azure subscriptions found. "
            "Check your credentials and permissions."
        )
        raise typer.Exit(code=1)

    if filter_ids:
        from cloudopt.scope import parse_subscription_id
        wanted_set = set()
        for raw in filter_ids:
            try:
                wanted_set.add(parse_subscription_id(raw))
            except ValueError:
                pass
        found_ids = {s.subscription_id.lower() for s in subs}
        missing = wanted_set - found_ids
        if missing:
            console.print(
                f"[yellow]Warning:[/yellow] {len(missing)} subscription ID(s) not found "
                f"or not accessible: {', '.join(sorted(missing))}"
            )

    return subs
