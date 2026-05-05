"""Azure VM SKU catalog — maps SKU name+region to vCPU and memory specs.

Uses azure-mgmt-compute's resource_skus API and caches results in-process
to avoid repeated calls when enriching large inventories.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient


@dataclass(frozen=True)
class SkuSpec:
    vcpus: int
    memory_gb: float


class SkuCatalog:
    """Lazy-loading cache of VM SKU specs keyed by (region, sku_name)."""

    def __init__(self, credential: DefaultAzureCredential) -> None:
        self._credential = credential
        # (sub_id, region) → {sku_name: SkuSpec}
        self._cache: dict[tuple[str, str], dict[str, SkuSpec]] = defaultdict(dict)
        self._loaded_subs: set[str] = set()

    def ensure_loaded(self) -> None:
        """No-op placeholder — loading is triggered lazily per subscription."""

    def get(
        self,
        subscription_id: str,
        region: str,
        sku_name: str,
    ) -> SkuSpec | None:
        """Return SkuSpec for the given SKU, loading from Azure if not cached."""
        cache_key = (subscription_id, region)
        if cache_key not in self._cache or sku_name not in self._cache[cache_key]:
            self._load(subscription_id, region)
        return self._cache[cache_key].get(sku_name)

    def _load(self, subscription_id: str, region: str) -> None:
        """Fetch all VM SKUs for a subscription+region from the Compute API."""
        if subscription_id in self._loaded_subs:
            return  # already loaded for all regions in this sub

        client = ComputeManagementClient(self._credential, subscription_id)
        for sku in client.resource_skus.list(filter=f"location eq '{region}'"):
            if sku.resource_type != "virtualMachines":
                continue

            vcpus: int = 0
            memory_gb: float = 0.0

            for cap in sku.capabilities or []:
                if cap.name == "vCPUsAvailable":
                    try:
                        vcpus = int(cap.value or 0)
                    except ValueError:
                        pass
                elif cap.name == "MemoryGB":
                    try:
                        memory_gb = float(cap.value or 0)
                    except ValueError:
                        pass

            if sku.name and (vcpus or memory_gb):
                key = (subscription_id, (sku.locations or [region])[0].lower())
                self._cache[key][sku.name] = SkuSpec(
                    vcpus=vcpus, memory_gb=memory_gb
                )

    def find_smaller_sku(
        self,
        subscription_id: str,
        region: str,
        current_sku: str,
        required_vcpus: int,
        required_memory_gb: float,
    ) -> str | None:
        """Return the name of the smallest SKU in the same family that satisfies
        the given vCPU and memory requirements, or None if no smaller option exists.
        """
        self._load(subscription_id, region)
        cache_key = (subscription_id, region.lower())
        catalog = self._cache.get(cache_key, {})

        current_spec = catalog.get(current_sku)
        if not current_spec:
            return None

        # Same SKU family prefix (e.g., "Standard_D" from "Standard_D4s_v3")
        family_prefix = _sku_family_prefix(current_sku)

        candidates: list[tuple[int, float, str]] = []  # (vcpus, memory, name)
        for name, spec in catalog.items():
            if name == current_sku:
                continue
            if not name.startswith(family_prefix):
                continue
            if spec.vcpus < required_vcpus or spec.memory_gb < required_memory_gb:
                continue
            # Must be strictly smaller than current
            if spec.vcpus > current_spec.vcpus or spec.memory_gb > current_spec.memory_gb:
                continue
            candidates.append((spec.vcpus, spec.memory_gb, name))

        if not candidates:
            return None

        # Pick smallest by vCPU then memory
        candidates.sort()
        return candidates[0][2]


def _sku_family_prefix(sku_name: str) -> str:
    """Extract the family prefix from a SKU name.

    Examples:
      "Standard_D4s_v3"  → "Standard_D"
      "Standard_E8s_v4"  → "Standard_E"
      "Standard_B2ms"    → "Standard_B"
    """
    # Strip "Standard_" prefix, take up to the first digit
    bare = sku_name.split("_", 1)[-1] if "_" in sku_name else sku_name
    prefix = ""
    for ch in bare:
        if ch.isdigit():
            break
        prefix += ch
    return f"Standard_{prefix}" if prefix else sku_name
