"""CLN-DSK-001, CLN-NIC-001, CLN-PIP-001, CLN-SNP-001 detectors — orphaned resources.

This detector operates on ``AzureResource`` objects collected by the
``resources`` collector rather than on ``VmInventory``.  The standard
``detect()`` signature is extended with an optional ``resources`` keyword
argument (SPEC §11.2.1 permits additional kwargs for detectors that need
extra inputs).

Implemented in Step 2:
  CLN-DSK-001 — unattached managed disk (managed_by empty)
  CLN-NIC-001 — NIC with no managed_by reference (best-effort proxy)
  CLN-PIP-001 — public IP with no managed_by reference (best-effort proxy)
  CLN-SNP-001 — all snapshots flagged for review (no age data in model)

Deferred (data not available in current AzureResource model):
  CLN-RGP-001 — empty resource groups (requires the full RG list)

NOTE: Finding.vm_id is used as a generic resource identifier for non-VM
findings; for cleanup findings it holds the orphaned resource's resource_id.
"""

from __future__ import annotations

from cloudopt.analyzer.detectors._shared import _rec_kwargs
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.analyzer.taxonomy import Category, SubCategory
from cloudopt.models import (
    AzureResource,
    CollectionThresholds,
    Finding,
    QuotaItem,
    VmInventory,
    VmMetrics,
)

_DISK_TYPE = "microsoft.compute/disks"
_NIC_TYPE = "microsoft.network/networkinterfaces"
_PIP_TYPE = "microsoft.network/publicipaddresses"
_SNAP_TYPE = "microsoft.compute/snapshots"


def detect(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    quota: list[QuotaItem],
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
    *,
    resources: list[AzureResource] | None = None,
) -> list[Finding]:
    """Emit CLN-* Findings for orphaned Azure resources.

    ``vms``, ``metrics``, and ``quota`` are accepted for interface uniformity
    but not used.  The caller must supply ``resources`` (the output of
    ``collect_resources()``) for any Findings to be emitted.
    """
    if not resources:
        return []
    out: list[Finding] = []
    for r in resources:
        rt = r.resource_type.lower()
        if rt == _DISK_TYPE:
            f = _check_disk(r)
        elif rt == _NIC_TYPE:
            f = _check_nic(r)
        elif rt == _PIP_TYPE:
            f = _check_pip(r)
        elif rt == _SNAP_TYPE:
            f = _check_snapshot(r)
        else:
            continue
        if f is not None:
            out.append(f)
    return out


def _check_disk(r: AzureResource) -> Finding | None:
    if r.managed_by:
        return None
    return Finding(
        vm_id=r.resource_id,
        category=Category.CLEANUP,
        subcategory=SubCategory.UNATTACHED_DISK,
        code="CLN-DSK-001",
        current=r.resource_type,
        proposed=None,
        rationale=(
            f"Managed disk '{r.name}' in resource group '{r.resource_group}' "
            "has no managed_by reference — it is not attached to any VM. "
            "Review and delete to reduce storage costs."
        ),
        **_rec_kwargs(category=Category.CLEANUP),
    )


def _check_nic(r: AzureResource) -> Finding | None:
    if r.managed_by:
        return None
    return Finding(
        vm_id=r.resource_id,
        category=Category.CLEANUP,
        subcategory=SubCategory.UNATTACHED_NIC,
        code="CLN-NIC-001",
        current=r.resource_type,
        proposed=None,
        rationale=(
            f"Network interface '{r.name}' in resource group '{r.resource_group}' "
            "has no managed_by reference — it may not be attached to any VM. "
            "Review and delete if no longer needed."
        ),
        **_rec_kwargs(category=Category.CLEANUP),
    )


def _check_pip(r: AzureResource) -> Finding | None:
    if r.managed_by:
        return None
    return Finding(
        vm_id=r.resource_id,
        category=Category.CLEANUP,
        subcategory=SubCategory.UNASSOCIATED_PUBLIC_IP,
        code="CLN-PIP-001",
        current=r.resource_type,
        proposed=None,
        rationale=(
            f"Public IP address '{r.name}' in resource group '{r.resource_group}' "
            "has no managed_by reference — it may be unassociated. "
            "Review and release to avoid idle IP charges."
        ),
        **_rec_kwargs(category=Category.CLEANUP),
    )


def _check_snapshot(r: AzureResource) -> Finding | None:
    return Finding(
        vm_id=r.resource_id,
        category=Category.CLEANUP,
        subcategory=SubCategory.UNUSED_SNAPSHOT,
        code="CLN-SNP-001",
        current=r.resource_type,
        proposed=None,
        rationale=(
            f"Snapshot '{r.name}' in resource group '{r.resource_group}' exists. "
            "Review whether this snapshot is still required; delete stale snapshots "
            "to reduce storage costs. (Age data not available in current collection.)"
        ),
        **_rec_kwargs(),
    )
