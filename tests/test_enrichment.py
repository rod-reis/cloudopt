"""Tests for cloudopt enrichment pipeline: loader, joiner, and schema models."""

from __future__ import annotations

import csv
import io
import textwrap
from pathlib import Path

import pytest

from cloudopt.enrichment.loader import load_monitoring_csv
from cloudopt.enrichment.joiner import join_monitoring_data
from cloudopt.enrichment.schema import (
    CANONICAL_CSV_COLUMNS,
    EnrichedVmMetrics,
    EnrichmentSummary,
    MonitoringConfidence,
    MonitoringDataPoint,
)
from cloudopt.models import VmInventory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_csv_path(tmp_path: Path, rows: list[dict]) -> Path:
    """Write *rows* as a canonical CSV to *tmp_path* and return the path."""
    path = tmp_path / "monitoring.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(CANONICAL_CSV_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _base_row(**overrides) -> dict:
    """Return a valid canonical CSV row dict, with optional overrides."""
    row = {
        "schema_version": "1.0",
        "source_tool": "datadog",
        "hostname": "myvm",
        "metric_name": "os.cpu.percent",
        "period_days": "30",
        "period_end_utc": "2025-01-31T00:00:00Z",
        "avg_value": "42.5",
        "p95_value": "80.1",
        "max_value": "95.0",
        "unit": "percent",
    }
    row.update(overrides)
    return row


def _make_vm(name: str = "myvm") -> VmInventory:
    return VmInventory(
        resource_id=f"/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/{name}",
        vm_name=name,
        resource_group="rg",
        region="eastus",
        vm_sku="Standard_D2s_v3",
        vcpus=2,
        memory_gb=8.0,
        os_type="Linux",
        subscription_id="00000000-0000-0000-0000-000000000001",
        subscription_name="test-sub",
    )


# ---------------------------------------------------------------------------
# Loader tests
# ---------------------------------------------------------------------------

class TestLoadMonitoringCsv:
    def test_valid_csv_returns_data_points(self, tmp_path: Path) -> None:
        path = _make_csv_path(tmp_path, [_base_row()])
        result = load_monitoring_csv(path)
        assert len(result) == 1
        dp = result[0]
        assert dp.hostname == "myvm"
        assert dp.metric_name == "os.cpu.percent"
        assert dp.avg_value == pytest.approx(42.5)
        assert dp.p95_value == pytest.approx(80.1)
        assert dp.max_value == pytest.approx(95.0)
        assert dp.unit == "percent"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_monitoring_csv(tmp_path / "nonexistent.csv")

    def test_missing_required_column_raises_value_error(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.csv"
        path.write_text("schema_version,hostname\n1.0,myvm\n", encoding="utf-8")
        with pytest.raises(ValueError, match="missing required column"):
            load_monitoring_csv(path)

    def test_unsupported_schema_version_skips_row(self, tmp_path: Path) -> None:
        path = _make_csv_path(tmp_path, [_base_row(schema_version="9.0")])
        result = load_monitoring_csv(path)
        assert result == []

    def test_unknown_metric_name_is_skipped(self, tmp_path: Path) -> None:
        path = _make_csv_path(tmp_path, [_base_row(metric_name="nonexistent.metric")])
        result = load_monitoring_csv(path)
        assert result == []

    def test_unknown_source_tool_coerced_to_custom(self, tmp_path: Path) -> None:
        path = _make_csv_path(tmp_path, [_base_row(source_tool="my_custom_tool")])
        result = load_monitoring_csv(path)
        assert len(result) == 1
        assert result[0].source_tool == "custom"

    def test_empty_numeric_field_returns_none(self, tmp_path: Path) -> None:
        path = _make_csv_path(tmp_path, [_base_row(p95_value="", max_value="")])
        result = load_monitoring_csv(path)
        assert len(result) == 1
        assert result[0].p95_value is None
        assert result[0].max_value is None

    def test_unparseable_numeric_field_returns_none(self, tmp_path: Path) -> None:
        path = _make_csv_path(tmp_path, [_base_row(avg_value="N/A")])
        result = load_monitoring_csv(path)
        assert len(result) == 1
        assert result[0].avg_value is None

    def test_period_days_zero_skips_row(self, tmp_path: Path) -> None:
        path = _make_csv_path(tmp_path, [_base_row(period_days="0")])
        result = load_monitoring_csv(path)
        assert result == []

    def test_multiple_rows_all_loaded(self, tmp_path: Path) -> None:
        rows = [
            _base_row(hostname="vm1"),
            _base_row(hostname="vm2", metric_name="os.memory.used_percent"),
        ]
        path = _make_csv_path(tmp_path, rows)
        result = load_monitoring_csv(path)
        assert len(result) == 2

    def test_extra_columns_silently_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "extra.csv"
        cols = list(CANONICAL_CSV_COLUMNS) + ["extra_col"]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=cols)
            writer.writeheader()
            row = _base_row()
            row["extra_col"] = "ignored"
            writer.writerow(row)
        result = load_monitoring_csv(path)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Joiner tests
# ---------------------------------------------------------------------------

class TestJoinMonitoringData:
    def _dp(self, hostname: str, metric: str = "os.cpu.percent") -> MonitoringDataPoint:
        return MonitoringDataPoint(
            schema_version="1.0",
            source_tool="prometheus",
            hostname=hostname,
            metric_name=metric,
            period_days=30,
            period_end_utc="2025-01-31T00:00:00Z",
            avg_value=55.0,
            p95_value=80.0,
            max_value=99.0,
            unit="percent",
        )

    def test_exact_hostname_match(self) -> None:
        vm = _make_vm("webserver-01")
        dp = self._dp("webserver-01")
        enriched, summary = join_monitoring_data([dp], [vm])
        assert len(enriched) == 1
        assert enriched[0].vm_name == "webserver-01"
        assert summary.matched_vm_count == 1

    def test_case_insensitive_match(self) -> None:
        vm = _make_vm("WebServer-01")
        dp = self._dp("webserver-01")
        enriched, summary = join_monitoring_data([dp], [vm])
        assert len(enriched) == 1
        assert enriched[0].vm_name == "WebServer-01"

    def test_short_name_match(self) -> None:
        vm = _make_vm("webserver-01")
        dp = self._dp("webserver-01.contoso.com")
        enriched, summary = join_monitoring_data([dp], [vm])
        assert len(enriched) == 1

    def test_unmatched_hostname_not_in_enriched(self) -> None:
        vm = _make_vm("vm-exists")
        dp = self._dp("totally-different-host")
        enriched, summary = join_monitoring_data([dp], [vm])
        assert len(enriched) == 0
        assert "totally-different-host" in summary.unmatched_hostnames

    def test_unmatched_vm_listed_in_summary(self) -> None:
        vm = _make_vm("vm-no-monitoring")
        enriched, summary = join_monitoring_data([], [vm])
        assert "vm-no-monitoring" in summary.unmatched_vm_names

    def test_multiple_metrics_per_vm_collected(self) -> None:
        vm = _make_vm("server-01")
        dps = [
            self._dp("server-01", "os.cpu.percent"),
            self._dp("server-01", "os.memory.used_percent"),
        ]
        enriched, summary = join_monitoring_data(dps, [vm])
        assert len(enriched) == 1
        assert len(enriched[0].data_points) == 2

    def test_empty_inputs_return_empty(self) -> None:
        enriched, summary = join_monitoring_data([], [])
        assert enriched == []
        assert summary.matched_vm_count == 0


# ---------------------------------------------------------------------------
# EnrichedVmMetrics model tests
# ---------------------------------------------------------------------------

class TestEnrichedVmMetrics:
    def _evm(self, metrics: list[str]) -> EnrichedVmMetrics:
        evm = EnrichedVmMetrics(vm_name="vm1", hostname="vm1", source_tool="datadog")
        for m in metrics:
            evm.data_points.append(
                MonitoringDataPoint(
                    schema_version="1.0",
                    source_tool="datadog",
                    hostname="vm1",
                    metric_name=m,
                    period_days=30,
                    period_end_utc="2025-01-31T00:00:00Z",
                    avg_value=50.0,
                    p95_value=None,
                    max_value=None,
                    unit="percent",
                )
            )
        return evm

    def test_no_data_has_no_os_data(self) -> None:
        evm = self._evm([])
        assert not evm.has_os_data

    def test_os_metric_sets_has_os_data(self) -> None:
        evm = self._evm(["os.cpu.percent"])
        assert evm.has_os_data

    def test_jvm_metric_sets_has_jvm_data(self) -> None:
        evm = self._evm(["jvm.heap.used_percent"])
        assert evm.has_jvm_data

    def test_dotnet_metric_sets_has_dotnet_data(self) -> None:
        evm = self._evm(["dotnet.gc.heap_bytes"])
        assert evm.has_dotnet_data

    def test_sql_metric_sets_has_sql_data(self) -> None:
        evm = self._evm(["sql.buffer.page_life_expectancy"])
        assert evm.has_sql_data

    def test_platform_only_confidence_with_no_data(self) -> None:
        evm = self._evm([])
        assert evm.confidence_tier == MonitoringConfidence.PLATFORM_ONLY

    def test_os_aware_confidence(self) -> None:
        evm = self._evm(["os.memory.used_percent"])
        assert evm.confidence_tier == MonitoringConfidence.OS_AWARE

    def test_workload_aware_confidence_from_jvm(self) -> None:
        evm = self._evm(["os.cpu.percent", "jvm.heap.used_percent"])
        assert evm.confidence_tier == MonitoringConfidence.WORKLOAD_AWARE

    def test_workload_aware_confidence_from_dotnet(self) -> None:
        evm = self._evm(["dotnet.gc.gen2_collections"])
        assert evm.confidence_tier == MonitoringConfidence.WORKLOAD_AWARE

    def test_workload_aware_confidence_from_sql(self) -> None:
        evm = self._evm(["sql.buffer.page_life_expectancy"])
        assert evm.confidence_tier == MonitoringConfidence.WORKLOAD_AWARE

    def test_get_returns_latest_avg(self) -> None:
        evm = self._evm(["os.cpu.percent"])
        dp = evm.get("os.cpu.percent")
        assert dp is not None
        assert dp.avg_value == pytest.approx(50.0)

    def test_get_missing_metric_returns_none(self) -> None:
        evm = self._evm([])
        assert evm.get("os.cpu.percent") is None
