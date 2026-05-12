"""TDD tests for collector/deployment_failures.py — mocked Azure SDK."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from cloudopt.collector.auth import SubscriptionInfo


def _sub(sub_id: str = "00000000-0000-0000-0000-000000000001") -> SubscriptionInfo:
    return SubscriptionInfo(
        subscription_id=sub_id,
        subscription_name="Prod",
        tenant_id="tenant-1",
    )


def _log_entry(
    *,
    resource_id: str = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
    operation_name: str = "Microsoft.Compute/virtualMachines/write",
    status_value: str = "Failed",
    status_code: str | None = None,
    event_timestamp: datetime | None = None,
    message: str = "AllocationFailed: Could not allocate",
) -> MagicMock:
    entry = MagicMock()
    entry.resource_id = resource_id
    entry.operation_name = MagicMock()
    entry.operation_name.value = operation_name
    entry.status = MagicMock()
    entry.status.value = status_value
    if status_code:
        entry.properties = {"statusCode": status_code}
    else:
        entry.properties = {}
    entry.event_timestamp = event_timestamp or datetime(2026, 3, 1, tzinfo=timezone.utc)
    entry.event_name = MagicMock()
    entry.event_name.value = "BeginRequest"
    # status_message in properties
    entry.status.localizedValue = message
    return entry


class TestCollectDeploymentFailures:
    def test_returns_empty_on_monitor_error(self):
        """Silently returns [] when the Monitor API call fails."""
        from cloudopt.collector.deployment_failures import collect_deployment_failures

        mock_cred = MagicMock()
        with patch(
            "cloudopt.collector.deployment_failures.MonitorManagementClient",
            side_effect=Exception("access denied"),
        ):
            result = collect_deployment_failures(mock_cred, [_sub()])
        assert result == []

    def test_returns_empty_when_no_subscriptions(self):
        """Returns [] immediately when subscriptions list is empty."""
        from cloudopt.collector.deployment_failures import collect_deployment_failures

        mock_cred = MagicMock()
        result = collect_deployment_failures(mock_cred, [])
        assert result == []

    def test_returns_failure_entry_list(self):
        """Returns DeploymentFailureEntry objects for each log event."""
        from cloudopt.collector.deployment_failures import collect_deployment_failures
        from cloudopt.models import DeploymentFailureEntry

        mock_cred = MagicMock()
        entry = _log_entry()

        mock_monitor_client = MagicMock()
        mock_monitor_client.activity_logs.list.return_value = [entry]

        with patch(
            "cloudopt.collector.deployment_failures.MonitorManagementClient",
            return_value=mock_monitor_client,
        ):
            result = collect_deployment_failures(mock_cred, [_sub()])

        assert len(result) == 1
        assert isinstance(result[0], DeploymentFailureEntry)

    def test_allocation_error_class(self):
        """AllocationFailed maps to error_class='allocation'."""
        from cloudopt.collector.deployment_failures import collect_deployment_failures

        mock_cred = MagicMock()
        entry = _log_entry(message="AllocationFailed: No capacity available")

        mock_monitor_client = MagicMock()
        mock_monitor_client.activity_logs.list.return_value = [entry]

        with patch(
            "cloudopt.collector.deployment_failures.MonitorManagementClient",
            return_value=mock_monitor_client,
        ):
            result = collect_deployment_failures(mock_cred, [_sub()])

        assert result[0].error_class == "allocation"

    def test_quota_error_class(self):
        """QuotaExceeded maps to error_class='quota'."""
        from cloudopt.collector.deployment_failures import collect_deployment_failures

        mock_cred = MagicMock()
        entry = _log_entry(message="QuotaExceeded for vCPUs in eastus")

        mock_monitor_client = MagicMock()
        mock_monitor_client.activity_logs.list.return_value = [entry]

        with patch(
            "cloudopt.collector.deployment_failures.MonitorManagementClient",
            return_value=mock_monitor_client,
        ):
            result = collect_deployment_failures(mock_cred, [_sub()])

        assert result[0].error_class == "quota"

    def test_image_error_class(self):
        """ImagePullBackOff maps to error_class='image'."""
        from cloudopt.collector.deployment_failures import collect_deployment_failures

        mock_cred = MagicMock()
        entry = _log_entry(
            resource_id="/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.ContainerService/managedClusters/aks1",
            message="ImagePullBackOff for container registry",
        )

        mock_monitor_client = MagicMock()
        mock_monitor_client.activity_logs.list.return_value = [entry]

        with patch(
            "cloudopt.collector.deployment_failures.MonitorManagementClient",
            return_value=mock_monitor_client,
        ):
            result = collect_deployment_failures(mock_cred, [_sub()])

        assert result[0].error_class == "image"

    def test_other_error_class(self):
        """Unrecognised error message maps to error_class='other'."""
        from cloudopt.collector.deployment_failures import collect_deployment_failures

        mock_cred = MagicMock()
        entry = _log_entry(message="SomeRandomError occurred during deployment")

        mock_monitor_client = MagicMock()
        mock_monitor_client.activity_logs.list.return_value = [entry]

        with patch(
            "cloudopt.collector.deployment_failures.MonitorManagementClient",
            return_value=mock_monitor_client,
        ):
            result = collect_deployment_failures(mock_cred, [_sub()])

        assert result[0].error_class == "other"

    def test_sku_not_available_maps_to_allocation(self):
        """SkuNotAvailable maps to error_class='allocation'."""
        from cloudopt.collector.deployment_failures import collect_deployment_failures

        mock_cred = MagicMock()
        entry = _log_entry(message="SkuNotAvailable: The requested VM size Standard_D8s_v5 is not available")

        mock_monitor_client = MagicMock()
        mock_monitor_client.activity_logs.list.return_value = [entry]

        with patch(
            "cloudopt.collector.deployment_failures.MonitorManagementClient",
            return_value=mock_monitor_client,
        ):
            result = collect_deployment_failures(mock_cred, [_sub()])

        assert result[0].error_class == "allocation"

    def test_operation_not_allowed_with_quota_maps_to_quota(self):
        """OperationNotAllowed with 'quota' substring maps to error_class='quota'."""
        from cloudopt.collector.deployment_failures import collect_deployment_failures

        mock_cred = MagicMock()
        entry = _log_entry(message="OperationNotAllowed: quota limit exceeded for cores")

        mock_monitor_client = MagicMock()
        mock_monitor_client.activity_logs.list.return_value = [entry]

        with patch(
            "cloudopt.collector.deployment_failures.MonitorManagementClient",
            return_value=mock_monitor_client,
        ):
            result = collect_deployment_failures(mock_cred, [_sub()])

        assert result[0].error_class == "quota"

    def test_resource_fields_populated(self):
        """resource_id, resource_name, resource_type, and subscription_id are populated."""
        from cloudopt.collector.deployment_failures import collect_deployment_failures

        mock_cred = MagicMock()
        resource_id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1"
        entry = _log_entry(resource_id=resource_id)

        mock_monitor_client = MagicMock()
        mock_monitor_client.activity_logs.list.return_value = [entry]

        with patch(
            "cloudopt.collector.deployment_failures.MonitorManagementClient",
            return_value=mock_monitor_client,
        ):
            result = collect_deployment_failures(mock_cred, [_sub()])

        f = result[0]
        assert f.subscription_id == "00000000-0000-0000-0000-000000000001"
        assert f.resource_name == "vm1"
        assert f.resource_type == "microsoft.compute/virtualmachines"
        assert "vm1" in f.resource_id

    def test_multiple_subscriptions_aggregated(self):
        """Entries from multiple subscriptions are all returned."""
        from cloudopt.collector.deployment_failures import collect_deployment_failures

        mock_cred = MagicMock()
        entry1 = _log_entry(resource_id="/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1")
        entry2 = _log_entry(resource_id="/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm2")

        mock_monitor_client = MagicMock()
        # Return different entries per call
        mock_monitor_client.activity_logs.list.side_effect = [[entry1], [entry2]]

        sub1 = _sub("00000000-0000-0000-0000-000000000001")
        sub2 = _sub("00000000-0000-0000-0000-000000000002")

        with patch(
            "cloudopt.collector.deployment_failures.MonitorManagementClient",
            return_value=mock_monitor_client,
        ):
            result = collect_deployment_failures(mock_cred, [sub1, sub2])

        assert len(result) == 2

    def test_skips_non_target_resource_types(self):
        """Entries for resource types outside VM/VMSS/AKS are excluded."""
        from cloudopt.collector.deployment_failures import collect_deployment_failures

        mock_cred = MagicMock()
        entry = _log_entry(
            resource_id="/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Network/loadBalancers/lb1",
            message="AllocationFailed",
        )

        mock_monitor_client = MagicMock()
        mock_monitor_client.activity_logs.list.return_value = [entry]

        with patch(
            "cloudopt.collector.deployment_failures.MonitorManagementClient",
            return_value=mock_monitor_client,
        ):
            result = collect_deployment_failures(mock_cred, [_sub()])

        assert result == []
