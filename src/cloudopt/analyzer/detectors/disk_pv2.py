"""SWP-DST-002 detector — Premium SSD v1 → Premium SSD v2 modernization.

Fires for **attached data disks** provisioned on Premium SSD v1
(``Premium_LRS``) and recommends moving them to Premium SSD v2
(``PremiumV2_LRS``).

Efficiency rationale (NOT cost):
    Premium SSD v1 couples a disk's baseline IOPS and throughput to its
    capacity P-tier — to reach a performance target you must over-provision
    capacity (e.g. a P30 1-TiB disk just to obtain 5,000 IOPS).  Premium
    SSD v2 **decouples** the three dimensions: capacity (1-GiB granularity),
    IOPS (3,000 → 80,000), and throughput (125 → 1,200 MBps) are each
    provisioned independently.  Modernizing therefore:
      * releases the *stranded capacity* that exists only to satisfy a
        performance-tier minimum, restoring a true capacity-vs-demand signal;
      * removes the size-for-IOPS coupling so the workload's performance
        headroom is set explicitly rather than as a side effect of size.

Scope gates (conservative — avoids false positives):
    * SKU is ``Premium_LRS`` (Pv1).
    * Data disk only (``osType`` empty) — Pv2 cannot back an OS disk.
    * Disk is ``Attached`` to a VM (``managedBy`` set) — unattached Pv1 is a
      cleanup concern (CLN-DSK-001), not a modernization one.

Confidence: MEDIUM by default (structural ARG signal).  Per-disk IOPS /
throughput saturation telemetry is the blocker to HIGH — it confirms the
exact performance envelope to provision on Pv2.
"""

from __future__ import annotations

from typing import Optional

from cloudopt.analyzer.confidence import score as _confidence_score
from cloudopt.analyzer.taxonomy import Category, FindingType, Readiness, SubCategory
from cloudopt.models import DiskInventory, Finding

_CODE = "SWP-DST-002"
_CURRENT_SKU = "Premium_LRS"
_PROPOSED_SKU = "PremiumV2_LRS"

# Pv2 free baseline (no extra capacity required to obtain it).
_PV2_BASELINE_IOPS = 3000
_PV2_BASELINE_MBPS = 125


def detect(disks: Optional[list[DiskInventory]]) -> list[Finding]:
    """Emit SWP-DST-002 Findings for attached Premium SSD v1 data disks."""
    if not disks:
        return []
    out: list[Finding] = []
    for disk in disks:
        finding = _evaluate(disk)
        if finding is not None:
            out.append(finding)
    return out


def _evaluate(disk: DiskInventory) -> Optional[Finding]:
    if not disk.is_premium_v1:
        return None
    if not disk.is_data_disk:
        return None  # Pv2 cannot back an OS disk
    if not _is_attached(disk):
        return None  # only modernize live, attached data disks

    scored = _confidence_score(None, Category.SWAP, code=_CODE)
    blockers = list(scored.blockers_to_high)
    blockers.append(
        "Supply per-disk IOPS and throughput consumed-percentage telemetry "
        "(e.g. Azure Monitor 'Data Disk IOPS/Bandwidth Consumed Percentage' "
        "per LUN, or a guest/APM export) to confirm the exact performance "
        "envelope to provision on Premium SSD v2 and unlock HIGH confidence."
    )

    return Finding(
        vm_id=disk.resource_id,
        category=Category.SWAP,
        subcategory=SubCategory.DISK_TIER,
        code=_CODE,
        finding_type=FindingType.RECOMMENDATION,
        current=_CURRENT_SKU,
        proposed=_PROPOSED_SKU,
        deltas=_deltas(disk),
        confidence=scored.confidence,
        confidence_score=scored.confidence_score,
        readiness=Readiness.LIKELY,
        evidence_sources=["arg-disk-properties", *scored.evidence_sources],
        blockers_to_high=blockers,
        customer_inputs_needed=_migration_inputs(disk),
        rationale=_rationale(disk),
    )


def _is_attached(disk: DiskInventory) -> bool:
    state_attached = (disk.disk_state or "").strip().lower() == "attached"
    return bool(disk.managed_by) and (state_attached or disk.disk_state is None)


def _deltas(disk: DiskInventory) -> dict:
    return {
        "signal": "premium-v1-data-disk",
        "performance_tier": disk.performance_tier,
        "disk_size_gb": disk.disk_size_gb,
        "provisioned_iops": disk.disk_iops_read_write,
        "provisioned_mbps": disk.disk_mbps_read_write,
        "pv2_baseline_iops": _PV2_BASELINE_IOPS,
        "pv2_baseline_mbps": _PV2_BASELINE_MBPS,
    }


def _rationale(disk: DiskInventory) -> str:
    size = f"{disk.disk_size_gb} GiB" if disk.disk_size_gb is not None else "an unknown size"
    tier = disk.performance_tier or "its current P-tier"
    iops = disk.disk_iops_read_write
    mbps = disk.disk_mbps_read_write
    perf = ""
    if iops is not None and mbps is not None:
        perf = (
            f" Its provisioned performance ({iops:,} IOPS / {mbps:,} MBps) is "
            "fixed by that tier rather than set to the workload's actual demand."
        )
    return (
        f"This {size} Premium SSD v1 data disk is on {tier}, which couples its "
        "baseline IOPS and throughput to capacity." + perf +
        " Premium SSD v2 decouples capacity (1-GiB granularity), IOPS "
        f"(baseline {_PV2_BASELINE_IOPS:,}, up to 80,000) and throughput "
        f"(baseline {_PV2_BASELINE_MBPS} MBps, up to 1,200) so each is "
        "provisioned independently. Modernizing releases capacity that exists "
        "only to reach a performance minimum and lets performance headroom be "
        "set explicitly. Validate the migration prerequisites below before "
        "scheduling the snapshot-based move."
    )


def _migration_inputs(disk: DiskInventory) -> list[str]:
    inputs = [
        "Premium SSD v2 requires zonal placement; confirm the target disk and "
        "its VM share the same availability zone "
        f"(disk zones: {disk.zones or 'none/regional'}).",
        "Premium SSD v2 does not support host caching; confirm the data disk's "
        "cache setting is None before migrating.",
        "Migration is snapshot-based (create snapshot → new Pv2 disk → swap "
        "attachment), not an in-place SKU change; plan a brief detach window.",
    ]
    if disk.managed_by_extended:
        inputs.append(
            "This disk is shared (multi-attach); validate all attached VMs "
            "support the Premium SSD v2 shared-disk configuration."
        )
    return inputs
