"""DCM-STP-001 detector — stopped / deallocated VMs flagged for decommissioning.
DCM-IDL-001 detector — running VMs with no measurable utilization.

Ports the RESOURCE_CLEANUP/deallocated rule from ``recommendations.py``
verbatim (SPEC §11.2.2).  Both PowerState/stopped (still billed) and
PowerState/deallocated are mapped to DCM-STP-001 per the SPEC §2.3 trigger
"Stopped (still billed)" and "Deallocated > N days".

DCM-IDL-001 fires on running VMs that have not been meaningfully used:
  - P95 of CPU utilization < 3 %
  - P100 (max) of CPU in the last 3 days  ≤ 2 %
  - Outbound network utilization < 2 % of SKU bandwidth over the lookback

NOTE on N-days duration: VmInventory does not carry a staleness timestamp.
Findings are emitted for any VM in a stopped/deallocated state, matching
the previous behaviour in recommendations.py (no duration threshold was
enforced there either).

New — gated behind optional flags passed to detect():
  DCM-DLC-001 — lower-env oversized (enable_dlc=True)
  DCM-ENV-001 — env-tag mismatch (enable_env_check=True)
"""

from __future__ import annotations

from typing import Optional

from cloudopt.analyzer.detectors._shared import (
    _build_workload_groups,
    _group_metrics,
    _network_util_pct,
    _rec_kwargs,
    _stat,
    _ts_p100_last_n_days,
)
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.analyzer.taxonomy import Category, SubCategory
from cloudopt.models import (
    CollectionThresholds,
    Finding,
    QuotaItem,
    VmInventory,
    VmMetrics,
)

_STOPPED_STATES = frozenset({"powerstate/deallocated", "powerstate/stopped"})
_RUNNING_STATES = frozenset({"powerstate/running"})

_DEV_ENVS = frozenset({"dev", "development", "test", "testing", "qa", "staging"})

# VMs with this many vCPUs or more are "oversized" in a lower-env context.
_DLC_OVERSIZED_VCPUS = 16

# Idle-detection thresholds (mirror CloudFit Logic 1)
_IDLE_CPU_P95_PCT = 3.0       # P95 of all hourly CPU samples must be below this
_IDLE_CPU_P100_3D_PCT = 2.0   # P100 (max) of last-3-day CPU samples must be below this
_IDLE_NETWORK_PCT = 2.0       # Outbound network utilisation % of SKU bandwidth


def detect(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    quota: list[QuotaItem],
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
    *,
    enable_dlc: bool = False,
    enable_env_check: bool = False,
) -> list[Finding]:
    """Emit DCM-STP-001, DCM-IDL-001, and optionally DCM-DLC-001 / DCM-ENV-001."""
    metrics_by_vm = _group_metrics(metrics)
    workloads = _build_workload_groups(vms)
    out: list[Finding] = []
    for group in workloads:
        out.extend(
            _evaluate(
                group,
                metrics_by_vm,
                thresholds,
                catalog,
                enable_dlc=enable_dlc,
                enable_env_check=enable_env_check,
            )
        )
    return out


def _evaluate(
    group,
    metrics_by_vm: dict[str, dict[str, VmMetrics]],
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
    *,
    enable_dlc: bool,
    enable_env_check: bool,
) -> list[Finding]:
    out: list[Finding] = []
    vm_id = group.parent_id if group.is_aggregated else group.members[0].resource_id
    sku = group.representative_sku

    # DCM-STP-001: any member in a stopped or deallocated power state
    stopped = [
        m for m in group.members
        if (m.power_state or "").lower() in _STOPPED_STATES
    ]
    if stopped:
        count = len(stopped)
        total = len(group.members)
        state_label = _state_label(stopped[0].power_state or "")
        days_stopped = stopped[0].days_stopped
        if days_stopped is not None:
            days_part = (
                f" (stopped for {days_stopped} days)"
                if days_stopped <= 90
                else " (stopped for over 90 days; Activity Log lookback exceeded)"
            )
        else:
            days_part = ""
        out.append(
            Finding(
                vm_id=vm_id,
                category=Category.DECOM,
                subcategory=SubCategory.STOPPED_ALLOCATED,
                code="DCM-STP-001",
                current=sku or None,
                proposed=None,
                rationale=(
                    f"{count} of {total} VM(s) in this workload "
                    f"are in a {state_label} state{days_part}. Review whether these can be "
                    "permanently decommissioned to free compute quota and reduce "
                    "management overhead."
                ),
                **_rec_kwargs(category=Category.DECOM),
            )
        )

    # DCM-IDL-001: running VM with no measurable utilization
    # Only evaluated when the workload has at least one running member and
    # metrics data is present.
    running = [
        m for m in group.members
        if (m.power_state or "").lower() in _RUNNING_STATES
    ]
    if running and not stopped:
        idl_finding = _check_idle(group, vm_id, sku, running, metrics_by_vm, thresholds, catalog)
        if idl_finding is not None:
            out.append(idl_finding)

    # DCM-DLC-001: lower-env oversized (behind flag)
    if enable_dlc:
        dlc_finding = _check_dlc(group, vm_id, sku)
        if dlc_finding is not None:
            out.append(dlc_finding)

    # DCM-ENV-001: env-tag mismatch (behind flag)
    if enable_env_check:
        env_finding = _check_env(group, vm_id, sku)
        if env_finding is not None:
            out.append(env_finding)

    return out


def _check_idle(
    group,
    vm_id: str,
    sku: str,
    running_members: list[VmInventory],
    metrics_by_vm: dict[str, dict[str, VmMetrics]],
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
) -> Optional[Finding]:
    """Emit DCM-IDL-001 when a running VM has negligible CPU and network utilisation."""
    cpu_p95_vals: list[float] = []
    cpu_p100_3d_vals: list[float] = []
    net_util_vals: list[float] = []

    for vm in running_members:
        vm_met = metrics_by_vm.get(vm.resource_id, {})
        cpu_p95 = _stat(vm_met, "Percentage CPU", "p95")
        if cpu_p95 is not None:
            cpu_p95_vals.append(cpu_p95)

        p100_3d = _ts_p100_last_n_days(vm_met, "Percentage CPU", n_days=3)
        if p100_3d is not None:
            cpu_p100_3d_vals.append(p100_3d)

        sku_spec = catalog.get(vm.subscription_id, vm.region, vm.vm_sku)
        bw = sku_spec.network_bandwidth_mbps if sku_spec else 0.0
        net_avg = _stat(vm_met, "Network Out Total", "avg")
        net_util = _network_util_pct(net_avg, bw)
        if net_util is not None:
            net_util_vals.append(net_util)

    if not cpu_p95_vals:
        return None

    # Aggregate across instances (VMSS: average; single VM: single value)
    agg_cpu_p95 = sum(cpu_p95_vals) / len(cpu_p95_vals)
    agg_cpu_p100_3d = (sum(cpu_p100_3d_vals) / len(cpu_p100_3d_vals)) if cpu_p100_3d_vals else None
    agg_net_util = (sum(net_util_vals) / len(net_util_vals)) if net_util_vals else None

    cpu_p95_ok = agg_cpu_p95 < _IDLE_CPU_P95_PCT
    cpu_p100_3d_ok = agg_cpu_p100_3d is None or agg_cpu_p100_3d <= _IDLE_CPU_P100_3D_PCT
    net_ok = agg_net_util is None or agg_net_util < _IDLE_NETWORK_PCT

    if not (cpu_p95_ok and cpu_p100_3d_ok and net_ok):
        return None

    net_part = (
        f", outbound network {agg_net_util:.1f}% of SKU bandwidth"
        if agg_net_util is not None
        else ""
    )
    p100_part = (
        f", P100 CPU last 3 days {agg_cpu_p100_3d:.2f}%"
        if agg_cpu_p100_3d is not None
        else ""
    )
    return Finding(
        vm_id=vm_id,
        category=Category.DECOM,
        subcategory=SubCategory.IDLE,
        code="DCM-IDL-001",
        current=sku or None,
        proposed=None,
        rationale=(
            f"VM is running but shows no meaningful utilisation over the "
            f"{thresholds.lookback_days}-day lookback: P95 CPU {agg_cpu_p95:.2f}% "
            f"(threshold {_IDLE_CPU_P95_PCT}%){p100_part}{net_part}. "
            "Consider shutting down or decommissioning."
        ),
        **_rec_kwargs(category=Category.DECOM),
    )


def _state_label(power_state: str) -> str:
    ps = power_state.lower()
    if "deallocated" in ps:
        return "deallocated (not billed for compute)"
    if "stopped" in ps:
        return "stopped (still billed for compute)"
    return "stopped/deallocated"


def _check_dlc(group, vm_id: str, sku: str):
    """Emit DCM-DLC-001 if any member is in a lower-env and oversized."""
    for vm in group.members:
        env = (vm.environment or "").lower().strip()
        if env not in _DEV_ENVS:
            continue
        if vm.vcpus >= _DLC_OVERSIZED_VCPUS:
            return Finding(
                vm_id=vm_id,
                category=Category.DECOM,
                subcategory=SubCategory.LOWER_ENV_OVERPROVISIONED,
                code="DCM-DLC-001",
                current=sku or None,
                proposed=None,
                rationale=(
                    f"VM is tagged as a lower environment ({vm.environment!r}) "
                    f"but has {vm.vcpus} vCPUs — production-sized. "
                    "Consider downsizing or scheduling during business hours only."
                ),
                **_rec_kwargs(category=Category.DECOM),
            )
    return None


def _check_env(group, vm_id: str, sku: str):
    """Emit DCM-ENV-001 if any member is missing an environment tag."""
    for vm in group.members:
        if not (vm.environment or "").strip():
            return Finding(
                vm_id=vm_id,
                category=Category.DECOM,
                subcategory=SubCategory.DEALLOCATED_STALE,
                code="DCM-ENV-001",
                current=sku or None,
                proposed=None,
                rationale=(
                    "VM has no environment tag. Without a tag it is impossible to "
                    "distinguish production from non-production workloads and apply "
                    "appropriate cost controls."
                ),
                **_rec_kwargs(category=Category.DECOM),
            )
    return None
