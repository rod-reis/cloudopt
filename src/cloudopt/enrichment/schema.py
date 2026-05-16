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
    "os.cpu.percent":               "percent",         # legacy name (pre-SPEC §7.4)
    "os.cpu.used_percent":          "percent",         # SPEC §7.4 canonical name
    "os.memory.used_percent":       "percent",         # used RAM % (excl. OS cache)
    "os.memory.committed_bytes":    "bytes",           # virtual memory committed (legacy)
    "os.memory.available_mb":       "megabytes",       # SPEC §7.4: available RAM
    "os.memory.swap_used_percent":  "percent",         # swap / pagefile usage
    "os.disk.used_percent":         "percent",         # disk space used % (legacy)
    "os.disk.iops_read":            "iops",            # legacy name
    "os.disk.read_iops":            "iops",            # SPEC §7.4 canonical name
    "os.disk.iops_write":           "iops",            # legacy name
    "os.disk.write_iops":           "iops",            # SPEC §7.4 canonical name
    "os.disk.read_mbps":            "megabytes_per_sec",  # SPEC §7.4
    "os.disk.write_mbps":           "megabytes_per_sec",  # SPEC §7.4
    "os.disk.queue_length":         "count",           # average disk queue depth (legacy)
    "os.network.bytes_in_per_sec":  "bytes_per_sec",   # legacy name
    "os.network.receive_mbps":      "megabytes_per_sec",  # SPEC §7.4 canonical name
    "os.network.bytes_out_per_sec": "bytes_per_sec",   # legacy name
    "os.network.send_mbps":         "megabytes_per_sec",  # SPEC §7.4 canonical name
    # ── JVM ────────────────────────────────────────────────────────────────
    "jvm.heap.used_percent":        "percent",         # heap used / heap max
    "jvm.heap.used_bytes":          "bytes",           # legacy name
    "jvm.heap.used_mb":             "megabytes",       # SPEC §7.4 canonical name
    "jvm.heap.max_bytes":           "bytes",           # legacy name
    "jvm.heap.max_mb":              "megabytes",       # SPEC §7.4 canonical name
    "jvm.gc.oldgen.time_ms":        "milliseconds",    # legacy: old-gen GC pause time
    "jvm.gc.pause_ms_avg":          "milliseconds",    # SPEC §7.4 canonical name
    "jvm.gc.pause_ms_p99":          "milliseconds",    # SPEC §7.4: p99 GC pause
    "jvm.gc.oldgen.count":          "count",           # legacy: old-gen GC collections
    "jvm.threads.count":            "count",           # legacy name
    "jvm.threads.live_count":       "count",           # SPEC §7.4 canonical name
    "jvm.nonheap.used_bytes":       "bytes",           # metaspace (legacy)
    # ── .NET CLR ───────────────────────────────────────────────────────────
    "dotnet.gc.heap_bytes":         "bytes",           # legacy name
    "dotnet.gc.heap_size_mb":       "megabytes",       # SPEC §7.4 canonical name
    "dotnet.gc.gen2_collections":   "count_per_min",   # legacy name
    "dotnet.gc.pause_ms_avg":       "milliseconds",    # SPEC §7.4 canonical name
    "dotnet.threadpool.queue_length": "count",         # legacy name
    "dotnet.threadpool.queue_depth_avg": "count",      # SPEC §7.4 canonical name
    "dotnet.exceptions.per_min":    "count_per_min",   # legacy name
    "dotnet.exceptions.rate":       "count_per_min",   # SPEC §7.4 canonical name
    # ── IIS ────────────────────────────────────────────────────────────────
    "iis.requests.per_sec":         "count_per_sec",
    "iis.connections.current":      "count",
    "iis.queue.length":             "count",
    "iis.worker.restarts":          "count",
    # ── SQL Server ─────────────────────────────────────────────────────────
    "sql.buffer.page_life_expectancy": "seconds",      # target > 300 s (legacy)
    "sql.buffer.cache_hit_ratio":   "percent",         # legacy name
    "sql.cpu.used_percent":         "percent",         # SPEC §7.4
    "sql.memory.buffer_pool_hit_percent": "percent",   # SPEC §7.4
    "sql.memory.target_mb":         "megabytes",       # target server memory (legacy)
    "sql.memory.used_mb":           "megabytes",       # total server memory used (legacy)
    "sql.disk.read_iops":           "iops",            # SPEC §7.4
    "sql.disk.write_iops":          "iops",            # SPEC §7.4
    "sql.connections.active_count": "count",           # SPEC §7.4
    "sql.waits.top_wait_type":      "text",            # most common wait type (legacy)
    "sql.waits.total_wait_ms_avg":  "milliseconds",    # SPEC §7.4
    "sql.batch_requests.per_sec":   "count_per_sec",   # SPEC §7.4
    # ── PostgreSQL ─────────────────────────────────────────────────────────
    "postgres.cpu.used_percent":            "percent",
    "postgres.connections.active_count":    "count",
    "postgres.cache.hit_ratio_percent":     "percent",
    "postgres.transactions.per_sec":        "count_per_sec",
    # ── MySQL ──────────────────────────────────────────────────────────────
    "mysql.cpu.used_percent":                       "percent",
    "mysql.connections.active_count":               "count",
    "mysql.innodb.buffer_pool_hit_ratio_percent":   "percent",
    "mysql.queries.per_sec":                        "count_per_sec",
}

#: Logical groups for display / filtering.
METRIC_GROUPS: dict[str, list[str]] = {
    "os":       [m for m in CANONICAL_METRICS if m.startswith("os.")],
    "jvm":      [m for m in CANONICAL_METRICS if m.startswith("jvm.")],
    "dotnet":   [m for m in CANONICAL_METRICS if m.startswith("dotnet.")],
    "iis":      [m for m in CANONICAL_METRICS if m.startswith("iis.")],
    "sql":      [m for m in CANONICAL_METRICS if m.startswith("sql.")],
    "postgres": [m for m in CANONICAL_METRICS if m.startswith("postgres.")],
    "mysql":    [m for m in CANONICAL_METRICS if m.startswith("mysql.")],
}

#: Maps canonical metric names (both legacy and SPEC §7.4) to GuestMetricRow field names.
#: Used by the enrichment joiner to populate GuestMetricRow from MonitoringDataPoints.
CANONICAL_TO_GUEST_FIELD: dict[str, str] = {
    # OS group
    "os.cpu.percent":               "os_cpu_used_percent",   # legacy
    "os.cpu.used_percent":          "os_cpu_used_percent",
    "os.memory.used_percent":       "os_memory_used_percent",
    "os.memory.available_mb":       "os_memory_available_mb",
    "os.memory.swap_used_percent":  "os_memory_swap_used_percent",
    "os.disk.iops_read":            "os_disk_read_iops",     # legacy
    "os.disk.read_iops":            "os_disk_read_iops",
    "os.disk.iops_write":           "os_disk_write_iops",    # legacy
    "os.disk.write_iops":           "os_disk_write_iops",
    "os.disk.read_mbps":            "os_disk_read_mbps",
    "os.disk.write_mbps":           "os_disk_write_mbps",
    "os.network.bytes_in_per_sec":  "os_network_receive_mbps",  # legacy (approx)
    "os.network.receive_mbps":      "os_network_receive_mbps",
    "os.network.bytes_out_per_sec": "os_network_send_mbps",     # legacy (approx)
    "os.network.send_mbps":         "os_network_send_mbps",
    # JVM group
    "jvm.heap.used_percent":        "jvm_heap_used_percent",
    "jvm.heap.used_bytes":          "jvm_heap_used_mb",      # legacy (convert bytes→MB at join time)
    "jvm.heap.used_mb":             "jvm_heap_used_mb",
    "jvm.heap.max_bytes":           "jvm_heap_max_mb",       # legacy (convert bytes→MB at join time)
    "jvm.heap.max_mb":              "jvm_heap_max_mb",
    "jvm.gc.oldgen.time_ms":        "jvm_gc_pause_ms_avg",   # legacy (closest equivalent)
    "jvm.gc.pause_ms_avg":          "jvm_gc_pause_ms_avg",
    "jvm.gc.pause_ms_p99":          "jvm_gc_pause_ms_p99",
    "jvm.threads.count":            "jvm_threads_live_count", # legacy
    "jvm.threads.live_count":       "jvm_threads_live_count",
    # .NET group
    "dotnet.gc.heap_bytes":         "dotnet_gc_heap_size_mb",  # legacy (convert bytes→MB)
    "dotnet.gc.heap_size_mb":       "dotnet_gc_heap_size_mb",
    "dotnet.gc.pause_ms_avg":       "dotnet_gc_pause_ms_avg",
    "dotnet.threadpool.queue_length":    "dotnet_threadpool_queue_depth_avg",  # legacy
    "dotnet.threadpool.queue_depth_avg": "dotnet_threadpool_queue_depth_avg",
    "dotnet.exceptions.per_min":    "dotnet_exceptions_rate",  # legacy
    "dotnet.exceptions.rate":       "dotnet_exceptions_rate",
    # IIS group
    "iis.requests.per_sec":         "iis_requests_per_sec",
    "iis.connections.current":      "iis_connections_current",
    "iis.queue.length":             "iis_queue_length",
    "iis.worker.restarts":          "iis_worker_restarts",
    # SQL Server group
    "sql.cpu.used_percent":                 "sql_cpu_used_percent",
    "sql.memory.buffer_pool_hit_percent":   "sql_memory_buffer_pool_hit_percent",
    "sql.buffer.cache_hit_ratio":           "sql_memory_buffer_pool_hit_percent",  # legacy
    "sql.disk.read_iops":                   "sql_disk_read_iops",
    "sql.disk.write_iops":                  "sql_disk_write_iops",
    "sql.connections.active_count":         "sql_connections_active_count",
    "sql.waits.total_wait_ms_avg":          "sql_waits_total_wait_ms_avg",
    "sql.batch_requests.per_sec":           "sql_batch_requests_per_sec",
    # PostgreSQL group
    "postgres.cpu.used_percent":            "postgres_cpu_used_percent",
    "postgres.connections.active_count":    "postgres_connections_active_count",
    "postgres.cache.hit_ratio_percent":     "postgres_cache_hit_ratio_percent",
    "postgres.transactions.per_sec":        "postgres_transactions_per_sec",
    # MySQL group
    "mysql.cpu.used_percent":                       "mysql_cpu_used_percent",
    "mysql.connections.active_count":               "mysql_connections_active_count",
    "mysql.innodb.buffer_pool_hit_ratio_percent":   "mysql_innodb_buffer_pool_hit_ratio_percent",
    "mysql.queries.per_sec":                        "mysql_queries_per_sec",
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


# ---------------------------------------------------------------------------
# Guest-OS metric snapshot (SPEC §7.4 — locked 39-column set)
# ---------------------------------------------------------------------------

class GuestMetricRow(BaseModel):
    """One row of guest-OS metrics for a VM or managed-compute group.

    Fields match exactly the columns defined in SPEC §7.4, in the same order.
    A None value means the metric was not available; ``has_any_data`` is True
    when at least one field is populated.
    """
    # ── OS group (10) ───────────────────────────────────────────────────────
    os_cpu_used_percent: Optional[float] = None
    os_memory_used_percent: Optional[float] = None
    os_memory_available_mb: Optional[float] = None
    os_memory_swap_used_percent: Optional[float] = None
    os_disk_read_iops: Optional[float] = None
    os_disk_write_iops: Optional[float] = None
    os_disk_read_mbps: Optional[float] = None
    os_disk_write_mbps: Optional[float] = None
    os_network_receive_mbps: Optional[float] = None
    os_network_send_mbps: Optional[float] = None

    # ── JVM group (6) ──────────────────────────────────────────────────────
    jvm_heap_used_percent: Optional[float] = None
    jvm_heap_used_mb: Optional[float] = None
    jvm_heap_max_mb: Optional[float] = None
    jvm_gc_pause_ms_avg: Optional[float] = None
    jvm_gc_pause_ms_p99: Optional[float] = None
    jvm_threads_live_count: Optional[float] = None

    # ── .NET group (4) ─────────────────────────────────────────────────────
    dotnet_gc_heap_size_mb: Optional[float] = None
    dotnet_gc_pause_ms_avg: Optional[float] = None
    dotnet_threadpool_queue_depth_avg: Optional[float] = None
    dotnet_exceptions_rate: Optional[float] = None

    # ── IIS group (4) ──────────────────────────────────────────────────────
    iis_requests_per_sec: Optional[float] = None
    iis_connections_current: Optional[float] = None
    iis_queue_length: Optional[float] = None
    iis_worker_restarts: Optional[float] = None

    # ── SQL Server group (7) ───────────────────────────────────────────────
    sql_cpu_used_percent: Optional[float] = None
    sql_memory_buffer_pool_hit_percent: Optional[float] = None
    sql_disk_read_iops: Optional[float] = None
    sql_disk_write_iops: Optional[float] = None
    sql_connections_active_count: Optional[float] = None
    sql_waits_total_wait_ms_avg: Optional[float] = None
    sql_batch_requests_per_sec: Optional[float] = None

    # ── PostgreSQL group (4) ───────────────────────────────────────────────
    postgres_cpu_used_percent: Optional[float] = None
    postgres_connections_active_count: Optional[float] = None
    postgres_cache_hit_ratio_percent: Optional[float] = None
    postgres_transactions_per_sec: Optional[float] = None

    # ── MySQL group (4) ────────────────────────────────────────────────────
    mysql_cpu_used_percent: Optional[float] = None
    mysql_connections_active_count: Optional[float] = None
    mysql_innodb_buffer_pool_hit_ratio_percent: Optional[float] = None
    mysql_queries_per_sec: Optional[float] = None

    @property
    def has_any_data(self) -> bool:
        """True when at least one guest metric field is populated."""
        return any(
            getattr(self, f) is not None
            for f in self.model_fields
        )

    @classmethod
    def from_enriched(cls, enriched: "EnrichedVmMetrics") -> "GuestMetricRow":
        """Build a GuestMetricRow from an EnrichedVmMetrics object."""
        kwargs: dict[str, Optional[float]] = {}
        for dp in enriched.data_points:
            field_name = CANONICAL_TO_GUEST_FIELD.get(dp.metric_name)
            if field_name is None:
                continue
            # Bytes-to-megabytes conversion for legacy byte-unit fields
            value = dp.avg_value
            if value is not None and dp.unit == "bytes" and field_name.endswith("_mb"):
                value = value / (1024 * 1024)
            kwargs[field_name] = value
        return cls(**kwargs)
