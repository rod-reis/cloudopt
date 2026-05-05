"""CSV export — one CSV file per logical sheet."""

from __future__ import annotations

import csv
from pathlib import Path

from cloudopt.models import (
    CollectionMetadata,
    VmInventory,
    VmMetrics,
    VmRecommendation,
    mask_subscription_ids_in_string,
)


def write_csv(
    vms: list[VmInventory],
    metrics: list[VmMetrics],
    recommendations: list[VmRecommendation],
    metadata: CollectionMetadata,
    output_dir: Path,
) -> None:
    """Write flat CSV files to *output_dir*."""
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_vm_inventory_csv(vms, output_dir / "vm_inventory.csv")
    _write_metrics_csv(metrics, output_dir / "metrics.csv")
    _write_recommendations_csv(recommendations, output_dir / "recommendations.csv")


def _write_vm_inventory_csv(vms: list[VmInventory], path: Path) -> None:
    headers = [
        "vm_name", "subscription_name", "subscription_id", "resource_group",
        "region", "vm_sku", "vcpus", "memory_gb", "os_type", "os_version",
        "power_state", "image_publisher", "image_offer", "image_sku", "image_version",
        "availability_zone", "nic_count", "disk_count", "disk_sizes_gb",
        "vmss_name", "availability_set_name", "resource_id",
        "workload", "application", "environment", "criticality", "owner", "custom",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for vm in vms:
            writer.writerow({
                "vm_name": vm.vm_name,
                "subscription_name": vm.subscription_name,
                "subscription_id": vm.masked_subscription_id(),
                "resource_group": vm.resource_group,
                "region": vm.region,
                "vm_sku": vm.vm_sku,
                "vcpus": vm.vcpus,
                "memory_gb": vm.memory_gb,
                "os_type": vm.os_type,
                "os_version": vm.os_version or "",
                "power_state": vm.power_state or "",
                "image_publisher": vm.image_publisher or "",
                "image_offer": vm.image_offer or "",
                "image_sku": vm.image_sku or "",
                "image_version": vm.image_version or "",
                "availability_zone": vm.availability_zone or "",
                "nic_count": vm.nic_count,
                "disk_count": vm.disk_count,
                "disk_sizes_gb": ";".join(str(int(s)) for s in vm.disk_sizes_gb),
                "vmss_name": vm.vmss_name or "",
                "availability_set_name": vm.availability_set_name or "",
                "resource_id": vm.masked_resource_id(),
                "workload": vm.workload or "",
                "application": vm.application or "",
                "environment": vm.environment or "",
                "criticality": vm.criticality or "",
                "owner": vm.owner or "",
                "custom": vm.custom or "",
            })


def _write_metrics_csv(metrics: list[VmMetrics], path: Path) -> None:
    headers = ["resource_id", "metric_name", "avg", "p50", "p95", "max", "min", "data_points"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for m in metrics:
            writer.writerow({
                "resource_id": mask_subscription_ids_in_string(m.resource_id),
                "metric_name": m.metric_name,
                "avg": m.avg,
                "p50": m.p50,
                "p95": m.p95,
                "max": m.max,
                "min": m.min,
                "data_points": len(m.time_series),
            })


def _write_recommendations_csv(recommendations: list[VmRecommendation], path: Path) -> None:
    headers = [
        "resource_id", "parent_resource_id", "member_count",
        "current_sku", "recommended_sku", "category", "subcategory",
        "reason", "estimated_savings_pct", "manual_override", "notes",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for r in recommendations:
            writer.writerow({
                "resource_id": r.masked_resource_id(),
                "parent_resource_id": r.masked_parent_resource_id() or "",
                "member_count": r.member_count,
                "current_sku": r.current_sku,
                "recommended_sku": r.recommended_sku or "",
                "category": r.category,
                "subcategory": r.subcategory,
                "reason": r.reason,
                "estimated_savings_pct": r.estimated_savings_pct or "",
                "manual_override": r.manual_override or "",
                "notes": r.notes or "",
            })
