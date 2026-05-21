"""CLN-DSK-001, CLN-NIC-001, CLN-PIP-001, CLN-SNP-001, CLN-RGP-001 detectors — orphaned resources.

This detector operates on ``AzureResource`` objects collected by the
``resources`` collector rather than on ``VmInventory``.  The standard
``detect()`` signature is extended with optional ``resources`` and
``empty_resource_groups`` keyword arguments.

Implemented:
  CLN-DSK-001 — unattached managed disk (managed_by empty) for ≥ 30 days
  CLN-NIC-001 — NIC with no managed_by reference (best-effort proxy)
  CLN-PIP-001 — public IP with no managed_by reference (best-effort proxy)
  CLN-SNP-001 — all snapshots flagged for review (no age data in model)
  CLN-RGP-001 — empty resource groups (no resources in ARG scan)

NOTE: Finding.vm_id is used as a generic resource identifier for non-VM
findings; for cleanup findings it holds the orphaned resource's resource_id.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from cloudopt.analyzer.detectors._shared import _rec_kwargs
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.analyzer.taxonomy import Category, SubCategory
from cloudopt.models import (
    AzureResource,
    CollectionThresholds,
    Finding,
    QuotaItem,
    ResourceGroupInfo,
    VmInventory,
    VmMetrics,
)

_DISK_TYPE = "microsoft.compute/disks"
_NIC_TYPE = "microsoft.network/networkinterfaces"
_PIP_TYPE = "microsoft.network/publicipaddresses"
_SNAP_TYPE = "microsoft.compute/snapshots"

_ORPHANED_DISK_MIN_DAYS = 30  # Must be unattached for at least this many days


def detect(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    quota: list[QuotaItem],
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
    *,
    resources: list[AzureResource] | None = None,
    empty_resource_groups: list[ResourceGroupInfo] | None = None,
) -> list[Finding]:
    """Emit CLN-* Findings for orphaned Azure resources and empty resource groups.

    ``vms``, ``metrics``, and ``quota`` are accepted for interface uniformity
    but not used.  The caller must supply ``resources`` (the output of
    ``collect_resources()``) for disk/NIC/PIP/snapshot findings, and
    ``empty_resource_groups`` (output of ``collect_empty_resource_groups()``)
    for CLN-RGP-001 findings.
    """
    out: list[Finding] = []

    if resources:
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

    if empty_resource_groups:
        for rg in empty_resource_groups:
            out.append(_check_empty_rg(rg))

    return out


def _check_disk(r: AzureResource) -> Finding | None:
    if r.managed_by:
        return None

    days_unattached: int | None = None
    age_note = ""
    if r.time_created:
        try:
            created = datetime.fromisoformat(r.time_created.replace("Z", "+00:00"))
            days_unattached = (datetime.now(tz=timezone.utc) - created).days
        except (ValueError, TypeError):
            pass

    if days_unattached is not None:
        if days_unattached < _ORPHANED_DISK_MIN_DAYS:
            return None  # recently created — not yet orphaned
        age_note = f" (unattached for ≥ {days_unattached} days)"
    else:
        age_note = " (creation date unavailable — age unconfirmed)"

    return Finding(
        vm_id=r.resource_id,
        category=Category.CLEANUP,
        subcategory=SubCategory.UNATTACHED_DISK,
        code="CLN-DSK-001",
        current=r.resource_type,
        proposed=None,
        rationale=(
            f"Managed disk '{r.name}' in resource group '{r.resource_group}' "
            f"has no managed_by reference — it is not attached to any VM{age_note}. "
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


def _check_empty_rg(rg: ResourceGroupInfo) -> Finding:
    return Finding(
        vm_id=rg.resource_id,
        category=Category.CLEANUP,
        subcategory=SubCategory.EMPTY_RESOURCE_GROUP,
        code="CLN-RGP-001",
        current="microsoft.resources/resourcegroups",
        proposed=None,
        rationale=(
            f"Resource group '{rg.name}' in subscription '{rg.subscription_name}' "
            f"(region: {rg.location}) contains no resources. "
            "Empty resource groups incur no direct cost but create noise in inventory "
            "and may indicate stale or abandoned deployments. Review and delete if "
            "the group is no longer needed."
        ),
        **_rec_kwargs(category=Category.CLEANUP),
    )
