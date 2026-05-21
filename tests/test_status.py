"""Tests for the finding status side-car CSV manager."""
import csv
from pathlib import Path

import pytest

from cloudopt.export.status import load_status, save_status, update_status, merge_status_into_findings


def test_load_status_empty(tmp_path):
    csv_path = tmp_path / "status.csv"
    # File does not exist → empty dict
    result = load_status(csv_path)
    assert result == {}


def test_save_and_load_roundtrip(tmp_path):
    csv_path = tmp_path / "status.csv"
    data = {
        "CODE-001:/subs/vm1": {
            "status": "open",
            "owner": "alice",
            "due_date": "2025-12-31",
            "notes": "test note",
            "updated_utc": "2025-01-01T00:00:00",
        }
    }
    save_status(csv_path, data)
    loaded = load_status(csv_path)
    assert "CODE-001:/subs/vm1" in loaded
    assert loaded["CODE-001:/subs/vm1"]["status"] == "open"
    assert loaded["CODE-001:/subs/vm1"]["owner"] == "alice"


def test_update_status_creates_file(tmp_path):
    csv_path = tmp_path / "new_status.csv"
    assert not csv_path.exists()
    update_status(csv_path, "CODE-002:/subs/vm2", "done", owner="bob")
    assert csv_path.exists()
    loaded = load_status(csv_path)
    assert "CODE-002:/subs/vm2" in loaded
    assert loaded["CODE-002:/subs/vm2"]["status"] == "done"
    assert loaded["CODE-002:/subs/vm2"]["owner"] == "bob"


def test_update_status_overwrites_existing(tmp_path):
    csv_path = tmp_path / "status.csv"
    update_status(csv_path, "CODE-003:/subs/vm3", "open")
    update_status(csv_path, "CODE-003:/subs/vm3", "in_progress", notes="working on it")
    loaded = load_status(csv_path)
    assert loaded["CODE-003:/subs/vm3"]["status"] == "in_progress"
    assert loaded["CODE-003:/subs/vm3"]["notes"] == "working on it"


def test_update_status_preserves_other_entries(tmp_path):
    csv_path = tmp_path / "status.csv"
    update_status(csv_path, "A:/vm1", "open")
    update_status(csv_path, "B:/vm2", "done")
    update_status(csv_path, "A:/vm1", "dismissed")
    loaded = load_status(csv_path)
    assert loaded["A:/vm1"]["status"] == "dismissed"
    assert loaded["B:/vm2"]["status"] == "done"


def test_merge_status_into_findings():
    class FakeFinding:
        code = "RSZ-DWN-001"
        vm_id = "/subscriptions/abc/vm1"
        category = type("c", (), {"value": "rightsize"})()
        subcategory = type("s", (), {"value": "downsize"})()
        finding_type = type("ft", (), {"value": "recommendation"})()
        current = "Standard_D4s_v3"
        proposed = "Standard_D2s_v3"
        deltas = {"vcpu": -2}
        confidence = type("conf", (), {"value": "HIGH"})()
        confidence_score = 85
        readiness = type("r", (), {"value": "READY"})()
        rationale = "Underutilized"
        blockers_to_high = []
        evidence_sources = []

    findings = [FakeFinding()]
    status_map = {"RSZ-DWN-001:/subscriptions/abc/vm1": {"status": "in_progress", "owner": "alice", "due_date": "", "notes": "", "updated_utc": ""}}
    result = merge_status_into_findings(findings, status_map)
    assert len(result) == 1
    assert result[0]["status"] == "in_progress"
    assert result[0]["code"] == "RSZ-DWN-001"


def test_merge_status_defaults_to_open():
    class FakeFinding:
        code = "RSZ-DWN-002"
        vm_id = "/subscriptions/xyz/vm2"
        category = type("c", (), {"value": "rightsize"})()
        subcategory = type("s", (), {"value": "downsize"})()
        finding_type = type("ft", (), {"value": "recommendation"})()
        current = "D4s_v3"
        proposed = "D2s_v3"
        deltas = {}
        confidence = type("conf", (), {"value": "MEDIUM"})()
        confidence_score = 60
        readiness = type("r", (), {"value": "LIKELY"})()
        rationale = ""
        blockers_to_high = []
        evidence_sources = []

    findings = [FakeFinding()]
    result = merge_status_into_findings(findings, {})
    assert result[0]["status"] == "open"
