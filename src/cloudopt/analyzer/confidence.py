"""Source-aware confidence scoring for the detector pipeline (SPEC §6.3).

This module is the single source of truth for mapping monitoring-data quality
to ``Confidence`` / ``confidence_score`` / ``evidence_sources`` / ``blockers_to_high``
for every Finding emitted by the Step-2 detector pipeline.

Design rules (SPEC §6.3):
  - Authoritative signals (power state, orphaned resource, quota, reservation
    utilization) are always HIGH — they come directly from Azure ARM APIs and
    do not depend on the quality of performance monitoring data.
  - Metric-dependent signals (rightsize, SKU swap) start at MEDIUM when only
    Azure Monitor host-level ("platform") data is available, because the proxy
    memory metric (Available Memory Bytes) can be misleading.
  - OS_AWARE enrichment (Datadog / Splunk / VM Insights os.* metrics) unlocks
    HIGH for all rightsize / swap signals — we now have real in-guest memory.
  - WORKLOAD_AWARE enrichment (JVM / .NET / SQL runtime metrics) also unlocks
    HIGH and adds the workload namespace to the evidence list, which is
    especially relevant for SWP-FAM-001 (family swap based on workload type).

Numeric scoring (Phase 2, SPEC §6.3 extended):
  score = base[category/code]
        + memory_quality_bonus   (0 / +5 / +15 / +20)
        + coverage_bonus         (0 / +10 / +20 — % of lookback with data)
        + corroboration_bonus    (0–+20 — additional agreeing sources)
        + stability_bonus        (0 / +5 / +10 — CV of time-series)
        - missing_axis_penalty   (−10 per axis below 70 % coverage)
        - change_impact_penalty  (−10 if HIGH change-impact-risk)

  Band map: ≥ 80 → HIGH, 50–79 → MEDIUM, < 50 → LOW.

Public API:
  score(enriched, category, *, code, coverage_pct, stability_cv,
        corroboration_sources, high_change_impact) -> ScoredConfidence
"""

from __future__ import annotations

from typing import Optional

from cloudopt.analyzer.taxonomy import Category, Confidence
from cloudopt.enrichment.schema import EnrichedVmMetrics, MonitoringConfidence

# ---------------------------------------------------------------------------
# Categories whose signals come directly from Azure ARM / resource APIs —
# no monitoring quality upgrade possible or required.
# ---------------------------------------------------------------------------
_AUTHORITATIVE_CATEGORIES: frozenset[Category] = frozenset(
    {
        Category.CLEANUP,
        Category.QUOTA,
        Category.CRR,
    }
)

# DCM-STP-001 / DCM-DLC-001 / DCM-ENV-001 are authoritative (power state = ARM fact).
# DCM-IDL-001 is metric-dependent and gets a lower base.
_AUTHORITATIVE_DECOM_CODES: frozenset[str] = frozenset(
    {"DCM-STP-001", "DCM-DLC-001", "DCM-ENV-001"}
)


# ---------------------------------------------------------------------------
# Numeric base scores per category / code (PLAN §2.1)
# ---------------------------------------------------------------------------

def _base_score(category: Category, code: Optional[str]) -> int:
    """Return the base confidence score (0–100) for a category/code pair."""
    if category in _AUTHORITATIVE_CATEGORIES:
        return 90
    if category is Category.DECOM:
        if code == "DCM-IDL-001":
            return 70
        return 90  # DCM-STP-001, DCM-DLC-001, DCM-ENV-001
    if code == "SWP-ARC-001":
        return 40  # candidate — lower base
    return 65  # RSZ-* / SWP-* metric-dependent


def _score_to_confidence_band(numeric_score: int) -> Confidence:
    """Derive the 3-tier Confidence band from a numeric score (PLAN §2.1)."""
    if numeric_score >= 80:
        return Confidence.HIGH
    if numeric_score >= 50:
        return Confidence.MEDIUM
    return Confidence.LOW


def _compute_numeric_score(
    enriched: Optional[EnrichedVmMetrics],
    category: Category,
    code: Optional[str] = None,
    *,
    coverage_pct: Optional[float] = None,
    stability_cv: Optional[float] = None,
    corroboration_sources: int = 0,
    high_change_impact: bool = False,
) -> tuple[int, dict[str, int]]:
    """Compute the numeric confidence score and return (score, breakdown)."""
    base = _base_score(category, code)

    # --- Memory quality bonus (from enrichment tier) ---
    if enriched is None:
        mem_bonus = 0
    elif enriched.confidence_tier == MonitoringConfidence.WORKLOAD_AWARE:
        mem_bonus = 20
    elif enriched.confidence_tier == MonitoringConfidence.OS_AWARE:
        mem_bonus = 15
    elif enriched.confidence_tier == MonitoringConfidence.PLATFORM_ONLY and enriched.source_tool:
        mem_bonus = 5
    else:
        mem_bonus = 0

    # --- Coverage bonus (fraction of lookback with data) ---
    if coverage_pct is None:
        cov_bonus = 0
    elif coverage_pct >= 90.0:
        cov_bonus = 20
    elif coverage_pct >= 70.0:
        cov_bonus = 10
    else:
        cov_bonus = 0

    # --- Corroboration bonus (Advisor agrees / App Insights healthy / etc.) ---
    corr_bonus = min(20, corroboration_sources * 10)

    # --- Stability bonus (low CV = stable metric series = more trustworthy) ---
    if stability_cv is None:
        stab_bonus = 0
    elif stability_cv < 0.2:
        stab_bonus = 10
    elif stability_cv < 0.5:
        stab_bonus = 5
    else:
        stab_bonus = 0

    # --- Missing axis penalty: REMOVED (base 65 already encodes "platform-only" quality level) ---

    # --- Change-impact penalty ---
    impact_penalty = 10 if high_change_impact else 0

    raw = base + mem_bonus + cov_bonus + corr_bonus + stab_bonus - impact_penalty
    final_score = max(0, min(100, raw))

    breakdown = {
        "base": base,
        "memory_quality_bonus": mem_bonus,
        "coverage_bonus": cov_bonus,
        "corroboration_bonus": corr_bonus,
        "stability_bonus": stab_bonus,
        "missing_axis_penalty": 0,
        "change_impact_penalty": -impact_penalty,
    }
    return final_score, breakdown


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


class ScoredConfidence:
    """Immutable result of a confidence scoring operation."""

    __slots__ = (
        "confidence",
        "confidence_score",
        "score_breakdown",
        "evidence_sources",
        "blockers_to_high",
    )

    def __init__(
        self,
        confidence: Confidence,
        confidence_score: int,
        evidence_sources: list[str],
        blockers_to_high: list[str],
        score_breakdown: Optional[dict[str, int]] = None,
    ) -> None:
        self.confidence: Confidence = confidence
        self.confidence_score: int = confidence_score
        self.evidence_sources: list[str] = evidence_sources
        self.blockers_to_high: list[str] = blockers_to_high
        self.score_breakdown: dict[str, int] = score_breakdown or {}

    def to_kwargs(self) -> dict:
        """Return a dict suitable for spreading into a Finding constructor."""
        return {
            "confidence": self.confidence,
            "confidence_score": self.confidence_score,
            "evidence_sources": list(self.evidence_sources),
            "blockers_to_high": list(self.blockers_to_high),
        }


# ---------------------------------------------------------------------------
# Public scoring entry point
# ---------------------------------------------------------------------------


def score(
    enriched: Optional[EnrichedVmMetrics],
    category: Category,
    *,
    code: Optional[str] = None,
    coverage_pct: Optional[float] = None,
    stability_cv: Optional[float] = None,
    corroboration_sources: int = 0,
    high_change_impact: bool = False,
) -> ScoredConfidence:
    """Return the confidence score for one Finding.

    Args:
        enriched:               The best ``EnrichedVmMetrics`` available for the VM or
                                workload group being assessed.  Pass ``None`` when no
                                monitoring CSV was loaded, or when the VM did not match
                                any row in the monitoring export.
        category:               The ``Category`` of the Finding being scored.
        code:                   The finding code (e.g. "DCM-IDL-001") for code-level
                                base score overrides.
        coverage_pct:           Percentage of the lookback window with data (0–100).
                                Pass ``None`` when not computed (defaults to 0 bonus).
        stability_cv:           Coefficient of variation of the primary metric time-series
                                (std / mean).  Pass ``None`` when not computed.
        corroboration_sources:  Number of independent sources that agree with the finding
                                (e.g. Advisor, App Insights, second monitoring tool).
        high_change_impact:     ``True`` when the VM is an isolated (non-AvSet/VMSS)
                                production workload — applies −10 penalty.

    Returns:
        A ``ScoredConfidence`` with ``confidence``, ``confidence_score``,
        ``evidence_sources``, ``blockers_to_high``, and ``score_breakdown``.
    """
    numeric_score, breakdown = _compute_numeric_score(
        enriched, category, code,
        coverage_pct=coverage_pct,
        stability_cv=stability_cv,
        corroboration_sources=corroboration_sources,
        high_change_impact=high_change_impact,
    )
    band = _score_to_confidence_band(numeric_score)
    evidence_sources, blockers_to_high = _build_evidence(enriched, band, category, code=code)

    return ScoredConfidence(
        confidence=band,
        confidence_score=numeric_score,
        evidence_sources=evidence_sources,
        blockers_to_high=blockers_to_high,
        score_breakdown=breakdown,
    )


# ---------------------------------------------------------------------------
# Evidence / blocker builders (unchanged from original logic)
# ---------------------------------------------------------------------------


def _build_evidence(
    enriched: Optional[EnrichedVmMetrics],
    band: Confidence,
    category: Category,
    *,
    code: Optional[str] = None,
) -> tuple[list[str], list[str]]:
    """Return (evidence_sources, blockers_to_high) for the given enrichment state."""
    if category in _AUTHORITATIVE_CATEGORIES:
        return ["arm-api"], []

    # DECOM: authoritative unless the code is explicitly metric-dependent (DCM-IDL-001)
    if category is Category.DECOM and code != "DCM-IDL-001":
        return ["arm-api"], []

    if enriched is None:
        return _platform_only_evidence(source_tool=None)

    tier = enriched.confidence_tier
    source_tool = enriched.source_tool

    if tier == MonitoringConfidence.WORKLOAD_AWARE:
        return _workload_aware_evidence(source_tool, enriched)
    if tier == MonitoringConfidence.OS_AWARE:
        return _os_aware_evidence(source_tool)

    return _platform_only_evidence(source_tool=source_tool)


def _platform_only_evidence(source_tool: Optional[str]) -> tuple[list[str], list[str]]:
    sources = ["platform"]
    if source_tool and source_tool not in sources:
        sources.append(source_tool)
    blockers = [
        (
            "Supply OS-level agent metrics (os.cpu.percent / os.memory.used_percent) "
            f"via the {source_tool} canonical CSV to unlock HIGH confidence."
            if source_tool
            else
            "No monitoring export supplied. Provide a canonical CSV export from "
            "Datadog, Splunk, Dynatrace, or another supported tool to unlock HIGH confidence."
        )
    ]
    return sources, blockers


def _os_aware_evidence(source_tool: str) -> tuple[list[str], list[str]]:
    return ["platform", source_tool], []


def _workload_aware_evidence(
    source_tool: str,
    enriched: EnrichedVmMetrics,
) -> tuple[list[str], list[str]]:
    namespaces: list[str] = ["platform", source_tool]
    if enriched.has_jvm_data:
        namespaces.append("jvm")
    if enriched.has_dotnet_data:
        namespaces.append("dotnet")
    if enriched.has_sql_data:
        namespaces.append("sql")
    return namespaces, []

