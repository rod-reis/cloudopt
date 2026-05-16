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
    VmInventory,
    VmMetrics,
)


def _rec_kwargs(
    enriched: Optional[EnrichedVmMetrics] = None,
    category: Optional[Category] = None,
) -> dict:
    """Return confidence-scored keyword defaults for a RECOMMENDATION Finding.

    Args:
        enriched:  Best ``EnrichedVmMetrics`` for the VM / workload group, or
                   ``None`` when no monitoring export matched this VM.
        category:  Finding category used to determine whether the signal is
                   authoritative (CLEANUP / QUOTA / RSVP / CRR / DECOM) or
                   metric-dependent (RIGHTSIZE / SWAP).
    """
    scored = _confidence_score(enriched, category or Category.RIGHTSIZE)
    return {
        "finding_type": FindingType.RECOMMENDATION,
        "confidence": scored.confidence,
        "readiness": Readiness.LIKELY,
        "evidence_sources": scored.evidence_sources,
        "blockers_to_high": scored.blockers_to_high,
        "customer_inputs_needed": [],
        "deltas": {},
    }


def _candidate_kwargs(
    enriched: Optional[EnrichedVmMetrics] = None,
    category: Optional[Category] = None,
) -> dict:
    """Return confidence-scored keyword defaults for a CANDIDATE Finding."""
    scored = _confidence_score(enriched, category or Category.RIGHTSIZE)
    return {
        "finding_type": FindingType.CANDIDATE,
        "confidence": None,
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
