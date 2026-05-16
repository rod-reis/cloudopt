"""Source-aware confidence scoring for the detector pipeline (SPEC §6.3).

This module is the single source of truth for mapping monitoring-data quality
to ``Confidence`` / ``evidence_sources`` / ``blockers_to_high`` for every
Finding emitted by the Step-2 detector pipeline.

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

Public API:
  score(enriched, category) -> ScoredConfidence
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

# DCM-STP-001 sub-signal that is authoritative (power state = ARM fact)
# The DECOM category also covers DCM-IDL-001 which IS metric-dependent,
# but that detector is deferred.  Until it lands, DECOM is fully authoritative.
_AUTHORITATIVE_CATEGORIES_DECOM: frozenset[Category] = frozenset(
    {Category.DECOM}
)


class ScoredConfidence:
    """Immutable result of a confidence scoring operation."""

    __slots__ = ("confidence", "evidence_sources", "blockers_to_high")

    def __init__(
        self,
        confidence: Confidence,
        evidence_sources: list[str],
        blockers_to_high: list[str],
    ) -> None:
        self.confidence: Confidence = confidence
        self.evidence_sources: list[str] = evidence_sources
        self.blockers_to_high: list[str] = blockers_to_high

    def to_kwargs(self) -> dict:
        """Return a dict suitable for spreading into a Finding constructor."""
        return {
            "confidence": self.confidence,
            "evidence_sources": list(self.evidence_sources),
            "blockers_to_high": list(self.blockers_to_high),
        }


def score(
    enriched: Optional[EnrichedVmMetrics],
    category: Category,
) -> ScoredConfidence:
    """Return the confidence score for one Finding.

    Args:
        enriched:   The best ``EnrichedVmMetrics`` available for the VM or
                    workload group being assessed.  Pass ``None`` when no
                    monitoring CSV was loaded, or when the VM did not match
                    any row in the monitoring export.
        category:   The ``Category`` of the Finding being scored.

    Returns:
        A ``ScoredConfidence`` with ``confidence``, ``evidence_sources``, and
        ``blockers_to_high``.
    """
    if category in _AUTHORITATIVE_CATEGORIES or category in _AUTHORITATIVE_CATEGORIES_DECOM:
        return _authoritative()

    # Metric-dependent categories: RIGHTSIZE, SWAP
    if enriched is None:
        return _platform_only_score(source_tool=None)

    tier = enriched.confidence_tier
    source_tool = enriched.source_tool

    if tier == MonitoringConfidence.WORKLOAD_AWARE:
        return _workload_aware_score(source_tool, enriched)
    if tier == MonitoringConfidence.OS_AWARE:
        return _os_aware_score(source_tool)

    # PLATFORM_ONLY tier from enriched object (os.* absent)
    return _platform_only_score(source_tool=source_tool)


# ---------------------------------------------------------------------------
# Internal scoring helpers
# ---------------------------------------------------------------------------

def _authoritative() -> ScoredConfidence:
    return ScoredConfidence(
        confidence=Confidence.HIGH,
        evidence_sources=["arm-api"],
        blockers_to_high=[],
    )


def _platform_only_score(source_tool: Optional[str]) -> ScoredConfidence:
    sources = ["platform"]
    if source_tool and source_tool not in sources:
        sources.append(source_tool)
    return ScoredConfidence(
        confidence=Confidence.MEDIUM,
        evidence_sources=sources,
        blockers_to_high=[
            "Supply OS-level agent metrics (os.cpu.percent / os.memory.used_percent) "
            f"via the {source_tool or 'monitoring export'} canonical CSV to unlock HIGH confidence."
            if source_tool
            else
            "No monitoring export supplied. Provide a canonical CSV export from "
            "Datadog, Splunk, Dynatrace, or another supported tool to unlock HIGH confidence."
        ],
    )


def _os_aware_score(source_tool: str) -> ScoredConfidence:
    return ScoredConfidence(
        confidence=Confidence.HIGH,
        evidence_sources=["platform", source_tool],
        blockers_to_high=[],
    )


def _workload_aware_score(
    source_tool: str,
    enriched: EnrichedVmMetrics,
) -> ScoredConfidence:
    namespaces: list[str] = ["platform", source_tool]
    if enriched.has_jvm_data:
        namespaces.append("jvm")
    if enriched.has_dotnet_data:
        namespaces.append("dotnet")
    if enriched.has_sql_data:
        namespaces.append("sql")
    return ScoredConfidence(
        confidence=Confidence.HIGH,
        evidence_sources=namespaces,
        blockers_to_high=[],
    )
