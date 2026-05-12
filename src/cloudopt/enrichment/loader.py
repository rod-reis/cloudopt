"""Load and validate the canonical monitoring CSV export.

The CSV must contain exactly the columns defined in CANONICAL_CSV_COLUMNS
(case-sensitive header names, any column order).  Extra columns are silently
ignored to support forward-compatibility.

Schema-version handling
-----------------------
- schema_version field per row must be in SUPPORTED_SCHEMA_VERSIONS.
- A future minor-version bump (e.g. 1.0 → 1.1) adds optional columns;
  this loader tolerates missing optional columns with a warning.
- A major-version bump (e.g. 1.x → 2.0) requires a loader update;
  rows with a new major version are skipped with a clear error message.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

from rich.console import Console

from cloudopt.enrichment.schema import (
    CANONICAL_CSV_COLUMNS,
    CANONICAL_METRICS,
    KNOWN_SOURCE_TOOLS,
    SUPPORTED_SCHEMA_VERSIONS,
    MonitoringDataPoint,
)

_console = Console()


def load_monitoring_csv(path: Path) -> list[MonitoringDataPoint]:
    """Parse *path* as a canonical monitoring CSV and return validated rows.

    Validation applied per-row:
    - schema_version must be in SUPPORTED_SCHEMA_VERSIONS (skip + warn if not)
    - source_tool must be in KNOWN_SOURCE_TOOLS (warn + coerce to "custom")
    - hostname must be non-empty (skip)
    - metric_name must be in CANONICAL_METRICS (skip + warn)
    - period_days must be a positive integer (skip + warn)
    - Numeric fields cast to float; empty string → None; unparseable → warn + None

    Raises:
        FileNotFoundError: path does not exist
        ValueError: required columns are missing from the CSV header
    """
    if not path.exists():
        raise FileNotFoundError(f"Monitoring export not found: {path}")

    raw_rows = _read_csv(path)
    if not raw_rows:
        _console.print("[yellow]Warning:[/yellow] Monitoring CSV is empty.")
        return []

    _validate_columns(set(raw_rows[0].keys()), path)

    data_points: list[MonitoringDataPoint] = []
    skipped = 0

    for lineno, row in enumerate(raw_rows, start=2):
        dp = _parse_row(row, lineno)
        if dp is not None:
            data_points.append(dp)
        else:
            skipped += 1

    if skipped:
        _console.print(
            f"[yellow]Warning:[/yellow] {skipped} row(s) skipped due to "
            "validation errors (see messages above)."
        )

    _console.print(
        f"  [green]✓[/green] Loaded {len(data_points)} monitoring data point(s) "
        f"covering {_distinct_hostnames(data_points)} host(s) from {path.name}."
    )
    return data_points


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return [dict(row) for row in reader]


def _validate_columns(present: set[str], path: Path) -> None:
    required = set(CANONICAL_CSV_COLUMNS)
    missing = required - present
    if missing:
        raise ValueError(
            f"{path.name} is missing required column(s): "
            f"{', '.join(sorted(missing))}.\n"
            f"Expected: {', '.join(CANONICAL_CSV_COLUMNS)}\n"
            "See docs/query_pack/README.md for the canonical format."
        )


def _parse_row(row: dict[str, str], lineno: int) -> Optional[MonitoringDataPoint]:
    schema_ver = row["schema_version"].strip()
    _major = schema_ver.split(".")[0]
    supported_majors = {v.split(".")[0] for v in SUPPORTED_SCHEMA_VERSIONS}
    if _major not in supported_majors:
        _console.print(
            f"[yellow]Warning:[/yellow] Line {lineno}: unsupported schema_version "
            f"'{schema_ver}' (major version '{_major}' not in "
            f"{sorted(supported_majors)}). Row skipped."
        )
        return None

    source_tool = row["source_tool"].strip().lower()
    if source_tool not in KNOWN_SOURCE_TOOLS:
        _console.print(
            f"[yellow]Warning:[/yellow] Line {lineno}: unknown source_tool "
            f"'{source_tool}' — treated as 'custom'."
        )
        source_tool = "custom"

    hostname = row["hostname"].strip()
    if not hostname:
        _console.print(f"[yellow]Warning:[/yellow] Line {lineno}: empty hostname. Row skipped.")
        return None

    metric_name = row["metric_name"].strip()
    if metric_name not in CANONICAL_METRICS:
        _console.print(
            f"[yellow]Warning:[/yellow] Line {lineno}: unknown metric_name "
            f"'{metric_name}'. Row skipped. "
            "See docs/query_pack/README.md for the metric catalog."
        )
        return None

    try:
        period_days = int(row["period_days"].strip())
        if period_days <= 0:
            raise ValueError("period_days must be > 0")
    except ValueError:
        _console.print(
            f"[yellow]Warning:[/yellow] Line {lineno}: invalid period_days "
            f"'{row['period_days']}'. Row skipped."
        )
        return None

    expected_unit = CANONICAL_METRICS[metric_name]
    avg_value = _parse_float(row.get("avg_value", ""), lineno, "avg_value")
    p95_value = _parse_float(row.get("p95_value", ""), lineno, "p95_value")
    max_value = _parse_float(row.get("max_value", ""), lineno, "max_value")

    # Text metrics: the string value lives in avg_value column; numerics are N/A
    text_value: Optional[str] = None
    if expected_unit == "text":
        text_value = row.get("avg_value", "").strip() or None
        avg_value = p95_value = max_value = None

    return MonitoringDataPoint(
        schema_version=schema_ver,
        source_tool=source_tool,
        hostname=hostname,
        metric_name=metric_name,
        period_days=period_days,
        period_end_utc=row["period_end_utc"].strip(),
        avg_value=avg_value,
        p95_value=p95_value,
        max_value=max_value,
        unit=expected_unit,
        text_value=text_value,
    )


def _parse_float(raw: str, lineno: int, field: str) -> Optional[float]:
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        return float(stripped)
    except ValueError:
        _console.print(
            f"[yellow]Warning:[/yellow] Line {lineno}: cannot parse "
            f"{field}='{stripped}' as float — treated as missing."
        )
        return None


def _distinct_hostnames(data_points: list[MonitoringDataPoint]) -> int:
    return len({dp.hostname for dp in data_points})
