"""DCM-STP-001 detector — stopped / deallocated VMs flagged for decommissioning.

Ports the RESOURCE_CLEANUP/deallocated rule from ``recommendations.py``
verbatim (SPEC §11.2.2).  Both PowerState/stopped (still billed) and
PowerState/deallocated are mapped to DCM-STP-001 per the SPEC §2.3 trigger
"Stopped (still billed)" and "Deallocated > N days".

NOTE on N-days duration: VmInventory does not carry a staleness timestamp.
Findings are emitted for any VM in a stopped/deallocated state, matching
the previous behaviour in recommendations.py (no duration threshold was
enforced there either).

Deferred (no existing logic to port):
  DCM-IDL-001 — requires metric-based idle check not present in Step 1.

New — gated behind optional flags passed to detect():
  DCM-DLC-001 — lower-env oversized (enable_dlc=True)
  DCM-ENV-001 — env-tag mismatch (enable_env_check=True)
"""

from __future__ import annotations

from cloudopt.analyzer.detectors._shared import (
    _build_workload_groups,
    _rec_kwargs,
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

_DEV_ENVS = frozenset({"dev", "development", "test", "testing", "qa", "staging"})

# VMs with this many vCPUs or more are "oversized" in a lower-env context.
_DLC_OVERSIZED_VCPUS = 16


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
    """Emit DCM-STP-001 Findings (and optionally DCM-DLC-001 / DCM-ENV-001)."""
    workloads = _build_workload_groups(vms)
    out: list[Finding] = []
    for group in workloads:
        out.extend(_evaluate(group, enable_dlc=enable_dlc, enable_env_check=enable_env_check))
    return out


def _evaluate(
    group,
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
                    f"are in a {state_label} state. Review whether these can be "
                    "permanently decommissioned to free compute quota and reduce "
                    "management overhead."
                ),
                **_rec_kwargs(),
            )
        )

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
                **_rec_kwargs(),
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
                **_rec_kwargs(),
            )
    return None
