"""Tests for the extended recommendations engine.

Covers:
  * Quota tier mapping (15 / 25 / 75 / 85)
  * Cross-subscription SKU-transfer suggestions
  * Legacy / previous-generation SKU filter
  * Priority + architect-review note + new column fields on every auto rec
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cloudopt.analyzer.recommendations import (
    QUOTA_CRITICAL_PCT,
    QUOTA_OVERPROVISIONED_PCT,
    QUOTA_REVIEW_PCT,
    QUOTA_WARNING_PCT,
    _is_legacy_sku,
    generate_cross_subscription_transfer_recommendations,
    generate_quota_recommendations,
    generate_recommendations,
    sort_recommendations,
)
from cloudopt.analyzer.sku_catalog import SkuCatalog, SkuSpec
from cloudopt.models import (
    ARCHITECT_REVIEW_NOTE,
    CSA_REVIEW_NOTE,  # backward-compat alias
    CollectionThresholds,
    QuotaItem,
    RecommendationCategory as Cat,
    RecommendationPriority as Pri,
    VmInventory,
    VmMetrics,
    VmRecommendation,
)


# ---------------------------------------------------------------------------
# Quota tier recommendations
# ---------------------------------------------------------------------------

def _quota(util_pct: float, *, sub="Sub-A", sub_id="aaaa-1111-1111-1111-111111111111",
           rt="standardDSv5Family", region="eastus", limit=100) -> QuotaItem:
    used = int(round(limit * util_pct / 100))
    return QuotaItem(
        subscription_id=sub_id,
        subscription_name=sub,
        region=region,
        resource_type=rt,
        display_name="Standard DSv5 Family vCPUs",
        current_usage=used,
        quota_limit=limit,
        utilization_pct=util_pct,
        alert=util_pct >= 80,
    )


class TestQuotaRecommendations:
    def test_critical_tier_emits_critical_priority(self):
        recs = generate_quota_recommendations([_quota(90)])
        assert len(recs) == 1
        r = recs[0]
        assert r.priority == Pri.CRITICAL
        assert r.category == Cat.QUOTA_OPTIMIZATION
        assert r.subcategory == Cat.QUOTA_CRITICAL
        assert r.notes == CSA_REVIEW_NOTE
        assert "90.0%" in r.reason

    def test_warning_tier_emits_high_priority(self):
        recs = generate_quota_recommendations([_quota(80)])
        assert len(recs) == 1
        assert recs[0].priority == Pri.HIGH
        assert recs[0].category == Cat.QUOTA_OPTIMIZATION
        assert recs[0].subcategory == Cat.QUOTA_WARNING

    def test_overprovisioned_tier_emits_high_priority(self):
        # Over-provisioned recs only fire when another subscription on the
        # same SKU is starved (>= warning) \u2014 otherwise nobody benefits.
        donor = _quota(10, sub="Donor", sub_id="dddd-1111-1111-1111-111111111111")
        receiver = _quota(85, sub="Receiver", sub_id="rrrr-1111-1111-1111-111111111111")
        recs = generate_quota_recommendations([donor, receiver])
        # Critical for receiver + over-provisioned for donor.
        subs = [(r.subcategory, r.priority) for r in recs]
        assert (Cat.QUOTA_OVERPROVISIONED, Pri.HIGH) in subs

    def test_overprovisioned_without_receiver_is_suppressed(self):
        # No subscription needs more quota \u2014 do not recommend trimming.
        recs = generate_quota_recommendations([_quota(10)])
        assert recs == []

    def test_review_tier_emits_medium_priority(self):
        donor = _quota(20, sub="Donor", sub_id="dddd-1111-1111-1111-111111111111")
        receiver = _quota(85, sub="Receiver", sub_id="rrrr-1111-1111-1111-111111111111")
        recs = generate_quota_recommendations([donor, receiver])
        subs = [(r.subcategory, r.priority) for r in recs]
        assert (Cat.QUOTA_REVIEW, Pri.MEDIUM) in subs

    def test_review_without_receiver_is_suppressed(self):
        recs = generate_quota_recommendations([_quota(20)])
        assert recs == []

    def test_healthy_band_emits_nothing(self):
        # Anything strictly between 25 and 75 is healthy.
        for pct in (26, 50, 74):
            assert generate_quota_recommendations([_quota(pct)]) == []

    def test_zero_limit_is_skipped(self):
        item = QuotaItem(
            subscription_id="x", subscription_name="X", region="eastus",
            resource_type="rt", display_name="dn",
            current_usage=0, quota_limit=0, utilization_pct=0.0, alert=False,
        )
        assert generate_quota_recommendations([item]) == []

    def test_threshold_boundaries(self):
        assert QUOTA_CRITICAL_PCT == 85.0
        assert QUOTA_WARNING_PCT == 75.0
        assert QUOTA_OVERPROVISIONED_PCT == 15.0
        assert QUOTA_REVIEW_PCT == 25.0
        # Exactly on boundary \u2192 respective tier (critical/warning fire alone;
        # over-provisioned/review need a matching receiver).
        assert generate_quota_recommendations([_quota(85)])[0].priority == Pri.CRITICAL
        assert generate_quota_recommendations([_quota(75)])[0].priority == Pri.HIGH
        donor15 = _quota(15, sub="Donor", sub_id="dddd-1111-1111-1111-111111111111")
        recv85 = _quota(85, sub="Recv", sub_id="rrrr-1111-1111-1111-111111111111")
        recs15 = generate_quota_recommendations([donor15, recv85])
        assert any(r.subcategory == Cat.QUOTA_OVERPROVISIONED for r in recs15)
        donor25 = _quota(25, sub="Donor", sub_id="dddd-1111-1111-1111-111111111111")
        recs25 = generate_quota_recommendations([donor25, recv85])
        assert any(r.subcategory == Cat.QUOTA_REVIEW for r in recs25)


# ---------------------------------------------------------------------------
# Cross-subscription SKU-transfer
# ---------------------------------------------------------------------------

class TestCrossSubscriptionTransfer:
    def test_emits_when_donor_and_receiver_exist(self):
        donor = _quota(20, sub="Donor", sub_id="dddd-1111-1111-1111-111111111111")
        receiver = _quota(85, sub="Receiver", sub_id="rrrr-1111-1111-1111-111111111111")
        recs = generate_cross_subscription_transfer_recommendations([donor, receiver])
        assert len(recs) == 1
        rec = recs[0]
        assert rec.priority == Pri.HIGH
        assert rec.category == Cat.REGION_EXPANSION
        assert rec.subcategory == Cat.CROSS_SUB_TRANSFER
        assert "Donor" in rec.reason
        assert "Receiver" in rec.reason
        assert rec.notes == CSA_REVIEW_NOTE

    def test_does_not_recommend_self_transfer(self):
        # Same sub appearing as both donor and receiver is impossible — but
        # if it did, we must not recommend moving to itself.
        same_sub_id = "aaaa-1111-1111-1111-111111111111"
        loaded = _quota(85, sub="Same", sub_id=same_sub_id)
        empty = _quota(20, sub="Same", sub_id=same_sub_id)
        recs = generate_cross_subscription_transfer_recommendations([loaded, empty])
        assert recs == []

    def test_no_donor_means_no_recommendation(self):
        a = _quota(80, sub="A", sub_id="aaaa-1111-1111-1111-111111111111")
        b = _quota(85, sub="B", sub_id="bbbb-1111-1111-1111-111111111111")
        recs = generate_cross_subscription_transfer_recommendations([a, b])
        assert recs == []

    def test_different_regions_emit_cross_region_expansion(self):
        # When the receiver has no same-region donor, surface cross-region donors
        # under the REGION_EXPANSION umbrella with subcategory=cross-region-transfer.
        donor = _quota(20, sub="Donor", sub_id="dddd-1111-1111-1111-111111111111", region="eastus")
        receiver = _quota(85, sub="Receiver", sub_id="rrrr-1111-1111-1111-111111111111", region="westeurope")
        recs = generate_cross_subscription_transfer_recommendations([donor, receiver])
        assert len(recs) == 1
        assert recs[0].category == Cat.REGION_EXPANSION
        assert recs[0].subcategory == Cat.CROSS_REGION_TRANSFER

    def test_different_resource_types_do_not_pair(self):
        donor = _quota(20, sub="Donor", sub_id="dddd-1111-1111-1111-111111111111", rt="standardDSv5Family")
        receiver = _quota(85, sub="Receiver", sub_id="rrrr-1111-1111-1111-111111111111", rt="standardESv5Family")
        assert generate_cross_subscription_transfer_recommendations([donor, receiver]) == []


# ---------------------------------------------------------------------------
# Legacy / previous-generation SKU filter
# ---------------------------------------------------------------------------

class TestLegacySkuFilter:
    @pytest.mark.parametrize("sku", [
        "Standard_D4_v3", "Standard_D4s_v3", "Standard_D2s_v2", "Standard_D1_v1",
        "Standard_A4_v2", "Standard_B2s_v2", "Standard_DC4s_v2",
        "Standard_E4s_v2", "Standard_F8s_v2",
        "Standard_A1", "Standard_D2", "Standard_F4",
    ])
    def test_legacy_skus_detected(self, sku):
        assert _is_legacy_sku(sku), f"{sku} should be flagged legacy"

    @pytest.mark.parametrize("sku", [
        "Standard_D4s_v4", "Standard_D8s_v5", "Standard_D2as_v5",
        "Standard_E4s_v4", "Standard_E8s_v5",
        "Standard_F8s_v3",  # F-series v3 still current
        "Standard_B2s",     # B-series unversioned is current
    ])
    def test_current_skus_not_flagged(self, sku):
        assert not _is_legacy_sku(sku), f"{sku} should NOT be flagged legacy"

    def test_recommendation_engine_drops_legacy_recommended_sku(self):
        # Build a VM whose right-size candidate is a v3 D-series.
        vm = VmInventory(
            vm_name="vm", subscription_id="aaaa", subscription_name="S",
            resource_group="rg",
            resource_id="/subscriptions/aaaa/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm",
            vm_sku="Standard_D8s_v5", vcpus=8, memory_gb=32.0,
            region="eastus", os_type="Linux",
        )
        metrics = [
            VmMetrics(resource_id=vm.resource_id, metric_name="Percentage CPU",
                      avg=20.0, p50=20.0, p95=30.0, max=40.0, min=10.0),
        ]
        catalog = MagicMock(spec=SkuCatalog)
        catalog.find_smaller_sku.return_value = "Standard_D4s_v3"  # legacy
        catalog.get.return_value = SkuSpec(vcpus=4, memory_gb=16.0)
        recs = generate_recommendations([vm], metrics, CollectionThresholds(), catalog)
        # The right-size rule must drop the legacy SKU and emit nothing.
        right_size = [r for r in recs if r.subcategory == Cat.RIGHT_SIZE]
        assert right_size == []


# ---------------------------------------------------------------------------
# Common contract: every auto-rec carries architect review note + priority
# ---------------------------------------------------------------------------

class TestRecommendationContract:
    def test_quota_rec_has_architect_note_and_priority(self):
        recs = generate_quota_recommendations([_quota(90), _quota(20)])
        assert all(r.notes == ARCHITECT_REVIEW_NOTE for r in recs)
        assert all(r.priority in {Pri.CRITICAL, Pri.HIGH, Pri.MEDIUM, Pri.LOW} for r in recs)
        assert all(r.recommendation for r in recs)

    def test_xsub_rec_has_architect_note_and_priority(self):
        donor = _quota(15, sub="D", sub_id="dddd-1111-1111-1111-111111111111")
        receiver = _quota(85, sub="R", sub_id="rrrr-1111-1111-1111-111111111111")
        recs = generate_cross_subscription_transfer_recommendations([donor, receiver])
        assert recs and all(r.notes == ARCHITECT_REVIEW_NOTE for r in recs)
        assert all(r.priority == Pri.HIGH for r in recs)

    def test_default_notes_field_on_blank_recommendation(self):
        # The model itself defaults notes to ARCHITECT_REVIEW_NOTE so even
        # hand-built rows ship with the marker.
        r = VmRecommendation()
        assert r.notes == ARCHITECT_REVIEW_NOTE


# ---------------------------------------------------------------------------
# Sorting helper
# ---------------------------------------------------------------------------

class TestSortRecommendations:
    def test_orders_by_priority_then_category(self):
        recs = [
            VmRecommendation(priority=Pri.LOW,      category="z"),
            VmRecommendation(priority=Pri.CRITICAL, category="a"),
            VmRecommendation(priority=Pri.MEDIUM,   category="m"),
            VmRecommendation(priority=Pri.HIGH,     category="h"),
        ]
        ordered = sort_recommendations(recs)
        assert [r.priority for r in ordered] == [
            Pri.CRITICAL, Pri.HIGH, Pri.MEDIUM, Pri.LOW,
        ]


# ---------------------------------------------------------------------------
# D. RESOURCE_CLEANUP — deallocated VM detection
# ---------------------------------------------------------------------------

def _make_deallocated_vm() -> VmInventory:
    return VmInventory(
        vm_name="stopped-vm",
        subscription_id="aaaa-1111-1111-1111-111111111111",
        subscription_name="Sub-A",
        resource_group="rg",
        resource_id="/subscriptions/aaaa-1111-1111-1111-111111111111/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/stopped-vm",
        vm_sku="Standard_D2s_v5",
        vcpus=2,
        memory_gb=8.0,
        region="eastus",
        os_type="Linux",
        power_state="PowerState/deallocated",
    )


class TestResourceCleanup:
    def test_deallocated_vm_emits_cleanup_rec(self):
        catalog = MagicMock(spec=SkuCatalog)
        vm = _make_deallocated_vm()
        recs = generate_recommendations([vm], [], CollectionThresholds(), catalog)
        cleanup = [r for r in recs if r.category == Cat.RESOURCE_CLEANUP]
        assert len(cleanup) == 1
        assert cleanup[0].subcategory == Cat.DECOMMISSION_CANDIDATE
        assert cleanup[0].priority == Pri.HIGH
        assert cleanup[0].notes == ARCHITECT_REVIEW_NOTE

    def test_running_vm_does_not_emit_cleanup_rec(self):
        catalog = MagicMock(spec=SkuCatalog)
        catalog.find_smaller_sku.return_value = None
        vm = VmInventory(
            vm_name="running-vm",
            subscription_id="aaaa-1111-1111-1111-111111111111",
            subscription_name="Sub-A",
            resource_group="rg",
            resource_id="/subscriptions/aaaa-1111-1111-1111-111111111111/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/running-vm",
            vm_sku="Standard_D2s_v5",
            vcpus=2,
            memory_gb=8.0,
            region="eastus",
            os_type="Linux",
            power_state="PowerState/running",
        )
        recs = generate_recommendations([vm], [], CollectionThresholds(), catalog)
        cleanup = [r for r in recs if r.category == Cat.RESOURCE_CLEANUP]
        assert cleanup == []
