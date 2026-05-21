"""Shared helpers used by multiple detector modules.

All functions here are verbatim ports from ``recommendations.py`` (Step 1 → Step 2
refactor per SPEC §11.2) with no threshold or heuristic changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from cloudopt.analyzer.confidence import score as _confidence_score
from cloudopt.analyzer.taxonomy import (
    Category,
    Confidence,
    FindingType,
    Readiness,
)
from cloudopt.enrichment.schema import EnrichedVmMetrics, MonitoringConfidence
from cloudopt.models import (
    MemoryQuality,
    VmInventory,
    VmMetrics,
)


def _rec_kwargs(
    enriched: Optional[EnrichedVmMetrics] = None,
    category: Optional[Category] = None,
    code: Optional[str] = None,
    *,
    coverage_pct: Optional[float] = None,
    stability_cv: Optional[float] = None,
    corroboration_sources: int = 0,
    high_change_impact: bool = False,
) -> dict:
    """Return confidence-scored keyword defaults for a RECOMMENDATION Finding.

    Args:
        enriched:               Best ``EnrichedVmMetrics`` for the VM / workload group, or
                                ``None`` when no monitoring export matched this VM.
        category:               Finding category used to determine whether the signal is
                                authoritative (CLEANUP / QUOTA / RSVP / CRR / DECOM) or
                                metric-dependent (RIGHTSIZE / SWAP).
        code:                   Finding code (e.g. "DCM-IDL-001") for code-level base
                                score overrides.
        coverage_pct:           Fraction of the lookback window with data (0–100).
        stability_cv:           Coefficient of variation of the primary metric series.
        corroboration_sources:  Number of independent sources agreeing with the finding.
        high_change_impact:     True when the VM has high change-impact risk (−10 penalty).
    """
    scored = _confidence_score(
        enriched,
        category or Category.RIGHTSIZE,
        code=code,
        coverage_pct=coverage_pct,
        stability_cv=stability_cv,
        corroboration_sources=corroboration_sources,
        high_change_impact=high_change_impact,
    )
    return {
        "finding_type": FindingType.RECOMMENDATION,
        "confidence": scored.confidence,
        "confidence_score": scored.confidence_score,
        "readiness": Readiness.LIKELY,
        "evidence_sources": scored.evidence_sources,
        "blockers_to_high": scored.blockers_to_high,
        "customer_inputs_needed": [],
        "deltas": {},
    }


def _candidate_kwargs(
    enriched: Optional[EnrichedVmMetrics] = None,
    category: Optional[Category] = None,
    code: Optional[str] = None,
) -> dict:
    """Return confidence-scored keyword defaults for a CANDIDATE Finding."""
    scored = _confidence_score(
        enriched,
        category or Category.RIGHTSIZE,
        code=code,
    )
    return {
        "finding_type": FindingType.CANDIDATE,
        "confidence": None,
        "confidence_score": scored.confidence_score,
        "readiness": Readiness.DISCOVERY,
        "evidence_sources": scored.evidence_sources,
        "blockers_to_high": [],
        "customer_inputs_needed": [],
        "deltas": {},
    }


# ---------------------------------------------------------------------------
# Workload grouping (verbatim port from recommendations.py)
# ---------------------------------------------------------------------------


@dataclass
class _WorkloadGroup:
    parent_id: str
    parent_type: str
    parent_name: str
    region: str
    subscription_id: str
    subscription_name: str
    resource_group: str
    members: list[VmInventory] = field(default_factory=list)

    @property
    def is_aggregated(self) -> bool:
        return (
            len(self.members) > 1
            or self.parent_type != "Microsoft.Compute/virtualMachines"
        )

    @property
    def representative_sku(self) -> str:
        skus = [m.vm_sku for m in self.members if m.vm_sku]
        if not skus:
            return ""
        return max(set(skus), key=skus.count)

    @property
    def representative_vcpus(self) -> int:
        vcpus = [m.vcpus for m in self.members if m.vcpus]
        return max(vcpus) if vcpus else 0

    @property
    def representative_memory_gb(self) -> float:
        mem = [m.memory_gb for m in self.members if m.memory_gb]
        return max(mem) if mem else 0.0


def _detect_parent(vm: VmInventory) -> tuple[str, str, str]:
    """Return (parent_id, parent_type, parent_name) for a VM."""
    sub = vm.subscription_id
    rg = vm.resource_group

    if vm.vmss_name:
        return (
            f"/subscriptions/{sub}/resourceGroups/{rg}/providers/"
            f"Microsoft.Compute/virtualMachineScaleSets/{vm.vmss_name}",
            "Microsoft.Compute/virtualMachineScaleSets",
            vm.vmss_name,
        )
    if vm.availability_set_name:
        return (
            f"/subscriptions/{sub}/resourceGroups/{rg}/providers/"
            f"Microsoft.Compute/availabilitySets/{vm.availability_set_name}",
            "Microsoft.Compute/availabilitySets",
            vm.availability_set_name,
        )
    if rg.lower().startswith("databricks-rg-"):
        ws_name = rg[len("databricks-rg-"):].split("-")[0] or rg
        return (
            f"/subscriptions/{sub}/resourceGroups/{rg}/providers/"
            f"Microsoft.Databricks/workspaces/{ws_name}",
            "Microsoft.Databricks/workspaces",
            ws_name,
        )
    if vm.image_sku and "avd" in vm.image_sku.lower():
        return (
            f"/subscriptions/{sub}/resourceGroups/{rg}/providers/"
            f"Microsoft.DesktopVirtualization/hostpools/{rg}",
            "Microsoft.DesktopVirtualization/hostpools",
            rg,
        )
    return (vm.resource_id, "Microsoft.Compute/virtualMachines", vm.vm_name)


def _build_workload_groups(vms: list[VmInventory]) -> list[_WorkloadGroup]:
    groups: dict[str, _WorkloadGroup] = {}
    for vm in vms:
        pid, ptype, pname = _detect_parent(vm)
        g = groups.get(pid)
        if g is None:
            g = _WorkloadGroup(
                parent_id=pid,
                parent_type=ptype,
                parent_name=pname,
                region=vm.region,
                subscription_id=vm.subscription_id,
                subscription_name=vm.subscription_name,
                resource_group=vm.resource_group,
            )
            groups[pid] = g
        g.members.append(vm)
    return list(groups.values())


# ---------------------------------------------------------------------------
# Enrichment helpers
# ---------------------------------------------------------------------------

_TIER_ORDER = {
    MonitoringConfidence.WORKLOAD_AWARE: 2,
    MonitoringConfidence.OS_AWARE: 1,
    MonitoringConfidence.PLATFORM_ONLY: 0,
}


def _best_enriched(
    members: list[VmInventory],
    enriched_map: Optional[dict[str, EnrichedVmMetrics]],
) -> Optional[EnrichedVmMetrics]:
    """Return the highest-confidence ``EnrichedVmMetrics`` for a workload group.

    For workload groups with multiple members (VMSS, AvailabilitySet, etc.) we
    pick the VM whose enrichment data has the highest confidence tier.  This is
    conservative — a single OS-aware VM in a group upgrades the whole group to
    os-aware, which is the right call because the missing VMs are likely
    identical instances.
    """
    if not enriched_map:
        return None
    best: Optional[EnrichedVmMetrics] = None
    best_rank = -1
    for vm in members:
        candidate = enriched_map.get(vm.resource_id)
        if candidate is None:
            continue
        rank = _TIER_ORDER.get(candidate.confidence_tier, 0)
        if rank > best_rank:
            best = candidate
            best_rank = rank
    return best


# ---------------------------------------------------------------------------
# Metric helpers (verbatim port from recommendations.py)
# ---------------------------------------------------------------------------


def _group_metrics(metrics: list[VmMetrics]) -> dict[str, dict[str, VmMetrics]]:
    result: dict[str, dict[str, VmMetrics]] = {}
    for m in metrics:
        result.setdefault(m.resource_id, {})[m.metric_name] = m
    return result


def _stat(
    vm_met: dict[str, VmMetrics],
    metric_name: str,
    stat: str,
) -> Optional[float]:
    m = vm_met.get(metric_name)
    if m is None:
        return None
    return getattr(m, stat, None)


def _mean(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _mem_utilization_pct(avail_bytes: Optional[float], total_gb: float) -> Optional[float]:
    if avail_bytes is None or total_gb <= 0:
        return None
    avail_gb = avail_bytes / (1024 ** 3)
    used_pct = (1 - avail_gb / total_gb) * 100
    return max(0.0, min(100.0, used_pct))


def _get_mem_pct(
    vm: VmInventory,
    vm_met: dict[str, VmMetrics],
    enriched: Optional[EnrichedVmMetrics],
) -> tuple[Optional[float], str]:
    """Return (memory_used_pct, data_source) for one VM.

    Prefers OS-agent ``os.memory.used_percent`` when available; falls back to
    the Azure Monitor "Available Memory Bytes" host metric.
    """
    if enriched is not None:
        dp = enriched.get("os.memory.used_percent")
        if dp is not None and dp.avg_value is not None:
            return dp.avg_value, "os-agent"
    mem_avail = _stat(vm_met, "Available Memory Bytes", "avg")
    return _mem_utilization_pct(mem_avail, vm.memory_gb), "platform"


# ---------------------------------------------------------------------------
# Legacy SKU detection (verbatim port from recommendations.py)
# ---------------------------------------------------------------------------

_GP_LEGACY_RE = re.compile(r"^Standard_(?:A|D|B|DC)\w*?_v[123]$", re.IGNORECASE)
_OTHER_LEGACY_RE = re.compile(r"^Standard_(?:E|F|G|H|L|M|N)\w*?_v[12]$", re.IGNORECASE)
_NO_VERSION_RE = re.compile(r"^Standard_(?:A|D|F|G)\d", re.IGNORECASE)


def _is_legacy_sku(sku: str) -> bool:
    if not sku:
        return False
    if _GP_LEGACY_RE.match(sku):
        return True
    if _OTHER_LEGACY_RE.match(sku):
        return True
    if _NO_VERSION_RE.match(sku) and "_v" not in sku.lower():
        return True
    return False


def _modern_replacement(sku: str) -> str:
    s = sku.lower()
    if "standard_d" in s or s.startswith("standard_b"):
        return "Standard_Dv5 / Dsv5 / Ddsv5 family"
    if "standard_e" in s:
        return "Standard_Ev5 / Esv5 / Edsv5 family"
    if "standard_f" in s:
        return "Standard_Fsv2 family"
    if "standard_a" in s:
        return "Standard_Dv5 / Dsv5 family (general purpose)"
    return "current-generation family in same workload class"


# ---------------------------------------------------------------------------
# Family-swap heuristic (verbatim port from recommendations.py)
# ---------------------------------------------------------------------------

_MEM_BOUND_MEM_PCT = 70.0
_MEM_BOUND_CPU_PCT = 25.0
_CPU_BOUND_CPU_PCT = 70.0
_CPU_BOUND_MEM_PCT = 25.0


def _suggest_family_swap(
    sku: str,
    cpu_pct: float,
    mem_pct: float,
) -> Optional[tuple[str, str]]:
    """Return (target_family_letter, signal_label) or None.

    signal_label is "memory-bound" or "compute-bound" — used in rationale text.
    Only applies to Standard_D* series (port 1:1 from recommendations.py).
    """
    if not sku.lower().startswith("standard_d"):
        return None
    if mem_pct >= _MEM_BOUND_MEM_PCT and cpu_pct < _MEM_BOUND_CPU_PCT:
        return ("E", "memory-bound")
    if cpu_pct >= _CPU_BOUND_CPU_PCT and mem_pct < _CPU_BOUND_MEM_PCT:
        return ("F", "compute-bound")
    return None


# ---------------------------------------------------------------------------
# Network utilization helpers
# ---------------------------------------------------------------------------

# Bytes per Mbit (1 Mbit = 125 000 bytes)
_BYTES_PER_MBIT = 125_000.0


def _network_util_pct(
    network_out_avg_bytes: Optional[float],
    bandwidth_mbps: float,
    interval_seconds: float = 3600.0,
) -> Optional[float]:
    """Return outbound network utilization as a percentage of SKU bandwidth.

    ``network_out_avg_bytes`` is the Azure Monitor "Network Out Total" **average**
    (bytes per interval).  ``bandwidth_mbps`` is the SKU's max outbound bandwidth
    in Mbps from the SKU catalog.  ``interval_seconds`` must match the collection
    interval (3600 for PT1H, 86400 for P1D).

    Returns None when either input is unavailable or bandwidth is zero.
    """
    if network_out_avg_bytes is None or bandwidth_mbps <= 0:
        return None
    capacity_bytes_per_interval = bandwidth_mbps * _BYTES_PER_MBIT * interval_seconds
    if capacity_bytes_per_interval <= 0:
        return None
    return min(100.0, (network_out_avg_bytes / capacity_bytes_per_interval) * 100.0)


# ---------------------------------------------------------------------------
# Time-series windowing helpers
# ---------------------------------------------------------------------------

import statistics as _statistics


def _ts_values(vm_met: dict[str, VmMetrics], metric_name: str) -> list[float]:
    """Return all time-series point values for a metric, or empty list."""
    m = vm_met.get(metric_name)
    if m is None:
        return []
    return [pt.value for pt in m.time_series]


def _ts_p100_last_n_days(
    vm_met: dict[str, VmMetrics],
    metric_name: str,
    n_days: int,
) -> Optional[float]:
    """Return the maximum (P100) of the last ``n_days`` of hourly values.

    Time-series points are sorted by their ISO timestamp string; the last
    ``n_days * 24`` entries correspond to the most recent window.
    """
    m = vm_met.get(metric_name)
    if m is None or not m.time_series:
        return None
    pts = sorted(m.time_series, key=lambda p: p.date)
    window = pts[-(n_days * 24):]
    if not window:
        return None
    return max(pt.value for pt in window)


def _ts_std(vm_met: dict[str, VmMetrics], metric_name: str) -> Optional[float]:
    """Return the population standard deviation of all time-series values."""
    vals = _ts_values(vm_met, metric_name)
    if len(vals) < 2:
        return None
    return _statistics.pstdev(vals)


# ---------------------------------------------------------------------------
# User-facing workload classification
# ---------------------------------------------------------------------------

# CloudFit uses a Microsoft Research method based on sub-minute CPU patterns.
# With hourly data we approximate via the coefficient of variation (CV = σ/μ).
# A high CV relative to the mean indicates a bursty / user-facing profile.
_USER_FACING_CV_THRESHOLD = 0.5   # CV >= 0.5 AND p95 > 2×avg → classify as user-facing
_USER_FACING_P95_RATIO = 2.0      # p95 must be at least 2× the mean to confirm burstiness


def _is_user_facing(
    vm_met: dict[str, VmMetrics],
) -> bool:
    """Return True when the CPU utilisation pattern suggests a user-facing workload.

    Heuristic: high coefficient of variation in hourly CPU AND P95 >> average.
    This approximates (with lower resolution) the Microsoft Research method used
    by CloudFit which operates on 30-minute samples.
    """
    vals = _ts_values(vm_met, "Percentage CPU")
    if len(vals) < 4:
        return False  # insufficient data — default to non-user-facing (conservative)
    mean = sum(vals) / len(vals)
    if mean <= 0:
        return False
    std = _statistics.pstdev(vals)
    cv = std / mean
    sorted_vals = sorted(vals)
    p95 = _percentile(sorted_vals, 95)
    return cv >= _USER_FACING_CV_THRESHOLD and p95 >= mean * _USER_FACING_P95_RATIO


def _percentile(sorted_values: list[float], pct: int) -> float:
    """Return the p-th percentile of a sorted list (linear interpolation)."""
    if not sorted_values:
        return 0.0
    idx = (pct / 100) * (len(sorted_values) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


# ---------------------------------------------------------------------------
# B-series burstable baseline table
# ---------------------------------------------------------------------------

# Baseline CPU % for B-series SKUs: the fraction of a vCPU's capacity that
# the VM can use sustainably before drawing from accumulated credits.
# Source: https://learn.microsoft.com/azure/virtual-machines/bsv2-series
# Keyed by vCPU count; covers the common B-series variants.
_BSERIES_BASELINE_PCT: dict[int, float] = {
    1:  10.0,   # B1s, B1ls, B1ms
    2:  40.0,   # B2s, B2ms, B2als_v2, B2as_v2
    4:  40.0,   # B4ms, B4als_v2, B4as_v2
    8:  40.0,   # B8ms, B8als_v2, B8as_v2
    16: 40.0,   # B16ms, B16als_v2, B16as_v2
    20: 40.0,   # B20ms
    32: 40.0,   # B32als_v2, B32as_v2
}

_BSERIES_RE = re.compile(r"^Standard_B\d", re.IGNORECASE)


def _is_bseries_sku(sku: str) -> bool:
    return bool(_BSERIES_RE.match(sku))


def _bseries_baseline_pct(vcpus: int) -> float:
    """Return the burstable baseline CPU % for a given vCPU count."""
    return _BSERIES_BASELINE_PCT.get(vcpus, 40.0)


def _bseries_credits_sufficient(
    cpu_avg_pct: float,
    vcpus: int,
    lookback_days: int,
) -> bool:
    """Return True when B-series CPU credits are sufficient over the lookback window.

    Credits accrue at (baseline_pct - avg_pct) * vcpus per hour when below
    baseline, and deplete when above.  If the long-run average is below baseline,
    credits are net-positive and the workload is sustainable on B-series.
    """
    baseline = _bseries_baseline_pct(vcpus)
    return cpu_avg_pct < baseline


# ---------------------------------------------------------------------------
# Phase 3 — Memory quality helpers (SPEC §3.2 / §3.3)
# ---------------------------------------------------------------------------

_AMA_TOOL = "ama"
_VMINSIGHTS_CLASSIC_TOOL = "vminsights-classic"


def _resolve_memory_quality(
    vm: VmInventory,
    vm_met: dict[str, VmMetrics],
    enriched: Optional[EnrichedVmMetrics],
) -> MemoryQuality:
    """Determine memory data quality per SPEC §3.3 source priority.

    Priority (highest → lowest): ama > vminsights-classic > customer > platform.
    Returns MISSING when no memory data is available at all.
    """
    if enriched is not None and enriched.has_os_data:
        tool = enriched.source_tool.lower()
        if tool == _AMA_TOOL:
            return MemoryQuality.AMA
        if tool == _VMINSIGHTS_CLASSIC_TOOL:
            return MemoryQuality.VMINSIGHTS_CLASSIC
        return MemoryQuality.CUSTOMER
    if _stat(vm_met, "Available Memory Bytes", "avg") is not None:
        return MemoryQuality.PLATFORM
    return MemoryQuality.MISSING


def _compute_mem_pressure_score(
    vm: VmInventory,
    vm_met: dict[str, VmMetrics],
) -> Optional[float]:
    """Return mem_pressure_score = 1 − (available_min_bytes / total_memory_bytes).

    Uses the *minimum* of ``Available Memory Bytes`` as the worst-case memory
    pressure indicator.  Clamped to [0.0, 1.0].  Returns None when the metric
    or VM memory spec is unavailable.
    """
    avail_min = _stat(vm_met, "Available Memory Bytes", "min")
    if avail_min is None or vm.memory_gb <= 0:
        return None
    total_bytes = vm.memory_gb * (1024 ** 3)
    pressure = 1.0 - (avail_min / total_bytes)
    return max(0.0, min(1.0, pressure))


def _compute_memory_disagreement(
    vm: VmInventory,
    vm_met: dict[str, VmMetrics],
    enriched: Optional[EnrichedVmMetrics],
) -> Optional[float]:
    """Return |platform_mem_pct − customer_mem_pct| when it exceeds 10 %, else None.

    Used to flag potential data-quality issues when two sources disagree on
    memory utilisation by more than the 10 % threshold defined in SPEC §3.3.
    """
    if enriched is None:
        return None
    os_dp = enriched.get("os.memory.used_percent")
    if os_dp is None or os_dp.avg_value is None:
        return None
    avail_avg = _stat(vm_met, "Available Memory Bytes", "avg")
    platform_pct = _mem_utilization_pct(avail_avg, vm.memory_gb)
    if platform_pct is None:
        return None
    diff = abs(platform_pct - os_dp.avg_value)
    return diff if diff > 10.0 else None


def enrich_vm_memory_quality(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    enriched_map: Optional[dict[str, EnrichedVmMetrics]] = None,
) -> None:
    """Populate memory_quality, mem_pressure_score, memory_disagreement_pct on each VM.

    Mutates each VmInventory in place.  Must be called before running detectors
    so that ``memory_quality`` is available for gating and confidence scoring.
    """
    metrics_by_vm = _group_metrics(metrics)
    for vm in vms:
        vm_met = metrics_by_vm.get(vm.resource_id, {})
        enriched = enriched_map.get(vm.vm_name) if enriched_map else None
        if enriched is None and enriched_map:
            enriched = enriched_map.get(vm.resource_id)
        vm.memory_quality = _resolve_memory_quality(vm, vm_met, enriched)
        vm.mem_pressure_score = _compute_mem_pressure_score(vm, vm_met)
        vm.memory_disagreement_pct = _compute_memory_disagreement(vm, vm_met, enriched)
