"""FastAPI local web dashboard server.

Reads the Excel workbook (or JSON) and serves a REST API consumed by the
single-page frontend. No authentication — localhost only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from cloudopt.export.excel import read_workbook, read_quota_from_workbook
from cloudopt.models import (
    CollectionMetadata,
    VmInventory,
    VmMetrics,
    VmRecommendation,
)

# ---------------------------------------------------------------------------
# App state (module-level, reloaded on /api/reload)
# ---------------------------------------------------------------------------

_DATA: dict[str, Any] = {
    "vms": [],
    "metrics_by_vm": {},
    "recommendations": [],
    "quota": [],
    "metadata": None,
    "path": None,
}


def create_app(data_path: Path) -> FastAPI:
    app = FastAPI(title="Azure CLOUDOPT Collector", docs_url=None, redoc_url=None)

    _load(data_path)

    # Serve static assets
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # -----------------------------------------------------------------------
    # Routes
    # -----------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index():
        template = Path(__file__).parent / "templates" / "index.html"
        return HTMLResponse(template.read_text(encoding="utf-8"))

    @app.post("/api/reload")
    async def reload():
        _load(_DATA["path"])
        return {"status": "ok", "vm_count": len(_DATA["vms"])}

    @app.get("/api/metadata")
    async def metadata():
        m: CollectionMetadata | None = _DATA["metadata"]
        if not m:
            return {}
        return m.model_dump()

    @app.get("/api/inventory")
    async def inventory(
        subscription: str | None = Query(None),
        resource_group: str | None = Query(None),
        sku: str | None = Query(None),
        vmss: str | None = Query(None),
        avset: str | None = Query(None),
        search: str | None = Query(None),
    ):
        vms: list[VmInventory] = _DATA["vms"]
        filtered = _filter_vms(vms, subscription, resource_group, sku, vmss, avset, search)
        return [_vm_json(v) for v in filtered]

    @app.get("/api/metrics/{resource_id:path}")
    async def vm_metrics(resource_id: str):
        metrics_by_vm: dict[str, dict[str, Any]] = _DATA["metrics_by_vm"]
        # resource_id in URL is masked — look up by masked form
        for raw_id, met in metrics_by_vm.items():
            from cloudopt.models import mask_subscription_ids_in_string
            if mask_subscription_ids_in_string(raw_id) == resource_id or raw_id == resource_id:
                return {
                    name: {
                        "avg": m.avg, "p50": m.p50, "p95": m.p95, "p99": m.p99,
                        "max": m.max, "min": m.min,
                        "time_series": [{"date": p.date, "value": p.value} for p in m.time_series],
                    }
                    for name, m in met.items()
                }
        raise HTTPException(status_code=404, detail="VM not found")

    @app.get("/api/summary/subscription")
    async def summary_subscription():
        return _aggregate_flat_sub(_DATA["vms"], _DATA["metrics_by_vm"])

    @app.get("/api/summary/resource-group")
    async def summary_resource_group():
        return _aggregate_flat_rg(_DATA["vms"], _DATA["metrics_by_vm"])

    @app.get("/api/summary/groups")
    async def summary_groups():
        return _aggregate_flat_groups(_DATA["vms"], _DATA["metrics_by_vm"])

    @app.get("/api/recommendations")
    async def recommendations(category: str | None = Query(None)):
        recs: list[VmRecommendation] = _DATA["recommendations"]
        if category:
            recs = [r for r in recs if r.category == category]
        return [_rec_json(r) for r in recs]

    @app.get("/api/quota")
    async def quota(alert_only: bool = Query(False)):
        items = _DATA["quota"]
        if alert_only:
            items = [q for q in items if q.alert]
        return [
            {
                "subscription_name": q.subscription_name,
                "region": q.region,
                "resource_type": q.resource_type,
                "display_name": q.display_name,
                "current_usage": q.current_usage,
                "quota_limit": q.quota_limit,
                "utilization_pct": q.utilization_pct,
                "alert": q.alert,
            }
            for q in items
        ]

    return app


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load(path: Path) -> None:
    _DATA["path"] = path
    if path.suffix.lower() == ".json":
        _load_json(path)
    else:
        _load_excel(path)


def _load_excel(path: Path) -> None:
    vms, metrics, recommendations, metadata = read_workbook(path)
    quota = read_quota_from_workbook(path)
    _store(vms, metrics, recommendations, metadata, quota)


def _load_json(path: Path) -> None:
    from cloudopt.models import CollectionThresholds, DailyDataPoint

    raw = json.loads(path.read_text(encoding="utf-8"))
    vms = []
    for d in raw.get("vms", []):
        try:
            vms.append(VmInventory(**{k: v for k, v in d.items() if k != "subscription_id"},
                                   subscription_id=d.get("subscription_id", "")))
        except Exception:
            pass

    metrics = []
    for d in raw.get("metrics", []):
        try:
            ts = [DailyDataPoint(**p) for p in d.get("time_series", [])]
            metrics.append(VmMetrics(**{k: v for k, v in d.items() if k != "time_series"},
                                      time_series=ts))
        except Exception:
            pass

    recommendations = []
    for d in raw.get("recommendations", []):
        try:
            recommendations.append(VmRecommendation(**d))
        except Exception:
            pass

    meta_raw = raw.get("metadata", {})
    try:
        metadata = CollectionMetadata(
            run_date=meta_raw.get("run_date", ""),
            tool_version=meta_raw.get("tool_version", ""),
            subscriptions_scanned=meta_raw.get("subscriptions_scanned", []),
            metrics_period_days=meta_raw.get("metrics_period_days", 30),
            total_vm_count=meta_raw.get("total_vm_count", 0),
            thresholds=CollectionThresholds(**meta_raw.get("thresholds", {})),
        )
    except Exception:
        metadata = None

    _store(vms, metrics, recommendations, metadata, [])


def _store(vms, metrics, recommendations, metadata, quota=None) -> None:
    from cloudopt.export.excel import _group_metrics
    _DATA["vms"] = vms
    _DATA["metrics_by_vm"] = _group_metrics(metrics)
    _DATA["recommendations"] = recommendations
    _DATA["quota"] = quota or []
    _DATA["metadata"] = metadata


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _vm_json(vm: VmInventory) -> dict:
    return {
        "resource_id": vm.masked_resource_id(),
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
        "image_publisher": vm.image_publisher,
        "image_offer": vm.image_offer,
        "image_sku": vm.image_sku,
        "image_version": vm.image_version,
        "availability_zone": vm.availability_zone,
        "nic_count": vm.nic_count,
        "disk_count": vm.disk_count,
        "vmss_name": vm.vmss_name,
        "availability_set_name": vm.availability_set_name,
        "workload": vm.workload,
        "application": vm.application,
        "environment": vm.environment,
        "criticality": vm.criticality,
        "owner": vm.owner,
        "custom": vm.custom,
    }


def _rec_json(r: VmRecommendation) -> dict:
    return {
        "priority": r.priority,
        "recommendation": r.recommendation,
        "category": r.category,
        "resource_id": r.masked_resource_id(),
        "current_sku_or_resource_type": r.current_sku or r.current_resource_type,
        "recommended_sku_or_resource_type": r.recommended_sku or r.recommended_resource_type,
        "reason": r.reason,
        "recommended_action": _rec_action_text(r),
        "estimated_optimization": r.estimated_optimization,
        "estimated_savings_pct": r.estimated_savings_pct,
        "manual_override": r.manual_override,
        "notes": r.notes,
    }


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def _filter_vms(
    vms: list[VmInventory],
    subscription: str | None,
    resource_group: str | None,
    sku: str | None,
    vmss: str | None,
    avset: str | None,
    search: str | None,
) -> list[VmInventory]:
    result = vms
    if subscription:
        result = [v for v in result if v.subscription_name == subscription]
    if resource_group:
        result = [v for v in result if v.resource_group == resource_group]
    if sku:
        result = [v for v in result if v.vm_sku == sku]
    if vmss:
        result = [v for v in result if v.vmss_name == vmss]
    if avset:
        result = [v for v in result if v.availability_set_name == avset]
    if search:
        q = search.lower()
        result = [
            v for v in result
            if q in v.vm_name.lower()
            or q in v.resource_group.lower()
            or q in v.vm_sku.lower()
        ]
    return result


def _aggregate_by(
    vms: list[VmInventory],
    metrics_by_vm: dict,
    group_field: str,
) -> list[dict]:
    groups: dict[str, list[VmInventory]] = {}
    for vm in vms:
        key = getattr(vm, group_field, None) or "(unknown)"
        groups.setdefault(key, []).append(vm)

    result = []
    for group_name, group_vms in sorted(groups.items()):
        result.append({
            "group_name": group_name,
            "vm_count": len(group_vms),
            "avg_cpu_pct": _avg_metric_group(group_vms, metrics_by_vm, "Percentage CPU", "avg"),
            "avg_mem_pct": _avg_mem_pct_group(group_vms, metrics_by_vm),
            "sku_distribution": _sku_dist(group_vms),
        })
    return result


def _aggregate_flat_sub(
    vms: list[VmInventory],
    metrics_by_vm: dict,
) -> list[dict]:
    """Flat rows: one per (subscription_name, vm_sku)."""
    groups: dict[tuple, list[VmInventory]] = {}
    for vm in vms:
        groups.setdefault((vm.subscription_name, vm.vm_sku), []).append(vm)
    result = []
    for (sub_name, sku), group_vms in sorted(groups.items()):
        result.append({
            "subscription_name": sub_name,
            "vm_sku": sku,
            "vm_count": len(group_vms),
            "avg_cpu_pct": _avg_metric_group(group_vms, metrics_by_vm, "Percentage CPU", "avg"),
            "avg_mem_pct": _avg_mem_pct_group(group_vms, metrics_by_vm),
        })
    return result


def _aggregate_flat_rg(
    vms: list[VmInventory],
    metrics_by_vm: dict,
) -> list[dict]:
    """Flat rows: one per (subscription_name, resource_group, vm_sku)."""
    groups: dict[tuple, list[VmInventory]] = {}
    for vm in vms:
        groups.setdefault((vm.subscription_name, vm.resource_group, vm.vm_sku), []).append(vm)
    result = []
    for (sub_name, rg, sku), group_vms in sorted(groups.items()):
        result.append({
            "subscription_name": sub_name,
            "resource_group": rg,
            "vm_sku": sku,
            "vm_count": len(group_vms),
            "avg_cpu_pct": _avg_metric_group(group_vms, metrics_by_vm, "Percentage CPU", "avg"),
            "avg_mem_pct": _avg_mem_pct_group(group_vms, metrics_by_vm),
        })
    return result


def _aggregate_flat_groups(
    vms: list[VmInventory],
    metrics_by_vm: dict,
) -> list[dict]:
    """Flat rows: one per (subscription_name, resource_group, group_name, group_type, vm_sku)."""
    groups: dict[tuple, list[VmInventory]] = {}
    for vm in vms:
        if vm.vmss_name:
            g_name, g_type = vm.vmss_name, "VMSS"
        elif vm.availability_set_name:
            g_name, g_type = vm.availability_set_name, "AvailabilitySet"
        else:
            g_name, g_type = "(ungrouped)", "None"
        key = (vm.subscription_name, vm.resource_group, g_name, g_type, vm.vm_sku)
        groups.setdefault(key, []).append(vm)
    result = []
    for (sub_name, rg, g_name, g_type, sku), group_vms in sorted(groups.items()):
        result.append({
            "subscription_name": sub_name,
            "resource_group": rg,
            "group_name": g_name,
            "group_type": g_type,
            "vm_sku": sku,
            "vm_count": len(group_vms),
            "avg_cpu_pct": _avg_metric_group(group_vms, metrics_by_vm, "Percentage CPU", "avg"),
            "avg_mem_pct": _avg_mem_pct_group(group_vms, metrics_by_vm),
        })
    return result


def _rec_action_text(r: VmRecommendation) -> str:
    """Plain-English recommended action for the CSA to present to the customer."""
    if r.category == "underutilized":
        if r.recommended_sku:
            return f"Resize to {r.recommended_sku} or decommission if unused"
        return "Review usage — decommission or resize to smallest available SKU"
    if r.category == "right-size":
        if r.recommended_sku:
            return f"Resize {r.current_sku} → {r.recommended_sku}"
        return "Resize to a smaller SKU (no smaller SKU found in current region)"
    if r.category == "PaaS-candidate":
        return "Migrate to Azure App Service, Container Apps, or Azure SQL"
    return ""


def _avg_metric_group(
    vms: list[VmInventory],
    metrics_by_vm: dict,
    metric_name: str,
    stat: str,
) -> float | None:
    values = []
    for vm in vms:
        if _is_stopped(vm):
            continue
        m = metrics_by_vm.get(vm.resource_id, {}).get(metric_name)
        v = getattr(m, stat, None) if m else None
        if v is not None:
            values.append(v)
    return round(sum(values) / len(values), 2) if values else None


def _avg_mem_pct_group(vms: list[VmInventory], metrics_by_vm: dict) -> float | None:
    values = []
    for vm in vms:
        if _is_stopped(vm):
            continue
        m = metrics_by_vm.get(vm.resource_id, {}).get("Available Memory Bytes")
        if m and m.avg is not None and vm.memory_gb > 0:
            avail_gb = m.avg / (1024 ** 3)
            pct = (1 - avail_gb / vm.memory_gb) * 100
            values.append(max(0.0, min(100.0, pct)))
    return round(sum(values) / len(values), 2) if values else None


def _is_stopped(vm: VmInventory) -> bool:
    state = (vm.power_state or "").lower()
    return state in ("powerstate/stopped", "powerstate/deallocated")


def _sku_dist(vms: list[VmInventory]) -> dict[str, int]:
    dist: dict[str, int] = {}
    for vm in vms:
        dist[vm.vm_sku] = dist.get(vm.vm_sku, 0) + 1
    return dict(sorted(dist.items(), key=lambda x: -x[1]))
