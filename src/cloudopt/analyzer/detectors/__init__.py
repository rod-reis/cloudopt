"""Detector package — public API for Step 2.

``run_all()`` is the canonical entry point that aggregates all detector
outputs.  The deprecated shims in ``recommendations.py`` delegate here.

SPEC §11.2 deferral notes (updated)
-------------------------------------
* SWP-GEN-001: implemented — generation-swap detector in swap.py.
* CLN-RGP-001: implemented — empty resource groups via collect_empty_resource_groups().
* QUOTA_REVIEW tier (old util <= 25%): removed from taxonomy; the shim's
  ``generate_quota_recommendations()`` preserves it for backward compatibility.
* Quota threshold discrepancy: old code used warning=75% / oversized=15%;
  new ``quota.detect()`` uses SPEC-canonical values from CollectionThresholds
  (default warning=70% / oversized=20%).  The shim overrides with old values.
"""

from __future__ import annotations

from typing import Optional

from cloudopt.analyzer.detectors import (
    burstable,
    cleanup,
    decom,
    disk_rightsize,
    disk_tier_swap,
    diskless,
    quota,
    reservations,
    rightsize,
    swap,
    upsize,
)
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.enrichment.schema import EnrichedVmMetrics
from cloudopt.models import (
    AzureResource,
    CapacityReservationGroup,
    CollectionThresholds,
    Finding,
    QuotaItem,
    ResourceGroupInfo,
    VmInventory,
    VmMetrics,
)


def run_all(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    quota_items: list[QuotaItem],
    thresholds: CollectionThresholds,
    catalog: SkuCatalog,
    *,
    resources: Optional[list[AzureResource]] = None,
    empty_resource_groups: Optional[list[ResourceGroupInfo]] = None,
    enable_dlc: bool = False,
    enable_env_check: bool = False,
    rsvp_orders: Optional[list] = None,  # unused, kept for backward compat
    crg_items: Optional[list[CapacityReservationGroup]] = None,
    enriched_map: Optional[dict[str, EnrichedVmMetrics]] = None,
) -> list[Finding]:
    """Run every registered detector and return the combined Finding list.

    Args:
        vms:                   VM inventory records.
        metrics:               Platform metrics records.
        quota_items:           Quota utilisation records.
        thresholds:            Detection thresholds (see CollectionThresholds).
        catalog:               SKU catalog used for right-size candidate lookup.
        resources:             Optional orphaned-resource list for cleanup detectors.
        empty_resource_groups: Optional empty resource groups (CLN-RGP-001).
        enable_dlc:            Enable DCM-DLC-001 (lower-env oversized) detector.
        enable_env_check:      Enable DCM-ENV-001 (missing env-tag) detector.
        crg_items:             Optional Capacity Reservation Groups (§2.6 detectors).
    """
    out: list[Finding] = []
    out.extend(rightsize.detect(vms, metrics, quota_items, thresholds, catalog, enriched_map=enriched_map))
    out.extend(burstable.detect(vms, metrics, quota_items, thresholds, catalog, enriched_map=enriched_map))
    out.extend(diskless.detect(vms, metrics, quota_items, thresholds, catalog, enriched_map=enriched_map))
    out.extend(swap.detect(vms, metrics, quota_items, thresholds, catalog, enriched_map=enriched_map))
    out.extend(upsize.detect(vms, metrics, quota_items, thresholds, catalog, enriched_map=enriched_map))
    out.extend(disk_rightsize.detect(vms, metrics, quota_items, thresholds, catalog, enriched_map=enriched_map))
    out.extend(disk_tier_swap.detect(vms, metrics, quota_items, thresholds, catalog, enriched_map=enriched_map))
    out.extend(
        decom.detect(
            vms, metrics, quota_items, thresholds, catalog,
            enable_dlc=enable_dlc,
            enable_env_check=enable_env_check,
        )
    )
    out.extend(
        cleanup.detect(
            vms, metrics, quota_items, thresholds, catalog,
            resources=resources,
            empty_resource_groups=empty_resource_groups,
        )
    )
    out.extend(quota.detect(vms, metrics, quota_items, thresholds, catalog))
    out.extend(
        reservations.detect(
            crg_items or [],
        )
    )
    return out
