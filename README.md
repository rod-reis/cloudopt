# cloudopt

**cloudopt** is a Cloud Efficiency tool focused on **Performance, Capacity, and Resiliency**.
It is a read-only Python CLI that collects Azure Virtual Machine inventory and
performance metrics across one or many subscriptions, producing a structured JSON artifact.
A separate `analyze` step transforms that JSON into an Excel workbook and launches a local
web dashboard — without requiring any Azure access on the analyst's machine.

> CloudOpt is **not** a cost-optimization or FinOps tool. Recommendations are framed
> around performance fit, capacity headroom, and resiliency posture. Cost reduction
> can be a consequence, never the goal.

> **Cloud Shell compatible** — `cloudopt collect` has no Excel dependency and runs in
> Azure Cloud Shell or any Python 3.11+ environment.  Customer data stays local and is
> **never written back to Azure**.

---

## Two-Phase Architecture

```mermaid
flowchart LR
    subgraph Customer["Customer (Azure access required)"]
        A[az login] --> B[cloudopt collect]
        B --> C[(cloudopt_report.json)]
    end

    subgraph Engineer["Engineer (no Azure access needed)"]
        C --> D[cloudopt analyze]
        D --> E[(cloudopt_report.xlsx)]
        E --> F[cloudopt dashboard]
        F --> G[Browser\nhttp://localhost:8080]
        E --> H[cloudopt export]
        H --> I[(JSON / CSV)]
    end
```

---

## How It Works

### Data Flow

```mermaid
flowchart TD
    CLI[cli.py\nTyper CLI entry point]

    subgraph Collectors["Collectors  src/cloudopt/collector/"]
        AUTH[auth.py\nDefaultAzureCredential]
        INV[inventory.py\nResource Graph KQL]
        MET[metrics.py\nAzure Monitor API]
        APP[appinsights.py\nApp Insights + Log Analytics]
        ADV[advisor.py\nAzure Advisor Graph]
        QUO[quota.py\nCompute Usages API]
        ZON[zones.py\nAvailability Zone mapping]
        THR[throttle.py\nToken-bucket rate limiter]
    end

    subgraph Models["Models  src/cloudopt/models.py"]
        VM[VmInventory]
        VMM[VmMetrics]
        AI[AppInsightsInventory\nAppInsightsMetrics]
        QI[QuotaItem]
        AR[AdvisorRecommendation]
        META[CollectionMetadata]
    end

    subgraph Analyzer["Analyzer  src/cloudopt/analyzer/"]
        SKU[sku_catalog.py\nAzure Compute SKU cache]
        REC[recommendations.py\nRecommendation engine]
    end

    subgraph Export["Export  src/cloudopt/export/"]
        JSON[json_export.py]
        XLS[excel.py]
        CSV[csv_export.py]
    end

    DASH[dashboard/app.py\nFastAPI local server]
    SCOPE[scope.py\nScope + filter resolution]

    CLI --> AUTH
    AUTH --> INV & MET & APP & ADV & QUO & ZON
    THR -. rate limiting .-> MET & APP
    INV --> VM
    MET --> VMM
    APP --> AI
    QUO --> QI
    ADV --> AR
    INV --> SKU
    VM & VMM & QI --> REC
    REC --> VmRecommendation
    VM & VMM & AI & QI & AR & META --> JSON
    JSON --> XLS
    XLS --> DASH
    SCOPE --> INV & APP & ADV
```

### Request Rate Control

All Azure Monitor and ARM calls go through `ThrottleManager` in `throttle.py`:

```
┌─────────────────────────────────────────────────────────┐
│  ThrottleManager (per subscription)                     │
│                                                         │
│  asyncio.Semaphore ──── max concurrent calls (default 5)│
│  TokenBucket       ──── max requests/sec   (default 20) │
│  ExponentialBackoff──── on 429 / transient errors       │
└─────────────────────────────────────────────────────────┘
```

Subscriptions are always processed **one at a time**; VMs and App Insights
components within a subscription are batched in parallel up to `--concurrency`.

### Scope Filtering

All filters are resolved by `scope.py` and applied in this strict order:

```
Tenant → Subscriptions → Locations (Regions) → ResourceGroups → Tags
```

Tag values are **kept in memory only** and are never written to any output file.

### Recommendation Engine

The engine produces `Finding` records grouped into **6 categories** and **26 detection codes** (25 recommendations + 1 candidate flag):

| Category  | Sub-codes | Signal                                                              |
| --------- | --------- | ------------------------------------------------------------------- |
| `rightsize` | RSZ-DWN-001, RSZ-UPS-001, RSZ-BSF-001, RSZ-BSM-001, RSZ-DSK-001 | Size mismatch — VM over- or under-provisioned for its workload |
| `swap`    | SWP-GEN-001, SWP-FAM-001, SWP-LFC-001, SWP-DST-001, SWP-DSK-001, SWP-ARC-001* | Wrong SKU family, generation, or architecture |
| `decom`   | DCM-IDL-001, DCM-STP-001, DCM-DLC-001, DCM-ENV-001 | Unused or stale VMs consuming capacity |
| `cleanup` | CLN-DSK-001, CLN-NIC-001, CLN-PIP-001, CLN-SNP-001, CLN-RGP-001 | Orphaned resources distorting capacity planning |
| `quota`   | QTA-OVR-001, QTA-WRN-001, QTA-CRI-001, QTA-CRG-001, QTA-OPS-001 | Quota thresholds and missing operational monitoring |
| `crr`     | CRR-UNU-001, CRR-UNF-001 | Capacity Reservation Group posture |

*`SWP-ARC-001` is a candidate flag (discovery only) — never auto-prescribed.

Every finding has a **numeric confidence score (0–100)**:
- ≥ 80 → **HIGH / READY** — act with normal change control
- 50–79 → **MEDIUM / LIKELY** — validate with workload owner first
- < 50 → **LOW / INSUFFICIENT** — treat as starting point for investigation

Score is boosted by: OS-agent or APM enrichment data, high metric coverage, App Insights SLO corroboration, workload archetype classification.

See [docs/RECOMMENDATIONS_CATALOG.md](docs/RECOMMENDATIONS_CATALOG.md) and [REPORTER.md](REPORTER.md) for full reference.

### Workload Archetype Classification

cloudopt classifies every VM into one of 7 archetypes using 30-day hourly CPU patterns:

| Archetype | Signal |
| --- | --- |
| `steady-24x7` | Consistent CPU load day and night, low variability |
| `business-hours` | Active Mon–Fri daytime; low outside business hours |
| `weekend-idle` | Low activity on weekends |
| `bursty` | High P95/P50 ratio with high coefficient of variation |
| `spiky` | Infrequent sharp spikes against a low baseline |
| `dev-test-irregular` | dev/test/qa tagged VM with irregular patterns |
| `unknown` | Insufficient data (< 48 hourly points) |

Archetypes feed recommendation logic (e.g., `RSZ-BSF-001` burstable fit is corroborated by `bursty` archetype) and are visible in the **Workload Archetypes** dashboard section.

---

## Installation

**From GitHub** (Azure Cloud Shell or any internet-connected machine):
```bash
pip install git+https://github.com/Azure/cloudopt.git
```

**From a zip file** (offline / air-gapped delivery):
```bash
unzip cloudopt.zip && cd cloudopt && pip install .
```

**From a local clone** (contributors):
```bash
git clone https://github.com/Azure/cloudopt.git
cd cloudopt && pip install -e ".[dev]"
```

Verify: `cloudopt version`

---

## Quick Start

```bash
# 1. Authenticate
az login

# 2. Collect from all accessible subscriptions (30 days of metrics)
cloudopt collect --output output/

# 3. Share output/cloudopt_report.json with the analyst/engineer
```

The engineer then runs (no Azure access required):

```bash
# Generate the Excel workbook
cloudopt analyze --from output/cloudopt_report.json

# Browse in a local dashboard
cloudopt dashboard --data output/cloudopt_report.xlsx
```

---

## Commands

| Command                  | Who runs it | Description                                            |
| ------------------------ | ----------- | ------------------------------------------------------ |
| `cloudopt collect`       | Customer    | Full collection: inventory + metrics + quota + Advisor |
| `cloudopt analyze`       | Engineer    | Generates Excel workbook from JSON                     |
| `cloudopt dashboard`     | Engineer    | Local FastAPI web dashboard                            |
| `cloudopt export`        | Engineer    | Re-exports workbook to JSON / CSV                      |
| `cloudopt update-status` | Engineer    | Updates finding status in the side-car CSV             |
| `cloudopt version`       | Anyone      | Print version                                          |

See [HOW_TO.md](HOW_TO.md) for full option reference.

---

## What Is Collected

### Azure Virtual Machines

- **Inventory**: resource ID, subscription, resource group, region, zone, SKU, vCPUs,
  memory, OS type, OS image, disk layout, NIC count, power state, VMSS / availability-set
- **Platform metrics** (30-day default, configurable 1–90 days) — sourced from the **Azure Monitor Metrics API** (host-level, no guest agent or VM Insights required):

| Metric                       | Stats                        |
| ---------------------------- | ---------------------------- |
| CPU %                        | avg, P50, P95, P99, max, min |
| Available Memory Bytes       | avg, P50, P95, P99, max, min |
| Disk Read / Write Bytes/sec  | avg, P50, P95, P99, max, min |
| Disk Read / Write IOPS       | avg, P50, P95, P99, max, min |
| Network In / Out Total Bytes | avg, P50, P95, P99, max, min |

> **Note:** These are Azure fabric platform metrics — not VM Insights, Log Analytics,
> Datadog, or Splunk. They work on every VM regardless of agent installation. Additional
> metric sources will be added as separate collectors in future releases.

### Application Insights

Standard metrics (Availability, Requests, Exceptions, Performance) via Azure Monitor.
JVM metrics (heap, GC, threads) via Log Analytics for workspace-linked components.

### Azure Advisor

SKU-change and right-sizing recommendations from the Advisor resource graph.

### Quota Utilization

Compute core quota usage per subscription + region, flagged when ≥ 80 % (configurable).

---

## Authentication

Uses `DefaultAzureCredential` — tries in order:

1. **Azure CLI** — `az login` *(recommended for interactive use)*
2. **Environment variables** — `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET`
3. **Managed Identity** — automatic when running inside Azure / Cloud Shell

---

## Project Structure

```
src/cloudopt/
├── cli.py                  # Typer CLI entry point (collect / analyze / dashboard / export)
├── models.py               # Pydantic v2 data models (VmInventory, VmMetrics, …)
├── scope.py                # Scope + filter resolution
├── config.py               # Interactive threshold prompts
├── collector/
│   ├── auth.py             # DefaultAzureCredential helpers, subscription enumeration
│   ├── inventory.py        # VM inventory via Resource Graph (cross-subscription KQL)
│   ├── metrics.py          # Azure Monitor per-VM metrics, checkpoint/resume
│   ├── appinsights.py      # App Insights inventory + Standard + JVM metrics
│   ├── advisor.py          # Azure Advisor SKU recommendations via Resource Graph
│   ├── quota.py            # Compute quota usage per subscription + region
│   ├── zones.py            # Availability-zone physical→logical mapping
│   └── throttle.py         # Token-bucket rate limiter + exponential backoff
├── analyzer/
│   ├── sku_catalog.py      # Azure Compute SKU cache (vCPUs, memory per region)
│   └── recommendations.py  # Recommendation engine (5 categories, priority scoring)
├── export/
│   ├── json_export.py      # JSON serialisation (subscription IDs masked)
│   ├── excel.py            # Multi-sheet Excel workbook generation (openpyxl)
│   └── csv_export.py       # CSV export (one file per logical sheet)
└── dashboard/
    ├── app.py              # FastAPI REST API + static frontend
    └── templates/
        └── index.html      # Single-page dashboard UI
tests/                      # pytest tests (136 tests, mock Azure SDK clients)
```

---

## Key Design Decisions

| Decision                          | Rationale                                                                    |
| --------------------------------- | ---------------------------------------------------------------------------- |
| JSON-first output                 | Collector runs in Cloud Shell (no Excel); analyst generates workbook locally |
| Read-only                         | Never writes to Azure resources                                              |
| `DefaultAzureCredential` only     | No secrets in code; works with CLI, MI, env vars                             |
| Resource Graph for inventory      | Single cross-subscription KQL call instead of per-RG ARM calls               |
| Token-bucket rate limiter         | ARM has per-subscription read budget; prevents 429 errors                    |
| Subscription IDs masked in output | Reduces accidental exposure when sharing JSON                                |
| Tag values never persisted        | Tag filters are in-memory only                                               |

---

## Running Tests

```bash
pytest                                   # all tests
pytest --cov=cloudopt --cov-report=term  # with coverage
pytest tests/test_metrics.py             # single file
```

---

## Documentation

| Guide                        | Purpose                                                                          |
| ---------------------------- | -------------------------------------------------------------------------------- |
| [HOW_TO.md](HOW_TO.md)       | Installation, authentication, quick start, command overview                      |
| [COLLECTOR.md](COLLECTOR.md) | Full `collect` reference — options, scope files, thresholds, what is collected   |
| [ANALYZER.md](ANALYZER.md)   | Excel generation, dashboard, export, workbook structure, analyst-editable fields |
| [REPORTER.md](REPORTER.md)   | Final report generation from the analyzed workbook *(coming soon)*               |

---

## License

MIT
