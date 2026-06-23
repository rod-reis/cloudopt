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

from cloudopt.analyzer.archetype import enrich_vm_archetype
from cloudopt.analyzer.detectors import (
    burstable,
    cleanup,
    decom,
    disk_pv2,
    disk_rightsize,
    disk_tier_swap,
    diskless,
    ops_hygiene,
    quota,
    reservations,
    rightsize,
    swap,
    upsize,
)
from cloudopt.analyzer.detectors._shared import enrich_vm_memory_quality
from cloudopt.analyzer.sku_catalog import SkuCatalog
from cloudopt.enrichment.schema import EnrichedVmMetrics
from cloudopt.models import (
    AppInsightsMetrics,
    AzureResource,
    CapacityAlert,
    CapacityReservationGroup,
    CollectionThresholds,
    DiskInventory,
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
    disks: Optional[list[DiskInventory]] = None,
    enable_dlc: bool = False,
    enable_env_check: bool = False,
    rsvp_orders: Optional[list] = None,  # unused, kept for backward compat
    crg_items: Optional[list[CapacityReservationGroup]] = None,
    enriched_map: Optional[dict[str, EnrichedVmMetrics]] = None,
    ai_metrics_by_resource: Optional[dict[str, list[AppInsightsMetrics]]] = None,
    capacity_alerts: Optional[list[CapacityAlert]] = None,
) -> list[Finding]:
    """Run every registered detector and return the combined Finding list.

    Args:
        vms:                     VM inventory records.
        metrics:                 Platform metrics records.
        quota_items:             Quota utilisation records.
        thresholds:              Detection thresholds (see CollectionThresholds).
        catalog:                 SKU catalog used for right-size candidate lookup.
        resources:               Optional orphaned-resource list for cleanup detectors.
        disks:                   Optional managed-disk inventory (ARG); drives the
                                 SWP-DST-002 Premium SSD v1 → v2 modernization detector.
        empty_resource_groups:   Optional empty resource groups (CLN-RGP-001).
        enable_dlc:              Enable DCM-DLC-001 (lower-env oversized) detector.
        enable_env_check:        Enable DCM-ENV-001 (missing env-tag) detector.
        crg_items:               Optional Capacity Reservation Groups (§2.6 detectors).
        enriched_map:            Optional OS/AMA enrichment metrics by resource ID.
        ai_metrics_by_resource:  Optional Application Insights metrics keyed by
                                 App Insights resource ID; used for SLO corroboration.
        capacity_alerts:         Optional list of Azure Monitor alert rules; used by
                                 the QTA-OPS-001 capacity ops hygiene detector.
    """
    out: list[Finding] = []
    # Phase 3: populate memory_quality, mem_pressure_score per VM before detectors run
    enrich_vm_memory_quality(vms, metrics, enriched_map=enriched_map)
    # Phase 4: populate workload_archetype, inferred_workload_role, appinsights_corroboration
    enrich_vm_archetype(vms, metrics, ai_metrics_by_resource=ai_metrics_by_resource)
    out.extend(rightsize.detect(vms, metrics, quota_items, thresholds, catalog, enriched_map=enriched_map))
    out.extend(burstable.detect(vms, metrics, quota_items, thresholds, catalog, enriched_map=enriched_map))
    out.extend(diskless.detect(vms, metrics, quota_items, thresholds, catalog, enriched_map=enriched_map))
    out.extend(swap.detect(vms, metrics, quota_items, thresholds, catalog, enriched_map=enriched_map))
    out.extend(upsize.detect(vms, metrics, quota_items, thresholds, catalog, enriched_map=enriched_map))
    out.extend(disk_rightsize.detect(vms, metrics, quota_items, thresholds, catalog, enriched_map=enriched_map))
    out.extend(disk_tier_swap.detect(vms, metrics, quota_items, thresholds, catalog, enriched_map=enriched_map))
    out.extend(disk_pv2.detect(disks))
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
    # Phase 5: capacity ops hygiene — one finding per subscription
    out.extend(
        ops_hygiene.detect(
            vms, metrics, quota_items, thresholds,
            capacity_alerts=capacity_alerts,
            crg_items=crg_items,
        )
    )
    return out
