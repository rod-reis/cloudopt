# Splunk Query Pack

Export VM metrics from Splunk in the canonical cloudopt CSV format.

## Prerequisites

- Splunk Enterprise or Splunk Cloud with Universal Forwarder on target VMs
- A Splunk user with `search` capability
- Python `requests` library: `pip install requests`

## Metric Mapping

| cloudopt metric               | Splunk source / field                                                |
| ----------------------------- | -------------------------------------------------------------------- |
| `os.cpu.used_percent`         | `Perfmon:CPU` index, `PercentProcessorTime`                          |
| `os.memory.used_percent`      | `Perfmon:Memory` — `(Committed Bytes / Total Physical Memory) * 100` |
| `os.memory.available_mb`      | `Perfmon:Memory` — `Available MBytes`                                |
| `os.memory.swap_used_percent` | `Perfmon:PagingFile` — `% Usage`                                     |
| `os.disk.read_iops`           | `Perfmon:LogicalDisk` — `Disk Reads/sec`                             |
| `os.disk.write_iops`          | `Perfmon:LogicalDisk` — `Disk Writes/sec`                            |
| `os.network.receive_mbps`     | `Perfmon:Network Interface` — `Bytes Received/sec` / 131072          |
| `os.network.send_mbps`        | `Perfmon:Network Interface` — `Bytes Sent/sec` / 131072              |

> For Linux hosts, data comes from the `nix` add-on (`cpu`, `vmstat`, `iostat`, `netstat`).

## Export Script

```python
#!/usr/bin/env python3
"""Export Splunk VM metrics to cloudopt canonical CSV format."""

from __future__ import annotations

import csv
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SPLUNK_HOST = os.environ.get("SPLUNK_HOST", "localhost")
SPLUNK_PORT = int(os.environ.get("SPLUNK_PORT", "8089"))
SPLUNK_USER = os.environ.get("SPLUNK_USER", "admin")
SPLUNK_PASSWORD = os.environ.get("SPLUNK_PASSWORD", "changeme")
SPLUNK_INDEX = os.environ.get("SPLUNK_INDEX", "main")
VERIFY_SSL = os.environ.get("SPLUNK_VERIFY_SSL", "true").lower() == "true"

PERIOD_DAYS = 30
OUTPUT_PATH = Path("monitoring_splunk.csv")
HOSTS_CSV = Path("cloudopt_hosts.csv")

SCHEMA_VERSION = "1.0"
SOURCE_TOOL = "splunk"

# SPL queries — {host} and {index} are substituted at runtime
# Each returns: avg_value, p95_value, max_value
SPL_QUERIES: list[tuple[str, str, str]] = [
    # (metric_name, spl_template, unit)
    (
        "os.cpu.used_percent",
        'search index={index} host={host} source="Perfmon:CPU" object="Processor" counter="% Processor Time" instance="_Total" | stats avg(Value) as avg_value, perc95(Value) as p95_value, max(Value) as max_value',
        "percent",
    ),
    (
        "os.memory.used_percent",
        'search index={index} host={host} source="Perfmon:Memory" counter="% Committed Bytes In Use" | stats avg(Value) as avg_value, perc95(Value) as p95_value, max(Value) as max_value',
        "percent",
    ),
    (
        "os.memory.available_mb",
        'search index={index} host={host} source="Perfmon:Memory" counter="Available MBytes" | stats avg(Value) as avg_value, perc95(Value) as p95_value, max(Value) as max_value',
        "MB",
    ),
    (
        "os.disk.read_iops",
        'search index={index} host={host} source="Perfmon:LogicalDisk" counter="Disk Reads/sec" instance="_Total" | stats avg(Value) as avg_value, perc95(Value) as p95_value, max(Value) as max_value',
        "iops",
    ),
    (
        "os.disk.write_iops",
        'search index={index} host={host} source="Perfmon:LogicalDisk" counter="Disk Writes/sec" instance="_Total" | stats avg(Value) as avg_value, perc95(Value) as p95_value, max(Value) as max_value',
        "iops",
    ),
]


def _load_hostnames(path: Path) -> list[str]:
    if not path.exists():
        print(f"ERROR: Host list not found: {path}", file=sys.stderr)
        sys.exit(1)
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [row["vm_name"] for row in reader if row.get("vm_name")]


def _run_spl(
    session: requests.Session,
    base_url: str,
    spl: str,
    earliest: str,
    latest: str,
) -> dict[str, float | None]:
    """Submit a blocking Splunk search and return the first result row."""
    search_url = f"{base_url}/services/search/jobs/export"
    payload = {
        "search": f"search {spl}" if not spl.startswith("search ") else spl,
        "earliest_time": earliest,
        "latest_time": latest,
        "output_mode": "json",
        "exec_mode": "blocking",
    }
    resp = session.post(search_url, data=payload, timeout=120)
    resp.raise_for_status()
    for line in resp.text.splitlines():
        try:
            import json
            obj = json.loads(line)
            if obj.get("result"):
                r = obj["result"]
                return {
                    "avg_value": float(r.get("avg_value", 0)) if r.get("avg_value") else None,
                    "p95_value": float(r.get("p95_value", 0)) if r.get("p95_value") else None,
                    "max_value": float(r.get("max_value", 0)) if r.get("max_value") else None,
                }
        except Exception:
            continue
    return {"avg_value": None, "p95_value": None, "max_value": None}


def main() -> None:
    now = datetime.now(timezone.utc)
    period_end = now.replace(minute=0, second=0, microsecond=0)
    period_start = period_end - timedelta(days=PERIOD_DAYS)
    period_end_utc = period_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    earliest = period_start.strftime("%Y-%m-%dT%H:%M:%S")
    latest = period_end.strftime("%Y-%m-%dT%H:%M:%S")

    hostnames = _load_hostnames(HOSTS_CSV)
    print(f"Exporting {len(hostnames)} host(s) over {PERIOD_DAYS} days…")

    base_url = f"https://{SPLUNK_HOST}:{SPLUNK_PORT}"
    auth = HTTPBasicAuth(SPLUNK_USER, SPLUNK_PASSWORD)
    session = requests.Session()
    session.auth = auth
    session.verify = VERIFY_SSL

    rows: list[dict] = []

    for host in hostnames:
        print(f"  {host}")
        for metric_name, spl_template, unit in SPL_QUERIES:
            spl = spl_template.replace("{host}", host).replace("{index}", SPLUNK_INDEX)
            try:
                result = _run_spl(session, base_url, spl, earliest, latest)
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
                "p95_value": round(result["p95_value"], 4) if result["p95_value"] else "",
                "max_value": round(result["max_value"], 4) if result["max_value"] else "",
                "unit": unit,
            })
        time.sleep(0.1)

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
