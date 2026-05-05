# cloudopt — Collector How-To Guide

A read-only CLI tool that collects Azure VM and Application Insights inventory,
performance metrics, quota utilisation, and Azure Advisor findings.
Outputs a **JSON file** that can be shared with a Microsoft engineer to
generate the Excel workbook and dashboard using the separate [Analyzer](ANALYZER.md).

> **Cloud Shell compatible** — `cloudopt collect` has no Excel dependency and
> runs entirely in Azure Cloud Shell or any Python 3.11+ environment.

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

```bash
pip install -e .
```

Or with development dependencies (tests + coverage):

```bash
git clone https://github.com/Azure/cloudopt.git
cd cloudopt
pip install -e ".[dev]"
```

Verify:

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
# Log in
az login

# Collect from all accessible subscriptions (30 days of metrics)
cloudopt collect

# Output is written to ./output/
#   cloudopt_report.json
```

Share `cloudopt_report.json` with the Microsoft engineer who will generate the
Excel workbook using `cloudopt analyze`. See [ANALYZER.md](ANALYZER.md).

---

## Commands

### `collect` — Run a full collection

```bash
cloudopt collect [OPTIONS]
```

| Option                             | Default        | Description                                                                                                                                                     |
| ---------------------------------- | -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--tenant-id` / `-t`               | —              | Microsoft Entra tenant GUID. When set, only subscriptions in this tenant are used and the credential is pinned to it.                                           |
| `--config-file` / `-c`             | —              | WARA-style scope file (see [Scope file](#scope-file-configfile)). CLI flags override values loaded from the file.                                               |
| `--subscriptions` / `-s`           | all accessible | Subscription ID(s) — bare GUID **or** `/subscriptions/<guid>`. Repeatable.                                                                                      |
| `--subscriptions-file` / `-f`      | —              | Path to a text file of subscription IDs (one per line)                                                                                                          |
| `--regions` / `--locations` / `-r` | all regions    | ARM region name(s), e.g. `eastus`. Repeatable. **Global filter** — applied to inventory, App Insights, Advisor, and quota queries.                              |
| `--resource-groups` / `-g`         | all RGs        | Full ARM RG IDs, e.g. `/subscriptions/<guid>/resourceGroups/RG1`. Repeatable. Each RG must reference a subscription that is in scope.                           |
| `--tags`                           | —              | Tag filter expression(s). Operators: `\|\|` = OR, `=~` = equals, `!~` = not equals. Repeatable. **Tag values are used in-memory only and are never persisted.** |
| `--metrics-days` / `-d`            | `30`           | Days of metrics history (1–90)                                                                                                                                  |
| `--output` / `-o`                  | `output/`      | Directory for output files                                                                                                                                      |
| `--dry-run`                        | off            | Count resources and show summary, but skip collection                                                                                                           |
| `--concurrency`                    | `5`            | Max concurrent Azure Monitor API calls per subscription (1–50)                                                                                                  |

**Filter order of operations** (applied in this exact order to every collected resource):

```
Tenant -> Subscriptions -> Locations -> ResourceGroups -> Tags
```

**Tag operators**

| Operator | Action                                                       |
| -------- | ------------------------------------------------------------ |
| `\|\|`   | Or (separates names on the LHS, separates values on the RHS) |
| `=~`     | Equals                                                       |
| `!~`     | Not equals                                                   |

Examples: `Environment\|\|Env=~Prod\|\|PD\|\|Production`, `Owner!~Bill`

**Examples**

```bash
# Specific subscriptions, 60 days of data
cloudopt collect -s aaaa-1111 -s bbbb-2222 --metrics-days 60

# Subscriptions can also be passed as full ARM paths
cloudopt collect -s /subscriptions/aaaa-1111

# Pin to a single tenant
cloudopt collect --tenant-id 11111111-2222-3333-4444-555555555555

# Load subscription IDs from a file (useful for 100s of subscriptions)
cloudopt collect --subscriptions-file my-subs.txt

# Target specific regions only (East US + West Europe) — global filter
cloudopt collect --regions eastus --regions westeurope

# Restrict to two resource groups in one subscription
cloudopt collect `
    -s /subscriptions/aaaa-1111 `
    -g /subscriptions/aaaa-1111/resourceGroups/RG-001 `
    -g /subscriptions/aaaa-1111/resourceGroups/RG-002

# Tag filters (in-memory only — never persisted)
cloudopt collect `
    --tags "Environment||Env=~Prod||Production" `
    --tags "Owner!~Bill"

# Read everything from a single scope file
cloudopt collect --config-file scope.txt

# Preview resource counts without collecting metrics
cloudopt collect --dry-run

# Write reports to a custom directory
cloudopt collect --output /tmp/azure-report
```

#### Scope file (configfile)

WARA-style text file that captures the full collection scope plus a few
runtime knobs in one place.  Sections are case-insensitive; lines starting
with `#` or `;` are comments.  CLI flags override anything loaded from the
file.

```ini
[tenantid]
11111111-2222-3333-4444-555555555555

[subscriptionids]
/subscriptions/aaaa-1111-1111-1111-111111111111
/subscriptions/bbbb-2222-2222-2222-222222222222

[locations]
eastus
westeurope

[resourcegroups]
/subscriptions/bbbb-2222-2222-2222-222222222222/resourceGroups/RG1
/subscriptions/bbbb-2222-2222-2222-222222222222/resourceGroups/RG2

[Tags]
Environment||Env=~Prod||PD||Production
Criticality=~High
Owner!~Bill

# Optional runtime knobs
[metricdays]
60

[concurrency]
8

[output]
./capacity-out
```

Aliases recognised: `[tenant]`, `[subscriptions]`, `[regions]`,
`[resourcegroup]`, `[metric_days]`, `[outputdir]`.

> **Tag values are intentionally never written to the JSON output.**
> They are kept in memory only long enough to decide which resources are
> in scope.

#### Subscriptions file format

Create a plain text file with one subscription ID per line.
Lines starting with `#` are treated as comments.

```
# Production
aaaa-1111-...
bbbb-2222-...

# Dev / Test
cccc-3333-...
```

Pass it with `--subscriptions-file my-subs.txt`.

> **Note:** When using `--subscriptions-file`, pass `--regions` and `--metrics-days`
> on the command line as usual — they apply to all subscriptions in the file.

#### Region filter

Use ARM region names (all lowercase, no spaces) as used in Azure Resource Manager:

```bash
cloudopt collect --regions eastus --regions westeurope --regions australiaeast
```

Common ARM region names:

| Azure Portal name | ARM name        |
| ----------------- | --------------- |
| East US           | `eastus`        |
| East US 2         | `eastus2`       |
| West US 2         | `westus2`       |
| West Europe       | `westeurope`    |
| North Europe      | `northeurope`   |
| UK South          | `uksouth`       |
| Southeast Asia    | `southeastasia` |
| Australia East    | `australiaeast` |

The filter is applied in **Resource Graph KQL** — no ARM API calls are made for
resources outside the specified regions. Omit `--regions` to collect all regions.
The pre-execution summary will show **"Region filter: all regions"** when no
region filter is set, or list the targeted regions when one is applied.

#### Pre-execution summary

Before any API calls are made the tool prints:

- **Services & Metrics** table — every metric that will be collected per service
- **Resources Discovered** table — VM and App Insights counts per subscription
- Output paths and concurrency settings

You are then asked to confirm before collection starts:

```
Proceed with collection? [Y/n]:
```

Enter `Y` (or press Enter) to continue, `N` to abort.

#### Threshold prompts

After confirming, you are prompted to configure **collection thresholds**
(press Enter to accept each default):

```
Underutilized CPU threshold (avg %)    [15.0]:
Underutilized Memory threshold (avg %) [20.0]:
Oversized CPU threshold (P95 %)        [40.0]:
Right-size headroom multiplier          [1.2]:
PaaS candidate CPU threshold (avg %)   [10.0]:
Quota alert threshold (utilization %)  [80.0]:
```

These thresholds are stored in the JSON output so the MS engineer can
reference them when authoring optimizations in the Excel workbook.

#### ARM rate-limit safety

Subscriptions are always processed **one at a time**. Within each subscription,
work is dispatched in batches of `--concurrency` (default **5**). This keeps
in-flight ARM API requests low even when scanning thousands of VMs across
hundreds of subscriptions. Raise `--concurrency` only if collection is slow
and you are not seeing throttle errors.

---

## What Is Collected

### Azure Virtual Machines

| Metric                        | Description                       |
| ----------------------------- | --------------------------------- |
| CPU % (avg / P50 / P95 / max) | Percentage CPU from Azure Monitor |
| Available Memory Bytes        | Free physical memory              |
| Disk Read / Write Bytes/sec   | Storage throughput                |
| Disk Read / Write IOPS        | Storage operations per second     |
| Network In / Out Total Bytes  | Network throughput                |

Also collected: VM inventory (SKU, vCPUs, memory, region, zones, OS image, power state,
disk layout, NIC count, VMSS / availability-set membership).

### Application Insights — Standard metrics

Collected via **Azure Monitor Metrics API** for every App Insights component.

| Category     | Metrics                                                                                             |
| ------------ | --------------------------------------------------------------------------------------------------- |
| Availability | Availability %                                                                                      |
| Requests     | Count, Duration (ms), Failed count                                                                  |
| Exceptions   | Total exceptions, Server exceptions                                                                 |
| Performance  | Process CPU %, Process Private Bytes, Available Memory Bytes, Processor CPU %, Process IO Bytes/sec |

### Application Insights — JVM metrics *(workspace-linked components only)*

Queried from the linked **Log Analytics workspace** using the `customMetrics` table.
Requires the component to use workspace-based (non-classic) ingestion mode.

| Category           | Metrics                                                    |
| ------------------ | ---------------------------------------------------------- |
| JVM Memory         | Heap Used, Heap Committed, Heap Max, Non-Heap Used (bytes) |
| Garbage Collection | GC Pause duration (ms), GC Count                           |
| Threads            | Thread Count                                               |

If `azure-monitor-query` is not installed or a workspace cannot be reached,
JVM metrics are silently skipped without failing the run.

### Azure Advisor

All Advisor recommendations in scope are collected and included in the JSON.
They appear in the **Optimizations** sheet of the Excel workbook after running
`cloudopt analyze`.

### Quota Utilisation

Core quota usage per subscription and region is collected from the Azure
Compute provider and included in the **Quota Utilisation** sheet.

---

## Output Files

After `cloudopt collect`, the output directory contains:

| File                   | Description                                             |
| ---------------------- | ------------------------------------------------------- |
| `cloudopt_report.json` | All collected data in JSON with masked subscription IDs |
| `.checkpoint.json`     | Internal resume file; deleted after a successful run    |

> **Subscription IDs** are partially masked (first 8 characters only) to
> reduce accidental data exposure when sharing the JSON file.

Share `cloudopt_report.json` with the Microsoft engineer. See [ANALYZER.md](ANALYZER.md)
for instructions on generating the Excel workbook from the JSON.

---

## Troubleshooting

**`AuthenticationFailedError` / `CredentialUnavailableError`**  
Run `az login` or verify that service principal environment variables are set.

**`ResourceNotFoundError` on Resource Graph**  
Your account may lack Reader access on one or more subscriptions.
Use `--subscriptions` or `--subscriptions-file` to target only subscriptions you have access to.

**JVM metrics not appearing**  
JVM metrics require the App Insights component to be workspace-based (non-classic ingestion)
and your identity to have **Log Analytics Reader** on the linked workspace.
Classic App Insights components do not have a Log Analytics workspace and will show
only standard metrics.

**Slow collection / throttle errors**  
`--concurrency` defaults to `5` to stay well within ARM rate limits. If you still
see 429 responses, lower it further. The built-in `ThrottleManager` automatically
halves concurrency and respects `Retry-After` headers on transient throttles.

**Interrupted run**  
A `.checkpoint.json` file in the output directory tracks completed VMs.
Re-run the same `cloudopt collect` command to resume from where collection stopped.
