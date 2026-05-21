"""Workload archetype classifier (Phase 4, SPEC §4.1 / §4.2).

Classifies each VM's workload behaviour from hourly CPU time-series data and
infers a workload role from the VM name and tags for use as corroboration
evidence in confidence scoring.

Archetype rules (evaluated in priority order):
  dev-test-irregular: ≥20% of hourly CPU readings are near-zero (<1%) AND CV>0.8
  bursty:             P95/P50 > 2.5  AND  CV ≥ 0.5
  spiky:              P99/P95 > 1.8
  business-hours:     weekday-work-hour mean / off-hour mean > 3.0
  weekend-idle:       weekday mean / weekend mean > 4.0
  steady-24x7:        CV < 0.2
  unknown:            insufficient data or none of the above

Public API:
  classify_archetype(ts_values, ts_timestamps) -> WorkloadArchetype
  infer_workload_role(vm_name, tags)            -> Optional[str]
  enrich_vm_archetype(vms, metrics, ...)        -> None  (mutates in place)
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime, timezone
from typing import Optional

from cloudopt.models import (
    AppInsightsMetrics,
    VmInventory,
    VmMetrics,
    WorkloadArchetype,
)

# ---------------------------------------------------------------------------
# Archetype thresholds
# ---------------------------------------------------------------------------

_MIN_TS_POINTS = 48          # require ≥48 hourly points (~2 days) to classify
_DEV_TEST_ZERO_THRESHOLD = 1.0    # CPU% below this counts as "near-zero"
_DEV_TEST_ZERO_FRACTION = 0.20    # ≥20% zero readings → candidate for dev-test
_DEV_TEST_CV_MIN = 0.8            # AND high CV

_BURSTY_P95_P50_RATIO = 2.5
_BURSTY_CV_MIN = 0.5

_SPIKY_P99_P95_RATIO = 1.8

_BIZ_HOURS_RATIO = 3.0       # peak-hours mean / off-hours mean
_BIZ_HOURS_START = 8         # 08:00 UTC
_BIZ_HOURS_END = 18          # 18:00 UTC (exclusive)

_WEEKEND_RATIO = 4.0         # weekday mean / weekend mean

_STEADY_CV_MAX = 0.2

# ---------------------------------------------------------------------------
# Workload-role inference dictionary (name/tag keywords)
# ---------------------------------------------------------------------------

_ROLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(sql|mssql|sqlserver|sql-server)\b", re.I), "sql"),
    (re.compile(r"\b(postgres|postgresql|pg)\b", re.I), "postgres"),
    (re.compile(r"\b(mysql|mariadb)\b", re.I), "mysql"),
    (re.compile(r"\b(mongodb|mongo)\b", re.I), "mongodb"),
    (re.compile(r"\b(redis|valkey)\b", re.I), "redis"),
    (re.compile(r"\b(kafka|broker)\b", re.I), "kafka"),
    (re.compile(r"\b(nginx|haproxy|envoy|gateway)\b", re.I), "nginx"),
    (re.compile(r"\b(iis|aspnet|asp-net)\b", re.I), "iis"),
    (re.compile(r"\b(tomcat|jboss|wildfly|payara)\b", re.I), "tomcat"),
    (re.compile(r"\b(jenkins|ci)\b", re.I), "jenkins"),
    (re.compile(r"\b(ado|azure-devops|azdo|build-agent|build-vm)\b", re.I), "ado-agent"),
    (re.compile(r"\b(gh-runner|github-runner|actions-runner)\b", re.I), "gh-runner"),
    (re.compile(r"\b(aks|kubernetes|k8s|node-pool)\b", re.I), "aks-node"),
]

# Tag keys to check for workload role hints
_ROLE_TAG_KEYS: tuple[str, ...] = ("workload-class", "workload_class", "role", "application", "app")

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = (pct / 100) * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] * (1 - (idx - lo)) + sorted_vals[hi] * (idx - lo)


def _parse_hour_weekday(date_str: str) -> tuple[int, int] | None:
    """Return (weekday, hour) from an ISO-8601 timestamp string, or None."""
    try:
        if date_str.endswith("Z"):
            date_str = date_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(date_str).astimezone(timezone.utc)
        return dt.weekday(), dt.hour  # weekday: 0=Mon … 6=Sun
    except (ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# Public: archetype classifier
# ---------------------------------------------------------------------------


def classify_archetype(
    ts_values: list[float],
    ts_timestamps: list[str],
) -> WorkloadArchetype:
    """Classify the workload archetype from a CPU hourly time series.

    Args:
        ts_values:     Hourly CPU % readings (same order as ts_timestamps).
        ts_timestamps: ISO-8601 timestamp strings matching each value.

    Returns:
        A WorkloadArchetype enum value.
    """
    if len(ts_values) < _MIN_TS_POINTS:
        return WorkloadArchetype.UNKNOWN

    vals = [v for v in ts_values if v is not None]
    if len(vals) < _MIN_TS_POINTS:
        return WorkloadArchetype.UNKNOWN

    mean = sum(vals) / len(vals)
    if mean <= 0:
        return WorkloadArchetype.UNKNOWN

    std = statistics.pstdev(vals)
    cv = std / mean if mean > 0 else 0.0

    sorted_vals = sorted(vals)
    p50 = _percentile(sorted_vals, 50)
    p95 = _percentile(sorted_vals, 95)
    p99 = _percentile(sorted_vals, 99)

    # Rule 1 — dev-test-irregular: many near-zero readings AND high CV
    near_zero_count = sum(1 for v in vals if v < _DEV_TEST_ZERO_THRESHOLD)
    if (
        near_zero_count / len(vals) >= _DEV_TEST_ZERO_FRACTION
        and cv >= _DEV_TEST_CV_MIN
    ):
        return WorkloadArchetype.DEV_TEST_IRREGULAR

    # Rule 2 — bursty: high P95/P50 ratio AND high CV
    if p50 > 0 and (p95 / p50) > _BURSTY_P95_P50_RATIO and cv >= _BURSTY_CV_MIN:
        return WorkloadArchetype.BURSTY

    # Rule 3 — spiky: extreme P99/P95 spike
    if p95 > 0 and (p99 / p95) > _SPIKY_P99_P95_RATIO:
        return WorkloadArchetype.SPIKY

    # Rules 4 & 5 require timestamp parsing
    parsed = [_parse_hour_weekday(ts) for ts in ts_timestamps]
    paired = [(wh, v) for wh, v in zip(parsed, vals) if wh is not None]

    if paired:
        # Rule 4 — business-hours
        work_vals = [v for (wd, hr), v in paired if wd < 5 and _BIZ_HOURS_START <= hr < _BIZ_HOURS_END]
        off_vals = [v for (wd, hr), v in paired if not (wd < 5 and _BIZ_HOURS_START <= hr < _BIZ_HOURS_END)]
        work_mean = sum(work_vals) / len(work_vals) if work_vals else 0.0
        off_mean = sum(off_vals) / len(off_vals) if off_vals else 0.0
        if off_mean > 0 and work_mean / off_mean > _BIZ_HOURS_RATIO:
            return WorkloadArchetype.BUSINESS_HOURS

        # Rule 5 — weekend-idle
        weekday_vals = [v for (wd, _hr), v in paired if wd < 5]
        weekend_vals = [v for (wd, _hr), v in paired if wd >= 5]
        wd_mean = sum(weekday_vals) / len(weekday_vals) if weekday_vals else 0.0
        we_mean = sum(weekend_vals) / len(weekend_vals) if weekend_vals else 0.0
        if we_mean > 0 and wd_mean / we_mean > _WEEKEND_RATIO:
            return WorkloadArchetype.WEEKEND_IDLE

    # Rule 6 — steady-24x7: very low CV
    if cv < _STEADY_CV_MAX:
        return WorkloadArchetype.STEADY_24X7

    return WorkloadArchetype.UNKNOWN


# ---------------------------------------------------------------------------
# Public: workload role inference
# ---------------------------------------------------------------------------


def infer_workload_role(
    vm_name: str,
    tags: dict,
) -> Optional[str]:
    """Infer a workload role from VM name and tags (corroboration only).

    Returns a short role label (e.g. "sql", "nginx") or None.
    The role is *never* used as a primary trigger — only as a corroboration
    signal in the evidence panel and as a +5 confidence bonus.
    """
    # Check tag values first (more reliable than name heuristics)
    for key in _ROLE_TAG_KEYS:
        value = tags.get(key) or tags.get(key.lower()) or ""
        if value:
            for pattern, role in _ROLE_PATTERNS:
                if pattern.search(value):
                    return role

    # Fall back to VM name
    for pattern, role in _ROLE_PATTERNS:
        if pattern.search(vm_name):
            return role

    return None


# ---------------------------------------------------------------------------
# Public: App Insights SLO corroboration
# ---------------------------------------------------------------------------

_AI_AVAIL_METRIC = "availabilityResults/availabilityPercentage"
_AI_DURATION_METRIC = "requests/duration"
_AI_AVAIL_SLO_PCT = 99.9
_AI_DURATION_MULTIPLIER = 1.2  # p95 < baseline × 1.2; without a baseline, use absolute cap
_AI_DURATION_ABS_CAP_MS = 2000.0  # fallback: p95 < 2 s considered healthy


def _ai_component_healthy(ai_metrics: list[AppInsightsMetrics]) -> bool:
    """Return True if an App Insights component meets availability SLO.

    Availability p99 ≥ 99.9 % is required. Request duration is checked if
    available; if absent, the availability check alone is sufficient.
    """
    avail_metric = next((m for m in ai_metrics if m.metric_name == _AI_AVAIL_METRIC), None)
    if avail_metric is None:
        return False
    avail_p99 = avail_metric.p99 or avail_metric.avg or 0.0
    if avail_p99 < _AI_AVAIL_SLO_PCT:
        return False

    dur_metric = next((m for m in ai_metrics if m.metric_name == _AI_DURATION_METRIC), None)
    if dur_metric is not None:
        dur_p95 = dur_metric.p95 or dur_metric.avg or 0.0
        if dur_p95 > _AI_DURATION_ABS_CAP_MS:
            return False

    return True


def build_appinsights_corroboration(
    ai_metrics_by_resource: dict[str, list[AppInsightsMetrics]],
    vms: list[VmInventory],
) -> dict[str, int]:
    """Return a per-VM corroboration count based on App Insights SLO health.

    Looks for a healthy App Insights component in the same resource group as
    each VM.  Returns a dict keyed by ``vm.resource_id`` → corroboration int
    (0 or 1).
    """
    # Build resource_group → healthy bool from App Insights data
    # Group metrics by resource_id first, then find the resource_group from VMs
    healthy_rgs: set[str] = set()
    for ai_resource_id, ai_metrics in ai_metrics_by_resource.items():
        if _ai_component_healthy(ai_metrics):
            # Extract resource group from the App Insights resource ID
            parts = ai_resource_id.lower().split("/")
            try:
                rg_idx = parts.index("resourcegroups") + 1
                healthy_rgs.add(parts[rg_idx])
            except (ValueError, IndexError):
                pass

    result: dict[str, int] = {}
    for vm in vms:
        rg = vm.resource_group.lower()
        result[vm.resource_id] = 1 if rg in healthy_rgs else 0
    return result


# ---------------------------------------------------------------------------
# Public: enrich VMs (mutates in place)
# ---------------------------------------------------------------------------


def enrich_vm_archetype(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    *,
    ai_metrics_by_resource: Optional[dict[str, list[AppInsightsMetrics]]] = None,
) -> None:
    """Populate workload_archetype, inferred_workload_role, appinsights_corroboration.

    Mutates each VmInventory in place.  Call before running detectors.
    """
    # Build metrics lookup
    cpu_by_vm: dict[str, VmMetrics] = {}
    for m in metrics:
        if m.metric_name == "Percentage CPU":
            cpu_by_vm[m.resource_id] = m

    # Build App Insights corroboration map
    if ai_metrics_by_resource:
        ai_corr = build_appinsights_corroboration(ai_metrics_by_resource, vms)
    else:
        ai_corr = {}

    for vm in vms:
        # Archetype
        cpu_m = cpu_by_vm.get(vm.resource_id)
        if cpu_m and cpu_m.time_series:
            ts_values = [pt.value for pt in cpu_m.time_series]
            ts_timestamps = [pt.date for pt in cpu_m.time_series]
            vm.workload_archetype = classify_archetype(ts_values, ts_timestamps)
        else:
            vm.workload_archetype = WorkloadArchetype.UNKNOWN

        # Workload role (from raw_properties tags if available, else fallback to empty dict)
        tags = vm.raw_properties.get("tags") or {}
        vm.inferred_workload_role = infer_workload_role(vm.vm_name, tags)

        # App Insights corroboration
        vm.appinsights_corroboration = ai_corr.get(vm.resource_id, 0)
