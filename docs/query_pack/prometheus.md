# Prometheus Query Pack

Export VM metrics from Prometheus (node_exporter / windows_exporter) in the canonical cloudopt CSV format.

## Prerequisites

- `node_exporter` (Linux) or `windows_exporter` (Windows) running on target VMs
- Prometheus scraping those exporters
- Python `requests` library: `pip install requests`

```bash
export PROMETHEUS_URL="http://prometheus:9090"
```

## Metric Mapping

### Linux (node_exporter)

| cloudopt metric               | PromQL expression                                                               |
| ----------------------------- | ------------------------------------------------------------------------------- |
| `os.cpu.used_percent`         | `100 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100` |
| `os.memory.used_percent`      | `(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100`       |
| `os.memory.available_mb`      | `node_memory_MemAvailable_bytes / 1048576`                                      |
| `os.memory.swap_used_percent` | `(1 - node_memory_SwapFree_bytes / node_memory_SwapTotal_bytes) * 100`          |
| `os.disk.read_iops`           | `rate(node_disk_reads_completed_total[5m])`                                     |
| `os.disk.write_iops`          | `rate(node_disk_writes_completed_total[5m])`                                    |
| `os.disk.read_mbps`           | `rate(node_disk_read_bytes_total[5m]) / 1048576`                                |
| `os.disk.write_mbps`          | `rate(node_disk_written_bytes_total[5m]) / 1048576`                             |
| `os.network.receive_mbps`     | `rate(node_network_receive_bytes_total[5m]) * 8 / 1000000`                      |
| `os.network.send_mbps`        | `rate(node_network_transmit_bytes_total[5m]) * 8 / 1000000`                     |

### Windows (windows_exporter)

| cloudopt metric          | PromQL expression                                                                      |
| ------------------------ | -------------------------------------------------------------------------------------- |
| `os.cpu.used_percent`    | `100 - windows_cpu_time_total{mode="idle"}`                                            |
| `os.memory.used_percent` | `(1 - windows_os_physical_memory_free_bytes / windows_cs_physical_memory_bytes) * 100` |
| `os.memory.available_mb` | `windows_os_physical_memory_free_bytes / 1048576`                                      |
| `os.disk.read_iops`      | `rate(windows_logical_disk_reads_total[5m])`                                           |
| `os.disk.write_iops`     | `rate(windows_logical_disk_writes_total[5m])`                                          |

## Export Script

```python
#!/usr/bin/env python3
"""Export Prometheus VM metrics to cloudopt canonical CSV format."""

from __future__ import annotations

import csv
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")

PERIOD_DAYS = 30
OUTPUT_PATH = Path("monitoring_prometheus.csv")
HOSTS_CSV = Path("cloudopt_hosts.csv")

SCHEMA_VERSION = "1.0"
SOURCE_TOOL = "prometheus"

# Aggregation step for range_query — 1 day gives 30 data points for 30-day window
STEP = "1d"

# (cloudopt_metric_name, promql_template, unit)
# {host} is replaced by the instance label value (hostname:port or hostname)
METRIC_DEFS: list[tuple[str, str, str]] = [
    (
        "os.cpu.used_percent",
        '100 - avg by (instance) (rate(node_cpu_seconds_total{{instance=~"{host}.*",mode="idle"}}[1h])) * 100',
        "percent",
    ),
    (
        "os.memory.used_percent",
        '(1 - node_memory_MemAvailable_bytes{{instance=~"{host}.*"}} / node_memory_MemTotal_bytes{{instance=~"{host}.*"}}) * 100',
        "percent",
    ),
    (
        "os.memory.available_mb",
        'node_memory_MemAvailable_bytes{{instance=~"{host}.*"}} / 1048576',
        "MB",
    ),
    (
        "os.memory.swap_used_percent",
        '(1 - node_memory_SwapFree_bytes{{instance=~"{host}.*"}} / node_memory_SwapTotal_bytes{{instance=~"{host}.*"}}) * 100',
        "percent",
    ),
    (
        "os.disk.read_iops",
        'sum by (instance) (rate(node_disk_reads_completed_total{{instance=~"{host}.*"}}[1h]))',
        "iops",
    ),
    (
        "os.disk.write_iops",
        'sum by (instance) (rate(node_disk_writes_completed_total{{instance=~"{host}.*"}}[1h]))',
        "iops",
    ),
    (
        "os.disk.read_mbps",
        'sum by (instance) (rate(node_disk_read_bytes_total{{instance=~"{host}.*"}}[1h])) / 1048576',
        "MBps",
    ),
    (
        "os.disk.write_mbps",
        'sum by (instance) (rate(node_disk_written_bytes_total{{instance=~"{host}.*"}}[1h])) / 1048576',
        "MBps",
    ),
    (
        "os.network.receive_mbps",
        'sum by (instance) (rate(node_network_receive_bytes_total{{instance=~"{host}.*"}}[1h])) * 8 / 1000000',
        "MBps",
    ),
    (
        "os.network.send_mbps",
        'sum by (instance) (rate(node_network_transmit_bytes_total{{instance=~"{host}.*"}}[1h])) * 8 / 1000000',
        "MBps",
    ),
]


def _load_hostnames(path: Path) -> list[str]:
    if not path.exists():
        print(f"ERROR: Host list not found: {path}", file=sys.stderr)
        sys.exit(1)
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [row["vm_name"] for row in reader if row.get("vm_name")]


def _range_query(
    promql: str,
    start: float,
    end: float,
) -> tuple[float | None, float | None, float | None]:
    """Query Prometheus range endpoint and return (avg, p95, max)."""
    params = {
        "query": promql,
        "start": start,
        "end": end,
        "step": STEP,
    }
    resp = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query_range",
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        return None, None, None
    all_values: list[float] = []
    for series in data.get("data", {}).get("result", []):
        for _ts, val in series.get("values", []):
            try:
                all_values.append(float(val))
            except (ValueError, TypeError):
                continue
    if not all_values:
        return None, None, None
    all_values.sort()
    avg = sum(all_values) / len(all_values)
    p95 = all_values[int(len(all_values) * 0.95)]
    return avg, p95, all_values[-1]


def main() -> None:
    now = datetime.now(timezone.utc)
    period_end = now.replace(minute=0, second=0, microsecond=0)
    period_start = period_end - timedelta(days=PERIOD_DAYS)
    start_ts = period_start.timestamp()
    end_ts = period_end.timestamp()
    period_end_utc = period_end.strftime("%Y-%m-%dT%H:%M:%SZ")

    hostnames = _load_hostnames(HOSTS_CSV)
    print(f"Exporting {len(hostnames)} host(s) over {PERIOD_DAYS} days…")

    rows: list[dict] = []
    for host in hostnames:
        print(f"  {host}")
        for metric_name, promql_template, unit in METRIC_DEFS:
            promql = promql_template.replace("{host}", host)
            try:
                avg, p95, max_val = _range_query(promql, start_ts, end_ts)
            except Exception as exc:
                print(f"    WARNING: {metric_name}: {exc}", file=sys.stderr)
                continue
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
        time.sleep(0.02)

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

## Thanos / Cortex / Mimir

For long-term storage backends (Thanos, Cortex, Mimir), point `PROMETHEUS_URL` at the
query-frontend endpoint — the same `/api/v1/query_range` API is compatible.
