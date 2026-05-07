"""CLI entry point for CLOUDOPT."""

from __future__ import annotations

import asyncio
import datetime
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from cloudopt import __version__

app = typer.Typer(
    name="cloudopt",
    help="CLOUDOPT — read-only Azure VM and App Insights capacity analysis.",
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_subscription_ids_from_file(path: Path) -> list[str]:
    """Read subscription IDs from a text file.

    Format: one subscription ID per line.  Lines starting with '#' and blank
    lines are ignored.  Leading/trailing whitespace is stripped.
    """
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            ids.append(stripped)
    return ids


def _print_pre_execution_summary(
    target_subs,
    resource_counts: dict,
    metrics_days: int,
    output_dir: Path,
    concurrency: int,
    arm_rate: float,
    regions: list[str] | None = None,
) -> None:
    """Print a rich summary table and prompt for confirmation before running."""
    console.print()
    console.rule("[bold cyan]CLOUDOPT — Pre-run Summary[/bold cyan]")
    console.print()

    # ── Services & Metrics ────────────────────────────────────────────────
    svc_table = Table(
        title="[bold]Services & Metrics to Collect[/bold]",
        show_header=True,
        header_style="bold magenta",
        show_lines=True,
        expand=False,
    )
    svc_table.add_column("Service", style="cyan", min_width=24)
    svc_table.add_column("Metrics", min_width=60)

    svc_table.add_row(
        "Azure Virtual Machines",
        "CPU % (avg/P95/max), Available Memory Bytes,\n"
        "Disk Read/Write Bytes/sec, Disk Read/Write IOPS,\n"
        "Network In/Out Total Bytes",
    )
    svc_table.add_row(
        "Application Insights\n(Standard)",
        "Availability %, Request Count, Request Duration (ms),\n"
        "Failed Requests, Exception Count, Server Exceptions,\n"
        "Process CPU %, Process Private Bytes,\n"
        "Available Memory Bytes, Processor CPU %,\n"
        "Process IO Bytes/sec",
    )
    svc_table.add_row(
        "Application Insights\n(JVM — workspace-linked)",
        "JVM Heap Used / Committed / Max (bytes),\n"
        "JVM Non-Heap Used (bytes),\n"
        "JVM GC Pause (ms), JVM GC Count,\n"
        "JVM Thread Count",
    )
    console.print(svc_table)
    console.print()

    # ── Resources discovered ──────────────────────────────────────────────
    res_table = Table(
        title="[bold]Resources Discovered[/bold]",
        show_header=True,
        header_style="bold magenta",
        show_lines=True,
        expand=False,
    )
    res_table.add_column("Subscription", style="cyan", min_width=32)
    res_table.add_column("VMs", justify="right", min_width=7)
    res_table.add_column("App Insights", justify="right", min_width=12)

    total_vms = 0
    total_ai = 0
    for sub in target_subs:
        sid = sub.subscription_id
        counts = resource_counts.get(sid, {"vms": 0, "appinsights": 0})
        vms_n = counts["vms"]
        ai_n = counts["appinsights"]
        total_vms += vms_n
        total_ai += ai_n
        masked = f"{sid[:8]}…"
        res_table.add_row(f"{sub.subscription_name} ({masked})", str(vms_n), str(ai_n))

    res_table.add_section()
    res_table.add_row("[bold]TOTAL[/bold]", f"[bold]{total_vms}[/bold]", f"[bold]{total_ai}[/bold]")
    console.print(res_table)
    console.print()

    # ── Processing & output ───────────────────────────────────────────────
    console.print(f"  [bold]Metrics period:[/bold]  {metrics_days} day(s)")
    if regions:
        console.print(f"  [bold]Region filter:[/bold]   {', '.join(regions)}")
    else:
        console.print("  [bold]Region filter:[/bold]   [dim]all regions[/dim]")
    console.print(f"  [bold]Concurrency:[/bold]     max {concurrency} concurrent API calls per subscription")
    console.print(
        f"  [bold]ARM rate cap:[/bold]    {arm_rate:.1f} req/s per subscription "
        "[dim](token bucket; halves on 429)[/dim]"
    )
    console.print(
        f"  [bold]Processing:[/bold]     sequential — 1 subscription at a time "
        f"(batches of {concurrency} VMs / {concurrency} App Insights components)"
    )
    console.print()
    console.print("  [bold]Output will be written to:[/bold]")
    console.print(f"    JSON  : [cyan]{output_dir}/cloudopt_export_<timestamp>.json[/cyan]")
    console.print(f"    Excel : [cyan]{output_dir}/cloudopt_report_<timestamp>.xlsx[/cyan] (via analyze)")
    console.print()


def _confirm_or_exit() -> None:
    """Ask the user to confirm before running.  Exits on 'no'."""
    answer = typer.prompt(
        "Proceed with collection?",
        default="Y",
        show_default=True,
    )
    if answer.strip().lower() not in ("y", "yes", ""):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------

@app.command()
def collect(
    tenant_id: Annotated[
        Optional[str],
        typer.Option(
            "--tenant-id",
            "-t",
            help=(
                "Microsoft Entra (Azure AD) tenant GUID. When set, only "
                "subscriptions in this tenant are considered and the "
                "credential is pinned to it."
            ),
        ),
    ] = None,
    config_file: Annotated[
        Optional[Path],
        typer.Option(
            "--config-file",
            "-c",
            help=(
                "Path to a WARA-style scope text file with "
                "[tenantid] / [subscriptionids] / [locations] / "
                "[resourcegroups] / [tags] / [metricdays] / [concurrency] / "
                "[output] sections. CLI flags override values loaded from "
                "this file."
            ),
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    subscriptions: Annotated[
        Optional[list[str]],
        typer.Option(
            "--subscriptions",
            "-s",
            help=(
                "Subscription IDs to target. Accepts bare GUIDs or full "
                "'/subscriptions/<guid>' paths. Repeatable. Omit to use "
                "all accessible subscriptions in the tenant."
            ),
        ),
    ] = None,
    subscriptions_file: Annotated[
        Optional[Path],
        typer.Option(
            "--subscriptions-file",
            "-f",
            help=(
                "Path to a text file containing subscription IDs, one per line. "
                "Lines starting with '#' are treated as comments. "
                "Useful when targeting hundreds of subscriptions."
            ),
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    regions: Annotated[
        Optional[list[str]],
        typer.Option(
            "--regions",
            "--locations",
            "-r",
            help=(
                "ARM region name(s) to target, e.g. 'eastus' or 'westeurope'. "
                "Repeatable. Acts as a GLOBAL filter applied to every "
                "downstream query (inventory, App Insights, Advisor, quota). "
                "Omit to collect all regions."
            ),
        ),
    ] = None,
    resource_groups: Annotated[
        Optional[list[str]],
        typer.Option(
            "--resource-groups",
            "-g",
            help=(
                "Full ARM resource-group IDs to target, e.g. "
                "'/subscriptions/<guid>/resourceGroups/RG1'. Repeatable. "
                "Each RG must reference a subscription that is in scope."
            ),
        ),
    ] = None,
    tags: Annotated[
        Optional[list[str]],
        typer.Option(
            "--tags",
            help=(
                "Tag filter expression(s). Repeatable. Operators: "
                "'||' = OR, '=~' = equals, '!~' = not equals. "
                "Example: --tags 'Environment||Env=~Prod||Production' "
                "--tags 'Owner!~Bill'. Tags are used in-memory only and "
                "are NEVER persisted to the output workbook or JSON."
            ),
        ),
    ] = None,
    metrics_days: Annotated[
        int,
        typer.Option(
            "--metrics-days",
            "-d",
            help="Number of days of metrics history to collect (1–90).",
            min=1,
            max=90,
        ),
    ] = 30,
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Directory to write output files.",
        ),
    ] = Path("output"),
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="List discovered VMs without collecting metrics.",
        ),
    ] = False,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            help=(
                "Maximum concurrent in-flight Azure Monitor calls per "
                "subscription. Hard ceiling on parallelism. The dominant "
                "control is now --arm-rate (RPS); raise --concurrency only "
                "if you also raise the rate."
            ),
            min=1,
            max=100,
        ),
    ] = 25,
    arm_rate: Annotated[
        float,
        typer.Option(
            "--arm-rate",
            help=(
                "Target ARM read requests-per-second per subscription. "
                "Default 20 RPS stays well within ARM's per-subscription "
                "read budget (~12,000/hour with bursts up to 250). Raise "
                "if you have many VMs and few subscriptions; lower if you "
                "hit 429s. See: https://learn.microsoft.com/azure/azure-resource-manager/management/request-limits-and-throttling"
            ),
            min=1.0,
            max=100.0,
        ),
    ] = 20.0,
) -> None:
    """Collect VM and Application Insights capacity data from Azure.

    Filtering order applied to every collected resource:

        Tenant -> Subscriptions -> Locations -> ResourceGroups -> Tags

    A pre-run summary is shown for confirmation.  Subscriptions are
    processed one at a time; VMs and App Insights components are processed
    in bounded batches of ``--concurrency`` per subscription.
    """
    from cloudopt.config import prompt_thresholds
    from cloudopt.collector.advisor import collect_advisor_sku_recommendations
    from cloudopt.collector.auth import build_credential, list_subscriptions
    from cloudopt.collector.inventory import collect_inventory, count_resources_by_type
    from cloudopt.collector.metrics import collect_metrics
    from cloudopt.collector.quota import collect_quota, sub_regions_from_vms
    from cloudopt.collector.zones import collect_zone_mappings
    from cloudopt.collector.appinsights import (
        collect_appinsights_inventory,
        collect_appinsights_metrics,
    )
    from cloudopt.analyzer.sku_catalog import SkuCatalog
    from cloudopt.export.json_export import write_json
    from cloudopt.scope import (
        ScopeFilter,
        build_scope,
        scope_from_config_file,
    )

    console.rule("[bold cyan]CLOUDOPT[/bold cyan]")
    console.print(f"[dim]Version {__version__}[/dim]\n")

    # ── Resolve scope (configfile + CLI overrides) ───────────────────────
    file_scope: ScopeFilter | None = None
    if config_file is not None:
        try:
            file_scope = scope_from_config_file(config_file)
        except Exception as exc:
            console.print(f"[red]Error:[/red] failed to parse {config_file}: {exc}")
            raise typer.Exit(code=1)
        console.print(f"[dim]  Loaded scope from {config_file}[/dim]")

    # CLI flag values override the file; pass through file values otherwise.
    eff_tenant = tenant_id or (file_scope.tenant_id if file_scope else None)

    cli_sub_strings: list[str] = list(subscriptions) if subscriptions else []
    if subscriptions_file is not None:
        cli_sub_strings.extend(_load_subscription_ids_from_file(subscriptions_file))
        console.print(
            f"[dim]  Loaded subscription IDs from {subscriptions_file}[/dim]"
        )
    file_subs: list[str] = list(file_scope.subscription_ids) if file_scope else []
    eff_subs: list[str] = cli_sub_strings or file_subs

    cli_regions = list(regions) if regions else []
    file_regions = list(file_scope.locations) if file_scope else []
    eff_regions = cli_regions or file_regions

    cli_rgs = list(resource_groups) if resource_groups else []
    file_rgs = (
        [
            f"/subscriptions/{r.subscription_id}/resourceGroups/{r.name}"
            for r in file_scope.resource_groups
        ]
        if file_scope
        else []
    )
    eff_rgs = cli_rgs or file_rgs

    cli_tags = list(tags) if tags else []
    file_tags_raw: list[str] = []  # tag filters in file → reserialise as raw strings
    if file_scope:
        for tf in file_scope.tag_filters:
            op = "=~" if tf.equals else "!~"
            file_tags_raw.append(
                "||".join(tf.names) + op + "||".join(tf.values)
            )
    eff_tags = cli_tags or file_tags_raw

    eff_metric_days = metrics_days
    if metrics_days == 30 and file_scope and file_scope.metric_days:
        eff_metric_days = file_scope.metric_days

    eff_concurrency = concurrency
    if concurrency == 25 and file_scope and file_scope.concurrency:
        eff_concurrency = file_scope.concurrency

    eff_arm_rate = arm_rate
    if arm_rate == 20.0 and file_scope and file_scope.arm_rate:
        eff_arm_rate = file_scope.arm_rate

    eff_output_dir = output_dir
    if output_dir == Path("output") and file_scope and file_scope.output_dir:
        eff_output_dir = file_scope.output_dir

    try:
        scope = build_scope(
            tenant_id=eff_tenant,
            subscriptions=eff_subs,
            locations=eff_regions,
            resource_groups=eff_rgs,
            tags=eff_tags,
            metric_days=eff_metric_days,
            concurrency=eff_concurrency,
            arm_rate=eff_arm_rate,
            output_dir=eff_output_dir,
        )
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    eff_output_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Auth ──────────────────────────────────────────────────────────
    console.print("[bold]Step 1:[/bold] Authenticating to Azure…")
    credential = build_credential(tenant_id=scope.tenant_id)

    target_subs = list_subscriptions(
        credential,
        list(scope.subscription_ids) or None,
        tenant_id=scope.tenant_id,
    )
    console.print(
        f"[green]✓[/green] {len(target_subs)} subscription(s) targeted.\n"
    )

    # ── 2. Pre-execution summary ─────────────────────────────────────────
    console.print("[bold]Step 2:[/bold] Counting resources across subscriptions…")
    resource_counts = count_resources_by_type(credential, target_subs, scope=scope)
    console.print(f"[green]✓[/green] Resource counts ready.\n")

    _print_pre_execution_summary(
        target_subs=target_subs,
        resource_counts=resource_counts,
        metrics_days=eff_metric_days,
        output_dir=eff_output_dir,
        concurrency=eff_concurrency,
        arm_rate=eff_arm_rate,
        regions=list(scope.locations) or None,
    )
    if scope.has_resource_group_filter:
        console.print(
            f"  [bold]Resource Groups:[/bold] {len(scope.resource_groups)} explicit RG filter(s)"
        )
    if scope.has_tag_filter:
        console.print(
            f"  [bold]Tag filters:[/bold]    {len(scope.tag_filters)} expression(s) "
            "[dim](in-memory only — never written)[/dim]"
        )
    console.print()

    if not dry_run:
        _confirm_or_exit()
        console.print()

    # ── 3. VM Inventory ──────────────────────────────────────────────────
    console.print("[bold]Step 3:[/bold] Collecting VM inventory via Resource Graph…")
    sku_catalog = SkuCatalog(credential)
    vms = collect_inventory(credential, target_subs, sku_catalog, scope=scope)
    console.print(f"[green]✓[/green] {len(vms)} VM(s) discovered.\n")

    if dry_run:
        console.print("[yellow]--dry-run: skipping metrics, recommendations, and export.[/yellow]")
        for vm in vms:
            console.print(
                f"  [dim]{vm.subscription_name}[/dim] / {vm.resource_group} / "
                f"[bold]{vm.vm_name}[/bold] ({vm.vm_sku})"
            )
        raise typer.Exit()

    # ── 4. Thresholds ────────────────────────────────────────────────────
    console.print("[bold]Step 4:[/bold] Configure recommendation and quota thresholds.")
    thresholds = prompt_thresholds()

    # ── 5. VM Metrics ────────────────────────────────────────────────────
    console.print("[bold]Step 5:[/bold] Collecting VM performance metrics from Azure Monitor…")
    console.print(
        "[dim]  Processing subscriptions sequentially; "
        f"batches of {eff_concurrency} VMs per subscription.[/dim]"
    )
    all_metrics = asyncio.run(
        collect_metrics(
            credential=credential,
            vms=vms,
            days=eff_metric_days,
            concurrency=eff_concurrency,
            arm_rate=eff_arm_rate,
            checkpoint_path=eff_output_dir / ".checkpoint.json",
        )
    )
    console.print(
        f"[green]✓[/green] Metrics collected for {len(all_metrics)} metric series.\n"
    )

    # ── 6. App Insights ──────────────────────────────────────────────────
    console.print("[bold]Step 6:[/bold] Collecting Application Insights inventory…")
    ai_components = collect_appinsights_inventory(credential, target_subs, scope=scope)
    console.print(f"[green]✓[/green] {len(ai_components)} App Insights component(s) discovered.\n")

    ai_metrics: list = []
    if ai_components:
        console.print(
            "[bold]Step 6b:[/bold] Collecting App Insights metrics "
            "(standard + JVM if workspace-linked)…"
        )
        console.print(
            "[dim]  Processing subscriptions sequentially; "
            f"batches of {eff_concurrency} components per subscription.[/dim]"
        )
        ai_metrics = asyncio.run(
            collect_appinsights_metrics(
                credential=credential,
                components=ai_components,
                days=eff_metric_days,
                concurrency=eff_concurrency,
                arm_rate=eff_arm_rate,
            )
        )
        jvm_count = sum(1 for m in ai_metrics if m.category.startswith("jvm"))
        console.print(
            f"[green]✓[/green] {len(ai_metrics)} App Insights metric series collected "
            f"({jvm_count} JVM).\n"
        )
    else:
        console.print("[dim]  No App Insights components found — skipping.[/dim]\n")

    # ── 7. Quota ─────────────────────────────────────────────────────────
    console.print("[bold]Step 7:[/bold] Collecting quota utilisation…")

    # Quota covers ALL in-scope subscriptions — not only those with VMs.
    # When the scope file lists only [resourcegroups] (no [subscriptionids]),
    # derive the target subscription IDs from the resource group references.
    quota_sub_ids = set(scope.quota_subscription_ids)
    quota_target_subs = (
        [s for s in target_subs if s.subscription_id.lower() in quota_sub_ids]
        if quota_sub_ids
        else target_subs
    )

    quota_items = collect_quota(
        credential=credential,
        subscriptions=quota_target_subs,
        scope=scope,
        vms_sub_regions=sub_regions_from_vms(vms),
        quota_alert_pct=thresholds.quota_alert_pct,
    )
    quota_alerts = sum(1 for q in quota_items if q.alert)
    console.print(
        f"[green]✓[/green] {len(quota_items)} quota entries collected "
        f"({quota_alerts} alert(s) above {thresholds.quota_alert_pct:.0f}%).\n"
    )

    # ── 7b. Advisor SKU-change recommendations ───────────────────────────
    console.print("[bold]Step 7b:[/bold] Collecting Azure Advisor SKU-change recommendations…")
    advisor_recs = collect_advisor_sku_recommendations(
        credential=credential,
        subscriptions=target_subs,
        scope=scope,
    )
    console.print(
        f"[green]✓[/green] {len(advisor_recs)} Advisor SKU-change recommendation(s) collected.\n"
    )

    # ── 7c. Subscription availability-zone mappings ──────────────────────
    console.print("[bold]Step 7c:[/bold] Collecting subscription availability-zone mappings…")
    zone_maps = collect_zone_mappings(credential=credential, subscriptions=target_subs)
    console.print(
        f"[green]✓[/green] {len(zone_maps)} zone mapping row(s) collected.\n"
    )

    # ── 8. Export ────────────────────────────────────────────────────────
    from cloudopt.models import (
        CollectionMetadata,
        WorkloadInfo,
        mask_subscription_id,
    )

    recommendations: list = []  # Left blank for CSA to fill in manually

    metadata = CollectionMetadata(
        run_date=datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        tool_version=__version__,
        subscriptions_scanned=[mask_subscription_id(s.subscription_id) for s in target_subs],
        metrics_period_days=eff_metric_days,
        total_vm_count=len(vms),
        total_appinsights_count=len(ai_components),
        thresholds=thresholds,
    )

    workload_info = WorkloadInfo()  # blank — CSA fills in alongside the customer

    _ts = datetime.datetime.now().strftime("%Y-%m-%d_%H_%M")
    json_path = eff_output_dir / f"cloudopt_export_{_ts}.json"

    write_json(
        vms, all_metrics, recommendations, metadata, json_path,
        quota=quota_items,
        appinsights=ai_components,
        appinsights_metrics=ai_metrics,
        advisor=advisor_recs,
        workload_info=workload_info,
        zone_mappings=zone_maps,
    )

    # Delete the checkpoint file now that data is safely written to JSON.
    # If it persists, the next collect run skips all VMs (they're already in
    # completed_ids) and produces a JSON with empty metrics.
    _ckpt = eff_output_dir / ".checkpoint.json"
    if _ckpt.exists():
        _ckpt.unlink(missing_ok=True)

    console.rule("[bold green]Collection complete[/bold green]")
    console.print(f"  JSON  : [cyan]{json_path}[/cyan]")
    console.print(
        "\nGenerate the Excel workbook with: [bold]CLOUDOPT analyze "
        f"--from {json_path}[/bold]\n"
    )


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------

@app.command()
def analyze(
    from_file: Annotated[
        Path,
        typer.Option(
            "--from",
            help="Path to the JSON file produced by the 'collect' command.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    output_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--output-dir",
            "-o",
            help=(
                "Directory to write the Excel workbook into. "
                "Defaults to the same directory as the input JSON file."
            ),
        ),
    ] = None,
) -> None:
    """Generate an Excel workbook from a collected JSON file.

    Separates collection (run by the customer or via Cloud Shell) from
    analysis (run by the Microsoft engineer with openpyxl installed locally).

    Example workflow:

    \b
        # Customer collects data (no Excel dependency required):
        CLOUDOPT collect --config-file scope.txt

        # Customer shares cloudopt_export_<timestamp>.json with the Microsoft engineer.

        # MS engineer generates the workbook:
        CLOUDOPT analyze --from cloudopt_export_<timestamp>.json
    """
    import json
    from cloudopt.export.excel import write_workbook
    from cloudopt.models import (
        AdvisorRecommendation,
        AppInsightsInventory,
        AppInsightsMetrics,
        CollectionMetadata,
        CollectionThresholds,
        DailyDataPoint,
        QuotaItem,
        SubscriptionZoneMapping,
        VmInventory,
        VmMetrics,
        VmRecommendation,
        WorkloadInfo,
    )

    console.print(f"Reading [cyan]{from_file}[/cyan]…")
    try:
        raw = json.loads(from_file.read_text(encoding="utf-8"))
    except Exception as exc:
        console.print(f"[red]Error:[/red] Could not read JSON file: {exc}")
        raise typer.Exit(code=1)

    # --- VMs ---------------------------------------------------------------
    vms: list[VmInventory] = []
    for d in raw.get("vms", []):
        try:
            vms.append(VmInventory(**{k: v for k, v in d.items() if k != "subscription_id"},
                                   subscription_id=d.get("subscription_id", "")))
        except Exception:
            pass

    # --- Metrics -----------------------------------------------------------
    metrics: list[VmMetrics] = []
    for d in raw.get("metrics", []):
        try:
            ts = [DailyDataPoint(**p) for p in d.get("time_series", [])]
            metrics.append(VmMetrics(**{k: v for k, v in d.items() if k != "time_series"},
                                      time_series=ts))
        except Exception:
            pass

    # --- Recommendations (user-authored, may be empty) --------------------
    recommendations: list[VmRecommendation] = []
    for d in raw.get("recommendations", []):
        try:
            recommendations.append(VmRecommendation(**d))
        except Exception:
            pass

    # --- Advisor -----------------------------------------------------------
    advisor: list[AdvisorRecommendation] = []
    for d in raw.get("advisor", []):
        try:
            advisor.append(AdvisorRecommendation(**d))
        except Exception:
            pass

    # --- Quota -------------------------------------------------------------
    quota: list[QuotaItem] = []
    for d in raw.get("quota", []):
        try:
            quota.append(QuotaItem(subscription_id=d.get("subscription_id", ""), **{
                k: v for k, v in d.items() if k != "subscription_id"
            }))
        except Exception:
            pass

    # --- App Insights ------------------------------------------------------
    appinsights: list[AppInsightsInventory] = []
    for d in raw.get("appinsights", []):
        try:
            appinsights.append(AppInsightsInventory(**{
                k: v for k, v in d.items() if k not in ("subscription_id", "workspace_linked")
            }, subscription_id=d.get("subscription_id", "")))
        except Exception:
            pass

    appinsights_metrics: list[AppInsightsMetrics] = []
    for d in raw.get("appinsights_metrics", []):
        try:
            ts = [DailyDataPoint(**p) for p in d.get("time_series", [])]
            appinsights_metrics.append(AppInsightsMetrics(
                **{k: v for k, v in d.items() if k != "time_series"},
                time_series=ts,
            ))
        except Exception:
            pass

    # --- Workload info -----------------------------------------------------
    wi_raw = raw.get("workload_info", {})
    try:
        workload_info = WorkloadInfo(**wi_raw) if wi_raw else WorkloadInfo()
    except Exception:
        workload_info = WorkloadInfo()

    # --- Zone mappings ----------------------------------------------------
    zone_mappings: list[SubscriptionZoneMapping] = []
    for d in raw.get("zone_mappings", []):
        try:
            zone_mappings.append(SubscriptionZoneMapping(**d))
        except Exception:
            pass

    # --- Metadata ----------------------------------------------------------
    meta_raw = raw.get("metadata", {})
    try:
        metadata = CollectionMetadata(
            run_date=meta_raw.get("run_date", ""),
            tool_version=meta_raw.get("tool_version", ""),
            subscriptions_scanned=meta_raw.get("subscriptions_scanned", []),
            metrics_period_days=meta_raw.get("metrics_period_days", 30),
            total_vm_count=meta_raw.get("total_vm_count", len(vms)),
            total_appinsights_count=meta_raw.get("total_appinsights_count", len(appinsights)),
            thresholds=CollectionThresholds(**meta_raw.get("thresholds", {})),
        )
    except Exception as exc:
        console.print(f"[yellow]Warning:[/yellow] Could not parse metadata: {exc}")
        metadata = CollectionMetadata(
            run_date="",
            tool_version="",
            subscriptions_scanned=[],
            metrics_period_days=30,
            total_vm_count=len(vms),
            total_appinsights_count=len(appinsights),
            thresholds=CollectionThresholds(),
        )

    console.print(
        f"  [green]✓[/green] {len(vms)} VM(s), {len(metrics)} metric series, "
        f"{len(quota)} quota entries, {len(appinsights)} App Insights component(s)."
    )

    # --- Write workbook ----------------------------------------------------
    out_dir = output_dir or from_file.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    # Reuse the timestamp embedded in cloudopt_export_{ts}.json; fallback to now.
    _stem = from_file.stem
    _ts = _stem[len("cloudopt_export_"):] if _stem.startswith("cloudopt_export_") else datetime.datetime.now().strftime("%Y-%m-%d_%H_%M")
    xlsx_path = out_dir / f"cloudopt_report_{_ts}.xlsx"

    console.print(f"Writing Excel workbook to [cyan]{xlsx_path}[/cyan]…")
    write_workbook(
        vms, metrics, recommendations, metadata, xlsx_path,
        quota=quota,
        appinsights=appinsights,
        appinsights_metrics=appinsights_metrics,
        advisor=advisor,
        workload_info=workload_info,
        zone_mappings=zone_mappings,
    )

    console.rule("[bold green]Analysis complete[/bold green]")
    console.print(f"  Excel : [cyan]{xlsx_path}[/cyan]")
    console.print(
        "\nStart the dashboard with: [bold]CLOUDOPT dashboard "
        f"--data {xlsx_path}[/bold]\n"
    )


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

@app.command()
def export(
    from_file: Annotated[
        Path,
        typer.Option("--from", help="Path to the Excel workbook to read."),
    ],
    to_dir: Annotated[
        Path,
        typer.Option("--to", help="Output directory for JSON and CSV files."),
    ] = Path("output"),
    fmt: Annotated[
        str,
        typer.Option("--format", "-f", help="Output format: json, csv, or all."),
    ] = "all",
) -> None:
    """Convert an existing Excel workbook to JSON and/or CSV.

    Use this after editing the workbook (overrides, notes, CSA fields) to
    produce a machine-readable version that reflects your changes.
    """
    from cloudopt.export.excel import read_workbook
    from cloudopt.export.json_export import write_json
    from cloudopt.export.csv_export import write_csv

    if not from_file.exists():
        console.print(f"[red]Error:[/red] File not found: {from_file}")
        raise typer.Exit(code=1)

    to_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"Reading [cyan]{from_file}[/cyan]…")
    data = read_workbook(from_file)

    if fmt in ("json", "all"):
        json_path = to_dir / (from_file.stem + ".json")
        write_json(*data, json_path)
        console.print(f"[green]✓[/green] JSON → {json_path}")

    if fmt in ("csv", "all"):
        write_csv(*data, to_dir)
        console.print(f"[green]✓[/green] CSV → {to_dir}/")

    console.print("Done.")


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------

@app.command()
def dashboard(
    data: Annotated[
        Path,
        typer.Option("--data", help="Path to the Excel workbook or JSON file."),
    ] = Path("output/cloudopt_report.xlsx"),  # override with timestamped file if needed
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Local port to serve the dashboard on."),
    ] = 8080,
    host: Annotated[
        str,
        typer.Option("--host", help="Host to bind to."),
    ] = "127.0.0.1",
) -> None:
    """Start the local web dashboard.

    Browse to http://localhost:<port> after running this command.
    """
    import uvicorn
    from cloudopt.dashboard.app import create_app

    if not data.exists():
        console.print(f"[red]Error:[/red] Data file not found: {data}")
        console.print(
            "Run [bold]CLOUDOPT collect[/bold] first to generate the workbook."
        )
        raise typer.Exit(code=1)

    web_app = create_app(data_path=data)
    console.print(
        f"\n[bold]Dashboard running at[/bold] [cyan]http://{host}:{port}[/cyan]"
    )
    console.print("Press [bold]Ctrl+C[/bold] to stop.\n")
    uvicorn.run(web_app, host=host, port=port, log_level="warning")


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------

@app.command()
def version() -> None:
    """Print the tool version."""
    console.print(f"CLOUDOPT [cyan]{__version__}[/cyan]")


if __name__ == "__main__":
    app()
