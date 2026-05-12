"""SWP-FAM-001, SWP-LFC-001, SWP-ARC-001 detectors — SKU swap signals.

Ports the SKU_SWAP and MODERNIZATION/legacy-family rules from
``recommendations.py`` verbatim (SPEC §11.2.2).

Mapping from old umbrella codes:
  SKU_SWAP / memory-bound     → SWP-FAM-001
  SKU_SWAP / compute-bound    → SWP-FAM-001
  MODERNIZATION / legacy-family → SWP-LFC-001 (replaces old MODERNIZATION umbrella)

New in Step 2 (no existing logic to port):
  SWP-ARC-001 (CANDIDATE) — basic ARM64 eligibility heuristic

Deferred (no existing logic to port):
  SWP-GEN-001, SWP-DST-001
"""

from __future__ import annotations

import re
from typing import Optional

from cloudopt.analyzer.detectors._shared import (
    _WorkloadGroup,
    _build_workload_groups,
    _candidate_kwargs,
    _get_mem_pct,
    _group_metrics,
    _is_legacy_sku,
    _mean,
    _modern_replacement,
    _rec_kwargs,
    _stat,
    _suggest_family_swap,
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

# ARM64-eligible pattern: Standard_D/E/F with numeric size and v5+ (no 'p' = x64)
# e.g. Standard_D8s_v5, Standard_E4s_v5 — ARM64 siblings are Standard_D8ps_v5 etc.
_ARM64_CANDIDATE_RE = re.compile(
    r"^Standard_[DEF]\d+[a-z]*s?_v[56789]$",
    re.IGNORECASE,
)
_ALREADY_ARM64_RE = re.compile(r"[A-Z]\d+p", re.IGNORECASE)


def detect(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    quota: list[QuotaItem],
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
) -> list[Finding]:
    """Emit SWP-FAM-001, SWP-LFC-001, and SWP-ARC-001 Findings."""
    metrics_by_vm = _group_metrics(metrics)
    workloads = _build_workload_groups(vms)
    out: list[Finding] = []
    for group in workloads:
        out.extend(_evaluate(group, metrics_by_vm, thresholds))
    return out


def _evaluate(
    group: _WorkloadGroup,
    metrics_by_vm: dict[str, dict[str, VmMetrics]],
    thresholds: CollectionThresholds,
) -> list[Finding]:
    out: list[Finding] = []

    cpu_avgs: list[float] = []
    cpu_p95s: list[float] = []
    mem_pcts: list[float] = []

    for vm in group.members:
        vm_met = metrics_by_vm.get(vm.resource_id, {})
        cpu_avg = _stat(vm_met, "Percentage CPU", "avg")
        cpu_p95 = _stat(vm_met, "Percentage CPU", "p95")
        mem_pct, _ = _get_mem_pct(vm, vm_met, None)

        if cpu_avg is not None:
            cpu_avgs.append(cpu_avg)
        if cpu_p95 is not None:
            cpu_p95s.append(cpu_p95)
        if mem_pct is not None:
            mem_pcts.append(mem_pct)

    cpu_avg = _mean(cpu_avgs)
    cpu_p95 = max(cpu_p95s) if cpu_p95s else None
    mem_pct_avg = _mean(mem_pcts)

    sku = group.representative_sku
    vm_id = (
        group.parent_id if group.is_aggregated else group.members[0].resource_id
    )

    # SWP-FAM-001: only when no size rec would fire (port existing logic 1:1)
    would_downsize = _would_downsize(cpu_avg, cpu_p95, mem_pct_avg, thresholds)
    if (
        not would_downsize
        and cpu_avg is not None
        and mem_pct_avg is not None
    ):
        swap = _suggest_family_swap(sku, cpu_avg, mem_pct_avg)
        if swap is not None:
            target_family, signal_label = swap
            kwargs = _rec_kwargs()
            kwargs["deltas"] = {"signal": signal_label}
            out.append(
                Finding(
                    vm_id=vm_id,
                    category=Category.SWAP,
                    subcategory=SubCategory.FAMILY,
                    code="SWP-FAM-001",
                    current=sku or None,
                    proposed=f"Standard_{target_family}* (same vCPU class)",
                    rationale=(
                        f"Avg CPU {cpu_avg:.1f}% / memory {mem_pct_avg:.1f}% indicates a "
                        f"{signal_label} workload. The {target_family}-series is a better fit."
                    ),
                    **kwargs,
                )
            )

    # SWP-LFC-001: legacy SKU check — always independent of size rec
    if sku and _is_legacy_sku(sku):
        modern_target = _modern_replacement(sku)
        out.append(
            Finding(
                vm_id=vm_id,
                category=Category.SWAP,
                subcategory=SubCategory.LIFECYCLE,
                code="SWP-LFC-001",
                current=sku,
                proposed=modern_target,
                rationale=(
                    f"{sku} belongs to a previous-generation family. Newer generations "
                    "deliver better price-performance and longer support."
                ),
                **_rec_kwargs(),
            )
        )

    # SWP-ARC-001: ARM64 eligibility candidate (new in Step 2)
    arm64_finding = _check_arm64(vm_id, sku)
    if arm64_finding is not None:
        out.append(arm64_finding)

    return out


def _would_downsize(
    cpu_avg: Optional[float],
    cpu_p95: Optional[float],
    mem_pct_avg: Optional[float],
    thresholds: CollectionThresholds,
) -> bool:
    """Return True only when the rightsize underutilized rule (Rule A) would fire.

    We suppress FAM swap when the VM is genuinely underutilised (both CPU and
    memory low), because rightsize already covers that case.  We do NOT suppress
    for the p95-only oversized path because a memory- or compute-bound workload
    can still have a low p95 value if the profile is uneven.
    """
    if cpu_avg is not None and mem_pct_avg is not None:
        if (
            cpu_avg < thresholds.underutilized_cpu_avg
            and mem_pct_avg < thresholds.underutilized_memory_avg
        ):
            return True
    return False


def _check_arm64(vm_id: str, sku: str) -> Optional[Finding]:
    """Return an SWP-ARC-001 CANDIDATE Finding if the SKU is ARM64-eligible.

    Heuristic: Standard D/E/F-series v5+ without the 'p' marker are x64 SKUs
    that have an ARM64 sibling (e.g. Standard_D8s_v5 → Standard_D8ps_v5).
    """
    if not sku:
        return None
    if _ALREADY_ARM64_RE.search(sku):
        return None
    if not _ARM64_CANDIDATE_RE.match(sku):
        return None

    kwargs = _candidate_kwargs()
    kwargs["customer_inputs_needed"] = [
        "verify ARM64 binary compatibility for all installed software"
    ]
    return Finding(
        vm_id=vm_id,
        category=Category.SWAP,
        subcategory=SubCategory.ARCHITECTURE,
        code="SWP-ARC-001",
        current=sku,
        proposed=_arm64_sibling(sku),
        rationale=(
            f"{sku} is an x64 SKU in a family that has an ARM64 sibling. "
            "ARM64 delivers comparable throughput at lower cost but requires "
            "OS and application compatibility validation."
        ),
        **kwargs,
    )


def _arm64_sibling(sku: str) -> Optional[str]:
    """Best-effort ARM64 sibling name: insert 'p' before the size digits."""
    # Standard_D8s_v5 → Standard_D8ps_v5
    m = re.match(r"^(Standard_[A-Z])(\d+)", sku, re.IGNORECASE)
    if not m:
        return None
    return f"{m.group(1)}{m.group(2)}p{sku[m.end():]}"
