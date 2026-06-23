"""FastAPI local web dashboard server.

Reads the Excel workbook (or JSON) and serves a REST API consumed by the
single-page frontend. No authentication — localhost only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from cloudopt.export.excel import read_workbook, read_quota_from_workbook, read_vmss_groups_from_workbook
from cloudopt.models import (
    CollectionMetadata,
    DiskInventory,
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
    "capacity_reservations": [],
    "deployment_failures": [],
    "findings": [],
    "source_coverage": [],
    "vmss_groups": [],
    "disks": [],
    "findings_status": {},
}


# ---------------------------------------------------------------------------
# Phase 6/7 helper functions
# ---------------------------------------------------------------------------

def _build_waterfall(vms, findings):
    """Return vCPU forecast data for the Right-sized Capacity chart."""
    from cloudopt.analyzer.taxonomy import Readiness, Category
    total_running_vcpu = sum(v.vcpus for v in vms if not _is_stopped(v))
    vms_by_id = {v.resource_id: v for v in vms}

    # Include READY + LIKELY so the forecast reflects the full opportunity,
    # not just HIGH-confidence findings (RSZ and DCM-IDL are MEDIUM by default).
    actionable = {Readiness.READY, Readiness.LIKELY}
    actionable_recs = [
        f for f in findings
        if f.readiness in actionable and f.finding_type.value == "recommendation"
    ]

    # Downsize: deltas["vcpu"] is negative (proposed − current for a smaller SKU)
    downsize_vcpu = sum(
        abs(f.deltas.get("vcpu", 0) or 0)
        for f in actionable_recs
        if f.category == Category.RIGHTSIZE and f.proposed
        and f.deltas and (f.deltas.get("vcpu", 0) or 0) < 0
    )

    # Decom: use VM's current vcpu since decom findings don't carry a delta
    seen_decom = set()
    decom_vcpu = 0
    for f in actionable_recs:
        if f.category == Category.DECOM and f.vm_id not in seen_decom:
            seen_decom.add(f.vm_id)
            vm = vms_by_id.get(f.vm_id)
            if vm:
                decom_vcpu += vm.vcpus

    return {
        "current_vcpu": total_running_vcpu,
        "downsize_recovery": int(downsize_vcpu),
        "decom_recovery": int(decom_vcpu),
        "remaining": max(0, total_running_vcpu - int(downsize_vcpu) - int(decom_vcpu)),
    }


def _build_archetypes(vms):
    """Return archetype distribution and VM list."""
    dist = {}
    vm_list = []
    for vm in vms:
        arch = vm.workload_archetype.value if hasattr(vm, 'workload_archetype') else "unknown"
        dist[arch] = dist.get(arch, 0) + 1
        vm_list.append({
            "vm_name": vm.vm_name,
            "subscription_name": vm.subscription_name,
            "resource_group": vm.resource_group,
            "vm_sku": vm.vm_sku,
            "vcpus": vm.vcpus,
            "archetype": arch,
            "inferred_role": vm.inferred_workload_role if hasattr(vm, 'inferred_workload_role') else None,
        })
    return {"distribution": dist, "vms": vm_list}


def _build_capacity_hygiene(findings):
    """Return per-subscription QTA-OPS-001 sub-check results."""
    hygiene_findings = [f for f in findings if f.code == "QTA-OPS-001"]
    result = []
    for f in hygiene_findings:
        sub_id = f.vm_id.replace("/subscriptions/", "").split("/")[0]
        subchecks = f.deltas.get("subchecks", []) if f.deltas else []
        row = {"subscription": sub_id, "overall": "fail" if subchecks else "pass"}
        for check in subchecks:
            label = check.get("label", "")
            passed = check.get("pass", False)
            if label.startswith("A:"): row["a"] = passed
            elif label.startswith("B:"): row["b"] = passed
            elif label.startswith("C:"): row["c"] = passed
            elif label.startswith("D:"): row["d"] = passed
            elif label.startswith("E:"): row["e"] = passed
        result.append(row)
    return result


def _build_quick_wins(findings, top=10):
    """Return top N findings sorted by confidence_score x |vcpu_delta|."""
    from cloudopt.analyzer.taxonomy import Readiness
    scored = []
    for f in findings:
        if f.readiness != Readiness.READY:
            continue
        score = f.confidence_score or 0
        vcpu_delta = abs(f.deltas.get("vcpu", 0) or 0) if f.deltas else 0
        priority = score * max(vcpu_delta, 1)
        scored.append((priority, f))
    scored.sort(key=lambda x: -x[0])
    result = []
    for priority, f in scored[:top]:
        result.append({
            "finding_id": f"{f.code}:{f.vm_id}",
            "code": f.code,
            "vm_id": f.vm_id,
            "category": f.category.value,
            "current": f.current,
            "proposed": f.proposed,
            "rationale": (f.rationale or "")[:200],
            "confidence_score": f.confidence_score,
            "confidence": f.confidence.value if f.confidence else None,
            "readiness": f.readiness.value,
            "vcpu_delta": f.deltas.get("vcpu", 0) if f.deltas else 0,
            "priority_score": round(priority, 1),
        })
    return result


def _load_findings_status(data_path: Path) -> None:
    """Load status side-car CSV if present."""
    from cloudopt.export.status import load_status
    csv_path = data_path.parent / (data_path.stem + "_status.csv")
    if csv_path.exists():
        _DATA["findings_status"] = load_status(csv_path)
    else:
        _DATA["findings_status"] = {}


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

    @app.get("/api/summary/overview")
    async def summary_overview():
        return _build_overview(_DATA["vms"], _DATA["findings"], _DATA["quota"])

    @app.get("/api/summary/subscription")
    async def summary_subscription():
        return _aggregate_by_subscription(_DATA["vms"], _DATA["metrics_by_vm"], _DATA["findings"])

    @app.get("/api/summary/resource-group")
    async def summary_resource_group():
        return _aggregate_by_rg(_DATA["vms"], _DATA["metrics_by_vm"], _DATA["findings"])

    @app.get("/api/summary/groups")
    async def summary_groups():
        return _aggregate_flat_groups(_DATA["vms"], _DATA["metrics_by_vm"], _DATA["findings"])

    @app.get("/api/summary/sku")
    async def summary_sku():
        return _aggregate_by_sku(_DATA["vms"], _DATA["metrics_by_vm"])

    @app.get("/api/summary/per-vm")
    async def summary_per_vm(
        subscription: str | None = Query(None),
        resource_group: str | None = Query(None),
        sku: str | None = Query(None),
        search: str | None = Query(None),
    ):
        return _aggregate_per_vm(
            _DATA["vms"], _DATA["metrics_by_vm"], _DATA["findings"],
            subscription, resource_group, sku, search,
        )

    @app.get("/api/vm-insights")
    async def vm_insights_view(
        subscription: str | None = Query(None),
        resource_group: str | None = Query(None),
        search: str | None = Query(None),
    ):
        return _get_vm_insights(_DATA["vms"], _DATA["metrics_by_vm"], subscription, resource_group, search)

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

    @app.get("/api/capacity-reservations")
    async def capacity_reservations_view():
        result = []
        for crg in _DATA["capacity_reservations"]:
            result.append({
                "group_name": crg.group_name,
                "subscription_id": crg.masked_subscription_id(),
                "group_id": crg.masked_group_id(),
                "region": crg.region,
                "zones": crg.zones,
                "reserved_count_total": crg.reserved_count_total,
                "used_count_total": crg.used_count_total,
                "fill_rate_pct": crg.fill_rate_pct,
                "reservations": [
                    {
                        "reservation_name": item.reservation_name,
                        "sku_name": item.sku_name,
                        "reserved_count": item.reserved_count,
                        "used_count": item.used_count,
                        "zone": item.zone,
                    }
                    for item in crg.reservations
                ],
            })
        return result

    @app.get("/api/deployment-failures")
    async def deployment_failures_view(
        error_class: str | None = Query(None),
    ):
        items = _DATA["deployment_failures"]
        if error_class:
            items = [f for f in items if f.error_class == error_class]
        return [
            {
                "resource_id": f.masked_resource_id(),
                "resource_name": f.resource_name,
                "resource_type": f.resource_type,
                "subscription_id": f.masked_subscription_id(),
                "resource_group": f.resource_group,
                "region": f.region,
                "error_class": f.error_class,
                "operation_name": f.operation_name,
                "status_message": f.status_message,
                "timestamp": f.timestamp,
            }
            for f in items
        ]

    @app.get("/api/findings")
    async def findings_view(
        type: str | None = Query(None),
        category: str | None = Query(None),
        readiness: str | None = Query(None),
        confidence: str | None = Query(None),
    ):
        items = _DATA["findings"]
        if type:
            items = [f for f in items if f.finding_type.value.lower() == type.lower()]
        if category:
            items = [f for f in items if f.category.value.lower() == category.lower()]
        if readiness:
            items = [f for f in items if f.readiness.value.lower() == readiness.lower()]
        if confidence:
            items = [f for f in items if f.confidence and f.confidence.value.lower() == confidence.lower()]
        return [_finding_json(f) for f in items]

    @app.get("/api/source-coverage")
    async def source_coverage_view():
        return _DATA["source_coverage"]

    @app.get("/api/disks")
    async def disks_view(
        subscription: str | None = Query(None),
        resource_group: str | None = Query(None),
        sku: str | None = Query(None),
        pv2_only: bool = Query(False),
        search: str | None = Query(None),
    ):
        items: list[DiskInventory] = _DATA["disks"]
        if subscription:
            items = [d for d in items if d.subscription_name == subscription]
        if resource_group:
            items = [d for d in items if d.resource_group == resource_group]
        if sku:
            items = [d for d in items if (d.sku_name or "") == sku]
        if pv2_only:
            items = [d for d in items if d.is_premium_v1 and d.is_data_disk and d.managed_by]
        if search:
            q = search.lower()
            items = [
                d for d in items
                if q in d.disk_name.lower() or q in d.resource_group.lower()
            ]
        return [_disk_json(d) for d in items]

    @app.get("/api/summary/waterfall")
    async def summary_waterfall():
        return _build_waterfall(_DATA["vms"], _DATA["findings"])

    @app.get("/api/summary/archetypes")
    async def summary_archetypes():
        return _build_archetypes(_DATA["vms"])

    @app.get("/api/summary/capacity-hygiene")
    async def summary_capacity_hygiene():
        return _build_capacity_hygiene(_DATA["findings"])

    @app.get("/api/summary/quick-wins")
    async def summary_quick_wins(top: int = Query(10)):
        return _build_quick_wins(_DATA["findings"], top=top)

    @app.get("/api/findings-status")
    async def findings_status_get():
        return _DATA.get("findings_status", {})

    @app.patch("/api/findings-status/{finding_id:path}")
    async def findings_status_patch(finding_id: str, body: dict):
        from cloudopt.export.status import update_status
        status_map = _DATA.get("findings_status", {})
        status = body.get("status", "open")
        owner = body.get("owner", "")
        due_date = body.get("due_date", "")
        notes = body.get("notes", "")
        # Write to side-car CSV if path is available
        data_path = _DATA.get("path")
        if data_path:
            csv_path = data_path.parent / (data_path.stem + "_status.csv")
            update_status(csv_path, finding_id, status, owner, due_date, notes)
            from cloudopt.export.status import load_status
            _DATA["findings_status"] = load_status(csv_path)
        else:
            import datetime
            status_map[finding_id] = {
                "status": status, "owner": owner,
                "due_date": due_date, "notes": notes,
                "updated_utc": datetime.datetime.utcnow().isoformat(),
            }
            _DATA["findings_status"] = status_map
        return {"ok": True, "finding_id": finding_id}

    @app.get("/api/export/findings-csv")
    async def export_findings_csv():
        from fastapi.responses import StreamingResponse
        from cloudopt.export.status import merge_status_into_findings
        import csv, io
        status_map = _DATA.get("findings_status", {})
        rows = merge_status_into_findings(_DATA["findings"], status_map)
        output = io.StringIO()
        if rows:
            fieldnames = list(rows[0].keys())
        else:
            fieldnames = ["finding_id", "code", "category", "vm_id", "readiness", "confidence_score", "status"]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=findings.csv"},
        )

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
    import logging as _logging
    _logger = _logging.getLogger(__name__)

    vms, metrics, recommendations, metadata = read_workbook(path)
    quota = read_quota_from_workbook(path)
    _store(vms, metrics, recommendations, metadata, quota)
    # Excel path: no capacity reservations or deployment failures stored in workbook
    _DATA["capacity_reservations"] = []
    _DATA["deployment_failures"] = []
    # Workload archetype enrichment (no AI metrics available from Excel)
    from cloudopt.analyzer.archetype import enrich_vm_archetype
    enrich_vm_archetype(vms, metrics)

    # Prefer pre-computed findings from the 'Decisions' sheet (produced with the
    # live SKU catalog at analysis time).  These include generation-swap and
    # rightsize findings that the offline catalog cannot produce.
    from cloudopt.export.excel import read_findings_from_workbook
    findings = read_findings_from_workbook(path)

    if not findings:
        # Fallback: re-run detectors with the offline no-op catalog.  Only
        # catalog-independent rules (legacy-SKU, stopped/idle VM, quota) will
        # fire.  Log any exception so silent failures become diagnosable.
        from cloudopt.analyzer.detectors import run_all
        from cloudopt.analyzer.sku_catalog import OfflineSkuCatalog
        meta = _DATA.get("metadata")
        thresholds = meta.thresholds if meta else None
        try:
            from cloudopt.models import CollectionThresholds
            if thresholds is None:
                thresholds = CollectionThresholds()
            findings = run_all(
                vms=vms,
                metrics=metrics,
                quota_items=_DATA.get("quota", []),
                thresholds=thresholds,
                catalog=OfflineSkuCatalog(),
                resources=None,
                rsvp_orders=_DATA.get("reservations", []),
                crg_items=[],
                enriched_map=None,
            )
        except Exception as _exc:
            _logger.warning(
                "run_all failed while loading Excel workbook '%s'; findings will be empty. Error: %s",
                path, _exc,
            )
            findings = []

    _DATA["findings"] = findings
    _load_findings_status(path)
    _DATA["vmss_groups"] = read_vmss_groups_from_workbook(path)
    from cloudopt.export.excel import read_disks_from_workbook
    _DATA["disks"] = read_disks_from_workbook(path)


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

    # --- Capacity Reservation Groups ---
    from cloudopt.models import CapacityReservationGroup
    crg: list[CapacityReservationGroup] = []
    for d in raw.get("capacity_reservations", []):
        try:
            crg.append(CapacityReservationGroup(**d))
        except Exception:
            pass
    _DATA["capacity_reservations"] = crg

    # --- Deployment Failures ---
    from cloudopt.models import DeploymentFailureEntry
    depfail: list[DeploymentFailureEntry] = []
    for d in raw.get("deployment_failures", []):
        try:
            depfail.append(DeploymentFailureEntry(**d))
        except Exception:
            pass
    _DATA["deployment_failures"] = depfail

    # --- Disk inventory (microsoft.compute/disks) ---
    from cloudopt.models import DiskInventory
    disks: list[DiskInventory] = []
    for d in raw.get("disks", []):
        try:
            disks.append(DiskInventory(**d))
        except Exception:
            pass
    _DATA["disks"] = disks

    # --- Capacity Alerts (QTA-OPS-001) ---
    from cloudopt.models import CapacityAlert, CapacityAlertType
    capacity_alerts: list[CapacityAlert] = []
    for d in raw.get("capacity_alerts", []):
        try:
            atype = d.get("alert_type", "metric_alert")
            try:
                atype_enum = CapacityAlertType(atype)
            except ValueError:
                atype_enum = CapacityAlertType.METRIC_ALERT
            capacity_alerts.append(CapacityAlert(
                resource_id=d.get("resource_id", ""),
                subscription_id=d.get("subscription_id", ""),
                alert_type=atype_enum,
                name=d.get("name", ""),
                enabled=d.get("enabled", False),
                signals=d.get("signals", []),
                scopes=d.get("scopes", []),
            ))
        except Exception:
            pass

    # --- VMSS Uniform groups ---
    from cloudopt.models import ManagedComputeGroupRow
    vmss_groups: list[ManagedComputeGroupRow] = []
    for d in raw.get("vmss_groups", []):
        try:
            vmss_groups.append(ManagedComputeGroupRow(**d))
        except Exception:
            pass
    _DATA["vmss_groups"] = vmss_groups

    # --- Workload archetype enrichment ---
    from cloudopt.analyzer.archetype import enrich_vm_archetype
    from cloudopt.models import AppInsightsMetrics as _AIM, DailyDataPoint as _DDP
    ai_metrics_list: list = []
    for d in raw.get("appinsights_metrics", []):
        try:
            ts2 = [_DDP(**p) for p in d.get("time_series", [])]
            ai_metrics_list.append(_AIM(**{k: v for k, v in d.items() if k != "time_series"}, time_series=ts2))
        except Exception:
            pass
    _ai_by_resource: dict = {}
    for _m in ai_metrics_list:
        _ai_by_resource.setdefault(_m.resource_id, []).append(_m)
    enrich_vm_archetype(vms, metrics, ai_metrics_by_resource=_ai_by_resource or None)

    # --- Detector pipeline → Finding list --------------------------------
    from cloudopt.analyzer.detectors import run_all
    from cloudopt.analyzer.sku_catalog import OfflineSkuCatalog
    from cloudopt.enrichment.joiner import join_monitoring_data

    meta = _DATA.get("metadata")
    thresholds = meta.thresholds if meta else None
    try:
        from cloudopt.models import CollectionThresholds
        if thresholds is None:
            thresholds = CollectionThresholds()
        findings = run_all(
            vms=vms,
            metrics=metrics,
            quota_items=_DATA.get("quota", []),
            thresholds=thresholds,
            catalog=OfflineSkuCatalog(),
            resources=None,
            rsvp_orders=_DATA.get("reservations", []),
            crg_items=_DATA.get("capacity_reservations", []),
            enriched_map=None,
            capacity_alerts=capacity_alerts or None,
        )
    except Exception:
        findings = []
    _DATA["findings"] = findings

    _load_findings_status(path)

    # --- Source coverage (from enriched metrics if available) ------------
    _DATA["source_coverage"] = []


def _store(vms, metrics, recommendations, metadata, quota=None) -> None:
    from cloudopt.export.excel import _group_metrics
    _DATA["vms"] = vms
    _DATA["metrics_by_vm"] = _group_metrics(metrics)
    _DATA["recommendations"] = recommendations
    _DATA["quota"] = quota or []
    _DATA["metadata"] = metadata
    # reservations and capacity_reservations are populated separately in _load_json


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


def _finding_json(f: Any) -> dict:
    return {
        "vm_id": f.vm_id,
        "category": f.category.value,
        "subcategory": f.subcategory.value if f.subcategory else None,
        "code": f.code,
        "finding_type": f.finding_type.value,
        "current": f.current,
        "proposed": f.proposed,
        "deltas": f.deltas,
        "evidence_sources": f.evidence_sources,
        "confidence": f.confidence.value if f.confidence else None,
        "confidence_score": f.confidence_score,
        "readiness": f.readiness.value,
        "blockers_to_high": f.blockers_to_high,
        "customer_inputs_needed": f.customer_inputs_needed,
        "rationale": f.rationale,
    }


def _disk_json(d: DiskInventory) -> dict:
    return {
        "resource_id": d.masked_resource_id(),
        "disk_name": d.disk_name,
        "subscription_name": d.subscription_name,
        "resource_group": d.resource_group,
        "location": d.location,
        "sku_name": d.sku_name,
        "performance_tier": d.performance_tier,
        "disk_size_gb": d.disk_size_gb,
        "disk_iops_read_write": d.disk_iops_read_write,
        "disk_mbps_read_write": d.disk_mbps_read_write,
        "bursting_enabled": d.bursting_enabled,
        "disk_state": d.disk_state,
        "os_type": d.os_type,
        "zones": d.zones,
        "encryption_type": d.encryption_type,
        "managed_by": d.masked_managed_by(),
        "time_created": d.time_created,
        "pv2_candidate": bool(d.is_premium_v1 and d.is_data_disk and d.managed_by),
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


def _build_overview(
    vms: list[VmInventory],
    findings: list[Any],
    quota: list[Any],
) -> dict:
    from cloudopt.analyzer.taxonomy import Readiness, Confidence, Category
    recs = [f for f in findings if f.finding_type.value == "recommendation"]
    by_cat: dict[str, dict] = {}
    for cat in Category:
        cat_recs = [f for f in recs if f.category == cat]
        by_cat[cat.value] = {
            "total": len(cat_recs),
            "ready": sum(1 for f in cat_recs if f.readiness == Readiness.READY),
            "likely": sum(1 for f in cat_recs if f.readiness == Readiness.LIKELY),
            "discovery": sum(1 for f in cat_recs if f.readiness == Readiness.DISCOVERY),
            "insufficient": sum(1 for f in cat_recs if f.readiness == Readiness.INSUFFICIENT),
        }
    by_conf: dict[str, int] = {conf.value: sum(1 for f in recs if f.confidence == conf) for conf in Confidence}

    # ── New Phase 6/7 fields ──────────────────────────────────────────────
    ready_recs = [f for f in recs if f.readiness == Readiness.READY]
    ready_pct = round(100.0 * len(ready_recs) / max(len(recs), 1), 1)

    scored = [f for f in recs if f.confidence_score is not None]
    confidence_avg = round(
        sum(f.confidence_score for f in scored) / max(len(scored), 1), 1
    ) if scored else 0

    # vCPU opportunity: include READY + LIKELY so the KPI reflects the full
    # right-sizing potential, not just HIGH-confidence findings (RSZ and
    # DCM-IDL are MEDIUM by default and would otherwise never contribute).
    actionable_readiness = {Readiness.READY, Readiness.LIKELY}
    actionable_recs = [f for f in recs if f.readiness in actionable_readiness]
    vcpu_opportunity = sum(
        abs(f.deltas.get("vcpu", 0) or 0)
        for f in actionable_recs
        if f.deltas and (f.deltas.get("vcpu", 0) or 0) < 0
    )
    # Add decom vcpu (decom findings don't carry vcpu delta — use VM inventory)
    vms_by_id = {v.resource_id: v for v in vms}
    seen_decom: set = set()
    for f in actionable_recs:
        if f.category == Category.DECOM and f.vm_id not in seen_decom:
            seen_decom.add(f.vm_id)
            vm = vms_by_id.get(f.vm_id)
            if vm:
                vcpu_opportunity += vm.vcpus

    generation_gap_count = sum(
        1 for f in recs
        if f.code.startswith("SWP-GEN-") or f.code.startswith("SWP-LFC-")
    )

    hist_buckets = [
        "0-10", "10-20", "20-30", "30-40", "40-50",
        "50-60", "60-70", "70-80", "80-90", "90-100",
    ]
    confidence_histogram: dict[str, int] = {b: 0 for b in hist_buckets}
    for f in recs:
        if f.confidence_score is not None:
            idx = min(int(f.confidence_score) // 10, 9)
            confidence_histogram[hist_buckets[idx]] += 1

    return {
        "total_vms": len(vms),
        "running_vms": sum(1 for v in vms if not _is_stopped(v)),
        "stopped_vms": sum(1 for v in vms if _is_stopped(v)),
        "total_findings": len(findings),
        "total_recommendations": len(recs),
        "ready_count": sum(1 for f in recs if f.readiness == Readiness.READY),
        "likely_count": sum(1 for f in recs if f.readiness == Readiness.LIKELY),
        "discovery_count": sum(1 for f in recs if f.readiness == Readiness.DISCOVERY),
        "quota_alerts": sum(1 for q in quota if q.alert),
        "by_category": by_cat,
        "by_confidence": by_conf,
        "ready_pct": ready_pct,
        "confidence_avg": confidence_avg,
        "vcpu_opportunity": int(vcpu_opportunity),
        "generation_gap_count": generation_gap_count,
        "confidence_histogram": confidence_histogram,
    }


def _aggregate_by_subscription(
    vms: list[VmInventory],
    metrics_by_vm: dict,
    findings: list[Any],
) -> list[dict]:
    """One row per subscription."""
    groups: dict[str, list[VmInventory]] = {}
    for vm in vms:
        groups.setdefault(vm.subscription_name, []).append(vm)
    # index findings by vm_id
    from cloudopt.analyzer.taxonomy import Readiness
    findings_by_vm: dict[str, list] = {}
    for f in findings:
        findings_by_vm.setdefault(f.vm_id, []).append(f)
    result = []
    for sub_name, sub_vms in sorted(groups.items()):
        vm_ids = {v.resource_id for v in sub_vms}
        sub_findings = [f for vid in vm_ids for f in findings_by_vm.get(vid, [])]
        result.append({
            "subscription_name": sub_name,
            "vm_count": len(sub_vms),
            "running_count": sum(1 for v in sub_vms if not _is_stopped(v)),
            "stopped_count": sum(1 for v in sub_vms if _is_stopped(v)),
            "avg_cpu_pct": _avg_metric_group(sub_vms, metrics_by_vm, "Percentage CPU", "avg"),
            "avg_mem_pct": _avg_mem_pct_group(sub_vms, metrics_by_vm),
            "finding_count": len(sub_findings),
            "ready_count": sum(1 for f in sub_findings if f.readiness == Readiness.READY),
            "sku_distribution": _sku_dist(sub_vms),
        })
    return result


def _aggregate_by_rg(
    vms: list[VmInventory],
    metrics_by_vm: dict,
    findings: list[Any],
) -> list[dict]:
    """One row per (subscription, resource_group)."""
    groups: dict[tuple, list[VmInventory]] = {}
    for vm in vms:
        groups.setdefault((vm.subscription_name, vm.resource_group), []).append(vm)
    from cloudopt.analyzer.taxonomy import Readiness
    findings_by_vm: dict[str, list] = {}
    for f in findings:
        findings_by_vm.setdefault(f.vm_id, []).append(f)
    result = []
    for (sub_name, rg), rg_vms in sorted(groups.items()):
        vm_ids = {v.resource_id for v in rg_vms}
        rg_findings = [f for vid in vm_ids for f in findings_by_vm.get(vid, [])]
        result.append({
            "subscription_name": sub_name,
            "resource_group": rg,
            "vm_count": len(rg_vms),
            "running_count": sum(1 for v in rg_vms if not _is_stopped(v)),
            "stopped_count": sum(1 for v in rg_vms if _is_stopped(v)),
            "avg_cpu_pct": _avg_metric_group(rg_vms, metrics_by_vm, "Percentage CPU", "avg"),
            "avg_mem_pct": _avg_mem_pct_group(rg_vms, metrics_by_vm),
            "finding_count": len(rg_findings),
            "ready_count": sum(1 for f in rg_findings if f.readiness == Readiness.READY),
        })
    return result


def _aggregate_by_sku(
    vms: list[VmInventory],
    metrics_by_vm: dict,
) -> list[dict]:
    """One row per vm_sku."""
    groups: dict[str, list[VmInventory]] = {}
    for vm in vms:
        groups.setdefault(vm.vm_sku, []).append(vm)
    result = []
    for sku, sku_vms in sorted(groups.items(), key=lambda x: -len(x[1])):
        sample = sku_vms[0]
        result.append({
            "vm_sku": sku,
            "vcpus": sample.vcpus,
            "memory_gb": sample.memory_gb,
            "vm_count": len(sku_vms),
            "running_count": sum(1 for v in sku_vms if not _is_stopped(v)),
            "stopped_count": sum(1 for v in sku_vms if _is_stopped(v)),
            "avg_cpu_pct": _avg_metric_group(sku_vms, metrics_by_vm, "Percentage CPU", "avg"),
            "avg_mem_pct": _avg_mem_pct_group(sku_vms, metrics_by_vm),
        })
    return result


def _aggregate_flat_groups(
    vms: list[VmInventory],
    metrics_by_vm: dict,
    findings: list[Any] | None = None,
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
    from cloudopt.analyzer.taxonomy import Readiness
    findings_by_vm: dict[str, list] = {}
    for f in (findings or []):
        findings_by_vm.setdefault(f.vm_id, []).append(f)
    result = []
    for (sub_name, rg, g_name, g_type, sku), group_vms in sorted(groups.items()):
        vm_ids = {v.resource_id for v in group_vms}
        grp_findings = [f for vid in vm_ids for f in findings_by_vm.get(vid, [])]
        result.append({
            "subscription_name": sub_name,
            "resource_group": rg,
            "group_name": g_name,
            "group_type": g_type,
            "vm_sku": sku,
            "vm_count": len(group_vms),
            "avg_cpu_pct": _avg_metric_group(group_vms, metrics_by_vm, "Percentage CPU", "avg"),
            "p95_cpu_pct": _avg_metric_group(group_vms, metrics_by_vm, "Percentage CPU", "p95"),
            "avg_mem_pct": _avg_mem_pct_group(group_vms, metrics_by_vm),
            "finding_count": len(grp_findings),
            "ready_count": sum(1 for f in grp_findings if f.readiness == Readiness.READY),
        })

    # Merge VMSS Uniform groups — these don't surface as individual VMs in ARG
    _SVC_LABEL: dict[str, str] = {
        "AKS": "VMSS Uniform (AKS)",
        "AVD": "VMSS Uniform (AVD)",
        "Databricks": "VMSS Uniform (Databricks)",
        "Azure Batch": "VMSS Uniform (Azure Batch)",
        "AML": "VMSS Uniform (AML)",
        "ARO": "VMSS Uniform (ARO)",
        "HDInsight": "VMSS Uniform (HDInsight)",
    }
    for g in (_DATA.get("vmss_groups") or []):
        svc_val = g.parent_service_type.value if g.parent_service_type else "Standalone VMSS"
        group_type = _SVC_LABEL.get(svc_val, "VMSS Uniform")
        result.append({
            "subscription_name": g.subscription_name,
            "resource_group": g.resource_group,
            "group_name": g.vmss_name or g.parent_service_name or "(unknown)",
            "group_type": group_type,
            "vm_sku": g.vm_sku,
            "vm_count": g.instance_count,
            "avg_cpu_pct": g.avg_cpu_pct,
            "p95_cpu_pct": g.p95_cpu_pct,
            "avg_mem_pct": g.avg_mem_pct,
        })

    result.sort(key=lambda r: (r["subscription_name"], r["resource_group"], r["group_name"], r["vm_sku"]))
    return result


def _aggregate_flat_sub(
    vms: list[VmInventory],
    metrics_by_vm: dict,
) -> list[dict]:
    """Kept for compatibility — delegates to _aggregate_by_subscription."""
    return _aggregate_by_subscription(vms, metrics_by_vm, _DATA.get("findings", []))


def _aggregate_flat_rg(
    vms: list[VmInventory],
    metrics_by_vm: dict,
) -> list[dict]:
    """Kept for compatibility — delegates to _aggregate_by_rg."""
    return _aggregate_by_rg(vms, metrics_by_vm, _DATA.get("findings", []))


def _rec_action_text(r: VmRecommendation) -> str:
    """Plain-English recommended action to present to the customer."""
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


def _aggregate_per_vm(
    vms: list[VmInventory],
    metrics_by_vm: dict,
    findings: list[Any],
    subscription: str | None = None,
    resource_group: str | None = None,
    sku: str | None = None,
    search: str | None = None,
) -> list[dict]:
    """One row per VM — used by the Perf by VM view."""
    from cloudopt.analyzer.taxonomy import Readiness

    findings_by_vm: dict[str, list] = {}
    for f in findings:
        findings_by_vm.setdefault(f.vm_id, []).append(f)

    result_vms = list(vms)
    if subscription:
        result_vms = [v for v in result_vms if v.subscription_name == subscription]
    if resource_group:
        result_vms = [v for v in result_vms if v.resource_group == resource_group]
    if sku:
        result_vms = [v for v in result_vms if v.vm_sku == sku]
    if search:
        q = search.lower()
        result_vms = [
            v for v in result_vms
            if q in v.vm_name.lower() or q in v.resource_group.lower()
        ]

    result = []
    for vm in result_vms:
        vm_findings = findings_by_vm.get(vm.resource_id, [])
        avset_vmss = vm.availability_set_name or vm.vmss_name or None
        result.append({
            "vm_name": vm.vm_name,
            "subscription_name": vm.subscription_name,
            "resource_group": vm.resource_group,
            "region": vm.region,
            "zone": vm.availability_zone,
            "vm_sku": vm.vm_sku,
            "vcpus": vm.vcpus,
            "memory_gb": vm.memory_gb,
            "power_state": vm.power_state,
            "avset_vmss": avset_vmss,
            "avg_cpu_pct": _avg_metric_group([vm], metrics_by_vm, "Percentage CPU", "avg"),
            "p95_cpu_pct": _avg_metric_group([vm], metrics_by_vm, "Percentage CPU", "p95"),
            "avg_mem_pct": _avg_mem_pct_group([vm], metrics_by_vm),
            "finding_count": len(vm_findings),
            "ready_count": sum(1 for f in vm_findings if f.readiness == Readiness.READY),
        })
    return result


def _get_vm_insights(
    vms: list[VmInventory],
    metrics_by_vm: dict,
    subscription: str | None = None,
    resource_group: str | None = None,
    search: str | None = None,
) -> list[dict]:
    """Per-VM platform metric row for the VM Insights view."""

    def _bytes_to_mbps(m_obj: Any, stat: str = "avg") -> float | None:
        if m_obj is None:
            return None
        v = getattr(m_obj, stat, None)
        return round(v / (1024 * 1024), 3) if v is not None else None

    result = []
    for vm in vms:
        if subscription and vm.subscription_name != subscription:
            continue
        if resource_group and vm.resource_group != resource_group:
            continue
        if search:
            q = search.lower()
            if q not in vm.vm_name.lower() and q not in vm.resource_group.lower():
                continue

        m = metrics_by_vm.get(vm.resource_id, {})
        cpu_m = m.get("Percentage CPU")
        mem_m = m.get("Available Memory Bytes")
        disk_read_m = m.get("Disk Read Bytes/Sec") or m.get("Data Disk Read Bytes/sec") or m.get("OS Disk Read Bytes/sec")
        disk_write_m = m.get("Disk Write Bytes/Sec") or m.get("Data Disk Write Bytes/sec") or m.get("OS Disk Write Bytes/sec")
        net_in_m = m.get("Network In Total") or m.get("Network In")
        net_out_m = m.get("Network Out Total") or m.get("Network Out")

        avg_cpu = round(cpu_m.avg, 2) if cpu_m and cpu_m.avg is not None else None
        p95_cpu = round(cpu_m.p95, 2) if cpu_m and getattr(cpu_m, "p95", None) is not None else None
        avg_mem_pct: float | None = None
        if mem_m and mem_m.avg is not None and vm.memory_gb > 0:
            avail_gb = mem_m.avg / (1024 ** 3)
            avg_mem_pct = round(max(0.0, min(100.0, (1 - avail_gb / vm.memory_gb) * 100)), 2)

        row: dict = {
            "vm_name": vm.vm_name,
            "subscription_name": vm.subscription_name,
            "resource_group": vm.resource_group,
            "vm_sku": vm.vm_sku,
            "avg_cpu_pct": avg_cpu,
            "p95_cpu_pct": p95_cpu,
            "avg_mem_pct": avg_mem_pct,
            "disk_read_mbps": _bytes_to_mbps(disk_read_m),
            "disk_write_mbps": _bytes_to_mbps(disk_write_m),
            "net_in_mbps": _bytes_to_mbps(net_in_m),
            "net_out_mbps": _bytes_to_mbps(net_out_m),
            "has_metrics": bool(m),
        }
        result.append(row)
    return result
