"""TDD tests for collector/reservations.py — mocked Azure SDK clients."""

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


class TestCollectReservations:
    def test_returns_empty_on_error(self):
        """Silently returns [] when the ARM call fails (e.g. permission error)."""
        from cloudopt.collector.reservations import collect_reservations

        mock_cred = MagicMock()
        with patch(
            "cloudopt.collector.reservations.ResourceGraphClient",
            side_effect=Exception("access denied"),
        ):
            result = collect_reservations(mock_cred, [_sub()])
        assert result == []

    def test_returns_orders_from_arg(self):
        """Returns a ReservationOrder list parsed from the ARG response."""
        from cloudopt.collector.reservations import collect_reservations
        from cloudopt.models import ReservationOrder

        mock_cred = MagicMock()
        mock_arg_client = MagicMock()
        # Simulate ARG response with one reservation order row
        mock_row = {
            "id": "/providers/Microsoft.Capacity/reservationOrders/ord-1",
            "displayName": "Prod-Reservation",
            "term": "P1Y",
            "expiryDate": "2026-01-01",
            "skuName": "Standard_D4s_v5",
            "location": "eastus",
            "reservedCount": 10,
            "appliedScopeType": "Shared",
            "appliedScopes": ["00000000-0000-0000-0000-000000000001"],
        }
        mock_arg_client.resources.return_value = MagicMock(
            data=[mock_row], skip_token=None
        )

        with patch(
            "cloudopt.collector.reservations.ResourceGraphClient",
            return_value=mock_arg_client,
        ):
            result = collect_reservations(mock_cred, [_sub()])

        assert len(result) == 1
        assert isinstance(result[0], ReservationOrder)
        assert result[0].order_id == "/providers/Microsoft.Capacity/reservationOrders/ord-1"
        assert result[0].sku_name == "Standard_D4s_v5"

    def test_joins_utilization_from_consumption(self):
        """utilization_pct is populated from the Consumption API when available."""
        from cloudopt.collector.reservations import collect_reservations

        mock_cred = MagicMock()
        mock_arg_client = MagicMock()
        mock_row = {
            "id": "/providers/Microsoft.Capacity/reservationOrders/ord-1",
            "displayName": "Prod",
            "term": "P1Y",
            "expiryDate": "2026-01-01",
            "skuName": "Standard_D4s_v5",
            "location": "eastus",
            "reservedCount": 5,
            "appliedScopeType": "Shared",
            "appliedScopes": ["00000000-0000-0000-0000-000000000001"],
        }
        mock_arg_client.resources.return_value = MagicMock(data=[mock_row], skip_token=None)

        # Consumption mock
        mock_util_detail = MagicMock()
        mock_util_detail.reservation_order_id = "ord-1"
        mock_util_detail.reservation_id = "res-1"
        mock_util_detail.utilized_percentage = 65.0
        mock_consumption_client = MagicMock()
        mock_consumption_client.reservations_details.list.return_value = [mock_util_detail]

        with (
            patch("cloudopt.collector.reservations.ResourceGraphClient", return_value=mock_arg_client),
            patch("cloudopt.collector.reservations.ConsumptionManagementClient", return_value=mock_consumption_client),
        ):
            result = collect_reservations(mock_cred, [_sub()])

        assert len(result) == 1
        assert result[0].utilization_pct is not None
