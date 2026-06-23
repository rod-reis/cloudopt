"""Tests for collector/disks.py — mocked Azure Resource Graph client."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from cloudopt.collector.auth import SubscriptionInfo
from cloudopt.models import DiskInventory

SUB = "00000000-0000-0000-0000-000000000001"


def _sub(sub_id: str = SUB) -> SubscriptionInfo:
    return SubscriptionInfo(subscription_id=sub_id, subscription_name="Prod", tenant_id="tenant-1")


def _row(**kw) -> dict:
    base = {
        "id": f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/disks/disk1",
        "name": "disk1",
        "subscriptionId": SUB,
        "resourceGroup": "rg",
        "location": "eastus",
        "skuName": "Premium_LRS",
        "skuTier": "Premium",
        "perfTier": "P30",
        "diskSizeGb": 1024,
        "iopsRW": 5000,
        "mbpsRW": 200,
        "iopsRO": None,
        "mbpsRO": None,
        "burstingEnabled": False,
        "diskState": "Attached",
        "osType": "",
        "managedBy": f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
        "managedByExtended": [],
        "zones": "1",
        "encryptionType": "EncryptionAtRestWithPlatformKey",
        "networkAccessPolicy": "AllowAll",
        "publicNetworkAccess": "Enabled",
        "diskControllerTypes": "SCSI",
        "hyperVGeneration": "V2",
        "timeCreated": "2024-01-01T00:00:00Z",
        "properties": {"tier": "P30", "diskSizeGB": 1024},
    }
    base.update(kw)
    return base


def _patch_client(rows: list[dict]):
    response = MagicMock()
    response.data = rows
    response.skip_token = None
    client = MagicMock()
    client.resources.return_value = response
    return patch("cloudopt.collector.disks.ResourceGraphClient", return_value=client)


class TestCollectDisks:
    def test_empty_subscriptions_returns_empty(self):
        from cloudopt.collector.disks import collect_disks

        assert collect_disks(MagicMock(), []) == []

    def test_parses_disk_row(self):
        from cloudopt.collector.disks import collect_disks

        with _patch_client([_row()]):
            result = collect_disks(MagicMock(), [_sub()])

        assert len(result) == 1
        d = result[0]
        assert isinstance(d, DiskInventory)
        assert d.disk_name == "disk1"
        assert d.sku_name == "Premium_LRS"
        assert d.performance_tier == "P30"
        assert d.disk_size_gb == 1024
        assert d.disk_iops_read_write == 5000
        assert d.disk_mbps_read_write == 200
        assert d.disk_state == "Attached"
        assert d.os_type is None  # empty osType promoted to None
        assert d.subscription_name == "Prod"
        assert d.is_premium_v1 and d.is_data_disk
        assert d.raw_properties == {"tier": "P30", "diskSizeGB": 1024}

    def test_query_failure_returns_empty(self):
        from cloudopt.collector.disks import collect_disks

        client = MagicMock()
        client.resources.side_effect = Exception("forbidden")
        with patch("cloudopt.collector.disks.ResourceGraphClient", return_value=client):
            result = collect_disks(MagicMock(), [_sub()])
        assert result == []

    def test_os_disk_row_is_not_data_disk(self):
        from cloudopt.collector.disks import collect_disks

        with _patch_client([_row(osType="Linux")]):
            result = collect_disks(MagicMock(), [_sub()])
        assert result[0].os_type == "Linux"
        assert result[0].is_data_disk is False
