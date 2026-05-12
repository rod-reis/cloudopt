# New Relic Query Pack

Export VM metrics from New Relic in the canonical cloudopt CSV format.

## Prerequisites

- New Relic Infrastructure agent on target VMs
- A New Relic User API key with `NerdGraph` access
- Python `requests` library: `pip install requests`

```bash
export NEW_RELIC_API_KEY="NRAK-..."
export NEW_RELIC_ACCOUNT_ID="<account-id>"
```

## Metric Mapping

| cloudopt metric               | New Relic metric / NRQL                                          |
| ----------------------------- | ---------------------------------------------------------------- |
| `os.cpu.used_percent`         | `SystemSample.cpuPercent`                                        |
| `os.memory.used_percent`      | `SystemSample.memoryUsedPercent`                                 |
| `os.memory.available_mb`      | `SystemSample.memoryAvailableMegabytes`                          |
| `os.memory.swap_used_percent` | `SystemSample.swapUsedPercent`                                   |
| `os.disk.read_iops`           | `StorageSample.readIoOperationsPerSecond`                        |
| `os.disk.write_iops`          | `StorageSample.writeIoOperationsPerSecond`                       |
| `os.disk.read_mbps`           | `StorageSample.readBytesPerSecond` / 1048576                     |
| `os.disk.write_mbps`          | `StorageSample.writeBytesPerSecond` / 1048576                    |
| `os.network.receive_mbps`     | `NetworkSample.receiveBytesPerSecond` × 8 / 1000000              |
| `os.network.send_mbps`        | `NetworkSample.transmitBytesPerSecond` × 8 / 1000000             |
| `jvm.heap.used_percent`       | `JvmThreadSample.heapMemoryUsed / JvmThreadSample.heapMemoryMax` |
| `jvm.gc.pause_ms_avg`         | `JvmThreadSample.gcConcurrentMarkSweepTime`                      |

## Export Script

```python
#!/usr/bin/env python3
"""Export New Relic VM metrics to cloudopt canonical CSV format."""

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

NR_API_KEY = os.environ["NEW_RELIC_API_KEY"]
NR_ACCOUNT_ID = os.environ["NEW_RELIC_ACCOUNT_ID"]
NR_GRAPHQL_URL = "https://api.newrelic.com/graphql"

PERIOD_DAYS = 30
OUTPUT_PATH = Path("monitoring_newrelic.csv")
HOSTS_CSV = Path("cloudopt_hosts.csv")

SCHEMA_VERSION = "1.0"
SOURCE_TOOL = "newrelic"

# (cloudopt_metric_name, nrql_template, unit)
# {host} is replaced with the VM name; {since} with the SINCE clause
NRQL_DEFS: list[tuple[str, str, str]] = [
    (
        "os.cpu.used_percent",
        "SELECT average(cpuPercent) AS avg_value, percentile(cpuPercent, 95) AS p95_value, max(cpuPercent) AS max_value FROM SystemSample WHERE hostname = '{host}' SINCE {days} days AGO",
        "percent",
    ),
    (
        "os.memory.used_percent",
        "SELECT average(memoryUsedPercent) AS avg_value, percentile(memoryUsedPercent, 95) AS p95_value, max(memoryUsedPercent) AS max_value FROM SystemSample WHERE hostname = '{host}' SINCE {days} days AGO",
        "percent",
    ),
    (
        "os.memory.available_mb",
        "SELECT average(memoryAvailableMegabytes) AS avg_value, percentile(memoryAvailableMegabytes, 5) AS p95_value, min(memoryAvailableMegabytes) AS max_value FROM SystemSample WHERE hostname = '{host}' SINCE {days} days AGO",
        "MB",
    ),
    (
        "os.disk.read_iops",
        "SELECT average(readIoOperationsPerSecond) AS avg_value, percentile(readIoOperationsPerSecond, 95) AS p95_value, max(readIoOperationsPerSecond) AS max_value FROM StorageSample WHERE hostname = '{host}' SINCE {days} days AGO",
        "iops",
    ),
    (
        "os.disk.write_iops",
        "SELECT average(writeIoOperationsPerSecond) AS avg_value, percentile(writeIoOperationsPerSecond, 95) AS p95_value, max(writeIoOperationsPerSecond) AS max_value FROM StorageSample WHERE hostname = '{host}' SINCE {days} days AGO",
        "iops",
    ),
]

_HEADERS = {
    "Content-Type": "application/json",
    "API-Key": NR_API_KEY,
}


def _load_hostnames(path: Path) -> list[str]:
    if not path.exists():
        print(f"ERROR: Host list not found: {path}", file=sys.stderr)
        sys.exit(1)
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [row["vm_name"] for row in reader if row.get("vm_name")]


def _run_nrql(nrql: str) -> dict[str, float | None]:
    """Execute a NRQL query via NerdGraph and return result columns."""
    gql = {
        "query": f"""
            {{
              actor {{
                account(id: {NR_ACCOUNT_ID}) {{
                  nrql(query: "{nrql.replace('"', '\\"')}") {{
                    results
                  }}
                }}
              }}
            }}
        """
    }
    resp = requests.post(NR_GRAPHQL_URL, headers=_HEADERS, json=gql, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    results = (
        data.get("data", {})
        .get("actor", {})
        .get("account", {})
        .get("nrql", {})
        .get("results", [])
    )
    if not results:
        return {"avg_value": None, "p95_value": None, "max_value": None}
    r = results[0]
    return {
        "avg_value": r.get("avg_value"),
        "p95_value": r.get("p95_value"),
        "max_value": r.get("max_value"),
    }


def main() -> None:
    now = datetime.now(timezone.utc)
    period_end = now.replace(minute=0, second=0, microsecond=0)
    period_end_utc = period_end.strftime("%Y-%m-%dT%H:%M:%SZ")

    hostnames = _load_hostnames(HOSTS_CSV)
    print(f"Exporting {len(hostnames)} host(s) over {PERIOD_DAYS} days…")

    rows: list[dict] = []
    for host in hostnames:
        print(f"  {host}")
        for metric_name, nrql_template, unit in NRQL_DEFS:
            nrql = nrql_template.replace("{host}", host).replace("{days}", str(PERIOD_DAYS))
            try:
                result = _run_nrql(nrql)
            except Exception as exc:
                print(f"    WARNING: {metric_name}: {exc}", file=sys.stderr)
                continue
            if result["avg_value"] is None:
                continue
            rows.append({
                "schema_version": SCHEMA_VERSION,
                "source_tool": SOURCE_TOOL,
                "hostname": host,
                "metric_name": metric_name,
                "period_days": PERIOD_DAYS,
                "period_end_utc": period_end_utc,
                "avg_value": round(result["avg_value"], 4),
                "p95_value": round(result["p95_value"], 4) if result["p95_value"] is not None else "",
                "max_value": round(result["max_value"], 4) if result["max_value"] is not None else "",
                "unit": unit,
            })
        time.sleep(0.05)  # New Relic rate limit: 3,000 req/min

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
