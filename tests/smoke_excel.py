"""Quick smoke test: verify DataValidation sqref in generated Excel is compact."""
from __future__ import annotations

import re
import sys
import tempfile
import zipfile

sys.path.insert(0, ".")
sys.path.insert(0, "src")
from tests.test_export import _make_vms, _make_metrics, _make_recs, _make_metadata
from cloudopt.export.excel import write_workbook

vms = _make_vms()
metrics = _make_metrics(vms)
recs = _make_recs(vms)
metadata = _make_metadata()

with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
    path = f.name

write_workbook(vms, metrics, recs, metadata, path)

with zipfile.ZipFile(path) as z:
    sheets = [n for n in z.namelist() if n.startswith("xl/worksheets/sheet")]
    print(f"Sheets: {len(sheets)} total")
    found = False
    for s in sheets:
        xml = z.read(s).decode()
        if "datavalidation" in xml.lower():
            found = True
            # Extract the <dataValidations> block first, then find sqref within it
            m_block = re.search(r"<dataValidations[^>]*>.*?</dataValidations>", xml, re.DOTALL | re.IGNORECASE)
            if not m_block:
                print(f"{s}: found 'dataValidation' but couldn't parse block")
                continue
            block = m_block.group(0)
            print(f"\n{s}: DataValidations block:\n{block[:400]}")
            m = re.search(r'\bsqref="([^"]+)"', block, re.IGNORECASE)
            sqref = m.group(1) if m else "(not found)"
            print(f"  -> sqref={sqref!r}")
            # Must NOT be space-separated per-cell list (the old bug)
            assert " " not in sqref, f"sqRef still per-cell: {sqref[:120]}"
            # For 1 recommendation, sqref is "K2"; for N it's "K2:K{N+1}"
            # Either way it must NOT contain spaces (the original bug)

if not found:
    print("No DataValidation sheet found (empty recommendations?)")
    sys.exit(1)

print("OK - sqref is compact")
