# Dynatrace Query Pack

Export VM metrics from Dynatrace in the canonical cloudopt CSV format.

## Prerequisites

- Dynatrace SaaS or Managed with OneAgent on target VMs
- A Dynatrace API token with `metrics.read` scope
- Python `requests` library: `pip install requests`

```bash
export DT_URL="https://<tenant>.live.dynatrace.com"
export DT_API_TOKEN="<your-api-token>"
```

## Metric Mapping

| cloudopt metric           | Dynatrace metric selector                             |
| ------------------------- | ----------------------------------------------------- |
| `os.cpu.used_percent`     | `builtin:host.cpu.usage`                              |
| `os.memory.used_percent`  | `builtin:host.mem.usage`                              |
| `os.memory.available_mb`  | `builtin:host.mem.avail` (bytes → MB ÷ 1048576)       |
| `os.disk.read_iops`       | `builtin:host.disk.readOps`                           |
| `os.disk.write_iops`      | `builtin:host.disk.writeOps`                          |
| `os.disk.read_mbps`       | `builtin:host.disk.throughput.read` (bytes/s → MB/s)  |
| `os.disk.write_mbps`      | `builtin:host.disk.throughput.write` (bytes/s → MB/s) |
| `os.network.receive_mbps` | `builtin:host.net.nic.bytesRx` (bytes/s → MB/s × 8)   |
| `os.network.send_mbps`    | `builtin:host.net.nic.bytesTx` (bytes/s → MB/s × 8)   |
| `jvm.heap.used_percent`   | `ext:jmx.jvm.heapMemoryUsage.used` (ratio → percent)  |
| `jvm.gc.pause_ms_avg`     | `builtin:tech.jvm.garbageCollection.suspensionTime`   |

> **Note:** JVM metrics require the Dynatrace JMX extension or Java OneAgent.

## Export Script

```python
#!/usr/bin/env python3
"""Export Dynatrace VM metrics to cloudopt canonical CSV format."""

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

DT_URL = os.environ["DT_URL"].rstrip("/")
DT_API_TOKEN = os.environ["DT_API_TOKEN"]

PERIOD_DAYS = 30
OUTPUT_PATH = Path("monitoring_dynatrace.csv")
HOSTS_CSV = Path("cloudopt_hosts.csv")

SCHEMA_VERSION = "1.0"
SOURCE_TOOL = "dynatrace"

# (cloudopt_metric_name, dt_selector, unit, scale_factor)
METRIC_DEFS: list[tuple[str, str, str, float]] = [
    ("os.cpu.used_percent",       "builtin:host.cpu.usage",                     "percent", 1.0),
    ("os.memory.used_percent",    "builtin:host.mem.usage",                      "percent", 1.0),
    ("os.memory.available_mb",    "builtin:host.mem.avail",                      "MB",      1 / 1048576),
    ("os.disk.read_iops",         "builtin:host.disk.readOps",                   "iops",    1.0),
    ("os.disk.write_iops",        "builtin:host.disk.writeOps",                  "iops",    1.0),
    ("os.disk.read_mbps",         "builtin:host.disk.throughput.read",           "MBps",    1 / 1048576),
    ("os.disk.write_mbps",        "builtin:host.disk.throughput.write",          "MBps",    1 / 1048576),
    ("os.network.receive_mbps",   "builtin:host.net.nic.bytesRx",               "MBps",    8 / 1000000),
    ("os.network.send_mbps",      "builtin:host.net.nic.bytesTx",               "MBps",    8 / 1000000),
]

HEADERS = {
    "Authorization": f"Api-Token {DT_API_TOKEN}",
    "Content-Type": "application/json",
}


def _load_hostnames(path: Path) -> list[str]:
    if not path.exists():
        print(f"ERROR: Host list not found: {path}", file=sys.stderr)
        sys.exit(1)
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [row["vm_name"] for row in reader if row.get("vm_name")]


def _query_metric(
    selector: str,
    host_filter: str,
    from_ts: str,
    to_ts: str,
    scale: float,
) -> tuple[float | None, float | None, float | None]:
    """Query Dynatrace Metrics v2 API and return (avg, p95, max)."""
    params = {
        "metricSelector": selector,
        "resolution": "1d",
        "from": from_ts,
        "to": to_ts,
        "entitySelector": f'type("HOST"),entityName("{host_filter}")',
    }
    resp = requests.get(
        f"{DT_URL}/api/v2/metrics/query",
        headers=HEADERS,
        params=params,
        timeout=30,
    )
    if resp.status_code == 404:
        return None, None, None
    resp.raise_for_status()
    data = resp.json()
    values: list[float] = []
    for series in data.get("result", [{}])[0].get("data", []):
        values.extend(v * scale for v in series.get("values", []) if v is not None)
    if not values:
        return None, None, None
    values_sorted = sorted(values)
    avg = sum(values) / len(values)
    p95 = values_sorted[int(len(values_sorted) * 0.95)]
    return avg, p95, values_sorted[-1]


def main() -> None:
    now = datetime.now(timezone.utc)
    period_end = now.replace(minute=0, second=0, microsecond=0)
    period_start = period_end - timedelta(days=PERIOD_DAYS)
    from_ts = period_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    to_ts = period_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    period_end_utc = to_ts

    hostnames = _load_hostnames(HOSTS_CSV)
    print(f"Exporting {len(hostnames)} host(s) over {PERIOD_DAYS} days…")

    rows: list[dict] = []
    for host in hostnames:
        print(f"  {host}")
        for metric_name, selector, unit, scale in METRIC_DEFS:
            try:
                avg, p95, max_val = _query_metric(selector, host, from_ts, to_ts, scale)
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
        time.sleep(0.1)  # stay within Dynatrace rate limit (50 req/min per token)

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
