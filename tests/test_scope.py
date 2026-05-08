"""Tests for scope/filter parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from cloudopt.scope import (
    ResourceGroupRef,
    ScopeFilter,
    TagFilter,
    build_scope,
    kql_location_clause,
    kql_resource_group_clause,
    parse_resource_group_id,
    parse_subscription_id,
    scope_from_config_file,
)


# ---------------------------------------------------------------------------
# parse_subscription_id / parse_resource_group_id
# ---------------------------------------------------------------------------

GUID = "11111111-2222-3333-4444-555555555555"


def test_parse_subscription_id_bare_guid():
    assert parse_subscription_id(GUID) == GUID


def test_parse_subscription_id_full_path():
    assert parse_subscription_id(f"/subscriptions/{GUID}") == GUID


def test_parse_subscription_id_uppercase_normalised():
    assert parse_subscription_id(GUID.upper()) == GUID


def test_parse_subscription_id_with_trailing_segments():
    assert (
        parse_subscription_id(f"/subscriptions/{GUID}/resourceGroups/Foo") == GUID
    )


@pytest.mark.parametrize("bad", ["", "   ", "not-a-guid", "/subscriptions/", "/sub/" + GUID])
def test_parse_subscription_id_rejects_invalid(bad):
    with pytest.raises(ValueError):
        parse_subscription_id(bad)


def test_parse_resource_group_id_ok():
    rg = parse_resource_group_id(f"/subscriptions/{GUID}/resourceGroups/MyRG")
    assert rg == ResourceGroupRef(subscription_id=GUID, name="myrg")


def test_parse_resource_group_id_case_insensitive_segments():
    rg = parse_resource_group_id(f"/SUBSCRIPTIONS/{GUID.upper()}/RESOURCEGROUPS/MyRG")
    assert rg == ResourceGroupRef(subscription_id=GUID, name="myrg")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "/subscriptions/" + GUID,
        f"/subscriptions/{GUID}/resourceGroups/",
        "/foo/" + GUID + "/resourceGroups/RG",
    ],
)
def test_parse_resource_group_id_rejects_invalid(bad):
    with pytest.raises(ValueError):
        parse_resource_group_id(bad)


# ---------------------------------------------------------------------------
# TagFilter
# ---------------------------------------------------------------------------

def test_tagfilter_equals_or_match():
    f = TagFilter.parse("Environment||Env=~Prod||PD||Production")
    assert f.equals is True
    assert f.matches({"Environment": "Prod"})
    assert f.matches({"env": "production"})  # case-insensitive
    assert not f.matches({"Environment": "Dev"})
    assert not f.matches({})


def test_tagfilter_not_equals():
    f = TagFilter.parse("Owner!~Bill")
    assert f.equals is False
    assert f.matches({})
    assert f.matches({"Owner": "Alice"})
    assert not f.matches({"Owner": "Bill"})
    assert not f.matches({"OWNER": "bill"})  # case-insensitive


def test_tagfilter_multi_name_not_equals():
    f = TagFilter.parse("Owner||Manager!~Bill||Bob")
    assert f.matches({"Owner": "Alice"})
    assert not f.matches({"Manager": "Bob"})


@pytest.mark.parametrize("bad", ["", "   ", "Foo", "Foo=Bar", "=~Bar", "Foo=~"])
def test_tagfilter_rejects_invalid(bad):
    with pytest.raises(ValueError):
        TagFilter.parse(bad)


# ---------------------------------------------------------------------------
# build_scope + KQL helpers
# ---------------------------------------------------------------------------

def test_build_scope_full():
    scope = build_scope(
        tenant_id="00000000-0000-0000-0000-000000000001",
        subscriptions=[GUID, f"/subscriptions/{GUID}"],
        locations=["EastUS", "westus"],
        resource_groups=[f"/subscriptions/{GUID}/resourceGroups/RG-1"],
        tags=["Environment=~Prod", "Owner!~Bill"],
        metric_days=60,
        concurrency=10,
        output_dir="out",
    )
    assert scope.tenant_id == "00000000-0000-0000-0000-000000000001"
    assert scope.subscription_ids == (GUID, GUID)
    assert scope.locations == ("eastus", "westus")
    assert scope.resource_groups == (
        ResourceGroupRef(subscription_id=GUID, name="rg-1"),
    )
    assert len(scope.tag_filters) == 2
    assert scope.metric_days == 60
    assert scope.concurrency == 10
    assert str(scope.output_dir).endswith("out")


def test_build_scope_rg_outside_sub_scope_rejected():
    other_sub = "99999999-9999-9999-9999-999999999999"
    with pytest.raises(ValueError, match="outside the -subscriptions scope"):
        build_scope(
            subscriptions=[GUID],
            resource_groups=[f"/subscriptions/{other_sub}/resourceGroups/RG"],
        )


def test_build_scope_invalid_tenant_rejected():
    with pytest.raises(ValueError, match="Tenant ID is not a valid GUID"):
        build_scope(tenant_id="not-a-guid")


def test_kql_location_clause_empty():
    assert kql_location_clause(ScopeFilter()) == ""


def test_kql_location_clause_filter():
    s = build_scope(locations=["eastus", "westus2"])
    out = kql_location_clause(s)
    assert "location in~ ('eastus', 'westus2')" in out


def test_kql_resource_group_clause_filter():
    s = build_scope(
        subscriptions=[GUID],
        resource_groups=[
            f"/subscriptions/{GUID}/resourceGroups/RG-1",
            f"/subscriptions/{GUID}/resourceGroups/RG-2",
        ],
    )
    out = kql_resource_group_clause(s)
    assert f"subscriptionId =~ '{GUID}'" in out
    assert "resourceGroup =~ 'rg-1'" in out
    assert "resourceGroup =~ 'rg-2'" in out
    assert " or " in out


# ---------------------------------------------------------------------------
# ScopeFilter membership helpers
# ---------------------------------------------------------------------------

def test_scope_in_scope_helpers():
    scope = build_scope(
        subscriptions=[GUID],
        locations=["eastus"],
        resource_groups=[f"/subscriptions/{GUID}/resourceGroups/RG-1"],
        tags=["Owner!~Bill"],
    )
    assert scope.in_scope_subscription(GUID)
    assert not scope.in_scope_subscription("00000000-0000-0000-0000-0000000000ff")
    assert scope.in_scope_location("EastUS")
    assert not scope.in_scope_location("westus")
    assert scope.in_scope_resource_group(GUID, "RG-1")
    assert not scope.in_scope_resource_group(GUID, "RG-2")
    assert scope.in_scope_tags({"Owner": "Alice"})
    assert not scope.in_scope_tags({"Owner": "Bill"})


# ---------------------------------------------------------------------------
# Configfile parser
# ---------------------------------------------------------------------------

def test_scope_from_config_file(tmp_path: Path):
    cfg = tmp_path / "scope.txt"
    cfg.write_text(
        f"""
        # cloudopt scope configuration file
        [tenantid]
        00000000-0000-0000-0000-000000000099

        [subscriptionids]
        /subscriptions/{GUID}
        ; comment line ignored
        {GUID}

        [locations]
        eastus
        westus

        [resourcegroups]
        /subscriptions/{GUID}/resourceGroups/RG1
        /subscriptions/{GUID}/resourceGroups/RG2

        [Tags]
        Environment||Env=~Prod||Production
        Owner!~Bill

        [metricdays]
        60

        [concurrency]
        8

        [output]
        ./out-dir
        """,
        encoding="utf-8",
    )
    scope = scope_from_config_file(cfg)
    assert scope.tenant_id == "00000000-0000-0000-0000-000000000099"
    assert scope.subscription_ids == (GUID, GUID)
    assert scope.locations == ("eastus", "westus")
    assert len(scope.resource_groups) == 2
    assert {r.name for r in scope.resource_groups} == {"rg1", "rg2"}
    assert len(scope.tag_filters) == 2
    assert scope.metric_days == 60
    assert scope.concurrency == 8
    assert str(scope.output_dir).endswith("out-dir")
