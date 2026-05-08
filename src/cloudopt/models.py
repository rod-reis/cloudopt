from __future__ import annotations

import re
from typing import Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

_SUB_GUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def mask_subscription_id(guid: str) -> str:
    """Return a partially masked subscription GUID (first 8 chars visible).

    Example: a1b2c3d4-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    """
    if not guid:
        return guid
    prefix = guid[:8]
    return f"{prefix}-xxxx-xxxx-xxxx-xxxxxxxxxxxx"


def mask_subscription_ids_in_string(value: str) -> str:
    """Replace all subscription GUIDs in an arbitrary string (e.g. resource IDs)."""
    return _SUB_GUID_RE.sub(lambda m: mask_subscription_id(m.group()), value)


# ---------------------------------------------------------------------------
# Configuration & Thresholds
# ---------------------------------------------------------------------------

class CollectionThresholds(BaseModel):
    underutilized_cpu_avg: float = Field(
        default=15.0,
        description="Average CPU % below which a VM is considered underutilized",
    )
    underutilized_memory_avg: float = Field(
        default=20.0,
        description="Average memory utilization % below which a VM is considered underutilized",
    )
    oversize_cpu_p95: float = Field(
        default=40.0,
        description="P95 CPU % below which a VM is considered oversized",
    )
    headroom_multiplier: float = Field(
        default=1.2,
        description="Buffer multiplier applied to P95 utilization when selecting a right-sized SKU",
    )
    paas_candidate_cpu_avg: float = Field(
        default=10.0,
        description="Average CPU % below which a low-disk-IO VM is flagged as a PaaS migration candidate",
    )
    quota_alert_pct: float = Field(
        default=80.0,
        description="Quota utilization % at or above which a quota entry is flagged as an alert",
    )


# ---------------------------------------------------------------------------
# VM Inventory
# ---------------------------------------------------------------------------

class VmInventory(BaseModel):
    # Core identifiers — stored internally as full values; masked at export time
    resource_id: str
    subscription_id: str
    subscription_name: str
    resource_group: str
    vm_name: str

    # VM specs
    vm_sku: str
    vcpus: int
    memory_gb: float
    region: str
    os_type: str
    os_version: Optional[str] = None       # from imageReference.exactVersion
    availability_zone: Optional[str] = None
    power_state: Optional[str] = None      # e.g. PowerState/running, PowerState/deallocated

    # Image reference (from storageProfile.imageReference)
    image_publisher: Optional[str] = None  # e.g. "MicrosoftWindowsServer"
    image_offer: Optional[str] = None      # e.g. "WindowsServer"
    image_sku: Optional[str] = None        # e.g. "2022-datacenter-azure-edition"
    image_version: Optional[str] = None    # resolved exact version, e.g. "20348.2402.240510"

    # Network & storage
    nic_count: int = 0
    disk_count: int = 0
    disk_sizes_gb: list[float] = Field(default_factory=list)

    # Grouping
    vmss_name: Optional[str] = None
    availability_set_name: Optional[str] = None

    # Analyst-editable annotation fields (blank by default, filled manually in Excel)
    workload: Optional[str] = None
    application: Optional[str] = None
    environment: Optional[str] = None
    criticality: Optional[str] = None
    owner: Optional[str] = None
    custom: Optional[str] = None

    # Full ``properties`` payload as returned by Azure Resource Graph for the
    # resources table.  Stored verbatim so consumers have access to every
    # field exposed by ARG, even ones we do not promote to first-class
    # columns.  Workload Owner tag values are intentionally NOT carried here.
    raw_properties: dict = Field(default_factory=dict)

    def masked_resource_id(self) -> str:
        return mask_subscription_ids_in_string(self.resource_id)

    def masked_subscription_id(self) -> str:
        return mask_subscription_id(self.subscription_id)


# ---------------------------------------------------------------------------
# VM Metrics
# ---------------------------------------------------------------------------

class DailyDataPoint(BaseModel):
    date: str  # ISO 8601 date string (YYYY-MM-DD)
    value: float


class VmMetrics(BaseModel):
    resource_id: str
    metric_name: str
    avg: Optional[float] = None
    p50: Optional[float] = None
    p95: Optional[float] = None
    p99: Optional[float] = None
    max: Optional[float] = None
    min: Optional[float] = None
    time_series: list[DailyDataPoint] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

class RecommendationCategory(str):
    """Recommendation categories.

    The CLOUDOPT model groups every finding into one of SIX top-level umbrella
    categories.  A more granular ``subcategory`` describes the specific
    signal (e.g. ``underutilized``) that fired the rule.

    Top-level umbrella (filled into ``VmRecommendation.category``):
      A. QUOTA_OPTIMIZATION       — quota tiers + cross-subscription transfer
      B. SKU_SWAP                 — same size, different family (CPU↔memory bound)
      C. RESIZING                 — same family, smaller (or larger) size
      D. RESOURCE_CLEANUP         — deallocated / idle VMs to decommission
      E. MODERNIZATION            — legacy → modern SKU, IaaS → PaaS, etc.
      F. REGION_EXPANSION         — move workloads to Non-Prod / DR / new regions

    Subcategories (filled into ``VmRecommendation.subcategory``):
      underutilized | oversized | right-size | PaaS-candidate
      decommission-candidate
      legacy-family | memory-bound | compute-bound
      quota-critical | quota-warning | quota-overprovisioned | quota-review
      cross-sub-transfer | cross-region-transfer
    """

    # Top-level umbrella categories (6 buckets)
    QUOTA_OPTIMIZATION = "Quota Optimization"
    SKU_SWAP = "SKU Swap Opportunities"
    RESIZING = "Resizing Opportunities"
    RESOURCE_CLEANUP = "Resource Cleanup and Decommissioning"
    MODERNIZATION = "Modernization Candidates"
    REGION_EXPANSION = "Region Expansion / Growth Shaping"

    # Subcategory tags (granular signal that fired the umbrella rule).
    # Kept as plain string constants so existing call sites and tests can
    # still reference Cat.UNDERUTILIZED, Cat.RIGHT_SIZE, etc.
    UNDERUTILIZED = "underutilized"
    OVERSIZED = "oversized"
    RIGHT_SIZE = "right-size"
    PAAS_CANDIDATE = "PaaS-candidate"
    LEGACY_FAMILY = "legacy-family"
    MEMORY_BOUND = "memory-bound"
    COMPUTE_BOUND = "compute-bound"
    QUOTA_CRITICAL = "quota-critical"
    QUOTA_WARNING = "quota-warning"
    QUOTA_OVERPROVISIONED = "quota-overprovisioned"
    QUOTA_REVIEW = "quota-review"
    DECOMMISSION_CANDIDATE = "decommission-candidate"
    CROSS_SUB_TRANSFER = "cross-sub-transfer"
    CROSS_REGION_TRANSFER = "cross-region-transfer"


class RecommendationPriority(str):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class OverrideStatus(str):
    ACCEPT = "accept"
    REJECT = "reject"
    DEFER = "defer"


# Default note added to every auto-generated recommendation so the
# architect/engineer always knows the row was machine-produced and needs a human pass.
ARCHITECT_REVIEW_NOTE = "Architect/Engineer to review"
CSA_REVIEW_NOTE = ARCHITECT_REVIEW_NOTE  # backward-compat alias


class VmRecommendation(BaseModel):
    """A single recommendation row.

    Recommendations are emitted at the **workload** level — when several VMs
    share a parent (VMSS, Availability Set, Databricks cluster, AVD host
    pool, …) the engine emits ONE row that targets the parent resource
    rather than N rows for the individual VMs.  ``parent_resource_id`` and
    ``member_resource_ids`` make this association explicit.

    The model intentionally covers VM right-sizing AND non-VM findings
    (quota tiers, cross-subscription / cross-region transfer suggestions)
    so the Recommendations worksheet can render them all in one table.
    """

    # Sequencing / classification
    priority: str = "medium"               # critical | high | medium | low
    recommendation: str = ""               # short human title (e.g. "Right-size VM")
    category: str = ""                     # one of the 5 umbrella categories
    subcategory: str = ""                  # granular signal (e.g. "underutilized")

    # Target — when aggregated, ``resource_id`` references the parent
    # resource (VMSS / AVSet / hostpool); otherwise it is the standalone VM.
    resource_id: str = ""
    parent_resource_id: str = ""           # same as resource_id when aggregated
    parent_resource_type: str = ""         # "Microsoft.Compute/virtualMachineScaleSets", …
    parent_resource_name: str = ""
    member_resource_ids: list[str] = Field(default_factory=list)
    member_count: int = 1                  # how many VMs are covered by this rec

    # Current → Recommended (SKU OR resource type, depending on category)
    current_sku: str = ""
    recommended_sku: Optional[str] = None
    current_resource_type: str = ""
    recommended_resource_type: str = ""

    # Explanation
    reason: str = ""
    estimated_optimization: str = ""       # free-form: "~50% vCPU reduction", "Avoid quota block", …

    # Analyst-editable
    manual_override: Optional[str] = None  # OverrideStatus value
    notes: Optional[str] = ARCHITECT_REVIEW_NOTE

    # Back-compat — older code/tests still set this directly
    estimated_savings_pct: Optional[float] = None

    def masked_resource_id(self) -> str:
        return mask_subscription_ids_in_string(self.resource_id)

    def masked_parent_resource_id(self) -> str:
        return mask_subscription_ids_in_string(self.parent_resource_id or self.resource_id)


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------

class QuotaItem(BaseModel):
    subscription_id: str
    subscription_name: str
    region: str
    resource_type: str
    display_name: str
    current_usage: int
    quota_limit: int
    utilization_pct: float  # 0–100
    alert: bool  # True if utilization_pct >= quota_alert_pct threshold

    def masked_subscription_id(self) -> str:
        return mask_subscription_id(self.subscription_id)


# ---------------------------------------------------------------------------
# Collection run metadata
# ---------------------------------------------------------------------------

class CollectionMetadata(BaseModel):
    run_date: str  # ISO 8601
    tool_version: str
    subscriptions_scanned: list[str]  # masked subscription IDs
    metrics_period_days: int
    total_vm_count: int
    total_appinsights_count: int = 0
    thresholds: CollectionThresholds


# ---------------------------------------------------------------------------
# Application Insights
# ---------------------------------------------------------------------------

class AppInsightsInventory(BaseModel):
    resource_id: str
    subscription_id: str
    subscription_name: str
    resource_group: str
    component_name: str
    kind: str = ""                          # "web", "java", "ios", "Node.JS", etc.
    application_type: str = ""             # "web", "other"
    workspace_resource_id: Optional[str] = None  # set for workspace-based (non-classic) components
    region: str
    tags: dict = Field(default_factory=dict)

    def masked_resource_id(self) -> str:
        return mask_subscription_ids_in_string(self.resource_id)

    def masked_subscription_id(self) -> str:
        return mask_subscription_id(self.subscription_id)


class AppInsightsMetrics(BaseModel):
    resource_id: str
    metric_name: str
    display_name: str
    # Grouping: "availability" | "requests" | "exceptions" | "performance"
    #           | "jvm_memory" | "jvm_gc" | "jvm_threads"
    category: str
    unit: str = ""
    avg: Optional[float] = None
    p50: Optional[float] = None
    p95: Optional[float] = None
    p99: Optional[float] = None
    max: Optional[float] = None
    min: Optional[float] = None
    time_series: list[DailyDataPoint] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Advisor (SKU-change recommendations)
# ---------------------------------------------------------------------------

class AdvisorRecommendation(BaseModel):
    """A single Azure Advisor recommendation that suggests a SKU change.

    Sourced from Resource Graph (``advisorresources``) and filtered to
    recommendations whose impact is a SKU/right-size change for compute or
    related resources.
    """

    recommendation_id: str
    subscription_id: str
    subscription_name: str
    resource_group: str = ""
    impacted_resource_id: str = ""
    impacted_resource_name: str = ""
    impacted_resource_type: str = ""
    category: str = ""        # Cost / Performance / etc.
    impact: str = ""          # High / Medium / Low
    short_description: str = ""
    current_sku: str = ""
    recommended_sku: str = ""
    annual_savings_usd: Optional[float] = None
    last_updated: str = ""

    def masked_subscription_id(self) -> str:
        return mask_subscription_id(self.subscription_id)

    def masked_impacted_resource_id(self) -> str:
        return mask_subscription_ids_in_string(self.impacted_resource_id)


# ---------------------------------------------------------------------------
# Workload Information
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Subscription Availability-Zone Mapping
# ---------------------------------------------------------------------------

class SubscriptionZoneMapping(BaseModel):
    """One row per (subscription, location, logical zone) from list_locations."""

    tenant_id: str
    subscription_id: str
    subscription_name: str
    location: str
    logical_zone: str
    physical_zone: str
    physical_zone_name: str


class WorkloadInfo(BaseModel):
    """Free-form workload context collected with the Workload Owner/SMEs.

    Rendered as a two-column table in the Excel workbook.  All fields
    default to empty strings so the worksheet ships ready to be filled in.
    """

    workload_name: str = ""
    azure_cloud: str = ""
    primary_region: str = ""
    secondary_dr_region: str = ""
    business_criticality: str = ""
    availability_dr_pattern: str = ""
    sla: str = ""
    rpo: str = ""
    rto: str = ""
    challenge_2: str = ""
    challenge_3: str = ""
