"""Round-trip and invariant tests for the Finding Pydantic model.

Covers the rules stated in SPEC.md §6.2 and §6.3:
- Finding round-trips through JSON losslessly.
- Candidate findings have confidence=None and readiness=DISCOVERY.
- Recommendation findings have a non-null confidence.
- blockers_to_high must be non-empty when confidence is not HIGH.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cloudopt.analyzer.taxonomy import (
    Category,
    Confidence,
    FindingType,
    Readiness,
    SubCategory,
)
from cloudopt.models import Finding


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_recommendation(
    *,
    confidence: Confidence = Confidence.HIGH,
    readiness: Readiness = Readiness.READY,
    blockers_to_high: list[str] | None = None,
) -> Finding:
    """Build a minimal valid recommendation Finding."""
    return Finding(
        vm_id="/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-001",
        category=Category.SWAP,
        subcategory=SubCategory.GENERATION,
        code="SWP-GEN-001",
        finding_type=FindingType.RECOMMENDATION,
        current="Standard_D8s_v3",
        proposed="Standard_D8s_v6",
        deltas={"vcpu": 0, "ram_gb": 0, "generation_gap": 3},
        evidence_sources=["platform"],
        confidence=confidence,
        readiness=readiness,
        blockers_to_high=blockers_to_high or [],
        rationale="Current SKU is 3 generations behind.",
    )


def _make_candidate() -> Finding:
    """Build a minimal valid candidate Finding."""
    return Finding(
        vm_id="/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-002",
        category=Category.SWAP,
        subcategory=SubCategory.ARCHITECTURE,
        code="SWP-ARC-001",
        finding_type=FindingType.CANDIDATE,
        current="Standard_D4s_v5",
        proposed="Standard_D4ps_v5",
        evidence_sources=["platform"],
        confidence=None,
        readiness=Readiness.DISCOVERY,
        customer_inputs_needed=["Confirm ARM64 binary compatibility"],
        rationale="ARM64 equivalent shape is available; requires customer validation.",
    )


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


class TestFindingRoundTrip:
    def test_recommendation_round_trips_json(self) -> None:
        finding = _make_recommendation()
        raw = finding.model_dump_json()
        restored = Finding.model_validate_json(raw)
        assert restored == finding

    def test_candidate_round_trips_json(self) -> None:
        finding = _make_candidate()
        raw = finding.model_dump_json()
        restored = Finding.model_validate_json(raw)
        assert restored == finding

    def test_recommendation_serialises_enum_values(self) -> None:
        finding = _make_recommendation()
        data = json.loads(finding.model_dump_json())
        assert data["category"] == "swap"
        assert data["subcategory"] == "generation"
        assert data["finding_type"] == "recommendation"
        assert data["confidence"] == "HIGH"
        assert data["readiness"] == "READY"

    def test_candidate_serialises_null_confidence(self) -> None:
        finding = _make_candidate()
        data = json.loads(finding.model_dump_json())
        assert data["confidence"] is None
        assert data["readiness"] == "DISCOVERY"
        assert data["finding_type"] == "candidate"

    def test_round_trip_preserves_deltas_dict(self) -> None:
        finding = _make_recommendation(
            confidence=Confidence.HIGH,
            readiness=Readiness.READY,
        )
        restored = Finding.model_validate(finding.model_dump())
        assert restored.deltas == {"vcpu": 0, "ram_gb": 0, "generation_gap": 3}

    def test_round_trip_preserves_evidence_sources(self) -> None:
        finding = Finding(
            vm_id="/subscriptions/00000000-0000-0000-0000-000000000003/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-003",
            category=Category.RIGHTSIZE,
            subcategory=SubCategory.DOWNSIZE,
            code="RSZ-DWN-001",
            finding_type=FindingType.RECOMMENDATION,
            evidence_sources=["platform", "ama", "customer:datadog:os"],
            confidence=Confidence.HIGH,
            readiness=Readiness.READY,
        )
        restored = Finding.model_validate_json(finding.model_dump_json())
        assert restored.evidence_sources == ["platform", "ama", "customer:datadog:os"]


# ---------------------------------------------------------------------------
# Candidate invariants (SPEC §6.2)
# ---------------------------------------------------------------------------


class TestCandidateFinding:
    def test_candidate_has_null_confidence(self) -> None:
        finding = _make_candidate()
        assert finding.confidence is None

    def test_candidate_has_discovery_readiness(self) -> None:
        finding = _make_candidate()
        assert finding.readiness is Readiness.DISCOVERY

    def test_candidate_rejects_non_null_confidence(self) -> None:
        with pytest.raises(ValidationError, match="confidence"):
            Finding(
                vm_id="/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-001",
                category=Category.SWAP,
                subcategory=SubCategory.ARCHITECTURE,
                code="SWP-ARC-001",
                finding_type=FindingType.CANDIDATE,
                confidence=Confidence.HIGH,  # must be None for candidates
                readiness=Readiness.DISCOVERY,
            )

    def test_candidate_rejects_non_discovery_readiness(self) -> None:
        with pytest.raises(ValidationError, match="readiness"):
            Finding(
                vm_id="/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-001",
                category=Category.SWAP,
                subcategory=SubCategory.ARCHITECTURE,
                code="SWP-ARC-001",
                finding_type=FindingType.CANDIDATE,
                confidence=None,
                readiness=Readiness.READY,  # must be DISCOVERY for candidates
            )


# ---------------------------------------------------------------------------
# Recommendation invariants (SPEC §6.2)
# ---------------------------------------------------------------------------


class TestRecommendationFinding:
    def test_recommendation_has_non_null_confidence(self) -> None:
        finding = _make_recommendation(confidence=Confidence.HIGH, readiness=Readiness.READY)
        assert finding.confidence is not None

    @pytest.mark.parametrize("conf", [Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW])
    def test_all_confidence_levels_accepted(self, conf: Confidence) -> None:
        blockers = ["Need guest memory data"] if conf is not Confidence.HIGH else []
        readiness = {
            Confidence.HIGH: Readiness.READY,
            Confidence.MEDIUM: Readiness.LIKELY,
            Confidence.LOW: Readiness.INSUFFICIENT,
        }[conf]
        finding = _make_recommendation(
            confidence=conf,
            readiness=readiness,
            blockers_to_high=blockers,
        )
        assert finding.confidence is conf

    def test_recommendation_rejects_null_confidence(self) -> None:
        with pytest.raises(ValidationError, match="confidence"):
            Finding(
                vm_id="/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-001",
                category=Category.SWAP,
                subcategory=SubCategory.GENERATION,
                code="SWP-GEN-001",
                finding_type=FindingType.RECOMMENDATION,
                confidence=None,  # invalid for recommendations
                readiness=Readiness.READY,
            )


# ---------------------------------------------------------------------------
# blockers_to_high enforcement (SPEC §6.3)
# ---------------------------------------------------------------------------


class TestBlockersToHigh:
    def test_high_confidence_allows_empty_blockers(self) -> None:
        finding = _make_recommendation(
            confidence=Confidence.HIGH,
            readiness=Readiness.READY,
            blockers_to_high=[],
        )
        assert finding.blockers_to_high == []

    def test_medium_confidence_requires_blockers(self) -> None:
        with pytest.raises(ValidationError, match="blockers_to_high"):
            _make_recommendation(
                confidence=Confidence.MEDIUM,
                readiness=Readiness.LIKELY,
                blockers_to_high=[],  # must be non-empty
            )

    def test_low_confidence_requires_blockers(self) -> None:
        with pytest.raises(ValidationError, match="blockers_to_high"):
            _make_recommendation(
                confidence=Confidence.LOW,
                readiness=Readiness.INSUFFICIENT,
                blockers_to_high=[],  # must be non-empty
            )

    def test_medium_confidence_with_blockers_accepted(self) -> None:
        finding = _make_recommendation(
            confidence=Confidence.MEDIUM,
            readiness=Readiness.LIKELY,
            blockers_to_high=["Provide OS-level memory data via AMA or customer CSV"],
        )
        assert finding.confidence is Confidence.MEDIUM
        assert len(finding.blockers_to_high) == 1

    def test_low_confidence_with_blockers_accepted(self) -> None:
        finding = _make_recommendation(
            confidence=Confidence.LOW,
            readiness=Readiness.INSUFFICIENT,
            blockers_to_high=[
                "Provide OS memory metrics",
                "Provide 30 days of disk IOPS data",
            ],
        )
        assert finding.confidence is Confidence.LOW
        assert len(finding.blockers_to_high) == 2
