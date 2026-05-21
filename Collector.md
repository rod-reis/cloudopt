# cloudopt — Collector Guide

The **Collector** is the first phase of the cloudopt workflow. It runs on any machine
with Azure access (including Azure Cloud Shell) and produces a single JSON file that
captures VM inventory, performance metrics, quota utilization, Azure Advisor findings,
Azure Monitor alert rules, and workload archetype classifications.

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
| `--config-file` / `-c`             | —              | Scope configuration file (see [Scope file](#scope-file---config-file)). CLI flags override values loaded from the file.                                         |
| `--subscriptions` / `-s`           | all accessible | Subscription ID(s) — bare GUID **or** `/subscriptions/<guid>`. Repeatable.                                                                                      |
| `--subscriptions-file` / `-f`      | —              | Path to a text file of subscription IDs (one per line).                                                                                                         |
| `--regions` / `--locations` / `-r` | all regions    | ARM region name(s), e.g. `eastus`. Repeatable. **Global filter** — applied to inventory, App Insights, Advisor, and quota queries.                              |
| `--resource-groups` / `-g`         | all RGs        | Full ARM RG IDs, e.g. `/subscriptions/<guid>/resourceGroups/RG1`. Repeatable. Each RG must reference a subscription that is in scope.                           |
| `--tags`                           | —              | Tag filter expression(s). Operators: `\|\|` = OR, `=~` = equals, `!~` = not equals. Repeatable. **Tag values are used in-memory only and are never persisted.** |
| `--metrics-days` / `-d`            | `30`           | Days of metrics history (1–90). Minimum 2 days needed for workload archetype classification (≥48 hourly points). Use 14+ days for reliable pattern detection.   |
| `--output` / `-o`                  | `output/`      | Directory for output files.                                                                                                                                     |
| `--dry-run`                        | off            | Count resources and show summary, but skip collection.                                                                                                          |
| `--concurrency`                    | `25`           | Max concurrent Azure Monitor API calls per subscription (1–100).                                                                                                |
| `--arm-rate`                       | `20.0`         | Target ARM read requests per second per subscription (1.0–100.0). Lower this if you hit 429 errors.                                                             |
| `--debug`                          | off            | Print per-step timing, total elapsed time, and enable verbose logging.                                                                                          |

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

# Enable per-step timing output
cloudopt collect --debug
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

[armrate]
20

[output]
./capacity-out

# Collect Azure Monitor alert rules for capacity ops hygiene (QTA-OPS-001).
# Disable to speed up collection when alert data is not needed.
[collect_alerts]
true

# Print per-step timing (equivalent to --debug flag)
[debug]
false

# Count resources only, skip collection (equivalent to --dry-run flag)
[dryrun]
false
```

### All recognised section names

| Section | Aliases | CLI flag equivalent |
|---|---|---|
| `[tenantid]` | `[tenant]` | `--tenant-id` |
| `[subscriptionids]` | `[subscriptions]` | `--subscriptions` |
| `[locations]` | `[regions]` | `--regions` / `--locations` |
| `[resourcegroups]` | `[resourcegroup]` | `--resource-groups` |
| `[tags]` | — | `--tags` |
| `[metricdays]` | `[metric_days]` | `--metrics-days` |
| `[concurrency]` | — | `--concurrency` |
| `[armrate]` | `[arm_rate]`, `[armratepersecond]` | `--arm-rate` |
| `[output]` | `[outputdir]` | `--output` |
| `[collect_alerts]` | `[collectalerts]` | *(config only — default: true)* |
| `[debug]` | — | `--debug` |
| `[dryrun]` | `[dry_run]` | `--dry-run` |

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
when reviewing findings in the Excel workbook or dashboard.

---

## ARM rate-limit safety

Subscriptions are always processed **one at a time**. Within each subscription, work
is dispatched in batches of `--concurrency` (default **25**). This keeps in-flight ARM
API requests low even when scanning thousands of VMs across hundreds of subscriptions.
The built-in `ThrottleManager` automatically halves concurrency and respects
`Retry-After` headers on transient 429 responses.

---

## Collection steps

The `collect` command runs the following steps in order:

| Step | Description |
|---|---|
| 1 | Authenticate to Azure via `DefaultAzureCredential` |
| 2 | Enumerate in-scope subscriptions |
| 3 | Collect VM + VMSS inventory via Resource Graph KQL |
| 4 | Collect App Insights inventory |
| 5 | Collect App Insights standard and JVM metrics |
| 6 | Collect capacity quota utilization per subscription/region |
| 7a | Collect Azure Advisor SKU-change recommendations |
| 7b | Collect availability-zone logical→physical mappings |
| 7c | Collect zone mappings |
| 7d | Collect full resource inventory (all ARM types, for cleanup detectors) |
| 7d-ii | Collect empty resource groups (CLN-RGP-001) |
| 7e | Collect VM stop history from Activity Log (stopped/deallocated VMs only) |
| 7f | Collect VMSS Uniform group inventory and CPU metrics |
| 7g | Resolve VMSS parent services (AKS, AVD, etc.) |
| **7h** | **Collect Azure Monitor alert rules** (quota, allocation failure, service health) |
| **7i** | **Classify workload archetypes** from hourly CPU patterns |
| 8 | Write `cloudopt_export_<timestamp>.json` |

Steps 7h and 7i are the inputs that power the **Capacity Ops Hygiene** scorecard
(`QTA-OPS-001`) and the **Workload Archetypes** dashboard section respectively.

### Disabling alert collection

Alert rule collection (Step 7h) makes additional Azure Resource Graph calls across all
subscriptions. If you want a faster run and don't need the hygiene scorecard, disable it:

```ini
# In your scope file:
[collect_alerts]
false
```

---

## What Is Collected

### Azure Virtual Machines

VM performance data is collected from the **Azure Monitor Metrics API** as
**host-level platform metrics** — the same counters visible under "Metrics" in
the Azure portal for a VM resource.  **No guest agent, VM Insights extension,
or Log Analytics workspace is required.** This means the tool works on every VM
regardless of OS type or agent installation state.

> **Scope of current metrics:** Because these are host-level counters, memory is
> limited to a single `Available Memory Bytes` value. Richer sources such as
> VM Insights (AMA), Datadog, or Splunk are provided via the optional monitoring
> CSV (`cloudopt analyze --monitoring`).

| Metric                                    | Description                                          |
| ----------------------------------------- | ---------------------------------------------------- |
| CPU % (avg / P50 / P95 / P99 / max / min) | Host-level CPU from Azure Monitor platform telemetry |
| Available Memory Bytes                    | Free physical memory (host counter)                  |
| Disk Read / Write Bytes/sec               | Storage throughput                                   |
| Disk Read / Write IOPS                    | Storage operations per second                        |
| Network In / Out Total Bytes              | Network throughput                                   |

Also collected: VM inventory (SKU, vCPUs, memory, region, zones, OS image, power state,
disk layout, NIC count, VMSS / availability-set membership).

### Workload Archetype Classification

After metrics are collected, cloudopt classifies every VM's CPU pattern into one
of 7 archetypes using ≥48 hourly data points (requires `--metrics-days 2` minimum;
14–30 days recommended for reliable classification):

| Archetype | Signal |
|---|---|
| `steady-24x7` | Low CV (< 0.2) — consistent load around the clock |
| `business-hours` | Weekday-work-hour mean ÷ off-hour mean > 3.0 |
| `weekend-idle` | Weekday mean ÷ weekend mean > 4.0 |
| `bursty` | P95/P50 > 2.5 AND CV ≥ 0.5 |
| `spiky` | P99/P95 > 1.8 |
| `dev-test-irregular` | ≥ 20% near-zero CPU readings AND CV > 0.8 |
| `unknown` | Insufficient data (< 48 hourly data points) |

Archetypes are stored in the JSON and used by the detector pipeline to corroborate
findings (e.g., `bursty` confirms a burstable-fit recommendation) and are shown in the
**Workload Archetypes** section of the dashboard.

### Azure Monitor Alert Rules (Capacity Ops Hygiene)

Collected via Resource Graph across all in-scope subscriptions. These are used by the
`QTA-OPS-001` detector to assess whether each subscription has the minimum monitoring
coverage for capacity operations:

| Sub-check | What is checked |
|---|---|
| A: Quota Alert | Metric alert on vCPU quota utilization |
| B: Alloc Failure Alert | Activity log alert for `AllocationFailed` / `SkuNotAvailable` |
| C: QuotaExceeded Alert | Activity log alert for `QuotaExceeded` |
| D: CRR Alert | CRG utilization alert (only when CRGs exist in that subscription) |
| E: Service Health | Service Health alert for the `Compute` category |

Disable with `[collect_alerts] false` in your scope file if not needed.

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

### Quota Utilization

Compute core quota usage per subscription and region, included in the
**Quota Utilization** sheet of the workbook.

---

## Output Files

After a successful `cloudopt collect`, the output directory contains:

| File                              | Description                                             |
| --------------------------------- | ------------------------------------------------------- |
| `cloudopt_export_<timestamp>.json` | All collected data in JSON with masked subscription IDs |
| `.checkpoint.json`                | Internal resume file; deleted after a successful run    |

> **Subscription IDs** are partially masked (first 8 characters only) to reduce
> accidental data exposure when sharing the JSON file.

Share `cloudopt_export_<timestamp>.json` with the engineer who will generate the Excel workbook.
See [ANALYZER.md](Analyzer.md).

---

## Troubleshooting

**`AuthenticationFailedError` / `CredentialUnavailableError`**  
Run `az login` or verify that service principal environment variables
(`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET`) are set.

**`ResourceNotFoundError` on Resource Graph**  
Your account may lack Reader access on one or more subscriptions.
Use `--subscriptions` or `--subscriptions-file` to target only subscriptions
you have access to.

**All archetypes show `unknown`**  
The VM has fewer than 48 hourly CPU data points. Increase `--metrics-days`
to at least 2 (48 points); 14+ days is recommended for reliable classification.

**JVM metrics not appearing**  
JVM metrics require the App Insights component to be workspace-based
(non-classic ingestion) and your identity to have **Log Analytics Reader** on
the linked workspace. Classic components will show only standard metrics.

**Capacity hygiene scorecard shows no data**  
Alert collection may have been disabled (`[collect_alerts] false`), or the
JSON was collected with an older version of cloudopt. Re-run `cloudopt collect`
to populate alert rule data.

**Slow collection / throttle errors**  
Lower `--concurrency` or `--arm-rate` if you see persistent 429 responses. The
`ThrottleManager` will also self-regulate by halving concurrency automatically on
throttle signals.

**Interrupted run**  
A `.checkpoint.json` file in the output directory tracks completed VMs.
Re-run the exact same `cloudopt collect` command to resume from where it stopped.
