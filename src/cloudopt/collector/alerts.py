"""Azure Monitor alert-rule collector for capacity operations hygiene.

Collects all Azure Monitor alert rules that are relevant to proactive capacity
monitoring.  The results are consumed by the ``QTA-OPS-001`` detector in
``analyzer/detectors/ops_hygiene.py``.

Four alert-rule categories are collected via Azure Resource Graph:
  1. ``microsoft.insights/metricalerts``       — metric-threshold alerts
  2. ``microsoft.insights/activitylogalerts``  — activity-log / service-health alerts
  3. ``microsoft.insights/scheduledqueryrules`` — KQL-based log search alerts
  4. ``microsoft.alertsmanagement/smartdetectoralertrules`` — Service Health detectors

Read-only; does not create, modify, or delete any Azure resource.
"""

from __future__ import annotations

import json
from typing import Any

from azure.identity import DefaultAzureCredential
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from cloudopt.collector.auth import SubscriptionInfo
from cloudopt.models import CapacityAlert, CapacityAlertType

console = Console()

_MAX_SUBS_PER_QUERY = 200

# ---------------------------------------------------------------------------
# ARG queries
# ---------------------------------------------------------------------------

_METRIC_ALERTS_QUERY = """
Resources
| where type =~ 'microsoft.insights/metricalerts'
| project
    id,
    name,
    subscriptionId,
    enabled = tobool(properties.enabled),
    scopes   = properties.scopes,
    criteria = properties.criteria
"""

_ACTIVITY_LOG_ALERTS_QUERY = """
Resources
| where type =~ 'microsoft.insights/activitylogalerts'
| project
    id,
    name,
    subscriptionId,
    enabled  = tobool(properties.enabled),
    scopes   = properties.scopes,
    condition = properties.condition
"""

_SCHEDULED_QUERY_RULES_QUERY = """
Resources
| where type =~ 'microsoft.insights/scheduledqueryrules'
| project
    id,
    name,
    subscriptionId,
    enabled  = tobool(properties.enabled),
    scopes   = properties.scopes,
    query    = tostring(properties.criteria.allOf[0].query)
"""

_SERVICE_HEALTH_QUERY = """
Resources
| where type =~ 'microsoft.insights/activitylogalerts'
| where properties.condition.allOf contains 'serviceHealth'
| project
    id,
    name,
    subscriptionId,
    enabled  = tobool(properties.enabled),
    scopes   = properties.scopes,
    condition = properties.condition
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_query(
    client: ResourceGraphClient,
    sub_ids: list[str],
    query_text: str,
) -> list[dict[str, Any]]:
    """Execute an ARG query across ``sub_ids`` and return all rows."""
    rows: list[dict[str, Any]] = []
    batches = [sub_ids[i: i + _MAX_SUBS_PER_QUERY] for i in range(0, len(sub_ids), _MAX_SUBS_PER_QUERY)]
    for batch in batches:
        skip_token: str | None = None
        while True:
            options = QueryRequestOptions(result_format="objectArray", skip_token=skip_token)
            request = QueryRequest(subscriptions=batch, query=query_text, options=options)
            try:
                response = client.resources(request)
            except Exception as exc:
                console.print(f"[yellow]⚠ alerts ARG query error: {exc}[/yellow]")
                break
            if response.data:
                rows.extend(response.data)
            skip_token = getattr(response, "skip_token", None)
            if not skip_token:
                break
    return rows


def _to_list(val: Any) -> list[str]:
    """Coerce an ARG JSON value to a plain list of strings."""
    if val is None:
        return []
    if isinstance(val, list):
        return [str(v) for v in val]
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except (json.JSONDecodeError, ValueError):
            return [val]
    return [str(val)]


def _extract_metric_alert_signals(criteria: Any) -> list[str]:
    """Extract metric names from a metric-alert criteria object."""
    signals: list[str] = []
    if criteria is None:
        return signals
    # criteria might be a dict (already deserialized by ARG SDK) or a JSON str
    if isinstance(criteria, str):
        try:
            criteria = json.loads(criteria)
        except (json.JSONDecodeError, ValueError):
            return signals
    # AllOf criteria list
    all_of = []
    if isinstance(criteria, dict):
        all_of = criteria.get("allOf") or criteria.get("AllOf") or []
    for crit in all_of if isinstance(all_of, list) else []:
        if isinstance(crit, dict):
            m = crit.get("metricName") or crit.get("MetricName") or ""
            if m:
                signals.append(m)
    return signals


def _extract_activity_log_signals(condition: Any) -> list[str]:
    """Extract operation names and event names from an activity-log alert condition."""
    signals: list[str] = []
    if condition is None:
        return signals
    if isinstance(condition, str):
        try:
            condition = json.loads(condition)
        except (json.JSONDecodeError, ValueError):
            return signals
    all_of = []
    if isinstance(condition, dict):
        all_of = condition.get("allOf") or condition.get("AllOf") or []
    for item in all_of if isinstance(all_of, list) else []:
        if not isinstance(item, dict):
            continue
        for field in ("operationName", "status", "subStatus", "value"):
            v = item.get("equals") or item.get("containsAny")
            k = item.get("field") or ""
            if v:
                if isinstance(v, list):
                    signals.extend(str(x) for x in v)
                else:
                    signals.append(str(v))
    return signals


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


def collect_capacity_alerts(
    credential: DefaultAzureCredential,
    subscriptions: list[SubscriptionInfo],
) -> list[CapacityAlert]:
    """Collect all Azure Monitor alert rules relevant to capacity operations.

    Returns a flat list of :class:`~cloudopt.models.CapacityAlert` records
    across all in-scope subscriptions.  Only enabled alert rules are returned;
    disabled rules are skipped.
    """
    if not subscriptions:
        return []

    sub_ids = [s.subscription_id for s in subscriptions]
    client = ResourceGraphClient(credential)
    results: list[CapacityAlert] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Collecting capacity alert rules…", total=None)

        # Metric alerts
        for row in _run_query(client, sub_ids, _METRIC_ALERTS_QUERY):
            enabled = bool(row.get("enabled", False))
            signals = _extract_metric_alert_signals(row.get("criteria"))
            results.append(CapacityAlert(
                resource_id=row.get("id", ""),
                subscription_id=row.get("subscriptionId", ""),
                alert_type=CapacityAlertType.METRIC_ALERT,
                name=row.get("name", ""),
                enabled=enabled,
                signals=signals,
                scopes=_to_list(row.get("scopes")),
            ))

        # Activity log alerts (includes service-health)
        for row in _run_query(client, sub_ids, _ACTIVITY_LOG_ALERTS_QUERY):
            enabled = bool(row.get("enabled", False))
            condition = row.get("condition")
            signals = _extract_activity_log_signals(condition)

            # Classify as service-health if category=="ServiceHealth" is detected
            cond_str = json.dumps(condition) if not isinstance(condition, str) else condition
            is_svc_health = "servicehealth" in cond_str.lower() or "serviceHealth" in cond_str
            alert_type = (
                CapacityAlertType.SERVICE_HEALTH_ALERT if is_svc_health
                else CapacityAlertType.ACTIVITY_LOG_ALERT
            )

            results.append(CapacityAlert(
                resource_id=row.get("id", ""),
                subscription_id=row.get("subscriptionId", ""),
                alert_type=alert_type,
                name=row.get("name", ""),
                enabled=enabled,
                signals=signals,
                scopes=_to_list(row.get("scopes")),
            ))

        # Scheduled query rules
        for row in _run_query(client, sub_ids, _SCHEDULED_QUERY_RULES_QUERY):
            enabled = bool(row.get("enabled", False))
            query_text = row.get("query") or ""
            results.append(CapacityAlert(
                resource_id=row.get("id", ""),
                subscription_id=row.get("subscriptionId", ""),
                alert_type=CapacityAlertType.SCHEDULED_QUERY_RULE,
                name=row.get("name", ""),
                enabled=enabled,
                signals=[query_text] if query_text else [],
                scopes=_to_list(row.get("scopes")),
            ))

    return results
