"""Tests for the numeric confidence scoring formula (Phase 2, PLAN §2.1).

All tests use only the public ``score()`` API + ``ScoredConfidence.score_breakdown``
dict (also public) — no private function imports, so the test works regardless
of which installed copy of the package Python resolves.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from cloudopt.analyzer.confidence import score, ScoredConfidence
from cloudopt.analyzer.taxonomy import Category, Confidence
from cloudopt.enrichment.schema import EnrichedVmMetrics, MonitoringConfidence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _enriched(tier: MonitoringConfidence, tool: str = "datadog") -> EnrichedVmMetrics:
    e = MagicMock(spec=EnrichedVmMetrics)
    e.confidence_tier = tier
    e.source_tool = tool
    e.has_jvm_data = False
    e.has_dotnet_data = False
    e.has_sql_data = False
    return e


def _bd(sc: ScoredConfidence) -> dict[str, int]:
    """Return score_breakdown dict (asserted non-None in each test that uses it)."""
    assert sc.score_breakdown, "score_breakdown must be populated"
    return sc.score_breakdown


# ---------------------------------------------------------------------------
# 1. Base score per category/code — verified via public score() API
# ---------------------------------------------------------------------------

class TestBaseScore:
    def test_cleanup_is_90(self):
        sc = score(None, Category.CLEANUP)
        assert _bd(sc)["base"] == 90

    def test_quota_is_90(self):
        sc = score(None, Category.QUOTA)
        assert _bd(sc)["base"] == 90

    def test_crr_is_90(self):
        sc = score(None, Category.CRR)
        assert _bd(sc)["base"] == 90

    def test_decom_stp_is_90(self):
        sc = score(None, Category.DECOM, code="DCM-STP-001")
        assert _bd(sc)["base"] == 90

    def test_decom_idl_is_70(self):
        sc = score(None, Category.DECOM, code="DCM-IDL-001")
        assert _bd(sc)["base"] == 70

    def test_rightsize_is_65(self):
        sc = score(None, Category.RIGHTSIZE, code="RSZ-DWN-001")
        assert _bd(sc)["base"] == 65

    def test_swap_arc_candidate_is_40(self):
        sc = score(None, Category.SWAP, code="SWP-ARC-001")
        assert _bd(sc)["base"] == 40


# ---------------------------------------------------------------------------
# 2. Band derivation from numeric score
# ---------------------------------------------------------------------------

class TestBandDerivation:
    def test_cleanup_is_high(self):
        sc = score(None, Category.CLEANUP)
        assert sc.confidence == Confidence.HIGH

    def test_rightsize_no_data_is_medium(self):
        # base=65 → MEDIUM (65 < 80)
        sc = score(None, Category.RIGHTSIZE)
        assert sc.confidence == Confidence.MEDIUM

    def test_os_aware_rightsize_is_high(self):
        # base=65 + OS_AWARE(+15) = 80 → HIGH
        e = _enriched(MonitoringConfidence.OS_AWARE)
        sc = score(e, Category.RIGHTSIZE)
        assert sc.confidence == Confidence.HIGH
        assert sc.confidence_score == 80

    def test_decom_idl_no_data_is_medium(self):
        sc = score(None, Category.DECOM, code="DCM-IDL-001")
        assert sc.confidence == Confidence.MEDIUM


# ---------------------------------------------------------------------------
# 3. Memory quality bonus
# ---------------------------------------------------------------------------

class TestMemoryQualityBonus:
    def test_no_enrichment_no_bonus(self):
        sc = score(None, Category.RIGHTSIZE)
        assert _bd(sc)["memory_quality_bonus"] == 0

    def test_platform_only_with_tool_five(self):
        e = _enriched(MonitoringConfidence.PLATFORM_ONLY)
        sc = score(e, Category.RIGHTSIZE)
        assert _bd(sc)["memory_quality_bonus"] == 5

    def test_os_aware_fifteen(self):
        e = _enriched(MonitoringConfidence.OS_AWARE)
        sc = score(e, Category.RIGHTSIZE)
        assert _bd(sc)["memory_quality_bonus"] == 15

    def test_workload_aware_twenty(self):
        e = _enriched(MonitoringConfidence.WORKLOAD_AWARE)
        sc = score(e, Category.RIGHTSIZE)
        assert _bd(sc)["memory_quality_bonus"] == 20


# ---------------------------------------------------------------------------
# 4. Coverage bonus
# ---------------------------------------------------------------------------

class TestCoverageBonus:
    def test_no_coverage_no_bonus(self):
        sc = score(None, Category.RIGHTSIZE)
        assert _bd(sc)["coverage_bonus"] == 0

    def test_low_coverage_no_bonus(self):
        sc = score(None, Category.RIGHTSIZE, coverage_pct=50.0)
        assert _bd(sc)["coverage_bonus"] == 0

    def test_mid_coverage_ten(self):
        sc = score(None, Category.RIGHTSIZE, coverage_pct=75.0)
        assert _bd(sc)["coverage_bonus"] == 10

    def test_high_coverage_twenty(self):
        sc = score(None, Category.RIGHTSIZE, coverage_pct=95.0)
        assert _bd(sc)["coverage_bonus"] == 20


# ---------------------------------------------------------------------------
# 5. Corroboration bonus
# ---------------------------------------------------------------------------

class TestCorroborationBonus:
    def test_zero_sources_no_bonus(self):
        sc = score(None, Category.RIGHTSIZE, corroboration_sources=0)
        assert _bd(sc)["corroboration_bonus"] == 0

    def test_one_source_ten(self):
        sc = score(None, Category.RIGHTSIZE, corroboration_sources=1)
        assert _bd(sc)["corroboration_bonus"] == 10

    def test_capped_at_twenty(self):
        sc = score(None, Category.RIGHTSIZE, corroboration_sources=5)
        assert _bd(sc)["corroboration_bonus"] == 20


# ---------------------------------------------------------------------------
# 6. Stability bonus
# ---------------------------------------------------------------------------

class TestStabilityBonus:
    def test_no_cv_no_bonus(self):
        sc = score(None, Category.RIGHTSIZE)
        assert _bd(sc)["stability_bonus"] == 0

    def test_stable_cv_ten(self):
        sc = score(None, Category.RIGHTSIZE, stability_cv=0.1)
        assert _bd(sc)["stability_bonus"] == 10

    def test_moderate_cv_five(self):
        sc = score(None, Category.RIGHTSIZE, stability_cv=0.4)
        assert _bd(sc)["stability_bonus"] == 5

    def test_bursty_cv_no_bonus(self):
        sc = score(None, Category.RIGHTSIZE, stability_cv=0.8)
        assert _bd(sc)["stability_bonus"] == 0


# ---------------------------------------------------------------------------
# 7. Penalties
# ---------------------------------------------------------------------------

class TestPenalties:
    def test_no_axis_penalty_for_no_enrichment(self):
        # Axis penalty removed; no-enrichment base score stays at 65
        sc = score(None, Category.RIGHTSIZE)
        assert _bd(sc)["missing_axis_penalty"] == 0

    def test_no_missing_axis_penalty_cleanup(self):
        sc = score(None, Category.CLEANUP)
        assert _bd(sc)["missing_axis_penalty"] == 0

    def test_change_impact_penalty(self):
        sc = score(None, Category.RIGHTSIZE, high_change_impact=True)
        assert _bd(sc)["change_impact_penalty"] == -10

    def test_no_change_impact_penalty_by_default(self):
        sc = score(None, Category.RIGHTSIZE)
        assert _bd(sc)["change_impact_penalty"] == 0

    def test_score_clamped_to_zero(self):
        sc = score(
            None, Category.RIGHTSIZE,
            high_change_impact=True,
            stability_cv=1.0,
            coverage_pct=0.0,
        )
        assert sc.confidence_score >= 0

    def test_score_clamped_to_hundred(self):
        e = _enriched(MonitoringConfidence.WORKLOAD_AWARE)
        sc = score(
            e, Category.CLEANUP,
            coverage_pct=100.0,
            corroboration_sources=3,
            stability_cv=0.1,
        )
        assert sc.confidence_score <= 100


# ---------------------------------------------------------------------------
# 8. End-to-end public API assertions
# ---------------------------------------------------------------------------

class TestPublicScoreAPI:
    def test_cleanup_authoritative_high_90(self):
        sc = score(None, Category.CLEANUP)
        assert sc.confidence == Confidence.HIGH
        assert sc.confidence_score == 90
        assert sc.evidence_sources == ["arm-api"]
        assert sc.blockers_to_high == []

    def test_rightsize_no_enrichment_has_blockers(self):
        sc = score(None, Category.RIGHTSIZE)
        assert "platform" in sc.evidence_sources
        assert sc.blockers_to_high  # non-empty

    def test_rightsize_os_aware_high_no_blockers(self):
        # base=65 + OS_AWARE(+15) = 80 → HIGH, no blockers (enrichment present)
        e = _enriched(MonitoringConfidence.OS_AWARE)
        sc = score(e, Category.RIGHTSIZE)
        assert sc.confidence == Confidence.HIGH
        assert sc.confidence_score == 80
        assert not sc.blockers_to_high

    def test_decom_idl_base_70_medium(self):
        sc = score(None, Category.DECOM, code="DCM-IDL-001")
        assert sc.confidence_score == 70
        assert sc.confidence == Confidence.MEDIUM

    def test_decom_stp_high_90(self):
        sc = score(None, Category.DECOM, code="DCM-STP-001")
        assert sc.confidence == Confidence.HIGH
        assert sc.confidence_score == 90

    def test_to_kwargs_includes_score(self):
        sc = score(None, Category.CLEANUP)
        kw = sc.to_kwargs()
        assert "confidence_score" in kw
        assert isinstance(kw["confidence_score"], int)

    def test_coverage_bonus_raises_score(self):
        base_sc = score(None, Category.RIGHTSIZE)
        with_cov = score(None, Category.RIGHTSIZE, coverage_pct=95.0)
        assert with_cov.confidence_score > base_sc.confidence_score

    def test_corroboration_raises_score(self):
        base_sc = score(None, Category.RIGHTSIZE)
        with_corr = score(None, Category.RIGHTSIZE, corroboration_sources=1)
        assert with_corr.confidence_score > base_sc.confidence_score

    def test_high_change_impact_lowers_score(self):
        normal = score(None, Category.RIGHTSIZE)
        risky  = score(None, Category.RIGHTSIZE, high_change_impact=True)
        assert risky.confidence_score < normal.confidence_score

    def test_confidence_score_is_int(self):
        for cat in Category:
            sc = score(None, cat)
            assert isinstance(sc.confidence_score, int), f"Failed for {cat}"
            assert 0 <= sc.confidence_score <= 100, f"Out of range for {cat}"
