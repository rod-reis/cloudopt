# cloudopt Customer Data Contract

> **This is a data contract, not a script library.** cloudopt does not ship runnable queries for any monitoring tool. Your monitoring team owns the export. We tell you exactly **what** we need and in **what shape**; how you produce it from your tool is your domain.

---

## Workflow

```
1. Microsoft engineer:  cloudopt collect                  → cloudopt_report.json
2. Microsoft engineer:  cloudopt request                  → request_package/
3. Customer:            (export per request_package)      → customer_export.csv
4. Microsoft engineer:  cloudopt analyze --monitoring …   → cloudopt_report.xlsx
```

The `request_package/` folder contains everything the customer's monitoring team needs:

```
request_package/
├── host_list.csv             # The VMs we want data for
├── metric_request.md         # Which metrics, by workload type
├── collection_spec.md        # Two-pass requirement (grain + window)
├── submission_template.csv   # Blank file in the v2 schema for the customer to fill
└── vendor_notes/
    ├── datadog.md
    ├── splunk.md
    ├── dynatrace.md
    ├── newrelic.md
    ├── elastic.md
    └── prometheus.md
```

---

## What we ask the customer to produce

A **single CSV file** (schema v2.0) containing **pre-aggregated** statistics — one row per `(VM, metric, pass)`. The customer's monitoring tool already computes percentiles natively; do that there and send us the summaries, not the raw points.

For 1,000 VMs × ~10 metrics × 2 passes this is ~20,000 rows — fits in a single email-able CSV.

### Why pre-aggregated and not raw time-series

- Raw 5-minute data for 1,000 VMs over 14 days is roughly 20 million rows. Not transferable, not necessary.
- Every supported monitoring tool computes avg / p95 / max / min natively — using its own indexed storage is far faster than re-computing on our side from raw points.
- We use the summaries to derive findings; the raw points add no signal beyond what avg/p95/max/min already capture for a 90-day or 14-day window.

### Why CSV (and not JSON)

- Every supported platform exports CSV natively (Datadog notebook export, NRQL, DQL, Splunk search, Elastic Discover, Prometheus → CSV via standard tools).
- The shape is flat — one row per `(VM, metric, pass)` — so CSV is the right tool. JSON would add nesting we don't need.
- Customer monitoring teams produce CSV constantly; JSON adds friction.

We use JSON internally in cloudopt because **our** model is hierarchical (multiple sources, series, findings per VM). The customer's data is flat, so it stays CSV.

---

## Two-pass collection (required)

For each metric, the customer produces **two rows**: one trend row, one peak row.

| Pass | `grain` | `window_days` | Aggregations to populate |
|---|---|---|---|
| **trend** | `PT1H` | 90 (default) — accepts 30 minimum | `avg_value`, `p95_value`, `max_value` (and `min_value` for memory-available metrics) |
| **peak** | `PT5M` | 14 | `max_value` for CPU-style metrics, `min_value` for memory-available |

`PT1M` is **not** requested — adds noise without changing decisions and inflates query cost on the customer's tool.

If the customer can only provide one pass, send the **trend** pass. The peak pass adds burst detection but is optional.

---

## Schema v2.0 — the only accepted format

| Column | Type | Required | Notes |
|---|---|---|---|
| `schema_version` | string | yes | Must be `2.0` |
| `vendor` | string | yes | `datadog` / `splunk` / `dynatrace` / `newrelic` / `elastic` / `prometheus` / `custom` |
| `vm_id` | string | optional | Azure resource ID if the customer can map; otherwise blank |
| `hostname` | string | yes | Used for fuzzy match against `host_list.csv` when `vm_id` is blank |
| `workload_group` | string | yes | `os` / `jvm` / `dotnet` / `iis` / `sql` / `postgres` / `mysql` |
| `metric_name` | string | yes | From the canonical catalog below — exact match required |
| `pass` | string | yes | `trend` or `peak` |
| `grain` | string | yes | `PT1H` for trend, `PT5M` for peak |
| `window_days` | integer | yes | E.g. `90` for trend, `14` for peak |
| `period_start_utc` | ISO-8601 | yes | UTC, e.g. `2026-02-11T00:00:00Z` |
| `period_end_utc` | ISO-8601 | yes | UTC |
| `avg_value` | float | conditional | Required for trend pass; may be blank for peak pass |
| `p95_value` | float | conditional | Required for trend pass; may be blank for peak pass |
| `max_value` | float | conditional | Required for any "max"-style metric (CPU, IOPS, requests/sec, etc.) |
| `min_value` | float | conditional | Required for any "min"-style metric (memory available, free disk) |
| `unit` | string | yes | Must match the catalog unit exactly |

### Example rows

```csv
schema_version,vendor,vm_id,hostname,workload_group,metric_name,pass,grain,window_days,period_start_utc,period_end_utc,avg_value,p95_value,max_value,min_value,unit
2.0,datadog,,myvm-prod-01,os,os.cpu.used_percent,trend,PT1H,90,2025-11-13T00:00:00Z,2026-02-11T00:00:00Z,18.4,42.1,76.3,,percent
2.0,datadog,,myvm-prod-01,os,os.cpu.used_percent,peak,PT5M,14,2026-01-29T00:00:00Z,2026-02-11T00:00:00Z,,,89.2,,percent
2.0,datadog,,myvm-prod-01,os,os.memory.available_mb,trend,PT1H,90,2025-11-13T00:00:00Z,2026-02-11T00:00:00Z,12480.0,,,4210.0,MB
2.0,datadog,,myvm-prod-01,jvm,jvm.heap.used_percent,trend,PT1H,90,2025-11-13T00:00:00Z,2026-02-11T00:00:00Z,52.1,78.3,91.7,,percent
```

### Compatibility

Schema v1.0 (`period_days`, no `pass` column) is read for one release with a deprecation warning, then removed. New engagements should produce v2.0 from the start.

---

## Canonical metric catalog

Frozen for v2.0. New metrics require a minor schema bump. The customer **must use these exact names** — no synonyms, no vendor-native names. The translation from vendor metric → canonical metric happens on the customer's side.

### OS group (requested for every VM)

| `metric_name` | `unit` | Description |
|---|---|---|
| `os.cpu.used_percent` | percent | CPU utilization (OS view) |
| `os.memory.used_percent` | percent | Memory used % (excluding OS cache) |
| `os.memory.available_mb` | MB | Available physical memory |
| `os.memory.swap_used_percent` | percent | Swap / page file utilization |
| `os.disk.read_iops` | iops | Disk read IOPS (all disks) |
| `os.disk.write_iops` | iops | Disk write IOPS (all disks) |
| `os.disk.read_mbps` | MBps | Disk read throughput |
| `os.disk.write_mbps` | MBps | Disk write throughput |
| `os.network.receive_mbps` | MBps | Network receive throughput |
| `os.network.send_mbps` | MBps | Network send throughput |

### JVM group (Java workloads)

| `metric_name` | `unit` |
|---|---|
| `jvm.heap.used_percent` | percent |
| `jvm.heap.used_mb` | MB |
| `jvm.heap.max_mb` | MB |
| `jvm.gc.pause_ms_avg` | ms |
| `jvm.gc.pause_ms_p99` | ms |
| `jvm.threads.live_count` | count |

### .NET group

| `metric_name` | `unit` |
|---|---|
| `dotnet.gc.heap_size_mb` | MB |
| `dotnet.gc.pause_ms_avg` | ms |
| `dotnet.threadpool.queue_depth_avg` | count |
| `dotnet.exceptions.rate` | per_sec |

### IIS group

| `metric_name` | `unit` |
|---|---|
| `iis.requests.per_sec` | per_sec |
| `iis.connections.current` | count |
| `iis.queue.length` | count |
| `iis.worker.restarts` | count |

### SQL Server group

| `metric_name` | `unit` |
|---|---|
| `sql.cpu.used_percent` | percent |
| `sql.memory.buffer_pool_hit_percent` | percent |
| `sql.disk.read_iops` | iops |
| `sql.disk.write_iops` | iops |
| `sql.connections.active_count` | count |
| `sql.waits.total_wait_ms_avg` | ms |
| `sql.batch_requests.per_sec` | per_sec |

### PostgreSQL group

| `metric_name` | `unit` |
|---|---|
| `postgres.cpu.used_percent` | percent |
| `postgres.connections.active_count` | count |
| `postgres.cache.hit_ratio_percent` | percent |
| `postgres.transactions.per_sec` | per_sec |

### MySQL group

| `metric_name` | `unit` |
|---|---|
| `mysql.cpu.used_percent` | percent |
| `mysql.connections.active_count` | count |
| `mysql.innodb.buffer_pool_hit_ratio_percent` | percent |
| `mysql.queries.per_sec` | per_sec |

---

## Confidence impact

The presence of customer data lifts the maximum confidence cloudopt is willing to assign to its findings:

| Sources present for a VM | Max confidence |
|---|---|
| Azure platform metrics only | MEDIUM (HIGH only for `decom.idle` and clear `rightsize.upsize`) |
| Platform + customer OS group | HIGH for right-size and SKU swap |
| Platform + customer OS + workload group (JVM / .NET / IIS / SQL / Postgres / MySQL) | HIGH for family-shift swaps |

If the customer cannot share data, cloudopt still produces findings — they will be labeled `MEDIUM` / `LOW` confidence with a `blockers_to_high[]` field telling the customer exactly what to share to upgrade them.

---

## Vendor notes

The `vendor_notes/` folder contains short, **non-prescriptive** pointers per platform — which API or feature naturally produces the v2 schema, common pitfalls, hostname conventions. The customer's monitoring team translates from vendor-native to canonical. We don't ship runnable scripts because:

- Vendor APIs evolve faster than we can maintain scripts for six platforms.
- Customer-specific tagging, namespacing, and metric availability vary too widely for a one-size-fits-all script.
- The customer's monitoring team knows their environment. They are the right party to write the export.

---

## Cost note

Most monitoring platforms charge for API query volume. Pre-aggregated queries (one summary per VM/metric/pass) are dramatically cheaper than raw time-series queries. The schema is designed to minimize API cost on the customer's side.
