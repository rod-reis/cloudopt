"""WARA-style scope/filter handling.

Defines a single immutable :class:`ScopeFilter` that captures the user's
collection scope (tenant, subscriptions, locations, resource groups, tag
filters, plus runtime knobs such as metric_days / concurrency / output_dir)
and parses both:

* the WARA-style ``configfile`` (text file with ``[section]`` blocks), and
* the equivalent CLI flags.

Tag filters are intentionally **never persisted** by callers — they are used
only to decide which resources are in scope at collection time.  The
:class:`ScopeFilter` exposes the parsed tag filters but the rest of the
pipeline is responsible for not writing them out.

Tag filter grammar (matches WARA):

    Operator  Action
    ||        Or     (separates names on the LHS, separates values on the RHS)
    =~        Equals
    !~        Not equals

Examples::

    Environment||Env=~Prod||PD||Production
    Criticality=~High
    Owner!~Bill

Filter order of operations (applied to discovered resources):

    Tenant -> Subscriptions -> Locations -> ResourceGroups -> Tags
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Subscription / Resource Group ID parsing
# ---------------------------------------------------------------------------

_GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def parse_subscription_id(value: str) -> str:
    """Return the bare GUID for a subscription, accepting either form.

    Accepts:
      - "00000000-0000-0000-0000-000000000000"
      - "/subscriptions/00000000-0000-0000-0000-000000000000"
      - "/subscriptions/<guid>/..." (extra path is ignored)
    """
    s = (value or "").strip().strip("/")
    if not s:
        raise ValueError("Empty subscription identifier")
    parts = s.split("/")
    # Accept bare GUID
    if len(parts) == 1 and _GUID_RE.match(parts[0]):
        return parts[0].lower()
    # Accept /subscriptions/<guid>[/...]
    if len(parts) >= 2 and parts[0].lower() == "subscriptions" and _GUID_RE.match(parts[1]):
        return parts[1].lower()
    raise ValueError(f"Not a valid subscription identifier: {value!r}")


@dataclass(frozen=True)
class ResourceGroupRef:
    subscription_id: str  # bare GUID, lowercased
    name: str             # resource group name, lowercased


def parse_resource_group_id(value: str) -> ResourceGroupRef:
    """Parse a full ARM resource group ID.

    Expected form (case-insensitive segments):
        /subscriptions/<sub-guid>/resourceGroups/<rg-name>
    """
    s = (value or "").strip().strip("/")
    if not s:
        raise ValueError("Empty resource group identifier")
    parts = s.split("/")
    if (
        len(parts) >= 4
        and parts[0].lower() == "subscriptions"
        and _GUID_RE.match(parts[1])
        and parts[2].lower() == "resourcegroups"
        and parts[3]
    ):
        return ResourceGroupRef(
            subscription_id=parts[1].lower(),
            name=parts[3].lower(),
        )
    raise ValueError(f"Not a valid resource group identifier: {value!r}")


# ---------------------------------------------------------------------------
# Tag filter
# ---------------------------------------------------------------------------

_TAG_OPS = ("!~", "=~")  # order matters: longest match first per side


@dataclass(frozen=True)
class TagFilter:
    """Parsed tag filter clause.

    ``names`` and ``values`` are lowercased lists; matching is case-insensitive.
    ``equals`` is True for ``=~`` and False for ``!~``.

    Semantics:
        equals=True  -> resource matches if ANY of `names` has a tag value
                        that is in `values`.
        equals=False -> resource matches if NONE of `names` exists with a
                        value in `values` (i.e. either the tag is absent
                        or its value is not in `values`).
    """

    names: tuple[str, ...]
    values: tuple[str, ...]
    equals: bool

    @classmethod
    def parse(cls, expr: str) -> "TagFilter":
        s = (expr or "").strip()
        if not s:
            raise ValueError("Empty tag filter expression")
        for op in _TAG_OPS:
            if op in s:
                lhs, rhs = s.split(op, 1)
                names = tuple(
                    n.strip().lower() for n in lhs.split("||") if n.strip()
                )
                values = tuple(
                    v.strip().lower() for v in rhs.split("||") if v.strip()
                )
                if not names:
                    raise ValueError(f"Tag filter has no names: {expr!r}")
                if not values:
                    raise ValueError(f"Tag filter has no values: {expr!r}")
                return cls(names=names, values=values, equals=(op == "=~"))
        raise ValueError(
            f"Tag filter must contain '=~' or '!~' operator: {expr!r}"
        )

    def matches(self, tags: dict | None) -> bool:
        """Return True iff a resource's tag dict satisfies this filter."""
        normalized: dict[str, str] = {
            (k or "").lower(): str(v or "").lower()
            for k, v in (tags or {}).items()
        }
        if self.equals:
            for name in self.names:
                v = normalized.get(name)
                if v is not None and v in self.values:
                    return True
            return False
        # not-equals: pass if no listed name has a forbidden value
        for name in self.names:
            v = normalized.get(name)
            if v is not None and v in self.values:
                return False
        return True


def matches_all_tag_filters(tags: dict | None, filters: Iterable[TagFilter]) -> bool:
    """Return True if a resource's tags satisfy ALL filters (AND semantics)."""
    for f in filters:
        if not f.matches(tags):
            return False
    return True


# ---------------------------------------------------------------------------
# ScopeFilter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopeFilter:
    """Immutable scope passed through the entire pipeline.

    All identifiers are normalised to lowercase bare GUIDs / names so that
    downstream comparisons can be straightforward case-insensitive matches.
    """

    tenant_id: str | None = None
    subscription_ids: tuple[str, ...] = ()           # bare GUIDs
    locations: tuple[str, ...] = ()                  # ARM region names, lowercased
    resource_groups: tuple[ResourceGroupRef, ...] = ()
    tag_filters: tuple[TagFilter, ...] = ()

    # Runtime knobs (also accepted via configfile)
    metric_days: int | None = None
    concurrency: int | None = None
    arm_rate: float | None = None
    output_dir: Path | None = None

    @property
    def quota_subscription_ids(self) -> tuple[str, ...]:
        """Subscription IDs to target for quota collection.

        Returns explicit ``subscription_ids`` when set; otherwise derives them
        from ``resource_groups`` entries so that a scope file that only lists
        ``[resourcegroups]`` still produces meaningful quota data for those
        subscriptions.  Returns an empty tuple when neither is configured,
        which callers interpret as "all accessible subscriptions".
        """
        if self.subscription_ids:
            return self.subscription_ids
        if self.resource_groups:
            seen: set[str] = set()
            result: list[str] = []
            for rg in self.resource_groups:
                if rg.subscription_id not in seen:
                    seen.add(rg.subscription_id)
                    result.append(rg.subscription_id)
            return tuple(result)
        return ()

    @property
    def has_resource_group_filter(self) -> bool:
        return bool(self.resource_groups)

    @property
    def has_tag_filter(self) -> bool:
        return bool(self.tag_filters)

    def resource_group_names_for(self, subscription_id: str) -> list[str]:
        """Lowercased RG names scoped to the given subscription, or []."""
        sub = subscription_id.lower()
        return [rg.name for rg in self.resource_groups if rg.subscription_id == sub]

    def in_scope_subscription(self, subscription_id: str) -> bool:
        if not self.subscription_ids:
            return True
        return subscription_id.lower() in self.subscription_ids

    def in_scope_resource_group(self, subscription_id: str, rg_name: str) -> bool:
        if not self.resource_groups:
            return True
        sub = subscription_id.lower()
        rg = (rg_name or "").lower()
        return any(
            r.subscription_id == sub and r.name == rg
            for r in self.resource_groups
        )

    def in_scope_location(self, region: str) -> bool:
        if not self.locations:
            return True
        return (region or "").lower() in self.locations

    def in_scope_tags(self, tags: dict | None) -> bool:
        if not self.tag_filters:
            return True
        return matches_all_tag_filters(tags, self.tag_filters)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_scope(
    *,
    tenant_id: str | None = None,
    subscriptions: Iterable[str] | None = None,
    locations: Iterable[str] | None = None,
    resource_groups: Iterable[str] | None = None,
    tags: Iterable[str] | None = None,
    metric_days: int | None = None,
    concurrency: int | None = None,
    arm_rate: float | None = None,
    output_dir: Path | str | None = None,
) -> ScopeFilter:
    """Validate and build a :class:`ScopeFilter` from raw user inputs."""
    sub_ids = tuple(parse_subscription_id(s) for s in (subscriptions or []) if s)
    rg_refs = tuple(parse_resource_group_id(r) for r in (resource_groups or []) if r)
    locs = tuple((l or "").strip().lower() for l in (locations or []) if (l or "").strip())
    tag_filters = tuple(TagFilter.parse(t) for t in (tags or []) if (t or "").strip())

    # Resource groups must reference in-scope subscriptions if any are given.
    if sub_ids and rg_refs:
        unknown = [
            f"/subscriptions/{r.subscription_id}/resourceGroups/{r.name}"
            for r in rg_refs
            if r.subscription_id not in sub_ids
        ]
        if unknown:
            raise ValueError(
                "Resource group(s) reference subscriptions outside the "
                f"-subscriptions scope: {unknown}"
            )

    # Tenant: light validation only — the credential layer does the real check.
    tid = tenant_id.strip() if tenant_id else None
    if tid and not _GUID_RE.match(tid):
        raise ValueError(f"Tenant ID is not a valid GUID: {tenant_id!r}")

    out = Path(output_dir) if output_dir is not None else None

    return ScopeFilter(
        tenant_id=tid.lower() if tid else None,
        subscription_ids=sub_ids,
        locations=locs,
        resource_groups=rg_refs,
        tag_filters=tag_filters,
        metric_days=metric_days,
        concurrency=concurrency,
        arm_rate=arm_rate,
        output_dir=out,
    )


# ---------------------------------------------------------------------------
# Configfile parser
# ---------------------------------------------------------------------------

# Recognised section names (case-insensitive, with/without spaces).
_SECTION_ALIASES: dict[str, str] = {
    "tenantid":         "tenantid",
    "tenant":           "tenantid",
    "subscriptionids":  "subscriptionids",
    "subscriptions":    "subscriptionids",
    "locations":        "locations",
    "regions":          "locations",
    "resourcegroups":   "resourcegroups",
    "resourcegroup":    "resourcegroups",
    "tags":             "tags",
    "metricdays":       "metricdays",
    "metric_days":      "metricdays",
    "concurrency":      "concurrency",
    "armrate":          "armrate",
    "arm_rate":         "armrate",
    "armratepersecond": "armrate",
    "output":           "output",
    "outputdir":        "output",
}


def parse_config_file(path: Path) -> dict[str, list[str]]:
    """Parse a WARA-style configfile into ``{section_key: [lines...]}``.

    Lines starting with ``#`` or ``;`` are comments.  Blank lines are ignored.
    Section headers are recognised case-insensitively and mapped through
    ``_SECTION_ALIASES``.
    """
    text = Path(path).read_text(encoding="utf-8-sig")
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            key = line[1:-1].strip().lower().replace(" ", "")
            current = _SECTION_ALIASES.get(key)
            if current is None:
                # Unknown section — keep an entry so we can warn later
                current = f"_unknown:{key}"
            sections.setdefault(current, [])
            continue
        if current is None:
            # Stray line outside any section — ignore
            continue
        sections.setdefault(current, []).append(line)
    return sections


def scope_from_config_file(path: Path) -> ScopeFilter:
    """Parse a configfile and return a fully-validated :class:`ScopeFilter`."""
    sections = parse_config_file(path)

    def _first(section: str) -> str | None:
        vals = sections.get(section) or []
        return vals[0] if vals else None

    tenant = _first("tenantid")
    subs = sections.get("subscriptionids") or []
    locs = sections.get("locations") or []
    rgs = sections.get("resourcegroups") or []
    tags = sections.get("tags") or []

    metric_days_str = _first("metricdays")
    metric_days = int(metric_days_str) if metric_days_str else None

    conc_str = _first("concurrency")
    concurrency = int(conc_str) if conc_str else None

    arm_rate_str = _first("armrate")
    arm_rate = float(arm_rate_str) if arm_rate_str else None

    output = _first("output")

    return build_scope(
        tenant_id=tenant,
        subscriptions=subs,
        locations=locs,
        resource_groups=rgs,
        tags=tags,
        metric_days=metric_days,
        concurrency=concurrency,
        arm_rate=arm_rate,
        output_dir=output,
    )


# ---------------------------------------------------------------------------
# KQL helpers
# ---------------------------------------------------------------------------


def kql_location_clause(scope: ScopeFilter) -> str:
    """KQL pipe fragment ``| where location in~ (...)`` or ``""``."""
    if not scope.locations:
        return ""
    quoted = ", ".join(f"'{r}'" for r in scope.locations)
    return f"\n| where location in~ ({quoted})"


def kql_resource_group_clause(scope: ScopeFilter) -> str:
    """KQL pipe fragment filtering by (subscriptionId, resourceGroup) tuple.

    Resource Graph results expose ``subscriptionId`` and ``resourceGroup``;
    we build an OR-of-AND match per RG.  Returns ``""`` when no RG filter.
    """
    if not scope.resource_groups:
        return ""
    parts = [
        f"(subscriptionId =~ '{r.subscription_id}' and resourceGroup =~ '{r.name}')"
        for r in scope.resource_groups
    ]
    return "\n| where " + " or ".join(parts)
