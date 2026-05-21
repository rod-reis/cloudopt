"""JSON export — serialises collection data to a structured JSON file."""

from __future__ import annotations

import json
from pathlib import Path

from cloudopt.enrichment.schema import EnrichedVmMetrics, EnrichmentSummary
from cloudopt.models import (
    AdvisorRecommendation,
    AppInsightsInventory,
    AppInsightsMetrics,
    AzureResource,
    CapacityReservationGroup,
    CollectionMetadata,
    ManagedComputeGroupRow,
    QuotaItem,
    ResourceGroupInfo,
    SubscriptionZoneMapping,
    VmInventory,
    VmMetrics,
    VmRecommendation,
    WorkloadInfo,
    mask_subscription_ids_in_string,
    mask_subscription_id,
)


def write_json(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    recommendations: list[VmRecommendation],
    metadata: CollectionMetadata,
    path: Path,
    *,
    quota: list[QuotaItem] | None = None,
    appinsights: list[AppInsightsInventory] | None = None,
    appinsights_metrics: list[AppInsightsMetrics] | None = None,
    advisor: list[AdvisorRecommendation] | None = None,
    workload_info: WorkloadInfo | None = None,
    zone_mappings: list[SubscriptionZoneMapping] | None = None,
    enriched_metrics: list[EnrichedVmMetrics] | None = None,
    enrichment_summary: EnrichmentSummary | None = None,
    resources: list[AzureResource] | None = None,
    capacity_reservations: list[CapacityReservationGroup] | None = None,
    vmss_groups: list[ManagedComputeGroupRow] | None = None,
    empty_resource_groups: list[ResourceGroupInfo] | None = None,
) -> None:
    """Write all collection data to a JSON file at *path*."""
    payload: dict = {
        "metadata": _metadata_dict(metadata),
        "workload_info": (workload_info or WorkloadInfo()).model_dump(),
        "vms": [_vm_dict(vm) for vm in vms],
        "metrics": [_metrics_dict(m) for m in metrics],
        "recommendations": [_rec_dict(r) for r in recommendations],
        "advisor": [_advisor_dict(a) for a in (advisor or [])],
        "quota": [_quota_dict(q) for q in (quota or [])],
        "appinsights": [_ai_dict(c) for c in (appinsights or [])],
        "appinsights_metrics": [_ai_metrics_dict(m) for m in (appinsights_metrics or [])],
        "zone_mappings": [_zone_mapping_dict(z) for z in (zone_mappings or [])],
        "resources": [_resource_dict(r) for r in (resources or [])],
        "capacity_reservations": [_crg_dict(c) for c in (capacity_reservations or [])],
        "vmss_groups": [g.model_dump() for g in (vmss_groups or [])],
        "empty_resource_groups": [rg.masked_dict() for rg in (empty_resource_groups or [])],
    }
    if enriched_metrics is not None:
        payload["enrichment"] = {
            "summary": enrichment_summary.model_dump() if enrichment_summary else None,
            "vm_metrics": [_enriched_vm_dict(e) for e in enriched_metrics],
        }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _vm_dict(vm: VmInventory) -> dict:
    return {
        "resource_id": vm.masked_resource_id(),
        "subscription_id": vm.masked_subscription_id(),
        "subscription_name": vm.subscription_name,
        "resource_group": vm.resource_group,
        "vm_name": vm.vm_name,
        "vm_sku": vm.vm_sku,
        "vcpus": vm.vcpus,
        "memory_gb": vm.memory_gb,
        "region": vm.region,
        "os_type": vm.os_type,
        "os_version": vm.os_version,
        "power_state": vm.power_state,
        "days_stopped": vm.days_stopped,
        "image_publisher": vm.image_publisher,
        "image_offer": vm.image_offer,
        "image_sku": vm.image_sku,
        "image_version": vm.image_version,
        "availability_zone": vm.availability_zone,
        "nic_count": vm.nic_count,
        "disk_count": vm.disk_count,
        "disk_sizes_gb": vm.disk_sizes_gb,
        "vmss_id": vm.vmss_id,
        "vmss_name": vm.vmss_name,
        "availability_set_id": vm.availability_set_id,
        "availability_set_name": vm.availability_set_name,
        "parent_service_type": vm.parent_service_type.value,
        "parent_service_id": vm.parent_service_id,
        "parent_service_name": vm.parent_service_name,
        "parent_pool_name": vm.parent_pool_name,
        "workload": vm.workload,
        "application": vm.application,
        "environment": vm.environment,
        "criticality": vm.criticality,
        "owner": vm.owner,
        "custom": vm.custom,
        "raw_properties": vm.raw_properties,
    }


def _advisor_dict(a: AdvisorRecommendation) -> dict:
    return {
        "recommendation_id": a.recommendation_id,
        "subscription_id": a.masked_subscription_id(),
        "subscription_name": a.subscription_name,
        "resource_group": a.resource_group,
        "impacted_resource_id": a.masked_impacted_resource_id(),
        "impacted_resource_name": a.impacted_resource_name,
        "impacted_resource_type": a.impacted_resource_type,
        "category": a.category,
        "impact": a.impact,
        "short_description": a.short_description,
        "current_sku": a.current_sku,
        "recommended_sku": a.recommended_sku,
        "annual_savings_usd": a.annual_savings_usd,
        "last_updated": a.last_updated,
    }


def _metrics_dict(m: VmMetrics) -> dict:
    return {
        "resource_id": mask_subscription_ids_in_string(m.resource_id),
        "metric_name": m.metric_name,
        "avg": m.avg,
        "p50": m.p50,
        "p95": m.p95,
        "p99": m.p99,
        "max": m.max,
        "min": m.min,
        "time_series": [{"date": p.date, "value": p.value} for p in m.time_series],
    }


def _rec_dict(r: VmRecommendation) -> dict:
    return {
        "priority": r.priority,
        "recommendation": r.recommendation,
        "category": r.category,
        "subcategory": r.subcategory,
        "resource_id": r.masked_resource_id(),
        "parent_resource_id": r.masked_parent_resource_id(),
        "parent_resource_type": r.parent_resource_type,
        "parent_resource_name": r.parent_resource_name,
        "member_count": r.member_count,
        "current_sku_or_resource_type": r.current_sku or r.current_resource_type,
        "recommended_sku_or_resource_type": r.recommended_sku or r.recommended_resource_type,
        "reason": r.reason,
        "estimated_optimization": r.estimated_optimization,
        "manual_override": r.manual_override,
        "notes": r.notes,
        # Back-compat numeric field for downstream tooling
        "estimated_savings_pct": r.estimated_savings_pct,
    }


def _metadata_dict(m: CollectionMetadata) -> dict:
    return {
        "run_date": m.run_date,
        "tool_version": m.tool_version,
        "subscriptions_scanned": [mask_subscription_id(s) for s in m.subscriptions_scanned],
        "metrics_period_days": m.metrics_period_days,
        "total_vm_count": m.total_vm_count,
        "total_appinsights_count": m.total_appinsights_count,
        "thresholds": m.thresholds.model_dump(),
    }


def _quota_dict(q: QuotaItem) -> dict:
    return {
        "subscription_name": q.subscription_name,
        "region": q.region,
        "resource_type": q.resource_type,
        "display_name": q.display_name,
        "current_usage": q.current_usage,
        "quota_limit": q.quota_limit,
        "utilization_pct": q.utilization_pct,
        "peak_usage_pct_30d": q.peak_usage_pct_30d,
        "allocation_failures_30d": q.allocation_failures_30d,
        "alert": q.alert,
    }


def _resource_dict(r: AzureResource) -> dict:
    return {
        "resource_id": r.masked_resource_id(),
        "name": r.name,
        "resource_type": r.resource_type,
        "subscription_id": r.masked_subscription_id(),
        "subscription_name": r.subscription_name,
        "resource_group": r.resource_group,
        "location": r.location,
        "kind": r.kind,
        "sku_name": r.sku_name,
        "sku_tier": r.sku_tier,
        "plan_name": r.plan_name,
        "plan_publisher": r.plan_publisher,
        "plan_product": r.plan_product,
        "zones": r.zones,
        "managed_by": r.managed_by,
    }


def _ai_dict(c: AppInsightsInventory) -> dict:
    return {
        "resource_id": c.masked_resource_id(),
        "subscription_id": c.masked_subscription_id(),
        "subscription_name": c.subscription_name,
        "resource_group": c.resource_group,
        "component_name": c.component_name,
        "kind": c.kind,
        "application_type": c.application_type,
        "workspace_linked": c.workspace_resource_id is not None,
        "region": c.region,
    }


def _ai_metrics_dict(m: AppInsightsMetrics) -> dict:
    return {
        "resource_id": mask_subscription_ids_in_string(m.resource_id),
        "metric_name": m.metric_name,
        "display_name": m.display_name,
        "category": m.category,
        "unit": m.unit,
        "avg": m.avg,
        "p50": m.p50,
        "p95": m.p95,
        "p99": m.p99,
        "max": m.max,
        "min": m.min,
        "time_series": [{"date": p.date, "value": p.value} for p in m.time_series],
    }


def _zone_mapping_dict(z: SubscriptionZoneMapping) -> dict:
    return {
        "tenant_id": z.tenant_id,
        "subscription_id": mask_subscription_id(z.subscription_id),
        "subscription_name": z.subscription_name,
        "location": z.location,
        "logical_zone": z.logical_zone,
        "physical_zone": z.physical_zone,
        "physical_zone_name": z.physical_zone_name,
    }


def _enriched_vm_dict(e: EnrichedVmMetrics) -> dict:
    return {
        "vm_name": e.vm_name,
        "confidence_tier": e.confidence_tier,
        "data_points": [
            {
                "source_tool": dp.source_tool,
                "hostname": dp.hostname,
                "metric_name": dp.metric_name,
                "period_days": dp.period_days,
                "period_end_utc": dp.period_end_utc,
                "avg_value": dp.avg_value,
                "p95_value": dp.p95_value,
                "max_value": dp.max_value,
                "unit": dp.unit,
                "text_value": dp.text_value,
            }
            for dp in e.data_points
        ],
    }


def _crg_dict(c: CapacityReservationGroup) -> dict:
    """Serialise a CapacityReservationGroup — counts and metadata; no $ fields."""
    return {
        "group_id": c.masked_group_id(),
        "group_name": c.group_name,
        "subscription_id": c.masked_subscription_id(),
        "resource_group": c.resource_group,
        "region": c.region,
        "zones": c.zones,
        "reserved_count_total": c.reserved_count_total,
        "used_count_total": c.used_count_total,
        "fill_rate_pct": c.fill_rate_pct,
        "reservations": [
            {
                "reservation_name": item.reservation_name,
                "sku_name": item.sku_name,
                "reserved_count": item.reserved_count,
                "used_count": item.used_count,
                "zone": item.zone,
            }
            for item in c.reservations
        ],
    }
