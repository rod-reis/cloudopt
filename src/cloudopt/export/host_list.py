"""Export VM host list as CSV for use with the monitoring query packs.

The customer feeds the ``vm_name`` column from this CSV into the
appropriate vendor query pack (see docs/query_pack/) to scope their
export to the exact VMs cloudopt collected.
"""

from __future__ import annotations

import csv
from pathlib import Path

from cloudopt.models import VmInventory

_HOST_LIST_COLUMNS: tuple[str, ...] = (
    "vm_name",
    "hostname",
    "os_type",
    "subscription_name",
    "resource_group",
    "region",
)


def write_host_list(vms: list[VmInventory], path: Path) -> None:
    """Write a CSV of VM identifiers to *path*.

    Columns:
        vm_name           — Azure VM name; use this as the hostname key in
                            monitoring queries (copy the whole column as a list)
        hostname          — same as vm_name by default; customer can override
                            if the monitoring tool registers a different name
        os_type           — Windows / Linux (useful for per-OS query variants)
        subscription_name — for grouping / filtering in large estates
        resource_group    — for grouping
        region            — for region-scoped monitoring filters
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(_HOST_LIST_COLUMNS))
        writer.writeheader()
        for vm in vms:
            writer.writerow({
                "vm_name":           vm.vm_name,
                "hostname":          vm.vm_name,
                "os_type":           vm.os_type,
                "subscription_name": vm.subscription_name,
                "resource_group":    vm.resource_group,
                "region":            vm.region,
            })
