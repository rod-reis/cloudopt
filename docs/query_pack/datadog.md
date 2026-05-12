# Datadog Query Pack

Export VM metrics from Datadog in the canonical cloudopt CSV format.

## Prerequisites

```bash
pip install datadog-api-client
export DD_API_KEY="<your-api-key>"
export DD_APP_KEY="<your-app-key>"
```

## Metric Mapping

| cloudopt metric                      | Datadog metric                          |
| ------------------------------------ | --------------------------------------- |
| `os.cpu.used_percent`                | `system.cpu.user` + `system.cpu.system` |
| `os.memory.used_percent`             | `system.mem.pct_usable` (inverted)      |
| `os.memory.available_mb`             | `system.mem.usable`                     |
| `os.memory.swap_used_percent`        | `system.swap.pct_free` (inverted)       |
| `os.disk.read_iops`                  | `system.io.r_s`                         |
| `os.disk.write_iops`                 | `system.io.w_s`                         |
| `os.disk.read_mbps`                  | `system.io.rkb_s` / 1024                |
| `os.disk.write_mbps`                 | `system.io.wkb_s` / 1024                |
| `os.network.receive_mbps`            | `system.net.bytes_rcvd` * 8 / 1e6       |
| `os.network.send_mbps`               | `system.net.bytes_sent` * 8 / 1e6       |
| `jvm.heap.used_percent`              | `jvm.heap_memory_committed` (ratio)     |
| `jvm.gc.pause_ms_avg`                | `jvm.gc.minor_collection_time`          |
| `sql.cpu.used_percent`               | `sqlserver.processor.cpu_usage`         |
| `sql.memory.buffer_pool_hit_percent` | `sqlserver.buffer.cache_hit_ratio`      |

> **Note:** Datadog JVM metrics require the [JVM integration](https://docs.datadoghq.com/integrations/java/)
> or Datadog APM with JVM runtime metrics enabled.

## Export Script

```python
#!/usr/bin/env python3
"""Export Datadog VM metrics to cloudopt canonical CSV format."""

from __future__ import annotations

import csv
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from datadog_api_client import ApiClient, Configuration
from datadog_api_client.v1.api.metrics_api import MetricsApi

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PERIOD_DAYS = 30
OUTPUT_PATH = Path("monitoring_datadog.csv")
HOSTS_CSV = Path("cloudopt_hosts.csv")   # from: cloudopt export-hosts

# Metric definitions: (cloudopt_name, dd_query_template, unit, transform)
# transform is a lambda applied to each value (avg/p95/max); None = identity
METRIC_DEFS = [
    ("os.cpu.used_percent",         "avg:system.cpu.user{host:{h}} + avg:system.cpu.system{host:{h}}", "percent",  None),
    ("os.memory.used_percent",      "100 - avg:system.mem.pct_usable{host:{h}} * 100",                  "percent",  None),
    ("os.memory.available_mb",      "avg:system.mem.usable{host:{h}} / 1048576",                        "MB",       None),
    ("os.memory.swap_used_percent", "100 - avg:system.swap.pct_free{host:{h}} * 100",                   "percent",  None),
    ("os.disk.read_iops",           "avg:system.io.r_s{host:{h}}",                                      "iops",     None),
    ("os.disk.write_iops",          "avg:system.io.w_s{host:{h}}",                                      "iops",     None),
    ("os.disk.read_mbps",           "avg:system.io.rkb_s{host:{h}} / 1024",                             "MBps",     None),
    ("os.disk.write_mbps",          "avg:system.io.wkb_s{host:{h}} / 1024",                             "MBps",     None),
    ("os.network.receive_mbps",     "avg:system.net.bytes_rcvd{host:{h}} * 8 / 1000000",                "MBps",     None),
    ("os.network.send_mbps",        "avg:system.net.bytes_sent{host:{h}} * 8 / 1000000",               "MBps",     None),
]

SCHEMA_VERSION = "1.0"
SOURCE_TOOL = "datadog"


def _load_hostnames(path: Path) -> list[str]:
    """Read VM hostnames from cloudopt host list CSV."""
    if not path.exists():
        print(f"ERROR: Host list not found: {path}", file=sys.stderr)
        sys.exit(1)
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [row["vm_name"] for row in reader if row.get("vm_name")]


def _query_metric(
    api: MetricsApi,
    query: str,
    start: int,
    end: int,
) -> tuple[float | None, float | None, float | None]:
    """Query Datadog and return (avg, p95, max) over the window."""
    try:
        resp = api.query_metrics(_from=start, to=end, query=query)
        if not resp.series:
            return None, None, None
        values = [p[1] for s in resp.series for p in s.pointlist if p[1] is not None]
        if not values:
            return None, None, None
        values_sorted = sorted(values)
        avg = sum(values) / len(values)
        p95 = values_sorted[int(len(values_sorted) * 0.95)]
        max_val = values_sorted[-1]
        return avg, p95, max_val
    except Exception as exc:  # noqa: BLE001
        print(f"  WARNING: query failed: {exc}", file=sys.stderr)
        return None, None, None


def main() -> None:
    now = datetime.now(timezone.utc)
    period_end = now.replace(minute=0, second=0, microsecond=0)
    period_start = period_end - timedelta(days=PERIOD_DAYS)
    ts_end = int(period_end.timestamp())
    ts_start = int(period_start.timestamp())
    period_end_utc = period_end.strftime("%Y-%m-%dT%H:%M:%SZ")

    hostnames = _load_hostnames(HOSTS_CSV)
    print(f"Exporting {len(hostnames)} host(s) over {PERIOD_DAYS} days…")

    config = Configuration()
    rows: list[dict] = []

    with ApiClient(config) as api_client:
        api = MetricsApi(api_client)

        for host in hostnames:
            print(f"  {host}")
            for metric_name, query_template, unit, _transform in METRIC_DEFS:
                query = query_template.replace("{h}", host)
                avg, p95, max_val = _query_metric(api, query, ts_start, ts_end)
                if avg is None:
                    continue
                rows.append({
                    "schema_version": SCHEMA_VERSION,
                    "source_tool": SOURCE_TOOL,
                    "hostname": host,
                    "metric_name": metric_name,
                    "period_days": PERIOD_DAYS,
                    "period_end_utc": period_end_utc,
                    "avg_value": round(avg, 4),
                    "p95_value": round(p95, 4) if p95 is not None else "",
                    "max_value": round(max_val, 4) if max_val is not None else "",
                    "unit": unit,
                })
            time.sleep(0.05)  # stay within Datadog rate limits (20 req/s)

    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "schema_version", "source_tool", "hostname", "metric_name",
            "period_days", "period_end_utc", "avg_value", "p95_value", "max_value", "unit",
        ])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} rows to {OUTPUT_PATH}")
    print(f"\nNext step:\n  cloudopt analyze --from cloudopt_export_<ts>.json --monitoring {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
```

## Cost Estimate

Each host × metric combination = 1 Datadog Metrics query API call.
For 100 hosts × 10 metrics = 1,000 calls, well within the free tier (250,000/month).
