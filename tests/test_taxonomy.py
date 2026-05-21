"""Registry shape tests for analyzer.taxonomy.

Verifies the invariants stated in SPEC.md §2:
- All 23 sub-codes are present.
- Codes are unique.
- Only ``swap.architecture`` (SWP-ARC-001) has finding_type=CANDIDATE.
- No ``modernize`` category exists.
"""
from __future__ import annotations

import pytest

from cloudopt.analyzer.taxonomy import (
    Category,
    FindingType,
    REGISTRY,
    REGISTRY_BY_CODE,
    RegistryEntry,
    SubCategory,
)

# ---------------------------------------------------------------------------
# Expected sub-codes from SPEC §2 (all 23)
# ---------------------------------------------------------------------------

_EXPECTED_CODES: set[str] = {
    # rightsize (5)
    "RSZ-DWN-001",
    "RSZ-UPS-001",
    "RSZ-BSF-001",
    "RSZ-BSM-001",
    "RSZ-DSK-001",
    # swap (4 recommendations + 1 candidate)
    "SWP-GEN-001",
    "SWP-FAM-001",
    "SWP-LFC-001",
    "SWP-DST-001",
    "SWP-ARC-001",
    # decom (4)
    "DCM-IDL-001",
    "DCM-STP-001",
    "DCM-DLC-001",
    "DCM-ENV-001",
    # cleanup (5)
    "CLN-DSK-001",
    "CLN-PIP-001",
    "CLN-NIC-001",
    "CLN-SNP-001",
    "CLN-RGP-001",
    # quota (5)
    "QTA-OVR-001",
    "QTA-WRN-001",
    "QTA-CRI-001",
    "QTA-CRG-001",
    "QTA-OPS-001",
    # crr (2)
    "CRR-UNU-001",
    "CRR-UNF-001",
}

_EXPECTED_SUBCODES_BY_CATEGORY: dict[str, set[str]] = {
    "rightsize": {"downsize", "upsize", "burstable-fit", "burstable-misfit", "disk-rightsize"},
    "swap": {"generation", "family", "lifecycle", "disk-tier", "architecture"},
    "decom": {"idle", "stopped-allocated", "deallocated-stale", "lower-env-overprovisioned"},
    "cleanup": {
        "unattached-disk",
        "unassociated-public-ip",
        "unattached-nic",
        "unused-snapshot",
        "empty-resource-group",
    },
    "quota": {"oversized", "warning", "critical-individual", "critical-groupable", "quota-ops-hygiene"},
}


class TestRegistryCompleteness:
    def test_registry_has_23_entries(self) -> None:
        assert len(REGISTRY) == 26

    def test_all_spec_codes_present(self) -> None:
        actual_codes = {e.code for e in REGISTRY}
        missing = _EXPECTED_CODES - actual_codes
        assert missing == set(), f"Missing codes from SPEC §2: {missing}"

    def test_no_extra_codes(self) -> None:
        actual_codes = {e.code for e in REGISTRY}
        extra = actual_codes - _EXPECTED_CODES
        assert extra == set(), f"Unexpected codes not in SPEC §2: {extra}"

    def test_codes_are_unique(self) -> None:
        codes = [e.code for e in REGISTRY]
        assert len(codes) == len(set(codes)), "Duplicate codes found in REGISTRY"

    def test_registry_by_code_covers_all_entries(self) -> None:
        assert len(REGISTRY_BY_CODE) == len(REGISTRY)
        for entry in REGISTRY:
            assert REGISTRY_BY_CODE[entry.code] is entry


class TestRegistryCandidates:
    def test_only_swap_architecture_is_candidate(self) -> None:
        candidates = [e for e in REGISTRY if e.finding_type is FindingType.CANDIDATE]
        candidate_codes = {c.code for c in candidates}
        assert "SWP-ARC-001" in candidate_codes

    def test_22_recommendations(self) -> None:
        recs = [e for e in REGISTRY if e.finding_type is FindingType.RECOMMENDATION]
        assert len(recs) == 25

    def test_candidate_is_in_swap_category(self) -> None:
        candidate = REGISTRY_BY_CODE["SWP-ARC-001"]
        assert candidate.category is Category.SWAP


class TestNoModernizeCategory:
    def test_modernize_not_in_category_enum(self) -> None:
        values = {m.value for m in Category}
        assert "modernize" not in values

    def test_no_registry_entry_uses_modernize(self) -> None:
        for entry in REGISTRY:
            assert entry.category.value != "modernize"


class TestSubcodesByCategory:
    @pytest.mark.parametrize("cat_value,expected_subs", list(_EXPECTED_SUBCODES_BY_CATEGORY.items()))
    def test_subcodes_per_category(self, cat_value: str, expected_subs: set[str]) -> None:
        category = Category(cat_value)
        actual = {e.subcategory.value for e in REGISTRY if e.category is category}
        assert actual == expected_subs, (
            f"Category '{cat_value}': sub-code mismatch. "
            f"Missing: {expected_subs - actual}, Extra: {actual - expected_subs}"
        )


class TestCodeFormat:
    """Structural invariants on code strings (format <CAT>-<SUB>-<NNN>)."""

    def test_all_codes_match_format(self) -> None:
        import re
        pattern = re.compile(r"^[A-Z]{3}-[A-Z]{3}-\d{3}$")
        for entry in REGISTRY:
            assert pattern.match(entry.code), (
                f"Code '{entry.code}' does not match <CAT>-<SUB>-<NNN> format"
            )

    def test_all_entries_have_non_empty_description(self) -> None:
        for entry in REGISTRY:
            assert entry.description.strip(), f"Empty description for {entry.code}"

    def test_registry_is_immutable_tuple(self) -> None:
        assert isinstance(REGISTRY, tuple)
        with pytest.raises((AttributeError, TypeError)):
            REGISTRY[0] = None  # type: ignore[index]

    def test_registry_entries_are_frozen(self) -> None:
        entry = REGISTRY[0]
        assert isinstance(entry, RegistryEntry)
        with pytest.raises((AttributeError, TypeError)):
            entry.code = "MUTATED"  # type: ignore[misc]
