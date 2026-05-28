# cloudopt — Reporter Guide

> **Audience:** Engineers who have run `cloudopt analyze` and want to track, prioritize, and present findings to the customer.
> **Focus:** Cloud Efficiency — Performance, Capacity, Resiliency. No cost or FinOps content.

---

## What the Reporter does

After `cloudopt analyze` produces the Excel workbook, the Reporter layer provides:

1. **Excel Executive Summary sheet** — auto-generated Sheet 0 with KPIs, top quick wins, and a capacity hygiene scorecard.
2. **Status side-car CSV** — lightweight `<workbook>_status.csv` file that tracks the disposition of every finding (open → in_progress → done → dismissed) without modifying the workbook.
3. **Dashboard views** — the local web dashboard (`cloudopt dashboard`) includes an Action Plan section, Workload Archetypes view, and a redesigned Summary Dashboard.
4. **CSV export** — download all findings + status from the dashboard for offline distribution.

---

## Excel Executive Summary sheet

The `Executive Summary` sheet is the **first sheet** in every workbook produced by `cloudopt analyze`. It contains three sections:

### Section 1 — Top KPIs

| KPI | Description |
|---|---|
| Total VMs | All VMs in scope (running + stopped + deallocated) |
| READY Actions | Findings with `readiness = READY` (HIGH confidence, act now) |
| READY % | READY findings ÷ total recommendations |
| Avg Confidence Score | Mean numeric confidence score across all recommendations (0–100) |
| vCPU Opportunity | vCPUs available to right-size from READY downsize + idle-removal findings |
| Generation Gap Count | VMs running ≥2 generations behind the current SKU in their family |

### Section 2 — Top 10 Quick Wins

Sorted by **priority score** = `confidence_score × |vcpu_delta|`. Each row shows:

| Column | Notes |
|---|---|
| Rank | 1–10 by priority score |
| Code | Finding code (e.g. `RSZ-DWN-001`) |
| Resource | VM or resource ID (subscription ID masked to 8 chars) |
| Current | Current SKU / state |
| Proposed | Recommended SKU / action |
| Confidence Score | Numeric score 0–100 |
| Rationale | Plain-English reason (truncated to 200 characters) |

### Section 3 — Capacity Ops Hygiene Scorecard

One row per subscription showing QTA-OPS-001 sub-check results:

| Column | What it checks |
|---|---|
| Subscription | Subscription ID (first 8 chars) |
| A: Quota Alert | Metric alert exists for vCPU quota utilization |
| B: Alloc Failure Alert | Activity log alert for `AllocationFailed` / `SkuNotAvailable` |
| C: QuotaExceeded Alert | Activity log alert for `QuotaExceeded` |
| D: CRR Alert | CRG utilization alert (only checked when CRGs exist) |
| E: Service Health | Service Health alert for `Compute` in scope regions |

Each cell shows ✓ (pass) or ✗ (missing).

> **Prerequisite:** Alert rule collection must be enabled during `cloudopt collect` (it is on by default).  
> If the scorecard appears empty, re-run `cloudopt collect` without `[collect_alerts] false` in the scope file.

---

## Finding Status Workflow

Every finding has a lifecycle tracked in a lightweight side-car CSV file placed next to the workbook:

```
output/
  cloudopt_report.xlsx
  cloudopt_report_status.csv   ← auto-created on first update-status call
```

### Status values

| Status | Meaning |
|---|---|
| `open` | Default; no action taken yet |
| `in_progress` | Customer / engineer is actively working on it |
| `done` | Action completed |
| `dismissed` | Finding acknowledged but not actioned (with reason in notes) |

### Finding IDs

Each finding has a stable ID in the form `<CODE>:<resource_id>`, for example:
- `RSZ-DWN-001:/subscriptions/abc12345.../resourceGroups/rg-prod/providers/Microsoft.Compute/virtualMachines/vm-web-01`
- `QTA-OPS-001:/subscriptions/abc12345...`

### CLI: update-status

```bash
cloudopt update-status <finding_id> <status> [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--owner TEXT` | (empty) | Name or alias of the person responsible |
| `--due DATE` | (empty) | Target completion date (YYYY-MM-DD) |
| `--notes TEXT` | (empty) | Any free-text context |
| `--data PATH` | Auto-detected `*.xlsx` in current dir | Path to the Excel workbook |

**Examples:**

```bash
# Mark a VM downsize as in-progress, assigned to the platform team
cloudopt update-status "RSZ-DWN-001:/subscriptions/.../vm-web-01" in_progress \
  --owner "platform-team" --due 2026-07-01 --notes "Scheduled for next maintenance window"

# Dismiss a finding with a reason
cloudopt update-status "SWP-GEN-001:/subscriptions/.../vm-sql-02" dismissed \
  --notes "VM scheduled for decommission in Q3; not worth upgrading generation"

# Mark done
cloudopt update-status "DCM-STP-001:/subscriptions/.../vm-old-test" done \
  --owner "cloud-ops" --notes "Deallocated and removed"
```

### Dashboard: inline status updates

In the **Action Plan** section of the web dashboard, you can update finding status inline without the CLI — click the status badge in any row to cycle through states. Changes are written to `<workbook_stem>_status.csv` immediately.

---

## Dashboard: Summary Dashboard

The redesigned Summary Dashboard (`cloudopt dashboard → Summary`) shows:

| Panel | Description |
|---|---|
| KPI cards | Total VMs, READY %, Avg Confidence Score, vCPU Opportunity, Generation Gap Count |
| Confidence Score Distribution | Histogram of all findings by score bucket (0–10, 10–20, …, 90–100); green ≥80, amber 50–79, red <50 |
| Right-sized Capacity Forecast | Bar chart: Today → After Right-sizing → After Removing Idle → Right-sized Footprint (in vCPU) |
| Top READY Decisions | Top 10 quick-win findings with score badges and one-line rationale |
| Capacity Ops Monitoring | Per-subscription pill scorecard for QTA-OPS-001 sub-checks |

---

## Dashboard: Workload Archetypes

The **Workload Archetypes** section (sidebar → Capacity Intelligence) shows how VMs behave over time:

| Archetype | What it means |
|---|---|
| `steady-24x7` | Consistent CPU load day and night, low variability |
| `business-hours` | Active Mon–Fri daytime; low usage outside business hours |
| `weekend-idle` | Low activity on weekends |
| `bursty` | High P95/P50 ratio with high coefficient of variation |
| `spiky` | Infrequent sharp spikes against a low baseline |
| `dev-test-irregular` | Tagged dev/test/qa environment with irregular patterns |
| `unknown` | Insufficient data (fewer than 48 hourly data points) |

Classification uses hourly CPU time-series from the JSON. Where App Insights SLO data (availability ≥ 99.9% p99) is present, it corroborates recommendations with an additional evidence source.

> **Prerequisite:** Archetypes require ≥ 48 hourly CPU data points per VM.  
> Collect with `--metrics-days 2` minimum; **14–30 days recommended** for reliable pattern detection.  
> If all archetypes show `unknown`, re-run `cloudopt collect --metrics-days 30`.

---

## Dashboard: Action Plan

The **Action Plan** section (sidebar → Capacity Intelligence) provides a single prioritised list of READY recommendations:

- **Priority score** = `confidence_score × max(|vcpu_delta|, 1)` — purely capacity-focused, no $ involved
- **Status filter chips** — filter by Open / In Progress / Done / Dismissed
- **CSV export** — download all filtered rows as a CSV for stakeholder distribution
- **Inline status update** — click the status badge to update without leaving the browser

---

## Confidence Score Reference

Every finding carries a numeric confidence score (0–100) computed deterministically from the evidence available:

| Score range | Confidence band | Readiness |
|---|---|---|
| ≥ 80 | HIGH | READY — act with normal change control |
| 50–79 | MEDIUM | LIKELY — validate with workload owner first |
| < 50 | LOW | INSUFFICIENT — treat as starting point for investigation |

**Score formula:**

```
score = base_score
      + memory_quality_bonus   (0–15, based on memory data source quality)
      + coverage_bonus         (0–10, based on metric coverage %)
      + corroboration_bonus    (0–20, 10 per additional data source beyond platform)
      + stability_bonus        (0–5, if no recent config change detected)
      - change_impact_penalty  (0–20, if proposed change has high blast radius)
```

**Base scores by category:**

| Category | Base |
|---|---|
| CLEANUP, QUOTA, CRR, DECOM (non-idle) | 90 |
| DCM-IDL-001 (idle detection) | 70 |
| RSZ-*, SWP-* | 65 |
| QTA-OPS-001 | 90 |

**Unlocking HIGH confidence (RSZ/SWP findings):** Supply OS-level agent data via the customer data CSV (see [SPEC.md §5](SPEC.md)) — `os.cpu.used_percent` and `os.memory.used_percent` from Datadog / Dynatrace / Splunk / VM Insights / Prometheus.

---

## Recommendation Codes Quick Reference

See [docs/RECOMMENDATIONS_CATALOG.md](docs/RECOMMENDATIONS_CATALOG.md) for full detail.

| Code | Category | What it detects |
|---|---|---|
| `RSZ-DWN-001` | rightsize | Oversized VM — downsize to smaller SKU |
| `RSZ-UPS-001` | rightsize | Undersized VM — upsize to larger SKU |
| `RSZ-BSF-001` | rightsize | D/E/F workload fits B-series credit model |
| `RSZ-BSM-001` | rightsize | B-series over credit budget — move to standard |
| `RSZ-DSK-001` | rightsize | Disk oversized / wrong tier for workload |
| `SWP-GEN-001` | swap | Older generation → newer generation (same family) |
| `SWP-FAM-001` | swap | Family swap by workload profile (compute/memory-bound) |
| `SWP-LFC-001` | swap | SKU on Azure's retiring list → modern replacement |
| `SWP-DST-001` | swap | Premium SSD → Standard SSD where workload allows |
| `SWP-DSK-001` | swap | Diskful → Diskless SKU (temp disk unused) |
| `SWP-ARC-001` | swap | ARM64 eligibility candidate (flag-only, never prescribed) |
| `DCM-IDL-001` | decom | Idle running VM (near-zero CPU + network + disk) |
| `DCM-STP-001` | decom | Stopped-allocated VM (still billed) |
| `DCM-DLC-001` | decom | Lower-env oversized VM (opt-in) |
| `DCM-ENV-001` | decom | Missing environment tag (opt-in) |
| `CLN-DSK-001` | cleanup | Unattached managed disk ≥ 30 days |
| `CLN-NIC-001` | cleanup | Unattached network interface |
| `CLN-PIP-001` | cleanup | Unassociated public IP |
| `CLN-SNP-001` | cleanup | Unused snapshot |
| `CLN-RGP-001` | cleanup | Empty resource group |
| `QTA-OVR-001` | quota | Quota over-allocated (< 20% utilization) |
| `QTA-WRN-001` | quota | Quota warning (70–85% utilization) |
| `QTA-CRI-001` | quota | Quota critical — individual increase needed |
| `QTA-CRG-001` | quota | Quota critical — groupable consolidation opportunity |
| `QTA-OPS-001` | quota | Capacity ops hygiene — missing monitoring coverage |
| `CRR-UNU-001` | crr | Capacity Reservation Group with no associated VMs |
| `CRR-UNF-001` | crr | Capacity Reservation Group underfilled |

---

## See also

- [Analyzer.md](Analyzer.md) — generating and editing the Excel workbook
- [COLLECTOR.md](Collector.md) — collecting data from Azure
- [docs/RECOMMENDATIONS_CATALOG.md](docs/RECOMMENDATIONS_CATALOG.md) — full recommendation reference
