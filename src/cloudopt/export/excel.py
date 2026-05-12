"""Multi-sheet Excel workbook generation using openpyxl.

Sheet layout (13 sheets):
  1. Workload Information
  2. Quota Utilization
  3. VM Inventory
  4. Performance Summary
  5. SKU Perf by Subscription
  6. SKU Perf by Resource Group
  7. SKU Perf by VMSS
  8. SKU Perf by Availability Set
  9. Optimizations
  10. Raw Metrics
  11. Collection Metadata
  12. SubscriptionsZoneMapping
  13. Inventory              ← full ARG resource inventory (new)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import (
    Alignment,
    Border,
    Font,
    PatternFill,
    Side,
)
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.worksheet.worksheet import Worksheet

from cloudopt.enrichment.schema import EnrichedVmMetrics
from cloudopt.models import (
    AdvisorRecommendation,
    AppInsightsInventory,
    AppInsightsMetrics,
    AzureResource,
    CapacityReservationGroup,
    CollectionMetadata,
    QuotaItem,
    ReservationOrder,
    SubscriptionZoneMapping,
    VmInventory,
    VmMetrics,
    VmRecommendation,
    WorkloadInfo,
    mask_subscription_id,
)

# ---------------------------------------------------------------------------
# Colour constants
# ---------------------------------------------------------------------------
_HDR_FILL = PatternFill("solid", fgColor="1F4E79")      # dark blue header
_HDR_FONT = Font(color="FFFFFF", bold=True, size=10)
_ALT_FILL = PatternFill("solid", fgColor="D6E4F0")      # alternating row
_CSA_FILL = PatternFill("solid", fgColor="BDD7EE")      # Analyst-editable column
_GREEN_FILL = PatternFill("solid", fgColor="C6EFCE")
_YELLOW_FILL = PatternFill("solid", fgColor="FFEB9C")
_RED_FILL = PatternFill("solid", fgColor="FFC7CE")

_THIN_BORDER = Border(
    left=Side(style="thin", color="B8CCE4"),
    right=Side(style="thin", color="B8CCE4"),
    bottom=Side(style="thin", color="B8CCE4"),
)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def write_workbook(
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
    resources: list[AzureResource] | None = None,
    reservations: list[ReservationOrder] | None = None,
    capacity_reservations: list[CapacityReservationGroup] | None = None,
) -> None:
    """Write the full Excel workbook to *path*."""
    wb = Workbook()
    wb.remove(wb.active)  # remove default blank sheet

    metrics_by_vm = _group_metrics(metrics)

    _sheet_workload_info(wb, workload_info or WorkloadInfo())
    _sheet_quota(wb, quota or [])
    _sheet_inventory(wb, vms)
    _sheet_perf_summary(wb, vms, metrics_by_vm)
    _sheet_sku_flat(
        wb, vms, metrics_by_vm, "SKU Perf by Subscription",
        ["subscription_name"], ["Subscription"], [30], "TblSkuBySub",
    )
    _sheet_sku_flat(
        wb, vms, metrics_by_vm, "SKU Perf by Resource Group",
        ["subscription_name", "resource_group"], ["Subscription", "Resource Group"],
        [30, 28], "TblSkuByRG",
    )
    vmss_vms = [v for v in vms if v.vmss_name]
    _sheet_sku_flat(
        wb, vmss_vms, metrics_by_vm, "SKU Perf by VMSS",
        ["subscription_name", "resource_group", "vmss_name"],
        ["Subscription", "Resource Group", "VMSS Name"], [30, 28, 28], "TblSkuByVMSS",
    )
    avset_vms = [v for v in vms if v.availability_set_name]
    _sheet_sku_flat(
        wb, avset_vms, metrics_by_vm, "SKU Perf by Availability Set",
        ["subscription_name", "resource_group", "availability_set_name"],
        ["Subscription", "Resource Group", "Availability Set"], [30, 28, 28], "TblSkuByAvSet",
    )
    _sheet_recommendations(wb, recommendations)
    _sheet_advisor(wb, advisor or [])
    _sheet_raw_metrics(wb, metrics)
    _sheet_appinsights(wb, appinsights or [], appinsights_metrics or [])
    _sheet_zone_mapping(wb, zone_mappings or [])
    if enriched_metrics:
        _sheet_monitoring_data(wb, enriched_metrics)
    _sheet_resources(wb, resources or [])
    _sheet_reservations(wb, reservations or [])
    _sheet_capacity_reservations(wb, capacity_reservations or [])
    _sheet_metadata(wb, metadata)

    wb.save(path)


def read_workbook(path: Path) -> tuple[list[VmInventory], list[VmMetrics], list[VmRecommendation], CollectionMetadata]:
    """Read an existing workbook back into model objects.

    Analyst-edited fields (workload, application, environment, criticality, owner,
    custom, manual_override, notes) are preserved from the file.
    """
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    vms = _read_inventory_sheet(wb)
    metrics = _read_raw_metrics_sheet(wb)
    recommendations = _read_recommendations_sheet(wb)
    metadata = _read_metadata_sheet(wb)
    return vms, metrics, recommendations, metadata


def read_quota_from_workbook(path: Path) -> list[QuotaItem]:
    """Read the Quota Utilization sheet from an existing workbook."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    return _read_quota_sheet(wb)


# ---------------------------------------------------------------------------
# Sheet 1: VM Inventory
# ---------------------------------------------------------------------------

_INVENTORY_COLS: list[tuple[str, str, int]] = [
    # (header, field_name_or_special, width)
    ("VM Name", "vm_name", 28),
    ("Subscription", "subscription_name", 30),
    ("Subscription ID", "masked_subscription_id", 42),
    ("Resource Group", "resource_group", 28),
    ("Region", "region", 16),
    ("VM SKU", "vm_sku", 22),
    ("vCPUs", "vcpus", 8),
    ("Memory (GB)", "memory_gb", 12),
    ("OS Type", "os_type", 12),
    ("OS Version", "os_version", 20),
    ("Power State", "power_state", 18),
    ("Image Publisher", "image_publisher", 26),
    ("Image Offer", "image_offer", 22),
    ("Image SKU", "image_sku", 24),
    ("Image Version", "image_version", 22),
    ("Avail. Zone", "availability_zone", 12),
    ("NIC Count", "nic_count", 10),
    ("Disk Count", "disk_count", 10),
    ("Disk Sizes (GB)", "disk_sizes_gb", 22),
    ("VMSS Name", "vmss_name", 24),
    ("Availability Set", "availability_set_name", 24),
    ("Resource ID", "masked_resource_id", 80),
    # Analyst-editable (light blue)
    ("Workload", "workload", 18),
    ("Application", "application", 18),
    ("Environment", "environment", 16),
    ("Criticality", "criticality", 14),
    ("Owner", "owner", 20),
    ("Custom", "custom", 20),
]

_CSA_START_COL = 23  # first analyst-editable column (1-indexed); shifts when columns added before it


def _sheet_inventory(wb: Workbook, vms: list[VmInventory]) -> None:
    ws = wb.create_sheet("VM Inventory")

    headers = [c[0] for c in _INVENTORY_COLS]
    _write_header(ws, headers)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    for row_idx, vm in enumerate(vms, start=2):
        alt = row_idx % 2 == 0
        for col_idx, (_, field, _) in enumerate(_INVENTORY_COLS, start=1):
            val: Any
            if field == "masked_subscription_id":
                val = vm.masked_subscription_id()
            elif field == "masked_resource_id":
                val = vm.masked_resource_id()
            elif field == "disk_sizes_gb":
                val = ", ".join(str(int(s)) for s in vm.disk_sizes_gb)
            else:
                val = getattr(vm, field, None)
                if val is None:
                    val = ""

            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = _THIN_BORDER
            cell.font = Font(size=9)
            cell.alignment = Alignment(vertical="top", wrap_text=False)

            is_csa = col_idx >= _CSA_START_COL
            if is_csa:
                cell.fill = _CSA_FILL
            elif alt:
                cell.fill = _ALT_FILL

    for col_idx, (_, _, width) in enumerate(_INVENTORY_COLS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    _add_table(ws, "TblInventory", len(vms))


# ---------------------------------------------------------------------------
# Sheet 2: Performance Summary
# ---------------------------------------------------------------------------

_PERF_METRICS = [
    ("Avg CPU %",       "Percentage CPU", "avg"),
    ("P95 CPU %",       "Percentage CPU", "p95"),
    ("P99 CPU %",       "Percentage CPU", "p99"),
    ("Max CPU % (Peak)", "Percentage CPU", "max"),
    ("Min CPU %",       "Percentage CPU", "min"),
    ("Avg Mem Avail (GB)", "Available Memory Bytes", "avg"),
    ("Disk Read IOps", "Disk Read Operations/Sec", "avg"),
    ("Disk Write IOps", "Disk Write Operations/Sec", "avg"),
    ("Net In (GB)", "Network In Total", "avg"),
    ("Net Out (GB)", "Network Out Total", "avg"),
]


def _sheet_perf_summary(
    wb: Workbook,
    vms: list[VmInventory],
    metrics_by_vm: dict[str, dict[str, VmMetrics]],
) -> None:
    ws = wb.create_sheet("Performance Summary")

    static_headers = ["VM Name", "Subscription", "Resource Group", "VM SKU", "vCPUs", "Mem (GB)"]
    metric_headers = [m[0] for m in _PERF_METRICS]
    headers = static_headers + metric_headers
    _write_header(ws, headers)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    for row_idx, vm in enumerate(vms, start=2):
        alt = row_idx % 2 == 0
        vm_met = metrics_by_vm.get(vm.resource_id, {})
        row_data: list[Any] = [
            vm.vm_name, vm.subscription_name, vm.resource_group,
            vm.vm_sku, vm.vcpus, vm.memory_gb,
        ]
        for _, metric_name, stat in _PERF_METRICS:
            m = vm_met.get(metric_name)
            val: float | None = getattr(m, stat, None) if m else None
            if val is not None and metric_name == "Available Memory Bytes":
                val = round(val / (1024 ** 3), 2)
            elif val is not None and metric_name in ("Network In Total", "Network Out Total"):
                val = round(val / (1024 ** 3), 4)
            row_data.append(val)

        for col_idx, value in enumerate(row_data, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.border = _THIN_BORDER
            cell.font = Font(size=9)
            if alt:
                cell.fill = _ALT_FILL

            # Format and colour-code CPU % columns (Avg, P95, P99, Max, Min)
            if col_idx in (7, 8, 9, 10, 11) and value is not None:
                cell.number_format = "0.00"
                if value < 40:
                    cell.fill = _GREEN_FILL
                elif value < 70:
                    cell.fill = _YELLOW_FILL
                else:
                    cell.fill = _RED_FILL

    _auto_width(ws, headers)
    _add_table(ws, "TblPerfSummary", len(vms))


# ---------------------------------------------------------------------------
# Sheet 3–6: SKU flat (normalized row-per-group+sku) sheets
# ---------------------------------------------------------------------------

def _sheet_sku_flat(
    wb: Workbook,
    vms: list[VmInventory],
    metrics_by_vm: dict[str, dict[str, VmMetrics]],
    sheet_name: str,
    group_model_fields: list[str],
    col_headers: list[str],
    col_widths: list[int],
    table_name: str,
) -> None:
    """Flat normalized view: one row per (group_fields..., vm_sku) combination.

    Columns: <group_fields...> | VM SKU | VM Count | Avg CPU % | Avg Mem %
    """
    ws = wb.create_sheet(sheet_name)
    headers = col_headers + ["VM SKU", "VM Count", "Avg CPU %", "P95 CPU %", "P99 CPU %", "Max CPU % (Peak)", "Min CPU %", "Avg Mem %"]
    _write_header(ws, headers)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    # Group vms by (group_fields..., vm_sku)
    row_groups: dict[tuple, list[VmInventory]] = {}
    for vm in vms:
        key = tuple(getattr(vm, f, None) or "(unknown)" for f in group_model_fields) + (vm.vm_sku,)
        row_groups.setdefault(key, []).append(vm)

    n_group = len(group_model_fields)
    avg_cpu_col = n_group + 3  # group cols + VM SKU + VM Count → Avg CPU
    mem_col = avg_cpu_col + 5  # after Avg, P95, P99, Max, Min

    for row_idx, (key, group_vms) in enumerate(sorted(row_groups.items()), start=2):
        alt = row_idx % 2 == 0
        *group_vals, sku = key
        avg_cpu = _avg_metric(group_vms, metrics_by_vm, "Percentage CPU", "avg")
        p95_cpu = _avg_metric(group_vms, metrics_by_vm, "Percentage CPU", "p95")
        p99_cpu = _avg_metric(group_vms, metrics_by_vm, "Percentage CPU", "p99")
        max_cpu = _avg_metric(group_vms, metrics_by_vm, "Percentage CPU", "max")
        min_cpu = _avg_metric(group_vms, metrics_by_vm, "Percentage CPU", "min")
        avg_mem = _avg_mem_pct(group_vms, metrics_by_vm)
        row_data = list(group_vals) + [sku, len(group_vms), avg_cpu, p95_cpu, p99_cpu, max_cpu, min_cpu, avg_mem]
        for col_idx, val in enumerate(row_data, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = _THIN_BORDER
            cell.font = Font(size=9)
            if alt:
                cell.fill = _ALT_FILL
        for cpu_col in range(avg_cpu_col, avg_cpu_col + 5):
            _colour_util(ws.cell(row=row_idx, column=cpu_col), row_data[cpu_col - 1])
        _colour_util(ws.cell(row=row_idx, column=mem_col), avg_mem)

    all_widths = col_widths + [22, 10, 13, 13, 13, 17, 13, 13]
    for col_idx, w in enumerate(all_widths, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = w

    if row_groups:
        _add_table(ws, table_name, len(row_groups))


# ---------------------------------------------------------------------------
# Recommendation action helper
# ---------------------------------------------------------------------------

def _rec_action(rec: "VmRecommendation") -> str:
    """Plain-English recommended action to present to the Workload Owner/SMEs."""
    if rec.category == "underutilized":
        if rec.recommended_sku:
            return f"Resize to {rec.recommended_sku} or decommission if unused"
        return "Review usage — decommission or resize to smallest available SKU"
    if rec.category == "right-size":
        if rec.recommended_sku:
            return f"Resize {rec.current_sku} → {rec.recommended_sku}"
        return "Resize to a smaller SKU (no smaller SKU found in current region)"
    if rec.category == "PaaS-candidate":
        return "Migrate to Azure App Service, Container Apps, or Azure SQL"
    return ""


# ---------------------------------------------------------------------------
# Sheet 7: Optimizations
# ---------------------------------------------------------------------------

# Order matches the reporting spec exactly:
#   Priority, Recommendation, Category, Subcategory, Resource ID, Members,
#   Current SKU/Resource Type, Recommended SKU/Resource Type,
#   Reason, Estimated Optimization, Override, Notes, Confidence, Evidence
_REC_COLS = [
    ("Priority", 12),
    ("Recommendation", 42),
    ("Category", 26),
    ("Subcategory", 22),
    ("Resource ID", 60),
    ("Members", 10),
    ("Current SKU / Resource Type", 32),
    ("Recommended SKU / Resource Type", 32),
    ("Reason", 50),
    ("Estimated Optimization", 28),
    ("Override", 14),
    ("Notes", 24),
    ("Confidence", 18),
    ("Evidence", 52),
]

_PRIORITY_FILLS = {
    "critical": PatternFill("solid", fgColor="C00000"),  # red
    "high":     PatternFill("solid", fgColor="ED7D31"),  # orange
    "medium":   PatternFill("solid", fgColor="FFC000"),  # yellow / gold
    "low":      PatternFill("solid", fgColor="70AD47"),  # green
}
_PRIORITY_FONTS = {
    "critical": Font(color="FFFFFF", bold=True, size=9),
    "high":     Font(color="FFFFFF", bold=True, size=9),
    "medium":   Font(color="000000", bold=True, size=9),
    "low":      Font(color="FFFFFF", bold=True, size=9),
}


def _sheet_recommendations(wb: Workbook, recommendations: list[VmRecommendation]) -> None:
    ws = wb.create_sheet("Optimizations")
    headers = [c[0] for c in _REC_COLS]
    _write_header(ws, headers)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    # Data validation for Override column (column 11).
    # sqref is set AFTER the row loop so we have the final row count; the DV
    # is only registered with the sheet if there are rows to cover.
    dv = DataValidation(
        type="list",
        formula1='"accept,reject,defer"',
        allow_blank=True,
        showErrorMessage=True,
        errorTitle="Invalid value",
        error="Choose: accept, reject, or defer",
    )

    for row_idx, rec in enumerate(recommendations, start=2):
        alt = row_idx % 2 == 0
        current = rec.current_sku or rec.current_resource_type or ""
        recommended = rec.recommended_sku or rec.recommended_resource_type or ""
        # When the rec covers multiple VMs (VMSS, AVSet, …) show the parent
        # resource ID and the member count; standalone VMs show the VM ID and 1.
        if rec.member_count > 1 and rec.parent_resource_id:
            displayed_id = rec.masked_parent_resource_id() or rec.masked_resource_id()
        else:
            displayed_id = rec.masked_resource_id()
        row_data = [
            rec.priority,
            rec.recommendation,
            rec.category,
            rec.subcategory,
            displayed_id,
            rec.member_count,
            current,
            recommended,
            rec.reason,
            rec.estimated_optimization,
            rec.manual_override or "",
            rec.notes or "",
            rec.confidence,
            " | ".join(rec.evidence) if rec.evidence else "",
        ]
        for col_idx, val in enumerate(row_data, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = _THIN_BORDER
            cell.font = Font(size=9)
            cell.alignment = Alignment(
                vertical="top",
                wrap_text=col_idx in (2, 9, 10, 12, 14),
            )
            if alt:
                cell.fill = _ALT_FILL

        # Priority cell — bold colour-coded badge
        prio_cell = ws.cell(row=row_idx, column=1)
        fill = _PRIORITY_FILLS.get((rec.priority or "").lower())
        font = _PRIORITY_FONTS.get((rec.priority or "").lower())
        if fill and font:
            prio_cell.fill = fill
            prio_cell.font = font
            prio_cell.alignment = Alignment(horizontal="center", vertical="center")

        # Override + Notes are analyst-editable (columns 11, 12)
        for col_idx in (11, 12):
            ws.cell(row=row_idx, column=col_idx).fill = _CSA_FILL

        # Confidence column (13) — colour-coded by tier
        conf_cell = ws.cell(row=row_idx, column=13)
        if rec.confidence == "workload-aware":
            conf_cell.fill = _GREEN_FILL
        elif rec.confidence == "os-aware":
            conf_cell.fill = _YELLOW_FILL

    # Register the DataValidation only if there are rows; set sqref as a
    # compact Excel range (e.g. "K2:K101") — NOT per-cell space-separated
    # list, which grows to thousands of chars and causes "Catastrophic failure"
    # in Excel's XML parser.
    if recommendations:
        dv.sqref = f"K2:K{len(recommendations) + 1}"
        ws.add_data_validation(dv)

    for col_idx, (_, width) in enumerate(_REC_COLS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    _add_table(ws, "TblRecommendations", len(recommendations))


# ---------------------------------------------------------------------------
# Sheet 2: Quota Utilization
# ---------------------------------------------------------------------------

_QUOTA_COLS = [
    ("Subscription", 30),
    ("Region", 16),
    ("Resource Type", 30),
    ("Display Name", 35),
    ("Current Usage", 14),
    ("Quota Limit", 14),
    ("Utilization %", 14),
    ("30d Peak %", 12),
    ("Alloc. Failures (30d)", 22),
    ("Alert", 8),
]


def _sheet_quota(wb: Workbook, quota_items: list[QuotaItem]) -> None:
    ws = wb.create_sheet("Quota Utilization")
    headers = [c[0] for c in _QUOTA_COLS]
    _write_header(ws, headers)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    for row_idx, item in enumerate(quota_items, start=2):
        alt = row_idx % 2 == 0
        row_data = [
            item.subscription_name,
            item.region,
            item.resource_type,
            item.display_name,
            item.current_usage,
            item.quota_limit,
            item.utilization_pct,
            item.peak_usage_pct_30d,
            item.allocation_failures_30d if item.allocation_failures_30d else "",
            "YES" if item.alert else "",
        ]
        for col_idx, val in enumerate(row_data, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = _THIN_BORDER
            cell.font = Font(size=9)
            cell.alignment = Alignment(vertical="top")
            if item.alert:
                cell.fill = _RED_FILL
            elif item.allocation_failures_30d > 0 and col_idx == 9:
                cell.fill = _RED_FILL
            elif alt:
                cell.fill = _ALT_FILL
        # Colour-code utilization % cell (col 7)
        util_cell = ws.cell(row=row_idx, column=7)
        if not item.alert:  # only recolour if not already red
            _colour_util(util_cell, item.utilization_pct)
        # Colour-code peak usage % cell (col 8)
        peak_cell = ws.cell(row=row_idx, column=8)
        if item.peak_usage_pct_30d is not None and not item.alert:
            _colour_util(peak_cell, item.peak_usage_pct_30d)

    for col_idx, (_, width) in enumerate(_QUOTA_COLS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    if quota_items:
        _add_table(ws, "TblQuota", len(quota_items))


def _read_quota_sheet(wb) -> list[QuotaItem]:
    # Support both old ("Quota Utilisation") and new ("Quota Utilization") sheet names
    sheet_name = next(
        (n for n in ("Quota Utilization", "Quota Utilisation") if n in wb.sheetnames), None
    )
    if sheet_name is None:
        return []
    ws = wb[sheet_name]
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return []
    # Detect column layout (10-col new vs 8-col legacy)
    header = [str(c or "").strip() for c in rows[0]]
    has_peak = "30d Peak %" in header
    has_failures = "Alloc. Failures (30d)" in header
    items: list[QuotaItem] = []
    for row in rows[1:]:
        if not any(row):
            continue
        try:
            if has_peak and has_failures:
                # New 10-column layout
                alert_val = str(row[9] or "").upper()
                peak = float(row[7]) if row[7] is not None else None
                failures = int(row[8] or 0)
            else:
                # Legacy 8-column layout
                alert_val = str(row[7] or "").upper()
                peak = None
                failures = 0
            items.append(
                QuotaItem(
                    subscription_id="",
                    subscription_name=str(row[0] or ""),
                    region=str(row[1] or ""),
                    resource_type=str(row[2] or ""),
                    display_name=str(row[3] or ""),
                    current_usage=int(row[4] or 0),
                    quota_limit=int(row[5] or 0),
                    utilization_pct=float(row[6] or 0),
                    alert=(alert_val == "YES"),
                    peak_usage_pct_30d=peak,
                    allocation_failures_30d=failures,
                )
            )
        except Exception:
            continue
    return items


# ---------------------------------------------------------------------------
# Sheet 9: Raw Metrics
# ---------------------------------------------------------------------------

def _sheet_raw_metrics(wb: Workbook, metrics: list[VmMetrics]) -> None:
    ws = wb.create_sheet("Raw Metrics")
    headers = ["Resource ID", "Metric", "Avg", "P50", "P95", "P99", "Max", "Min", "Data Points"]
    _write_header(ws, headers)

    from cloudopt.models import mask_subscription_ids_in_string

    for row_idx, m in enumerate(metrics, start=2):
        row = [
            mask_subscription_ids_in_string(m.resource_id),
            m.metric_name,
            _fmt(m.avg), _fmt(m.p50), _fmt(m.p95), _fmt(m.p99), _fmt(m.max), _fmt(m.min),
            len(m.time_series),
        ]
        for col_idx, val in enumerate(row, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.font = Font(size=9)

    _auto_width(ws, headers)


# ---------------------------------------------------------------------------
# Sheet 10: Application Insights
# ---------------------------------------------------------------------------

def _sheet_appinsights(
    wb: Workbook,
    components: list[AppInsightsInventory],
    metrics: list[AppInsightsMetrics],
) -> None:
    """App Insights inventory + summarised metrics in a single sheet."""

    ws = wb.create_sheet("App Insights")

    # Build a lookup: resource_id → {metric_name: AppInsightsMetrics}
    metrics_lookup: dict[str, dict[str, AppInsightsMetrics]] = {}
    for m in metrics:
        metrics_lookup.setdefault(m.resource_id, {})[m.metric_name] = m

    # Ordered metric columns to show in the sheet
    METRIC_COLS: list[tuple[str, str]] = [
        ("availabilityResults/availabilityPercentage", "Avail %"),
        ("requests/count",                             "Req Count"),
        ("requests/duration",                          "Req Dur ms"),
        ("requests/failed",                            "Failed Req"),
        ("exceptions/count",                           "Exceptions"),
        ("performanceCounters/processCpuPercentage",   "Proc CPU %"),
        ("performanceCounters/processPrivateBytes",    "Priv Bytes"),
        ("performanceCounters/memoryAvailableBytes",   "Avail Mem"),
        ("jvm/memory/heap/used",                       "JVM Heap Used"),
        ("jvm/memory/heap/max",                        "JVM Heap Max"),
        ("jvm/gc/pause",                               "JVM GC ms"),
        ("jvm/gc/count",                               "JVM GC Cnt"),
        ("jvm/threads/count",                          "JVM Threads"),
    ]

    headers = [
        "Component", "Subscription", "Resource Group", "Region",
        "Kind", "Type", "Workspace?",
    ] + [f"{label} (avg)" for _, label in METRIC_COLS]

    _write_header(ws, headers)
    col_widths = [32, 26, 22, 16, 12, 10, 12] + [14] * len(METRIC_COLS)
    for col_i, width in enumerate(col_widths, start=1):
        ws.column_dimensions[get_column_letter(col_i)].width = width

    for row_i, comp in enumerate(components, start=2):
        comp_metrics = metrics_lookup.get(comp.resource_id, {})
        metric_avgs = [
            comp_metrics[m_name].avg if m_name in comp_metrics else None
            for m_name, _ in METRIC_COLS
        ]
        row_data = [
            comp.component_name,
            comp.subscription_name,
            comp.resource_group,
            comp.region,
            comp.kind,
            comp.application_type,
            "Yes" if comp.workspace_resource_id else "No",
        ] + metric_avgs

        fill = _ALT_FILL if row_i % 2 == 0 else None
        for col_i, value in enumerate(row_data, start=1):
            cell = ws.cell(row=row_i, column=col_i, value=value)
            cell.border = _THIN_BORDER
            if fill:
                cell.fill = fill
            if isinstance(value, float) and col_i > 7:
                cell.number_format = "#,##0.00"

    if components:
        tbl_ref = f"A1:{get_column_letter(len(headers))}{len(components) + 1}"
        tbl = Table(displayName="TblAppInsights", ref=tbl_ref)
        tbl.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium9", showRowStripes=True
        )
        ws.add_table(tbl)


# ---------------------------------------------------------------------------
# Sheet 0: Workload Information (filled in with Workload Owner/SMEs)
# ---------------------------------------------------------------------------

_WORKLOAD_ROWS: list[tuple[str, str]] = [
    ("Workload Name",                  "workload_name"),
    ("Azure Cloud",                    "azure_cloud"),
    ("Primary Region",                 "primary_region"),
    ("Secondary/DR Region",            "secondary_dr_region"),
    ("Criticality for the Business",   "business_criticality"),
    ("Availability/DR Design Pattern", "availability_dr_pattern"),
    ("SLA",                            "sla"),
    ("RPO",                            "rpo"),
    ("RTO",                            "rto"),
    ("Top 2 Challenge/Pain point",     "challenge_2"),
    ("Top 3 Challenge/Pain point",     "challenge_3"),
]


def _sheet_workload_info(wb: Workbook, info: WorkloadInfo) -> None:
    """Two-column table for capturing workload context from the Workload Owner/SMEs."""
    ws = wb.create_sheet("Workload Information")
    ws.column_dimensions["A"].width = 36
    ws.column_dimensions["B"].width = 60
    ws.freeze_panes = "A2"

    headers = ["Property", "Workload Information"]
    _write_header(ws, headers)

    for row_idx, (label, field) in enumerate(_WORKLOAD_ROWS, start=2):
        prop_cell = ws.cell(row=row_idx, column=1, value=label)
        prop_cell.font = Font(bold=True, size=10)
        prop_cell.border = _THIN_BORDER
        prop_cell.alignment = Alignment(vertical="center", wrap_text=True)

        val_cell = ws.cell(row=row_idx, column=2, value=getattr(info, field, "") or "")
        val_cell.border = _THIN_BORDER
        val_cell.fill = _CSA_FILL
        val_cell.alignment = Alignment(vertical="center", wrap_text=True)
        val_cell.font = Font(size=10)
        ws.row_dimensions[row_idx].height = 22


# ---------------------------------------------------------------------------
# Sheet: Advisor SKU-change Recommendations
# ---------------------------------------------------------------------------

_ADVISOR_COLS: list[tuple[str, int]] = [
    ("Subscription",         28),
    ("Resource Group",       24),
    ("Resource Name",        28),
    ("Resource Type",        38),
    ("Category",             14),
    ("Impact",               10),
    ("Recommendation",       60),
    ("Current SKU",          18),
    ("Recommended SKU",      18),
    ("Annual Savings (USD)", 18),
    ("Last Updated",         22),
    ("Resource ID",          70),
]


def _sheet_advisor(wb: Workbook, recs: list[AdvisorRecommendation]) -> None:
    """Azure Advisor recommendations that suggest a SKU change."""
    ws = wb.create_sheet("Advisor SKU Changes")
    headers = [c[0] for c in _ADVISOR_COLS]
    _write_header(ws, headers)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    for col_idx, (_, width) in enumerate(_ADVISOR_COLS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    for row_idx, rec in enumerate(recs, start=2):
        alt = row_idx % 2 == 0
        row_data = [
            rec.subscription_name,
            rec.resource_group,
            rec.impacted_resource_name,
            rec.impacted_resource_type,
            rec.category,
            rec.impact,
            rec.short_description,
            rec.current_sku,
            rec.recommended_sku,
            rec.annual_savings_usd,
            rec.last_updated,
            rec.masked_impacted_resource_id(),
        ]
        for col_idx, val in enumerate(row_data, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = _THIN_BORDER
            cell.font = Font(size=9)
            cell.alignment = Alignment(vertical="top", wrap_text=col_idx in (7, 12))
            if alt:
                cell.fill = _ALT_FILL
        impact_cell = ws.cell(row=row_idx, column=6)
        impact_val = (rec.impact or "").lower()
        if impact_val == "high":
            impact_cell.fill = _RED_FILL
        elif impact_val == "medium":
            impact_cell.fill = _YELLOW_FILL
        elif impact_val == "low":
            impact_cell.fill = _GREEN_FILL

    if recs:
        _add_table(ws, "TblAdvisor", len(recs))


# Sheet 11: Subscriptions Zone Mapping
# ---------------------------------------------------------------------------

_ZONE_MAPPING_COLS: list[tuple[str, int]] = [
    ("Tenant ID", 36),
    ("Subscription ID", 42),
    ("Subscription Name", 36),
    ("Location", 20),
    ("Logical Zone", 14),
    ("Physical Zone", 24),
    ("Physical Zone Name", 26),
]


def _sheet_zone_mapping(
    wb: Workbook, zone_mappings: list[SubscriptionZoneMapping]
) -> None:
    """Physical-to-logical AZ mapping per subscription and location."""
    ws = wb.create_sheet("SubscriptionsZoneMapping")
    headers = [c[0] for c in _ZONE_MAPPING_COLS]
    _write_header(ws, headers)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    for col_idx, (_, width) in enumerate(_ZONE_MAPPING_COLS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    for row_idx, zm in enumerate(zone_mappings, start=2):
        alt = row_idx % 2 == 0
        row_data = [
            zm.tenant_id,
            mask_subscription_id(zm.subscription_id),
            zm.subscription_name,
            zm.location,
            zm.logical_zone,
            zm.physical_zone,
            zm.physical_zone_name,
        ]
        for col_idx, val in enumerate(row_data, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = _THIN_BORDER
            cell.font = Font(size=9)
            cell.alignment = Alignment(vertical="top")
            if alt:
                cell.fill = _ALT_FILL

    if zone_mappings:
        _add_table(ws, "TblZoneMapping", len(zone_mappings))


# Sheet 13: Monitoring Data
# ---------------------------------------------------------------------------

_MONITORING_COLS = [
    ("VM Name", 28),
    ("Source Tool", 16),
    ("Metric Group", 16),
    ("Metric Name", 34),
    ("Avg Value", 12),
    ("P95 Value", 12),
    ("Max Value", 12),
    ("Unit", 14),
    ("Confidence", 18),
]


def _sheet_monitoring_data(
    wb: Workbook,
    enriched_metrics: list[EnrichedVmMetrics],
) -> None:
    """Write monitoring data in tall format — one row per metric per VM."""
    from cloudopt.enrichment.schema import METRIC_GROUPS

    ws = wb.create_sheet("Monitoring Data")
    headers = [c[0] for c in _MONITORING_COLS]
    _write_header(ws, headers)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    # Build reverse mapping: metric_name → group
    metric_to_group: dict[str, str] = {}
    for group_name, metric_names in METRIC_GROUPS.items():
        for mn in metric_names:
            metric_to_group[mn] = group_name

    row_idx = 2
    for enriched in enriched_metrics:
        for dp in enriched.data_points:
            alt = row_idx % 2 == 0
            group_name = metric_to_group.get(dp.metric_name, "other")
            row_data = [
                enriched.vm_name,
                dp.source_tool,
                group_name,
                dp.metric_name,
                dp.avg_value,
                dp.p95_value,
                dp.max_value,
                dp.unit,
                enriched.confidence_tier,
            ]
            for col_idx, val in enumerate(row_data, start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.border = _THIN_BORDER
                cell.font = Font(size=9)
                cell.alignment = Alignment(vertical="top")
                if alt:
                    cell.fill = _ALT_FILL
            # Confidence column colour
            conf_cell = ws.cell(row=row_idx, column=9)
            if enriched.confidence_tier == "workload-aware":
                conf_cell.fill = _GREEN_FILL
            elif enriched.confidence_tier == "os-aware":
                conf_cell.fill = _YELLOW_FILL
            row_idx += 1

    for col_idx, (_, width) in enumerate(_MONITORING_COLS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    row_count = row_idx - 2
    if row_count > 0:
        _add_table(ws, "TblMonitoringData", row_count)


# ---------------------------------------------------------------------------
# Sheet 13: Inventory (full ARG resource list, tags excluded)
# ---------------------------------------------------------------------------

_RESOURCES_COLS: list[tuple[str, str, int]] = [
    # (header, field_name_or_special, width)
    ("Name", "name", 36),
    ("Type", "resource_type", 52),
    ("Resource Group", "resource_group", 28),
    ("Subscription", "subscription_name", 30),
    ("Subscription ID", "masked_subscription_id", 42),
    ("Region", "location", 16),
    ("Kind", "kind", 20),
    ("SKU Name", "sku_name", 22),
    ("SKU Tier", "sku_tier", 16),
    ("Plan Name", "plan_name", 22),
    ("Plan Publisher", "plan_publisher", 22),
    ("Plan Product", "plan_product", 22),
    ("Zones", "zones", 12),
    ("Managed By", "managed_by", 50),
    ("Resource ID", "masked_resource_id", 80),
]


def _sheet_resources(wb: Workbook, resources: list[AzureResource]) -> None:
    ws = wb.create_sheet("Inventory")
    headers = [c[0] for c in _RESOURCES_COLS]
    _write_header(ws, headers)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    for row_idx, resource in enumerate(resources, start=2):
        alt = row_idx % 2 == 0
        for col_idx, (_, field, _) in enumerate(_RESOURCES_COLS, start=1):
            if field == "masked_resource_id":
                val: Any = resource.masked_resource_id()
            elif field == "masked_subscription_id":
                val = resource.masked_subscription_id()
            else:
                val = getattr(resource, field, None) or ""
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = _THIN_BORDER
            cell.font = Font(size=9)
            cell.alignment = Alignment(vertical="top", wrap_text=False)
            if alt:
                cell.fill = _ALT_FILL

    for col_idx, (_, _, width) in enumerate(_RESOURCES_COLS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    if resources:
        _add_table(ws, "TblInventory_ARG", len(resources))


# Sheet 12: Collection Metadata
# ---------------------------------------------------------------------------

def _sheet_metadata(wb: Workbook, metadata: CollectionMetadata) -> None:
    ws = wb.create_sheet("Collection Metadata")
    ws.column_dimensions["A"].width = 36
    ws.column_dimensions["B"].width = 50

    rows: list[tuple[str, Any]] = [
        ("Run Date (UTC)", metadata.run_date),
        ("Tool Version", metadata.tool_version),
        ("Metrics Period (days)", metadata.metrics_period_days),
        ("Total VM Count", metadata.total_vm_count),
        ("", ""),
        ("Subscriptions Scanned", ""),
    ]
    for sub_id in metadata.subscriptions_scanned:
        rows.append(("", sub_id))

    rows += [
        ("", ""),
        ("Thresholds Used", ""),
        ("  Underutilized CPU (avg %)", metadata.thresholds.underutilized_cpu_avg),
        ("  Underutilized Memory (avg %)", metadata.thresholds.underutilized_memory_avg),
        ("  Oversized CPU (P95 %)", metadata.thresholds.oversize_cpu_p95),
        ("  Headroom Multiplier", metadata.thresholds.headroom_multiplier),
        ("  PaaS Candidate CPU (avg %)", metadata.thresholds.paas_candidate_cpu_avg),
    ]

    for row_idx, (key, val) in enumerate(rows, start=1):
        key_cell = ws.cell(row=row_idx, column=1, value=key)
        val_cell = ws.cell(row=row_idx, column=2, value=val)
        if key and not key.startswith("  ") and val == "":
            key_cell.font = Font(bold=True, size=10)
        else:
            key_cell.font = Font(size=9)
            val_cell.font = Font(size=9)

    # ------------------------------------------------------------------
    # Performance Metrics Reference table
    # Checked metrics (✅) are the ones actively collected and used by
    # the recommendations engine.
    # ------------------------------------------------------------------
    _METRICS: list[tuple[str, str, bool]] = [
        ("Avg",          "baseline context",            False),
        ("P50",          "typical workload",            False),
        ("P95",          "primary optimization signal", True),
        ("P99",          "safety / risk signal",        True),
        ("Max",          "spike detection",             False),
        ("Min",          "anomaly / idle",              False),
        ("Data Points",  "confidence level",            True),
    ]
    _CHECKED_FILL = PatternFill("solid", fgColor="C6EFCE")   # light green

    # Blank separator row, then section header
    section_start = row_idx + 2
    hdr_cell = ws.cell(row=section_start, column=1, value="Performance Metrics Reference")
    hdr_cell.font = Font(bold=True, size=10)

    # Column headers
    col_hdr_row = section_start + 1
    for col, label in enumerate(("Metric", "Why"), start=1):
        cell = ws.cell(row=col_hdr_row, column=col, value=label)
        cell.fill = _HDR_FILL
        cell.font = _HDR_FONT
        cell.alignment = Alignment(horizontal="left")

    # Data rows
    for offset, (metric, reason, checked) in enumerate(_METRICS, start=1):
        data_row = col_hdr_row + offset
        label = f"\u2705 {metric}" if checked else metric
        m_cell = ws.cell(row=data_row, column=1, value=label)
        r_cell = ws.cell(row=data_row, column=2, value=reason)
        if checked:
            m_cell.fill = _CHECKED_FILL
            r_cell.fill = _CHECKED_FILL
        m_cell.font = Font(size=9, bold=checked)
        r_cell.font = Font(size=9)


# ---------------------------------------------------------------------------
# Sheet: Reservations (RI / Savings Plans — counts, dates, percentages only)
# No $ / cost columns anywhere (SPEC §1.2).
# ---------------------------------------------------------------------------

_RSV_COLS: list[tuple[str, int]] = [
    ("Order ID (masked)",      40),
    ("Display Name",           35),
    ("SKU",                    22),
    ("Region",                 18),
    ("Term",                   10),
    ("Expiry Date",            14),
    ("Reserved Count",         16),
    ("Applied Scope Type",     20),
    ("Utilization (%)",        16),
]


def _sheet_reservations(wb: Workbook, reservations: list[ReservationOrder]) -> None:
    ws = wb.create_sheet("Reservations")

    # Header row
    for col_idx, (label, _) in enumerate(_RSV_COLS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=label)
        cell.fill = _HDR_FILL
        cell.font = _HDR_FONT
        cell.alignment = Alignment(horizontal="left")
        cell.border = _THIN_BORDER

    for row_idx, r in enumerate(reservations, start=2):
        vals: list[Any] = [
            r.masked_applied_scope_ids()[0] if r.applied_scope_ids else r.order_id,
            r.display_name,
            r.sku_name,
            r.region,
            r.term,
            r.expiry_date,
            r.reserved_count,
            r.applied_scope_type,
            r.utilization_pct,
        ]
        alt = row_idx % 2 == 0
        for col_idx, val in enumerate(vals, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = _THIN_BORDER
            cell.font = Font(size=9)
            cell.alignment = Alignment(vertical="top", wrap_text=False)
            if alt:
                cell.fill = _ALT_FILL

    for col_idx, (_, width) in enumerate(_RSV_COLS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    if reservations:
        _add_table(ws, "TblReservations", len(reservations))


# ---------------------------------------------------------------------------
# Sheet: Capacity Reservations (CRGs — counts and fill rates only)
# No $ / cost columns anywhere (SPEC §1.2).
# ---------------------------------------------------------------------------

_CRG_COLS: list[tuple[str, int]] = [
    ("Group Name",             28),
    ("Subscription (masked)",  36),
    ("Region",                 18),
    ("Zones",                  14),
    ("Reservation Name",       28),
    ("SKU",                    22),
    ("Reserved Count",         16),
    ("Used Count",             14),
    ("Fill Rate (%)",          14),
]


def _sheet_capacity_reservations(
    wb: Workbook,
    capacity_reservations: list[CapacityReservationGroup],
) -> None:
    ws = wb.create_sheet("Capacity Reservations")

    # Header row
    for col_idx, (label, _) in enumerate(_CRG_COLS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=label)
        cell.fill = _HDR_FILL
        cell.font = _HDR_FONT
        cell.alignment = Alignment(horizontal="left")
        cell.border = _THIN_BORDER

    row_idx = 2
    for crg in capacity_reservations:
        zones_str = ", ".join(crg.zones) if crg.zones else ""
        masked_sub = crg.masked_subscription_id()
        if crg.reservations:
            for item in crg.reservations:
                fill_pct: float | None = None
                if item.reserved_count > 0:
                    fill_pct = round(100.0 * item.used_count / item.reserved_count, 1)
                vals: list[Any] = [
                    crg.group_name,
                    masked_sub,
                    crg.region,
                    zones_str,
                    item.reservation_name,
                    item.sku_name,
                    item.reserved_count,
                    item.used_count,
                    fill_pct,
                ]
                alt = row_idx % 2 == 0
                for col_idx, val in enumerate(vals, start=1):
                    cell = ws.cell(row=row_idx, column=col_idx, value=val)
                    cell.border = _THIN_BORDER
                    cell.font = Font(size=9)
                    cell.alignment = Alignment(vertical="top", wrap_text=False)
                    if alt:
                        cell.fill = _ALT_FILL
                row_idx += 1
        else:
            # CRG with no reservations — emit one summary row
            vals = [
                crg.group_name, masked_sub, crg.region, zones_str,
                "", "", 0, 0, None,
            ]
            alt = row_idx % 2 == 0
            for col_idx, val in enumerate(vals, start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.border = _THIN_BORDER
                cell.font = Font(size=9)
                if alt:
                    cell.fill = _ALT_FILL
            row_idx += 1

    for col_idx, (_, width) in enumerate(_CRG_COLS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    last_data_row = row_idx - 2  # last filled row
    if last_data_row >= 1:
        _add_table(ws, "TblCapacityReservations", last_data_row)


# ---------------------------------------------------------------------------
# Read-back helpers (for export command)
# ---------------------------------------------------------------------------

def _read_inventory_sheet(wb) -> list[VmInventory]:
    if "VM Inventory" not in wb.sheetnames:
        return []
    ws = wb["VM Inventory"]
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return []
    headers = [str(h).strip() if h else "" for h in rows[0]]

    def col(row, name):
        try:
            return row[headers.index(name)]
        except (ValueError, IndexError):
            return None

    vms = []
    for row in rows[1:]:
        if not any(row):
            continue
        try:
            vms.append(
                VmInventory(
                    resource_id=col(row, "Resource ID") or "",
                    subscription_id="",  # masked in file, not recoverable
                    subscription_name=col(row, "Subscription") or "",
                    resource_group=col(row, "Resource Group") or "",
                    vm_name=col(row, "VM Name") or "",
                    vm_sku=col(row, "VM SKU") or "",
                    vcpus=int(col(row, "vCPUs") or 0),
                    memory_gb=float(col(row, "Memory (GB)") or 0),
                    region=col(row, "Region") or "",
                    os_type=col(row, "OS Type") or "Unknown",
                    os_version=col(row, "OS Version"),
                    power_state=col(row, "Power State"),
                    image_publisher=col(row, "Image Publisher"),
                    image_offer=col(row, "Image Offer"),
                    image_sku=col(row, "Image SKU"),
                    image_version=col(row, "Image Version"),
                    availability_zone=col(row, "Avail. Zone"),
                    nic_count=int(col(row, "NIC Count") or 0),
                    disk_count=int(col(row, "Disk Count") or 0),
                    disk_sizes_gb=[
                        float(s.strip()) for s in str(col(row, "Disk Sizes (GB)") or "").split(",")
                        if s.strip()
                    ],
                    vmss_name=col(row, "VMSS Name"),
                    availability_set_name=col(row, "Availability Set"),
                    workload=col(row, "Workload"),
                    application=col(row, "Application"),
                    environment=col(row, "Environment"),
                    criticality=col(row, "Criticality"),
                    owner=col(row, "Owner"),
                    custom=col(row, "Custom"),
                )
            )
        except Exception:
            continue
    return vms


def _read_raw_metrics_sheet(wb) -> list[VmMetrics]:
    """Read the Raw Metrics sheet back into VmMetrics objects (no time series)."""
    if "Raw Metrics" not in wb.sheetnames:
        return []
    ws = wb["Raw Metrics"]
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return []
    metrics: list[VmMetrics] = []
    for row in rows[1:]:
        if not any(row):
            continue
        try:
            metrics.append(VmMetrics(
                resource_id=str(row[0] or ""),
                metric_name=str(row[1] or ""),
                avg=float(row[2]) if row[2] is not None else None,
                p50=float(row[3]) if row[3] is not None else None,
                p95=float(row[4]) if row[4] is not None else None,
                p99=float(row[5]) if row[5] is not None else None,
                max=float(row[6]) if row[6] is not None else None,
                min=float(row[7]) if row[7] is not None else None,
                time_series=[],
            ))
        except Exception:
            continue
    return metrics


def _read_recommendations_sheet(wb) -> list[VmRecommendation]:
    if "Optimizations" not in wb.sheetnames:
        return []
    ws = wb["Optimizations"]
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return []

    recs = []
    for row in rows[1:]:
        if not any(row):
            continue
        try:
            # Column order:
            # 0 Priority | 1 Recommendation | 2 Category | 3 ResourceID
            # 4 Current SKU/RT | 5 Recommended SKU/RT | 6 Reason
            # 7 Estimated Optimization | 8 Override | 9 Notes
            current_val = str(row[4] or "")
            recommended_val = str(row[5] or "")
            recs.append(
                VmRecommendation(
                    priority=str(row[0] or "medium"),
                    recommendation=str(row[1] or ""),
                    category=str(row[2] or ""),
                    resource_id=str(row[3] or ""),
                    current_sku=current_val,
                    recommended_sku=recommended_val or None,
                    current_resource_type=current_val,
                    recommended_resource_type=recommended_val,
                    reason=str(row[6] or ""),
                    estimated_optimization=str(row[7] or ""),
                    manual_override=str(row[8]) if row[8] else None,
                    notes=str(row[9]) if row[9] else None,
                )
            )
        except Exception:
            continue
    return recs


def _read_metadata_sheet(wb) -> CollectionMetadata:
    from cloudopt.models import CollectionMetadata, CollectionThresholds

    if "Collection Metadata" not in wb.sheetnames:
        return CollectionMetadata(
            run_date="", tool_version="", subscriptions_scanned=[],
            metrics_period_days=30, total_vm_count=0,
            thresholds=CollectionThresholds(),
        )
    ws = wb["Collection Metadata"]
    kv: dict[str, Any] = {}
    for row in ws.iter_rows(values_only=True):
        if row[0] and row[1] is not None:
            kv[str(row[0]).strip()] = row[1]

    return CollectionMetadata(
        run_date=str(kv.get("Run Date (UTC)", "")),
        tool_version=str(kv.get("Tool Version", "")),
        subscriptions_scanned=[],
        metrics_period_days=int(kv.get("Metrics Period (days)", 30)),
        total_vm_count=int(kv.get("Total VM Count", 0)),
        thresholds=CollectionThresholds(
            underutilized_cpu_avg=float(kv.get("  Underutilized CPU (avg %)", 15)),
            underutilized_memory_avg=float(kv.get("  Underutilized Memory (avg %)", 20)),
            oversize_cpu_p95=float(kv.get("  Oversized CPU (P95 %)", 40)),
            headroom_multiplier=float(kv.get("  Headroom Multiplier", 1.2)),
            paas_candidate_cpu_avg=float(kv.get("  PaaS Candidate CPU (avg %)", 10)),
        ),
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _write_header(ws: Worksheet, headers: list[str]) -> None:
    ws.row_dimensions[1].height = 18
    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.fill = _HDR_FILL
        cell.font = _HDR_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = _THIN_BORDER


def _add_table(ws: Worksheet, name: str, data_rows: int) -> None:
    if data_rows < 1:
        return
    max_col = get_column_letter(ws.max_column)
    ref = f"A1:{max_col}{data_rows + 1}"
    table = Table(displayName=name, ref=ref)
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium9",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    ws.add_table(table)
    # A Table provides its own AutoFilter on the header row; the sheet-level
    # AutoFilter (set earlier via ws.auto_filter.ref) would produce a duplicate
    # <autoFilter> element that Excel rejects, causing tables to be removed.
    ws.auto_filter.ref = None


def _auto_width(ws: Worksheet, headers: list[str]) -> None:
    for col_idx, header in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = max(14, len(header) + 4)


def _colour_util(cell, value: float | None) -> None:
    if value is None:
        return
    if value < 40:
        cell.fill = _GREEN_FILL
    elif value < 70:
        cell.fill = _YELLOW_FILL
    else:
        cell.fill = _RED_FILL


def _fmt(val: float | None, decimals: int = 2) -> float | None:
    if val is None:
        return None
    return round(val, decimals)


def _group_metrics(metrics: list[VmMetrics]) -> dict[str, dict[str, VmMetrics]]:
    """Group metrics by resource_id → metric_name → VmMetrics."""
    result: dict[str, dict[str, VmMetrics]] = {}
    for m in metrics:
        result.setdefault(m.resource_id, {})[m.metric_name] = m
    return result


def _avg_metric(
    vms: list[VmInventory],
    metrics_by_vm: dict[str, dict[str, VmMetrics]],
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
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _avg_mem_pct(
    vms: list[VmInventory],
    metrics_by_vm: dict[str, dict[str, VmMetrics]],
) -> float | None:
    """Return average memory utilization % across running VMs (derived from available bytes + total GB)."""
    values = []
    for vm in vms:
        if _is_stopped(vm):
            continue
        m = metrics_by_vm.get(vm.resource_id, {}).get("Available Memory Bytes")
        if m and m.avg is not None and vm.memory_gb > 0:
            avail_gb = m.avg / (1024 ** 3)
            used_pct = (1 - avail_gb / vm.memory_gb) * 100
            values.append(max(0.0, min(100.0, used_pct)))
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _is_stopped(vm: VmInventory) -> bool:
    """Return True when the VM is deallocated or stopped (not running)."""
    state = (vm.power_state or "").lower()
    return state in ("powerstate/stopped", "powerstate/deallocated")
