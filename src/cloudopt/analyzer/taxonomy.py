"""cloudopt — optimization taxonomy.

Single source of truth for finding codes, enums, and the frozen registry of
all 23 sub-codes defined in SPEC.md §2.

No detector logic lives here.  This module is intentionally import-safe:
it depends only on the standard library.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# ---------------------------------------------------------------------------
# Enums (SPEC §6.2, §3, §14)
# ---------------------------------------------------------------------------


class Category(str, Enum):
    """Top-level optimization category (SPEC §2).  No ``modernize`` member."""

    RIGHTSIZE = "rightsize"
    SWAP = "swap"
    DECOM = "decom"
    CLEANUP = "cleanup"
    QUOTA = "quota"
    RSVP = "rsvp"   # Reservations & Savings Plans (SPEC §2.6)
    CRR = "crr"     # Capacity Reservation Groups (SPEC §2.6)


class SubCategory(str, Enum):
    """Granular sub-code within a Category (SPEC §2)."""

    # --- rightsize ---
    DOWNSIZE = "downsize"
    UPSIZE = "upsize"
    BURSTABLE_FIT = "burstable-fit"
    BURSTABLE_MISFIT = "burstable-misfit"
    DISK_RIGHTSIZE = "disk-rightsize"

    # --- swap ---
    GENERATION = "generation"
    FAMILY = "family"
    LIFECYCLE = "lifecycle"
    DISK_TIER = "disk-tier"
    ARCHITECTURE = "architecture"

    # --- decom ---
    IDLE = "idle"
    STOPPED_ALLOCATED = "stopped-allocated"
    DEALLOCATED_STALE = "deallocated-stale"
    LOWER_ENV_OVERPROVISIONED = "lower-env-overprovisioned"

    # --- cleanup ---
    UNATTACHED_DISK = "unattached-disk"
    UNASSOCIATED_PUBLIC_IP = "unassociated-public-ip"
    UNATTACHED_NIC = "unattached-nic"
    UNUSED_SNAPSHOT = "unused-snapshot"
    EMPTY_RESOURCE_GROUP = "empty-resource-group"

    # --- quota ---
    QUOTA_OVERSIZED = "oversized"
    QUOTA_WARNING = "warning"
    QUOTA_CRITICAL_INDIVIDUAL = "critical-individual"
    QUOTA_CRITICAL_GROUPABLE = "critical-groupable"

    # --- rsvp (reservations / savings plans, SPEC §2.6) ---
    RSVP_UNDERUTILIZED = "rsvp-underutilized"
    RSVP_EXPIRING = "rsvp-expiring"
    RSVP_UNCOVERED_STEADY = "rsvp-uncovered-steady"

    # --- crr (capacity reservation groups, SPEC §2.6) ---
    CRR_UNUSED = "crr-unused"
    CRR_UNDERFILLED = "crr-underfilled"


class FindingType(str, Enum):
    """Whether the tool asserts an action or surfaces a possibility (SPEC §6.2)."""

    RECOMMENDATION = "recommendation"
    CANDIDATE = "candidate"


class Confidence(str, Enum):
    """Evidence-based confidence level for recommendations (SPEC §6.2).

    ``null`` for candidates — use ``Optional[Confidence]`` in models.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Readiness(str, Enum):
    """Actionability tier derived from confidence (SPEC §6.2)."""

    READY = "READY"          # HIGH confidence recommendation
    LIKELY = "LIKELY"        # MEDIUM confidence recommendation
    INSUFFICIENT = "INSUFFICIENT"  # LOW confidence recommendation
    DISCOVERY = "DISCOVERY"  # all candidates


class MetricSource(str, Enum):
    """Data source tag for a MetricSeries (SPEC §3.2)."""

    PLATFORM = "platform"
    AMA = "ama"
    VMINSIGHTS_CLASSIC = "vminsights-classic"
    CUSTOMER = "customer"


class MetricGrain(str, Enum):
    """Time granularity for a MetricSeries (SPEC §3.1)."""

    PT1H = "PT1H"
    PT5M = "PT5M"


class Pass(str, Enum):
    """Collection pass type (SPEC §14 glossary)."""

    TREND = "trend"   # PT1H / 90-day window
    PEAK = "peak"     # PT5M / 14-day window


# ---------------------------------------------------------------------------
# Registry entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistryEntry:
    """Immutable descriptor for a single finding code."""

    code: str             # stable identifier, format <CAT>-<SUB>-<NNN>
    category: Category
    subcategory: SubCategory
    finding_type: FindingType
    description: str      # one-line trigger description


# ---------------------------------------------------------------------------
# Frozen registry — 23 sub-codes, 22 recommendations + 1 candidate
# (SPEC §2)
# ---------------------------------------------------------------------------

REGISTRY: tuple[RegistryEntry, ...] = (
    # ---- rightsize (5) ---------------------------------------------------
    RegistryEntry(
        code="RSZ-DWN-001",
        category=Category.RIGHTSIZE,
        subcategory=SubCategory.DOWNSIZE,
        finding_type=FindingType.RECOMMENDATION,
        description="Sustained low utilization — fewer vCPU/RAM, same family/generation.",
    ),
    RegistryEntry(
        code="RSZ-UPS-001",
        category=Category.RIGHTSIZE,
        subcategory=SubCategory.UPSIZE,
        finding_type=FindingType.RECOMMENDATION,
        description="Sustained pressure — more vCPU/RAM, same family/generation.",
    ),
    RegistryEntry(
        code="RSZ-BSF-001",
        category=Category.RIGHTSIZE,
        subcategory=SubCategory.BURSTABLE_FIT,
        finding_type=FindingType.RECOMMENDATION,
        description=(
            "On D/E/F series, workload profile fits the B-series credit model."
        ),
    ),
    RegistryEntry(
        code="RSZ-BSM-001",
        category=Category.RIGHTSIZE,
        subcategory=SubCategory.BURSTABLE_MISFIT,
        finding_type=FindingType.RECOMMENDATION,
        description="On B-series, workload is exceeding the credit budget.",
    ),
    RegistryEntry(
        code="RSZ-DSK-001",
        category=Category.RIGHTSIZE,
        subcategory=SubCategory.DISK_RIGHTSIZE,
        finding_type=FindingType.RECOMMENDATION,
        description=(
            "Disk is over- or under-sized for actual IOPS or capacity used."
        ),
    ),
    # ---- swap (4 recommendations + 1 candidate) --------------------------
    RegistryEntry(
        code="SWP-GEN-001",
        category=Category.SWAP,
        subcategory=SubCategory.GENERATION,
        finding_type=FindingType.RECOMMENDATION,
        description=(
            "vN → vN+k same family (e.g. D8s_v3 → D8s_v6); newer CPU and"
            " accelerated networking by default."
        ),
    ),
    RegistryEntry(
        code="SWP-FAM-001",
        category=Category.SWAP,
        subcategory=SubCategory.FAMILY,
        finding_type=FindingType.RECOMMENDATION,
        description=(
            "Different SKU family better matches workload profile"
            " (e.g., Av2 → Dasv6, Dsv3 → Esv6 for memory-bound)."
        ),
    ),
    RegistryEntry(
        code="SWP-LFC-001",
        category=Category.SWAP,
        subcategory=SubCategory.LIFECYCLE,
        finding_type=FindingType.RECOMMENDATION,
        description=(
            "Current SKU is retiring or already retired — mandatory move required."
        ),
    ),
    RegistryEntry(
        code="SWP-DST-001",
        category=Category.SWAP,
        subcategory=SubCategory.DISK_TIER,
        finding_type=FindingType.RECOMMENDATION,
        description=(
            "Premium SSD → Standard SSD, or v1 → v2 disks, when usage"
            " does not justify the current tier."
        ),
    ),
    RegistryEntry(
        code="SWP-ARC-001",
        category=Category.SWAP,
        subcategory=SubCategory.ARCHITECTURE,
        finding_type=FindingType.CANDIDATE,  # flag-only, never auto-prescribed
        description=(
            "x64 → ARM64 eligibility: same shape exists on ARM64;"
            " requires customer binary-compatibility validation."
        ),
    ),
    # ---- decom (4) -------------------------------------------------------
    RegistryEntry(
        code="DCM-IDL-001",
        category=Category.DECOM,
        subcategory=SubCategory.IDLE,
        finding_type=FindingType.RECOMMENDATION,
        description=(
            "Sustained near-zero CPU, network, and disk activity over the window."
        ),
    ),
    RegistryEntry(
        code="DCM-STP-001",
        category=Category.DECOM,
        subcategory=SubCategory.STOPPED_ALLOCATED,
        finding_type=FindingType.RECOMMENDATION,
        description="VM is stopped (still billed) for more than N days.",
    ),
    RegistryEntry(
        code="DCM-DLC-001",
        category=Category.DECOM,
        subcategory=SubCategory.DEALLOCATED_STALE,
        finding_type=FindingType.RECOMMENDATION,
        description="VM has been deallocated for more than N days with no state change.",
    ),
    RegistryEntry(
        code="DCM-ENV-001",
        category=Category.DECOM,
        subcategory=SubCategory.LOWER_ENV_OVERPROVISIONED,
        finding_type=FindingType.RECOMMENDATION,
        description=(
            "Dev/test/QA tagged VM is production-sized and running 24×7."
        ),
    ),
    # ---- cleanup (5) -----------------------------------------------------
    RegistryEntry(
        code="CLN-DSK-001",
        category=Category.CLEANUP,
        subcategory=SubCategory.UNATTACHED_DISK,
        finding_type=FindingType.RECOMMENDATION,
        description="Managed disk is not attached to any VM.",
    ),
    RegistryEntry(
        code="CLN-PIP-001",
        category=Category.CLEANUP,
        subcategory=SubCategory.UNASSOCIATED_PUBLIC_IP,
        finding_type=FindingType.RECOMMENDATION,
        description="Public IP is not bound to any NIC, load balancer, or gateway.",
    ),
    RegistryEntry(
        code="CLN-NIC-001",
        category=Category.CLEANUP,
        subcategory=SubCategory.UNATTACHED_NIC,
        finding_type=FindingType.RECOMMENDATION,
        description="NIC is not attached to any VM.",
    ),
    RegistryEntry(
        code="CLN-SNP-001",
        category=Category.CLEANUP,
        subcategory=SubCategory.UNUSED_SNAPSHOT,
        finding_type=FindingType.RECOMMENDATION,
        description="Snapshot is older than N days with no recent activity.",
    ),
    RegistryEntry(
        code="CLN-RGP-001",
        category=Category.CLEANUP,
        subcategory=SubCategory.EMPTY_RESOURCE_GROUP,
        finding_type=FindingType.RECOMMENDATION,
        description="Resource group contains no resources.",
    ),
    # ---- quota (4) -------------------------------------------------------
    RegistryEntry(
        code="QTA-OVR-001",
        category=Category.QUOTA,
        subcategory=SubCategory.QUOTA_OVERSIZED,
        finding_type=FindingType.RECOMMENDATION,
        description=(
            "< 20% utilization (30-day max) AND quota exceeds the Azure default;"
            " reduction candidate."
        ),
    ),
    RegistryEntry(
        code="QTA-WRN-001",
        category=Category.QUOTA,
        subcategory=SubCategory.QUOTA_WARNING,
        finding_type=FindingType.RECOMMENDATION,
        description="Quota utilization is 70–85% of limit (30-day max).",
    ),
    RegistryEntry(
        code="QTA-CRI-001",
        category=Category.QUOTA,
        subcategory=SubCategory.QUOTA_CRITICAL_INDIVIDUAL,
        finding_type=FindingType.RECOMMENDATION,
        description=(
            "> 85% utilization — request an individual quota increase"
            " for this subscription/SKU."
        ),
    ),
    RegistryEntry(
        code="QTA-CRG-001",
        category=Category.QUOTA,
        subcategory=SubCategory.QUOTA_CRITICAL_GROUPABLE,
        finding_type=FindingType.RECOMMENDATION,
        description=(
            "> 85% utilization — eligible for quota-group consolidation"
            " across subscriptions."
        ),
    ),
    # ---- rsvp (5, SPEC §2.6) --------------------------------------------
    RegistryEntry(
        code="RSV-UND-001",
        category=Category.RSVP,
        subcategory=SubCategory.RSVP_UNDERUTILIZED,
        finding_type=FindingType.RECOMMENDATION,
        description=(
            "RI / Savings Plan utilization < 80% over last 30 days."
        ),
    ),
    RegistryEntry(
        code="RSV-EXP-001",
        category=Category.RSVP,
        subcategory=SubCategory.RSVP_EXPIRING,
        finding_type=FindingType.RECOMMENDATION,
        description=(
            "RI / Savings Plan expiring in ≤ 60 days — review renewal or"
            " scope change."
        ),
    ),
    RegistryEntry(
        code="RSV-UNC-001",
        category=Category.RSVP,
        subcategory=SubCategory.RSVP_UNCOVERED_STEADY,
        finding_type=FindingType.CANDIDATE,
        description=(
            "Steady-state VM (90-day p95 CPU > 20%, no stop events) running"
            " on-demand with no RI/SP coverage."
        ),
    ),
    RegistryEntry(
        code="CRR-UNU-001",
        category=Category.CRR,
        subcategory=SubCategory.CRR_UNUSED,
        finding_type=FindingType.RECOMMENDATION,
        description=(
            "Capacity Reservation Group with 0 associated VMs — potential"
            " capacity waste for ≥ 30 days."
        ),
    ),
    RegistryEntry(
        code="CRR-UNF-001",
        category=Category.CRR,
        subcategory=SubCategory.CRR_UNDERFILLED,
        finding_type=FindingType.RECOMMENDATION,
        description=(
            "CRG with reservedCount > usedCount — reserved capacity not"
            " fully utilised for ≥ 30 days."
        ),
    ),
)

# ---------------------------------------------------------------------------
# Fast lookup helpers (module-level constants, built once)
# ---------------------------------------------------------------------------

REGISTRY_BY_CODE: dict[str, RegistryEntry] = {e.code: e for e in REGISTRY}
"""Stable-code → RegistryEntry mapping for O(1) lookup."""
