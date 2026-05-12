"""TDD tests for collector/capacity_reservations.py — mocked Azure SDK."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cloudopt.collector.auth import SubscriptionInfo


def _sub(sub_id: str = "00000000-0000-0000-0000-000000000001") -> SubscriptionInfo:
    return SubscriptionInfo(
        subscription_id=sub_id,
        subscription_name="Prod",
        tenant_id="tenant-1",
    )


class TestCollectCapacityReservations:
    def test_returns_empty_on_error(self):
        """Silently returns [] when the ARM call fails."""
        from cloudopt.collector.capacity_reservations import collect_capacity_reservations

        mock_cred = MagicMock()
        with patch(
            "cloudopt.collector.capacity_reservations.ComputeManagementClient",
            side_effect=Exception("access denied"),
        ):
            result = collect_capacity_reservations(mock_cred, [_sub()])
        assert result == []

    def test_returns_crg_list(self):
        """Returns a CapacityReservationGroup list parsed from the compute API."""
        from cloudopt.collector.capacity_reservations import collect_capacity_reservations
        from cloudopt.models import CapacityReservationGroup

        mock_cred = MagicMock()

        # Mock CRG object returned by list_by_subscription
        mock_crg = MagicMock()
        mock_crg.id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Compute/capacityReservationGroups/crg1"
        mock_crg.name = "crg1"
        mock_crg.location = "eastus"
        mock_crg.zones = None

        # Mock capacity reservation within the CRG
        mock_cr = MagicMock()
        mock_cr.name = "cr1"
        mock_cr.sku = MagicMock(name_="Standard_D4s_v5", capacity=5)
        mock_cr.sku.name = "Standard_D4s_v5"
        mock_cr.sku.capacity = 5
        mock_cr.virtual_machines_allocated = [MagicMock(), MagicMock()]  # 2 VMs

        mock_compute_client = MagicMock()
        mock_compute_client.capacity_reservation_groups.list_by_subscription.return_value = [mock_crg]
        mock_compute_client.capacity_reservations.list_by_capacity_reservation_group.return_value = [mock_cr]

        with patch(
            "cloudopt.collector.capacity_reservations.ComputeManagementClient",
            return_value=mock_compute_client,
        ):
            result = collect_capacity_reservations(mock_cred, [_sub()])

        assert len(result) == 1
        assert isinstance(result[0], CapacityReservationGroup)
        assert result[0].group_name == "crg1"
        assert result[0].region == "eastus"

    def test_reserved_and_used_counts(self):
        """reserved_count and used_count are correctly populated."""
        from cloudopt.collector.capacity_reservations import collect_capacity_reservations

        mock_cred = MagicMock()

        mock_crg = MagicMock()
        mock_crg.id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Compute/capacityReservationGroups/crg1"
        mock_crg.name = "crg1"
        mock_crg.location = "eastus"
        mock_crg.zones = None

        mock_cr = MagicMock()
        mock_cr.name = "cr1"
        mock_cr.sku = MagicMock()
        mock_cr.sku.name = "Standard_D4s_v5"
        mock_cr.sku.capacity = 8
        mock_cr.virtual_machines_allocated = [MagicMock() for _ in range(3)]  # 3 used

        mock_compute_client = MagicMock()
        mock_compute_client.capacity_reservation_groups.list_by_subscription.return_value = [mock_crg]
        mock_compute_client.capacity_reservations.list_by_capacity_reservation_group.return_value = [mock_cr]

        with patch(
            "cloudopt.collector.capacity_reservations.ComputeManagementClient",
            return_value=mock_compute_client,
        ):
            result = collect_capacity_reservations(mock_cred, [_sub()])

        assert len(result) == 1
        item = result[0].reservations[0]
        assert item.reserved_count == 8
        assert item.used_count == 3
