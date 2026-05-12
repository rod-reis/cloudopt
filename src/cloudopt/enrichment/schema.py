"""Canonical schema for 3rd-party monitoring data enrichment.

Customers export a single CSV in the canonical format defined here,
regardless of which monitoring tool they use.  cloudopt joins this
file with the Azure VM inventory to produce richer recommendations.

Canonical CSV columns (must appear in exactly this order as headers):

    schema_version, source_tool, hostname, metric_name, period_days,
    period_end_utc, avg_value, p95_value, max_value, unit

See docs/query_pack/README.md for the full metric catalog and query
packs for each supported monitoring tool.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

CANONICAL_SCHEMA_VERSION: str = "1.0"

#: When the major version (integer part before ".") changes, the loader
#: raises ValueError.  Minor version bumps add new optional columns; the
#: loader tolerates them with a warning.
SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0"})

CANONICAL_CSV_COLUMNS: tuple[str, ...] = (
    "schema_version",
    "source_tool",
    "hostname",
    "metric_name",
    "period_days",
    "period_end_utc",
    "avg_value",
    "p95_value",
    "max_value",
    "unit",
)


# ---------------------------------------------------------------------------
# Canonical metric catalog  (metric_name → expected unit)
# ---------------------------------------------------------------------------
# Extend this dict to add new metrics in future schema versions.
# All query packs MUST produce metric names from this catalog exactly.

CANONICAL_METRICS: dict[str, str] = {
    # ── OS — always available when a monitoring agent is installed ─────────
    "os.cpu.percent":               "percent",         # total CPU utilisation
    "os.memory.used_percent":       "percent",         # used RAM % (excl. OS cache)
    "os.memory.committed_bytes":    "bytes",           # virtual memory committed
    "os.memory.swap_used_percent":  "percent",         # swap / pagefile usage
    "os.disk.used_percent":         "percent",         # disk space used %
    "os.disk.iops_read":            "iops",
    "os.disk.iops_write":           "iops",
    "os.disk.queue_length":         "count",           # average disk queue depth
    "os.network.bytes_in_per_sec":  "bytes_per_sec",
    "os.network.bytes_out_per_sec": "bytes_per_sec",
    # ── JVM ────────────────────────────────────────────────────────────────
    "jvm.heap.used_percent":        "percent",         # heap used / heap max
    "jvm.heap.used_bytes":          "bytes",
    "jvm.heap.max_bytes":           "bytes",
    "jvm.gc.oldgen.time_ms":        "milliseconds",    # old-gen GC pause time
    "jvm.gc.oldgen.count":          "count",           # old-gen GC collections
    "jvm.threads.count":            "count",
    "jvm.nonheap.used_bytes":       "bytes",           # metaspace
    # ── .NET CLR ───────────────────────────────────────────────────────────
    "dotnet.gc.heap_bytes":         "bytes",
    "dotnet.gc.gen2_collections":   "count_per_min",
    "dotnet.threadpool.queue_length": "count",
    "dotnet.exceptions.per_min":    "count_per_min",
    # ── SQL Server ─────────────────────────────────────────────────────────
    "sql.buffer.page_life_expectancy": "seconds",      # target > 300 s
    "sql.buffer.cache_hit_ratio":   "percent",
    "sql.memory.target_mb":         "megabytes",       # target server memory
    "sql.memory.used_mb":           "megabytes",       # total server memory used
    "sql.waits.top_wait_type":      "text",            # most common wait type
}

#: Logical groups for display / filtering.
METRIC_GROUPS: dict[str, list[str]] = {
    "os":     [m for m in CANONICAL_METRICS if m.startswith("os.")],
    "jvm":    [m for m in CANONICAL_METRICS if m.startswith("jvm.")],
    "dotnet": [m for m in CANONICAL_METRICS if m.startswith("dotnet.")],
    "sql":    [m for m in CANONICAL_METRICS if m.startswith("sql.")],
}

KNOWN_SOURCE_TOOLS: frozenset[str] = frozenset({
    "datadog", "splunk", "dynatrace", "newrelic", "elastic", "prometheus", "custom",
})


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class MonitoringDataPoint(BaseModel):
    """One aggregated metric value for one hostname over the collection period.

    Numeric metrics: avg_value / p95_value / max_value are floats.
    Text metrics (unit == "text", e.g. sql.waits.top_wait_type): all numeric
    fields are None; the value is in text_value.
    """

    schema_version: str
    source_tool: str
    hostname: str
    metric_name: str
    period_days: int
    period_end_utc: str          # ISO 8601 UTC datetime
    avg_value: Optional[float] = None
    p95_value: Optional[float] = None
    max_value: Optional[float] = None
    unit: str
    text_value: Optional[str] = None   # populated when unit == "text"


class EnrichedVmMetrics(BaseModel):
    """All monitoring data points matched to a single Azure VM."""

    vm_name: str
    hostname: str
    source_tool: str
    data_points: list[MonitoringDataPoint] = Field(default_factory=list)

    def get(self, metric_name: str) -> Optional[MonitoringDataPoint]:
        """Return the data point for *metric_name*, or None if absent."""
        for dp in self.data_points:
            if dp.metric_name == metric_name:
                return dp
        return None

    @property
    def has_os_data(self) -> bool:
        return any(dp.metric_name.startswith("os.") for dp in self.data_points)

    @property
    def has_jvm_data(self) -> bool:
        return any(dp.metric_name.startswith("jvm.") for dp in self.data_points)

    @property
    def has_dotnet_data(self) -> bool:
        return any(dp.metric_name.startswith("dotnet.") for dp in self.data_points)

    @property
    def has_sql_data(self) -> bool:
        return any(dp.metric_name.startswith("sql.") for dp in self.data_points)

    @property
    def confidence_tier(self) -> str:
        """Return the monitoring confidence tier for this VM's enrichment data."""
        if self.has_jvm_data or self.has_dotnet_data or self.has_sql_data:
            return MonitoringConfidence.WORKLOAD_AWARE
        if self.has_os_data:
            return MonitoringConfidence.OS_AWARE
        return MonitoringConfidence.PLATFORM_ONLY


class EnrichmentSummary(BaseModel):
    """Statistics from a monitoring data join operation."""

    source_tools: list[str]
    total_hostnames_in_export: int
    matched_vm_count: int
    unmatched_hostnames: list[str]    # in the CSV but not found in Azure inventory
    unmatched_vm_names: list[str]     # in Azure inventory but not in the CSV
    schema_version: str
    metrics_present: list[str]        # sorted list of distinct metric names found


# ---------------------------------------------------------------------------
# Confidence constants
# ---------------------------------------------------------------------------

class MonitoringConfidence:
    """String constants for recommendation data-source confidence tiers."""

    PLATFORM_ONLY  = "platform-only"    # Azure Monitor host-level metrics only
    OS_AWARE       = "os-aware"         # OS-level agent data (memory, swap, etc.)
    WORKLOAD_AWARE = "workload-aware"   # JVM / .NET / SQL runtime metrics present
