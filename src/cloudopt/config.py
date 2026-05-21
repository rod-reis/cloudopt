"""Interactive threshold configuration via Rich prompts."""

from __future__ import annotations

from rich.console import Console
from rich.prompt import FloatPrompt

from cloudopt.models import CollectionThresholds

console = Console()


def prompt_thresholds(lookback_days: int = 30) -> CollectionThresholds:
    """Interactively prompt the CSA/customer for recommendation thresholds.

    Each prompt shows the default value; pressing Enter accepts it.
    ``lookback_days`` is the metric collection window; it is stored on the
    returned thresholds so detectors can contextualise rationale text.
    """
    defaults = CollectionThresholds()

    console.print()
    console.rule("[bold cyan]Recommendation Thresholds[/bold cyan]")
    console.print(
        "[dim]Press Enter to accept each default, or type a custom value.[/dim]\n"
    )

    underutilized_cpu_avg = FloatPrompt.ask(
        "  Underutilized CPU threshold (avg %)",
        default=defaults.underutilized_cpu_avg,
        console=console,
    )
    underutilized_memory_avg = FloatPrompt.ask(
        "  Underutilized Memory threshold (avg %)",
        default=defaults.underutilized_memory_avg,
        console=console,
    )
    oversize_cpu_p95 = FloatPrompt.ask(
        "  Oversized CPU threshold (P95 %)",
        default=defaults.oversize_cpu_p95,
        console=console,
    )
    headroom_multiplier = FloatPrompt.ask(
        "  Right-size headroom multiplier",
        default=defaults.headroom_multiplier,
        console=console,
    )
    paas_candidate_cpu_avg = FloatPrompt.ask(
        "  PaaS candidate CPU threshold (avg %)",
        default=defaults.paas_candidate_cpu_avg,
        console=console,
    )
    quota_alert_pct = FloatPrompt.ask(
        "  Quota alert threshold (utilization %)",
        default=defaults.quota_alert_pct,
        console=console,
    )

    thresholds = CollectionThresholds(
        underutilized_cpu_avg=underutilized_cpu_avg,
        underutilized_memory_avg=underutilized_memory_avg,
        oversize_cpu_p95=oversize_cpu_p95,
        headroom_multiplier=headroom_multiplier,
        paas_candidate_cpu_avg=paas_candidate_cpu_avg,
        quota_alert_pct=quota_alert_pct,
        lookback_days=lookback_days,
    )

    console.print()
    console.print("[green]✓[/green] Thresholds configured.\n")
    return thresholds
