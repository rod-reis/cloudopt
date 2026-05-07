"""Tests for the availability-zone mapping collector.

Mocks urllib.request.urlopen to avoid real ARM API calls.
"""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from cloudopt.collector.auth import SubscriptionInfo
from cloudopt.collector.zones import _list_locations_raw, collect_zone_mappings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SUB_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TENANT_ID = "tttttttt-tttt-tttt-tttt-tttttttttttt"

_SAMPLE_RESPONSE = {
    "value": [
        {
            "name": "eastus",
            "type": "Region",
            "displayName": "East US",
            "availabilityZoneMappings": [
                {"logicalZone": "1", "physicalZone": "eastus-az1"},
                {"logicalZone": "2", "physicalZone": "eastus-az3"},
                {"logicalZone": "3", "physicalZone": "eastus-az2"},
            ],
        },
        {
            "name": "eastus2",
            "type": "Region",
            "displayName": "East US 2",
            "availabilityZoneMappings": [
                {"logicalZone": "1", "physicalZone": "eastus2-az1"},
            ],
        },
        {
            # Region with no AZ support — should be skipped
            "name": "westus3",
            "type": "Region",
            "displayName": "West US 3",
        },
    ]
}


def _fake_urlopen(url_body: bytes):
    """Return a context-manager mock whose .read() yields url_body."""
    resp = MagicMock()
    resp.read.return_value = url_body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _make_sub(sub_id: str = SUB_ID) -> SubscriptionInfo:
    return SubscriptionInfo(
        subscription_id=sub_id,
        subscription_name="Test Sub",
        tenant_id=TENANT_ID,
    )


def _make_credential(token: str = "fake-token") -> MagicMock:
    cred = MagicMock()
    access_token = MagicMock()
    access_token.token = token
    cred.get_token.return_value = access_token
    return cred


# ---------------------------------------------------------------------------
# _list_locations_raw
# ---------------------------------------------------------------------------


class TestListLocationsRaw:
    def test_returns_locations(self):
        body = json.dumps(_SAMPLE_RESPONSE).encode()
        with patch("urllib.request.urlopen", return_value=_fake_urlopen(body)):
            locs = _list_locations_raw("tok", SUB_ID)
        assert len(locs) == 3
        assert locs[0]["name"] == "eastus"

    def test_follows_next_link(self):
        page1 = {"value": [{"name": "eastus"}], "nextLink": "https://arm/page2"}
        page2 = {"value": [{"name": "westus"}]}

        responses = [
            _fake_urlopen(json.dumps(page1).encode()),
            _fake_urlopen(json.dumps(page2).encode()),
        ]
        with patch("urllib.request.urlopen", side_effect=responses):
            locs = _list_locations_raw("tok", SUB_ID)
        assert [l["name"] for l in locs] == ["eastus", "westus"]

    def test_raises_on_http_error(self):
        err = urllib.error.HTTPError(
            url="https://arm", code=403, msg="Forbidden", hdrs={}, fp=BytesIO(b"denied")
        )
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(RuntimeError, match="ARM HTTP 403"):
                _list_locations_raw("tok", SUB_ID)

    def test_correct_api_version_in_url(self):
        body = json.dumps({"value": []}).encode()
        captured_urls: list[str] = []

        def fake_open(req, timeout=30):
            captured_urls.append(req.full_url)
            return _fake_urlopen(body)

        with patch("urllib.request.urlopen", side_effect=fake_open):
            _list_locations_raw("tok", SUB_ID)

        assert "api-version=2022-12-01" in captured_urls[0]
        assert SUB_ID in captured_urls[0]

    def test_bearer_token_in_auth_header(self):
        body = json.dumps({"value": []}).encode()
        captured_headers: list[dict] = []

        def fake_open(req, timeout=30):
            captured_headers.append(dict(req.headers))
            return _fake_urlopen(body)

        with patch("urllib.request.urlopen", side_effect=fake_open):
            _list_locations_raw("my-secret-token", SUB_ID)

        assert "Bearer my-secret-token" in captured_headers[0]["Authorization"]


# ---------------------------------------------------------------------------
# collect_zone_mappings
# ---------------------------------------------------------------------------


class TestCollectZoneMappings:
    def test_emits_one_row_per_logical_zone(self):
        body = json.dumps(_SAMPLE_RESPONSE).encode()
        cred = _make_credential()
        sub = _make_sub()

        with patch("urllib.request.urlopen", return_value=_fake_urlopen(body)):
            result = collect_zone_mappings(cred, [sub])

        # 3 zones for eastus + 1 zone for eastus2 = 4 total; westus3 skipped
        assert len(result) == 4

    def test_row_fields_are_correct(self):
        body = json.dumps(_SAMPLE_RESPONSE).encode()
        cred = _make_credential()
        sub = _make_sub()

        with patch("urllib.request.urlopen", return_value=_fake_urlopen(body)):
            result = collect_zone_mappings(cred, [sub])

        eastus_rows = [r for r in result if r.location == "eastus"]
        assert len(eastus_rows) == 3

        zone1 = next(r for r in eastus_rows if r.logical_zone == "1")
        assert zone1.physical_zone == "1"          # trimmed trailing digit only
        assert zone1.physical_zone_name == "eastus-az1"  # full value preserved
        assert zone1.subscription_id == SUB_ID
        assert zone1.tenant_id == TENANT_ID

    def test_skips_regions_without_az_mappings(self):
        body = json.dumps(_SAMPLE_RESPONSE).encode()
        cred = _make_credential()
        sub = _make_sub()

        with patch("urllib.request.urlopen", return_value=_fake_urlopen(body)):
            result = collect_zone_mappings(cred, [sub])

        locations = {r.location for r in result}
        assert "westus3" not in locations

    def test_logs_warning_and_continues_on_http_error(self, capsys):
        err = urllib.error.HTTPError(
            url="https://arm", code=403, msg="Forbidden", hdrs={}, fp=BytesIO(b"no")
        )
        cred = _make_credential()
        sub = _make_sub()

        with patch("urllib.request.urlopen", side_effect=err):
            result = collect_zone_mappings(cred, [sub])

        assert result == []

    def test_aggregates_multiple_subscriptions(self):
        body = json.dumps(_SAMPLE_RESPONSE).encode()
        cred = _make_credential()
        sub1 = _make_sub("aaaa-1111-2222-3333-444444444444")
        sub2 = _make_sub("bbbb-1111-2222-3333-444444444444")

        responses = [
            _fake_urlopen(body),
            _fake_urlopen(body),
        ]
        with patch("urllib.request.urlopen", side_effect=responses):
            result = collect_zone_mappings(cred, [sub1, sub2])

        sub_ids = {r.subscription_id for r in result}
        assert sub_ids == {sub1.subscription_id, sub2.subscription_id}
        assert len(result) == 8  # 4 rows per subscription

    def test_get_token_called_with_arm_scope(self):
        body = json.dumps({"value": []}).encode()
        cred = _make_credential()
        sub = _make_sub()

        with patch("urllib.request.urlopen", return_value=_fake_urlopen(body)):
            collect_zone_mappings(cred, [sub])

        cred.get_token.assert_called_with("https://management.azure.com/.default")

    def test_empty_subscriptions_returns_empty(self):
        cred = _make_credential()
        result = collect_zone_mappings(cred, [])
        assert result == []
        cred.get_token.assert_not_called()
