# cloudopt — Collector Guide

The **Collector** is the first phase of the cloudopt workflow. It runs on any machine
with Azure access (including Azure Cloud Shell) and produces a single JSON file that
captures VM inventory, performance metrics, quota utilisation, and Azure Advisor findings.

> **Read-only** — the collector never writes to Azure resources.  
> **Cloud Shell compatible** — no Excel dependency; runs in Python 3.11+ environments.

---

## The `collect` command

```bash
cloudopt collect [OPTIONS]
```

| Option                             | Default        | Description                                                                                                                                                     |
| ---------------------------------- | -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--tenant-id` / `-t`               | —              | Microsoft Entra tenant GUID. When set, only subscriptions in this tenant are used and the credential is pinned to it.                                           |
| `--config-file` / `-c`             | —              | Scope configuration file (see [Scope file](#scope-file---config-file)). CLI flags override values loaded from the file.                                            |
| `--subscriptions` / `-s`           | all accessible | Subscription ID(s) — bare GUID **or** `/subscriptions/<guid>`. Repeatable.                                                                                      |
| `--subscriptions-file` / `-f`      | —              | Path to a text file of subscription IDs (one per line).                                                                                                         |
| `--regions` / `--locations` / `-r` | all regions    | ARM region name(s), e.g. `eastus`. Repeatable. **Global filter** — applied to inventory, App Insights, Advisor, and quota queries.                              |
| `--resource-groups` / `-g`         | all RGs        | Full ARM RG IDs, e.g. `/subscriptions/<guid>/resourceGroups/RG1`. Repeatable. Each RG must reference a subscription that is in scope.                           |
| `--tags`                           | —              | Tag filter expression(s). Operators: `\|\|` = OR, `=~` = equals, `!~` = not equals. Repeatable. **Tag values are used in-memory only and are never persisted.** |
| `--metrics-days` / `-d`            | `30`           | Days of metrics history (1–90).                                                                                                                                 |
| `--output` / `-o`                  | `output/`      | Directory for output files.                                                                                                                                     |
| `--dry-run`                        | off            | Count resources and show summary, but skip collection.                                                                                                          |
| `--concurrency`                    | `5`            | Max concurrent Azure Monitor API calls per subscription (1–50).                                                                                                 |

---

## Scope and Filtering

### Filter order of operations

All filters are applied in this strict order to every collected resource:

```
Tenant → Subscriptions → Locations (Regions) → ResourceGroups → Tags
```

### Tag operators

| Operator | Action                                                       |
| -------- | ------------------------------------------------------------ |
| `\|\|`   | Or (separates names on the LHS, separates values on the RHS) |
| `=~`     | Equals                                                       |
| `!~`     | Not equals                                                   |

Examples: `Environment||Env=~Prod||PD||Production`, `Owner!~Bill`

> Tag values are **kept in memory only** and are never written to any output file.

---

## Examples

```bash
# Collect from all accessible subscriptions (30 days of metrics)
cloudopt collect

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

# Write output to a custom directory
cloudopt collect --output /tmp/azure-report
```

---

## Scope file (`--config-file`)

A scope configuration text file that captures the full collection scope and runtime settings
in one place. Sections are case-insensitive; lines starting with `#` or `;` are
comments. CLI flags override anything loaded from the file.

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

---

## Subscriptions file (`--subscriptions-file`)

Plain text file with one subscription ID per line. Lines starting with `#` are comments.

```
# Production
aaaa-1111-...
bbbb-2222-...

# Dev / Test
cccc-3333-...
```

> When using `--subscriptions-file`, pass `--regions` and `--metrics-days` on the
> command line as usual — they apply to all subscriptions in the file.

---

## Region filter

Use ARM region names (all lowercase, no spaces):

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

The filter is pushed into **Resource Graph KQL** — no ARM API calls are made for
resources outside the specified regions. Omit `--regions` to collect all regions.

---

## Pre-execution summary

Before any API calls are made the tool prints:

- **Services & Metrics** table — every metric that will be collected per service
- **Resources Discovered** table — VM and App Insights counts per subscription
- Output paths and concurrency settings

You are then asked to confirm:

```
Proceed with collection? [Y/n]:
```

---

## Threshold prompts

After confirming, you are prompted to configure collection thresholds
(press Enter to accept each default):

```
Underutilized CPU threshold (avg %)    [15.0]:
Underutilized Memory threshold (avg %) [20.0]:
Oversized CPU threshold (P95 %)        [40.0]:
Right-size headroom multiplier          [1.2]:
PaaS candidate CPU threshold (avg %)   [10.0]:
Quota alert threshold (utilization %)  [80.0]:
```

These thresholds are stored in the JSON output so the engineer can reference them
when authoring optimizations in the Excel workbook.

---

## ARM rate-limit safety

Subscriptions are always processed **one at a time**. Within each subscription, work
is dispatched in batches of `--concurrency` (default **5**). This keeps in-flight ARM
API requests low even when scanning thousands of VMs across hundreds of subscriptions.
The built-in `ThrottleManager` automatically halves concurrency and respects
`Retry-After` headers on transient 429 responses.

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

Collected via the **Azure Monitor Metrics API** for every App Insights component.

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

If the workspace cannot be reached, JVM metrics are silently skipped without
failing the run.

### Azure Advisor

All Advisor recommendations in scope are collected and appear in the
**Optimizations** sheet of the Excel workbook after running `cloudopt analyze`.

### Quota Utilisation

Compute core quota usage per subscription and region, included in the
**Quota Utilisation** sheet of the workbook.

---

## Output Files

After a successful `cloudopt collect`, the output directory contains:

| File                   | Description                                             |
| ---------------------- | ------------------------------------------------------- |
| `cloudopt_report.json` | All collected data in JSON with masked subscription IDs |
| `.checkpoint.json`     | Internal resume file; deleted after a successful run    |

> **Subscription IDs** are partially masked (first 8 characters only) to reduce
> accidental data exposure when sharing the JSON file.

Share `cloudopt_report.json` with the engineer who will generate the Excel workbook.
See [ANALYZER.md](ANALYZER.md).

---

## Troubleshooting

**`AuthenticationFailedError` / `CredentialUnavailableError`**  
Run `az login` or verify that service principal environment variables
(`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET`) are set.

**`ResourceNotFoundError` on Resource Graph**  
Your account may lack Reader access on one or more subscriptions.
Use `--subscriptions` or `--subscriptions-file` to target only subscriptions
you have access to.

**JVM metrics not appearing**  
JVM metrics require the App Insights component to be workspace-based
(non-classic ingestion) and your identity to have **Log Analytics Reader** on
the linked workspace. Classic components will show only standard metrics.

**Slow collection / throttle errors**  
Lower `--concurrency` if you see persistent 429 responses. The `ThrottleManager`
will also self-regulate by halving concurrency automatically on throttle signals.

**Interrupted run**  
A `.checkpoint.json` file in the output directory tracks completed VMs.
Re-run the exact same `cloudopt collect` command to resume from where it stopped.
3. Add corresponding Pydantic models to `models.py`
4. Write mocked unit tests in `tests/` (mock the Azure SDK clients, not HTTP)
5. Export new data in `export/json_export.py`
