# cloudopt — How-To Guide

**cloudopt** is a read-only CLI that collects Azure VM inventory and performance metrics,
produces a JSON report, and transforms it into an Excel workbook and local web dashboard.

> **Cloud Shell compatible** — `cloudopt collect` runs in Azure Cloud Shell or any
> Python 3.11+ environment. Customer data is never written back to Azure.

---

## Prerequisites

| Requirement                                | Notes                                                       |
| ------------------------------------------ | ----------------------------------------------------------- |
| Python 3.11+                               | `python --version`                                          |
| Azure CLI or service principal credentials | See [Authentication](#authentication)                       |
| **Reader** role on target subscriptions    | Resource Graph + Azure Monitor access                       |
| **Log Analytics Reader** *(optional)*      | Required for JVM metrics from workspace-linked App Insights |

---

## Installation

### Option 1 — Install directly from GitHub (Azure Cloud Shell or any machine with internet access)

No clone or download required:

```bash
pip install git+https://github.com/Azure/cloudopt.git
```

### Option 2 — Install from a zip file (air-gapped or offline delivery)

If you received `cloudopt.zip` (e.g. via email or SharePoint):

```bash
unzip cloudopt.zip
cd cloudopt
pip install .
```

On Windows:

```powershell
Expand-Archive cloudopt.zip -DestinationPath cloudopt
cd cloudopt
pip install .
```

### Option 3 — Clone and install (contributors / developers)

```bash
git clone https://github.com/Azure/cloudopt.git
cd cloudopt
pip install -e .          # editable install
pip install -e ".[dev]"   # with tests + coverage tools
```

Verify any of the above:

```bash
cloudopt version
```

---

## Authentication

The tool uses **Azure DefaultAzureCredential**, which tries these sources in order:

1. **Azure CLI** — run `az login` before collecting (recommended for interactive use)
2. **Environment variables** — set `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET` for service principal auth
3. **Managed Identity** — automatically used when running inside Azure (Cloud Shell, VM, Container)

---

## Quick Start

```bash
# Step 1 — Authenticate (customer)
az login

# Step 2 — Collect from all accessible subscriptions
cloudopt collect

# Step 3 — Share output/cloudopt_report.json with the engineer

# Step 4 — Generate Excel workbook (engineer, no Azure access needed)
cloudopt analyze --from output/cloudopt_report.json

# Step 5 — Browse the data locally
cloudopt dashboard --data output/cloudopt_report.xlsx
```

---

## Commands

| Command              | Who runs it                         | Description                                          | Reference                    |
| -------------------- | ----------------------------------- | ---------------------------------------------------- | ---------------------------- |
| `cloudopt collect`   | Architect/Engineer or Workload SMEs | Collect inventory + metrics + quota + Advisor → JSON | [COLLECTOR.md](COLLECTOR.md) |
| `cloudopt analyze`   | Architect/Engineer                  | Generate Excel workbook from JSON                    | [ANALYZER.md](ANALYZER.md)   |
| `cloudopt dashboard` | Architect/Engineer                  | Launch local web dashboard from the workbook         | [ANALYZER.md](ANALYZER.md)   |
| `cloudopt export`    | Architect/Engineer                  | Re-export workbook to JSON or CSV                    | [ANALYZER.md](ANALYZER.md)   |
| `cloudopt version`   | Anyone                              | Print installed version                              |                              |

---

## Module Guides

| Guide                        | Purpose                                                                                      |
| ---------------------------- | -------------------------------------------------------------------------------------------- |
| [COLLECTOR.md](COLLECTOR.md) | Full `collect` option reference, scope files, thresholds, what is collected, troubleshooting |
| [ANALYZER.md](ANALYZER.md)   | Excel generation, dashboard, export, workbook structure, analyst-editable fields             |
| [REPORTER.md](REPORTER.md)   | Final report generation from the analyzed workbook *(coming soon)*                           |
