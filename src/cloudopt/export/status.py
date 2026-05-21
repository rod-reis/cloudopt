"""Finding status side-car CSV manager.

Columns: finding_id,status,owner,due_date,notes,updated_utc
States: open | in_progress | done | dismissed
finding_id = f"{finding.code}:{finding.vm_id}" (deterministic, stable)
"""

from __future__ import annotations

import csv
import datetime
from pathlib import Path

_COLUMNS = ["finding_id", "status", "owner", "due_date", "notes", "updated_utc"]


def load_status(csv_path: Path) -> dict[str, dict]:
    """Load the side-car CSV into {finding_id: row_dict}.

    Returns an empty dict if the file does not exist.
    """
    if not csv_path.exists():
        return {}
    result: dict[str, dict] = {}
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            fid = row.get("finding_id", "")
            if fid:
                result[fid] = dict(row)
    return result


def save_status(csv_path: Path, status_map: dict[str, dict]) -> None:
    """Write *status_map* back to *csv_path*, creating the file if needed."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for finding_id, row in status_map.items():
            writer.writerow({
                "finding_id": finding_id,
                "status": row.get("status", "open"),
                "owner": row.get("owner", ""),
                "due_date": row.get("due_date", ""),
                "notes": row.get("notes", ""),
                "updated_utc": row.get("updated_utc", ""),
            })


def update_status(
    csv_path: Path,
    finding_id: str,
    status: str,
    owner: str = "",
    due_date: str = "",
    notes: str = "",
) -> None:
    """Update or insert the status row for *finding_id* in *csv_path*.

    Creates the file if it does not exist.  All other rows are preserved.
    """
    status_map = load_status(csv_path)
    status_map[finding_id] = {
        "finding_id": finding_id,
        "status": status,
        "owner": owner,
        "due_date": due_date,
        "notes": notes,
        "updated_utc": datetime.datetime.utcnow().isoformat(),
    }
    save_status(csv_path, status_map)


def merge_status_into_findings(
    findings: list,
    status_map: dict[str, dict],
) -> list[dict]:
    """Return a list of dicts with finding fields plus status merged in.

    The ``status`` field defaults to ``"open"`` if the finding is not present
    in *status_map*.
    """
    result = []
    for f in findings:
        finding_id = f"{f.code}:{f.vm_id}"
        st = status_map.get(finding_id, {})
        row: dict = {
            "finding_id": finding_id,
            "code": f.code,
            "category": f.category.value if hasattr(f.category, "value") else f.category,
            "subcategory": f.subcategory.value if f.subcategory and hasattr(f.subcategory, "value") else (f.subcategory or ""),
            "finding_type": f.finding_type.value if hasattr(f.finding_type, "value") else f.finding_type,
            "vm_id": f.vm_id,
            "current": f.current or "",
            "proposed": f.proposed or "",
            "deltas": str(f.deltas) if f.deltas else "",
            "confidence": f.confidence.value if f.confidence and hasattr(f.confidence, "value") else (f.confidence or ""),
            "confidence_score": f.confidence_score if f.confidence_score is not None else "",
            "readiness": f.readiness.value if hasattr(f.readiness, "value") else f.readiness,
            "rationale": (f.rationale or "")[:500],
            "blockers_to_high": "; ".join(f.blockers_to_high) if f.blockers_to_high else "",
            "evidence_sources": "; ".join(str(s) for s in f.evidence_sources) if f.evidence_sources else "",
            "status": st.get("status", "open"),
            "owner": st.get("owner", ""),
            "due_date": st.get("due_date", ""),
            "notes": st.get("notes", ""),
            "updated_utc": st.get("updated_utc", ""),
        }
        result.append(row)
    return result
