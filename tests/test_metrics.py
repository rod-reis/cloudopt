"""Tests for metrics collection helpers: _percentile and checkpoint round-trip."""
import json
import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# _percentile
# ---------------------------------------------------------------------------

from cloudopt.collector.metrics import _percentile


class TestPercentile:
    def test_empty_returns_zero(self):
        """Empty list returns 0.0 sentinel."""
        assert _percentile([], 50) == pytest.approx(0.0)

    def test_single_value(self):
        assert _percentile([42.0], 50) == pytest.approx(42.0)
        assert _percentile([42.0], 95) == pytest.approx(42.0)

    def test_p50_of_sorted(self):
        data = [10.0, 20.0, 30.0, 40.0, 50.0]
        assert _percentile(data, 50) == pytest.approx(30.0)

    def test_p95_upper_tail(self):
        data = list(range(1, 101, 1))  # 1..100
        result = _percentile(data, 95)
        assert result == pytest.approx(95.05, abs=1.0)

    def test_p0_minimum(self):
        data = [5.0, 10.0, 15.0]
        assert _percentile(data, 0) == pytest.approx(5.0)

    def test_p100_maximum(self):
        data = [5.0, 10.0, 15.0]
        assert _percentile(data, 100) == pytest.approx(15.0)

    def test_unsorted_input(self):
        data = [50.0, 10.0, 30.0, 20.0, 40.0]
        # P50 of [10,20,30,40,50] = 30
        assert _percentile(data, 50) == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# Checkpoint save / load
# (checkpoint stores completed resource IDs as a set[str], not VmMetrics)
# ---------------------------------------------------------------------------

from cloudopt.collector.metrics import _save_checkpoint, _load_checkpoint


class TestCheckpoint:
    def test_save_and_load_roundtrip(self, tmp_path):
        ids = {
            "/subscriptions/a1b2c3d4-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-a",
            "/subscriptions/a1b2c3d4-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-b",
        }
        checkpoint_path = tmp_path / ".checkpoint.json"
        _save_checkpoint(checkpoint_path, ids)

        assert checkpoint_path.exists()
        loaded = _load_checkpoint(checkpoint_path)
        assert loaded == ids

    def test_load_nonexistent_returns_empty_set(self, tmp_path):
        result = _load_checkpoint(tmp_path / "ghost.json")
        assert result == set()

    def test_save_preserves_all_ids(self, tmp_path):
        ids = {f"/subscriptions/sub-id/rg/rg/vm/vm-{i}" for i in range(10)}
        path = tmp_path / ".ckpt.json"
        _save_checkpoint(path, ids)
        loaded = _load_checkpoint(path)
        assert loaded == ids
