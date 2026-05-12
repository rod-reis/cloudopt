# Elastic Query Pack

Export VM metrics from Elastic (Metricbeat / Elastic Agent) in the canonical cloudopt CSV format.

## Prerequisites

- Elastic Agent or Metricbeat with `system` module running on target VMs
- Elasticsearch cluster with `metricbeat-*` or `metrics-*` data streams
- Python `elasticsearch` library: `pip install elasticsearch`

```bash
export ELASTIC_URL="https://<cluster>.es.io:443"
export ELASTIC_API_KEY="<base64-api-key>"
```

## Metric Mapping

| cloudopt metric               | Elastic field                            |
| ----------------------------- | ---------------------------------------- |
| `os.cpu.used_percent`         | `system.cpu.total.pct` × 100             |
| `os.memory.used_percent`      | `system.memory.actual.used.pct` × 100    |
| `os.memory.available_mb`      | `system.memory.actual.free` / 1048576    |
| `os.memory.swap_used_percent` | `system.memory.swap.used.pct` × 100      |
| `os.disk.read_iops`           | `system.diskio.read.ops`                 |
| `os.disk.write_iops`          | `system.diskio.write.ops`                |
| `os.disk.read_mbps`           | `system.diskio.read.bytes` / 1048576     |
| `os.disk.write_mbps`          | `system.diskio.write.bytes` / 1048576    |
| `os.network.receive_mbps`     | `system.network.in.bytes` × 8 / 1000000  |
| `os.network.send_mbps`        | `system.network.out.bytes` × 8 / 1000000 |

> **Note:** Field names for Elastic Agent data streams (8.x) use ECS naming (`host.cpu.usage`
> etc.). Adjust index patterns and field paths to match your deployment.

## Export Script

```python
#!/usr/bin/env python3
"""Export Elastic VM metrics to cloudopt canonical CSV format."""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from elasticsearch import Elasticsearch

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ELASTIC_URL = os.environ["ELASTIC_URL"]
ELASTIC_API_KEY = os.environ["ELASTIC_API_KEY"]
INDEX_PATTERN = os.environ.get("ELASTIC_INDEX", "metricbeat-*,metrics-*")

PERIOD_DAYS = 30
OUTPUT_PATH = Path("monitoring_elastic.csv")
HOSTS_CSV = Path("cloudopt_hosts.csv")

SCHEMA_VERSION = "1.0"
SOURCE_TOOL = "elastic"

# (cloudopt_metric_name, es_field, scale, unit)
METRIC_DEFS: list[tuple[str, str, float, str]] = [
    ("os.cpu.used_percent",       "system.cpu.total.pct",          100.0,           "percent"),
    ("os.memory.used_percent",    "system.memory.actual.used.pct", 100.0,           "percent"),
    ("os.memory.available_mb",    "system.memory.actual.free",     1 / 1048576,     "MB"),
    ("os.memory.swap_used_percent","system.memory.swap.used.pct",  100.0,           "percent"),
    ("os.disk.read_iops",         "system.diskio.read.ops",        1.0,             "iops"),
    ("os.disk.write_iops",        "system.diskio.write.ops",       1.0,             "iops"),
    ("os.disk.read_mbps",         "system.diskio.read.bytes",      1 / 1048576,     "MBps"),
    ("os.disk.write_mbps",        "system.diskio.write.bytes",     1 / 1048576,     "MBps"),
    ("os.network.receive_mbps",   "system.network.in.bytes",       8 / 1_000_000,   "MBps"),
    ("os.network.send_mbps",      "system.network.out.bytes",      8 / 1_000_000,   "MBps"),
]


def _load_hostnames(path: Path) -> list[str]:
    if not path.exists():
        print(f"ERROR: Host list not found: {path}", file=sys.stderr)
        sys.exit(1)
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [row["vm_name"] for row in reader if row.get("vm_name")]


def _agg_query(
    es: Elasticsearch,
    host: str,
    field: str,
    scale: float,
    gte: str,
    lt: str,
) -> tuple[float | None, float | None, float | None]:
    """Run avg/percentile/max aggregations for a single field."""
    body = {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"host.name": host}},
                    {"range": {"@timestamp": {"gte": gte, "lt": lt}}},
                ]
            }
        },
        "aggs": {
            "avg_val": {"avg": {"field": field}},
            "p95_val": {"percentiles": {"field": field, "percents": [95]}},
            "max_val": {"max": {"field": field}},
        },
    }
    resp = es.search(index=INDEX_PATTERN, body=body)
    aggs = resp.get("aggregations", {})
    avg_raw = aggs.get("avg_val", {}).get("value")
    p95_raw = aggs.get("p95_val", {}).get("values", {}).get("95.0")
    max_raw = aggs.get("max_val", {}).get("value")
    if avg_raw is None:
        return None, None, None
    return avg_raw * scale, (p95_raw * scale if p95_raw else None), (max_raw * scale if max_raw else None)


def main() -> None:
    now = datetime.now(timezone.utc)
    period_end = now.replace(minute=0, second=0, microsecond=0)
    period_start = period_end - timedelta(days=PERIOD_DAYS)
    gte = period_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    lt = period_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    period_end_utc = lt

    hostnames = _load_hostnames(HOSTS_CSV)
    print(f"Exporting {len(hostnames)} host(s) over {PERIOD_DAYS} days…")

    es = Elasticsearch(ELASTIC_URL, api_key=ELASTIC_API_KEY)
    rows: list[dict] = []

    for host in hostnames:
        print(f"  {host}")
        for metric_name, field, scale, unit in METRIC_DEFS:
            try:
                avg, p95, max_val = _agg_query(es, host, field, scale, gte, lt)
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
