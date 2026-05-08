# cloudopt — Analyzer How-To Guide

The **Analyzer** step runs on the engineer's local machine. It
takes a `cloudopt_report.json` file collected by the customer and produces a
multi-sheet Excel workbook (`cloudopt_report.xlsx`) plus an optional local
web dashboard for exploration.

> **Why separate?**  The collector can run in Azure Cloud Shell without any
> Excel dependency. The customer collects and shares the JSON; the engineer
> generates the workbook locally.

---

## Prerequisites

| Requirement            | Notes                                                                 |
| ---------------------- | --------------------------------------------------------------------- |
| Python 3.11+           | `python --version`                                                    |
| `cloudopt` installed   | See [HOW_TO.md](HOW_TO.md) — install from GitHub, zip, or local clone |
| `cloudopt_report.json` | Produced by the customer running `cloudopt collect`                   |

The `openpyxl` package (Excel generation) **is included** in the default
dependency set, so a standard `pip install -e .` covers everything.

---

## Quick Start

```bash
# 1. Generate Excel from the collected JSON
cloudopt analyze --from output/cloudopt_report.json

# 2. Open the workbook
#    output/cloudopt_report.xlsx

# 3. (Optional) Browse data in a local web dashboard
cloudopt dashboard --data output/cloudopt_report.xlsx
```

---

## Commands

### `analyze` — Generate Excel workbook from JSON

```bash
cloudopt analyze --from <json_path> [OPTIONS]
```

| Option                | Default                | Description                             |
| --------------------- | ---------------------- | --------------------------------------- |
| `--from`              | *(required)*           | Path to the `cloudopt_report.json` file |
| `--output-dir` / `-o` | Same directory as JSON | Directory for the output `.xlsx` file   |

The output file is named after the JSON stem: `cloudopt_report.json` → `cloudopt_report.xlsx`.

```bash
# Basic usage
cloudopt analyze --from output/cloudopt_report.json

# Write workbook to a different directory
cloudopt analyze --from output/cloudopt_report.json --output-dir /tmp/review
```

---

### `dashboard` — Browse the workbook in a browser

```bash
cloudopt dashboard [OPTIONS]
```

| Option          | Default                       | Description                |
| --------------- | ----------------------------- | -------------------------- |
| `--data`        | `output/cloudopt_report.xlsx` | Path to the Excel workbook |
| `--port` / `-p` | `8080`                        | Local port                 |
| `--host`        | `127.0.0.1`                   | Bind address               |

```bash
cloudopt dashboard --data output/cloudopt_report.xlsx
# Browse to http://localhost:8080
```

Press `Ctrl+C` to stop the server.

---

### `export` — Convert the workbook to JSON or CSV

Use this **after editing the workbook** (filling in Optimizations, adding
notes, overriding fields) to republish a machine-readable version that
reflects your changes.

```bash
cloudopt export --from output/cloudopt_report.xlsx [OPTIONS]
```

| Option            | Default                    | Description                |
| ----------------- | -------------------------- | -------------------------- |
| `--from`          | *(required)*               | Path to the Excel workbook |
| `--to`            | Same directory as workbook | Output directory           |
| `--format` / `-f` | `all`                      | `json`, `csv`, or `all`    |

```bash
# Export back to JSON after editing the workbook
cloudopt export --from output/cloudopt_report.xlsx --format json

# Export all sheets as CSV files
cloudopt export --from output/cloudopt_report.xlsx --format csv --to output/csv
```

---

## Excel Workbook Structure

The generated workbook contains the following sheets:

| Sheet                        | Contents                                                                                                                             |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| VM Inventory                 | One row per VM: SKU, vCPUs, memory, region, zones, OS image, power state, disk layout, NIC count, VMSS / availability-set membership |
| Performance Summary          | Avg / P50 / P95 / Max CPU, memory, disk I/O, and network per VM                                                                      |
| SKU Perf by Subscription     | Metrics aggregated by subscription                                                                                                   |
| SKU Perf by Resource Group   | Metrics aggregated by resource group                                                                                                 |
| SKU Perf by VMSS             | Metrics for VMSS-grouped VMs                                                                                                         |
| SKU Perf by Availability Set | Metrics for availability-set VMs                                                                                                     |
| **Optimizations**            | Analyst-authored right-sizing and migration findings (pre-populated from Azure Advisor; empty rows for manual additions)                 |
| Quota Utilisation            | Core quota usage per subscription / region                                                                                           |
| Raw Metrics                  | Full daily time-series for every collected metric                                                                                    |
| App Insights                 | Inventory + summarised metrics for all App Insights components                                                                       |
| Collection Metadata          | Run timestamp, thresholds, subscription list                                                                                         |

> **Subscription IDs** are partially masked (first 8 characters only) to
> reduce accidental data exposure when sharing reports.

---

## Analyst-Editable Fields

The **VM Inventory** sheet contains six blank columns for the analyst to complete
before presenting findings to the customer:

| Column      | Purpose                        |
| ----------- | ------------------------------ |
| Workload    | Application or service name    |
| Application | Business application or system |
| Environment | e.g. Production, Dev, Test     |
| Criticality | e.g. High, Medium, Low         |
| Owner       | Team or person responsible     |
| Custom      | Free-text notes                |

---

## Optimizations Sheet

The **Optimizations** sheet is intentionally left blank during collection. It
is the primary deliverable for an engagement — fill it in based on your
analysis of the Performance Summary, Quota Utilisation, and Azure Advisor
findings already captured in the workbook.

Suggested columns to populate per finding:

| Column                   | Guidance                                           |
| ------------------------ | -------------------------------------------------- |
| Workload                 | Name from VM Inventory                             |
| Category                 | e.g. Resizing, SKU Swap, Quota, Modernization      |
| Resource ID              | VM, VMSS, or resource group the finding applies to |
| Current SKU              | Observed SKU                                       |
| Recommended SKU / Action | Proposed change                                    |
| Justification            | Metric-backed rationale                            |
| Priority                 | High / Medium / Low                                |
| Notes                    | Observations or caveats                            |

After populating, use `cloudopt export` to publish an updated JSON/CSV.

---

## Troubleshooting

**`FileNotFoundError` on `cloudopt analyze`**  
Verify the path passed to `--from` exists and points to a valid `cloudopt_report.json`.

**Workbook opens but sheets are empty**  
Check that `cloudopt collect` completed without errors (exit code 0) and that the
JSON file is not zero-length.

**Dashboard shows no data**  
Ensure `--data` points to the `.xlsx` file, not the JSON. If you only have the
JSON, run `cloudopt analyze --from cloudopt_report.json` first.

**`openpyxl` not found**  
Re-install using one of the methods in [HOW_TO.md](HOW_TO.md). The `openpyxl`
package is included in the default dependency set.
