"""Azure VM SKU catalog — maps SKU name+region to vCPU and memory specs.

Uses azure-mgmt-compute's resource_skus API and caches results in-process
to avoid repeated calls when enriching large inventories.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient


@dataclass(frozen=True)
class SkuSpec:
    vcpus: int
    memory_gb: float
    network_bandwidth_mbps: float = 0.0   # MaxNetworkBandwidth capability (Mbps); 0 = unknown
    accelerated_networking: bool = False  # AcceleratedNetworkingEnabled capability


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
            network_bandwidth_mbps: float = 0.0
            accelerated_networking: bool = False

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
                elif cap.name == "MaxNetworkBandwidth":
                    try:
                        network_bandwidth_mbps = float(cap.value or 0)
                    except ValueError:
                        pass
                elif cap.name == "AcceleratedNetworkingEnabled":
                    accelerated_networking = (cap.value or "").lower() == "true"

            if sku.name and (vcpus or memory_gb):
                key = (subscription_id, (sku.locations or [region])[0].lower())
                self._cache[key][sku.name] = SkuSpec(
                    vcpus=vcpus,
                    memory_gb=memory_gb,
                    network_bandwidth_mbps=network_bandwidth_mbps,
                    accelerated_networking=accelerated_networking,
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

    def find_larger_sku(
        self,
        subscription_id: str,
        region: str,
        current_sku: str,
        max_scale_factor: float = 2.0,
    ) -> str | None:
        """Return the smallest SKU in the same family that is strictly larger.

        Only considers SKUs with more vCPUs than the current one and up to
        ``max_scale_factor`` × the current vCPU count.  Returns None when the
        VM is already at the top of its family or no option is within the scale
        limit.
        """
        self._load(subscription_id, region)
        cache_key = (subscription_id, region.lower())
        catalog = self._cache.get(cache_key, {})

        current_spec = catalog.get(current_sku)
        if not current_spec:
            return None

        family_prefix = _sku_family_prefix(current_sku)

        candidates: list[tuple[int, float, str]] = []
        for name, spec in catalog.items():
            if name == current_sku:
                continue
            if not name.lower().startswith(family_prefix.lower()):
                continue
            if spec.vcpus <= current_spec.vcpus:
                continue
            if spec.vcpus > current_spec.vcpus * max_scale_factor:
                continue
            # Require at least as much memory (don't trade memory for vCPUs)
            if spec.memory_gb < current_spec.memory_gb:
                continue
            candidates.append((spec.vcpus, spec.memory_gb, name))

        if not candidates:
            return None

        # Pick the smallest step up
        candidates.sort()
        return candidates[0][2]

    def find_newer_generation_sku(
        self,
        subscription_id: str,
        region: str,
        current_sku: str,
    ) -> tuple[str, int] | None:
        """Return ``(newer_sku_name, new_generation)`` or None if already latest.

        Matches on the SKU base name (everything except the _vN suffix) and
        requires identical vCPU count with memory within 5% tolerance.  Returns
        the highest available generation.
        """
        self._load(subscription_id, region)
        cache_key = (subscription_id, region.lower())
        catalog = self._cache.get(cache_key, {})

        current_spec = catalog.get(current_sku)
        if not current_spec:
            return None

        current_gen = _sku_generation_version(current_sku)
        if current_gen == 0:
            return None  # no _vN suffix — generation comparison not applicable

        base = _sku_base_name(current_sku).lower()
        mem_tol = max(current_spec.memory_gb * 0.05, 0.5)

        candidates: list[tuple[int, str]] = []  # (generation, sku_name)
        for name, spec in catalog.items():
            if _sku_base_name(name).lower() != base:
                continue
            gen = _sku_generation_version(name)
            if gen <= current_gen:
                continue
            if spec.vcpus != current_spec.vcpus:
                continue
            if abs(spec.memory_gb - current_spec.memory_gb) > mem_tol:
                continue
            candidates.append((gen, name))

        if not candidates:
            return None

        candidates.sort(reverse=True)
        best_gen, best_name = candidates[0]
        return best_name, best_gen

    def find_arm64_equivalent_sku(
        self,
        subscription_id: str,
        region: str,
        current_sku: str,
    ) -> str | None:
        """Return an ARM64 (Ampere Altra) SKU with the same shape, or None.

        ARM64 SKUs carry 'p' immediately after the family letter
        (e.g. Standard_Dps_v5, Standard_Eps_v5).  A match requires identical
        vCPUs and memory within 5% tolerance.  The highest available generation
        is returned when multiple matches exist.
        """
        self._load(subscription_id, region)
        cache_key = (subscription_id, region.lower())
        catalog = self._cache.get(cache_key, {})

        current_spec = catalog.get(current_sku)
        if not current_spec:
            return None

        mem_tol = max(current_spec.memory_gb * 0.05, 0.5)
        candidates: list[tuple[int, str]] = []
        for name, spec in catalog.items():
            if not _is_arm64_sku(name):
                continue
            if spec.vcpus != current_spec.vcpus:
                continue
            if abs(spec.memory_gb - current_spec.memory_gb) > mem_tol:
                continue
            candidates.append((_sku_generation_version(name), name))

        if not candidates:
            return None

        candidates.sort(reverse=True)
        return candidates[0][1]


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


_SKU_GEN_RE = re.compile(r"_v(\d+)$", re.IGNORECASE)
_ARM64_RE = re.compile(r"^Standard_[A-Z]p", re.IGNORECASE)


def _sku_generation_version(sku_name: str) -> int:
    """Extract the generation version number from a SKU name.

    Returns 0 for SKUs without a _vN suffix (e.g. Standard_B2ms).

    Examples:
      "Standard_D4s_v3"  → 3
      "Standard_E8as_v5" → 5
      "Standard_B2ms"    → 0
    """
    m = _SKU_GEN_RE.search(sku_name)
    return int(m.group(1)) if m else 0


def _sku_base_name(sku_name: str) -> str:
    """Strip the _vN generation suffix to produce the base name.

    Used for generation-swap matching: two SKUs that share the same base
    name but differ only in their _vN suffix are the same 'shape' in
    different generations.

    Examples:
      "Standard_D4s_v3"  → "Standard_D4s"
      "Standard_E8as_v5" → "Standard_E8as"
      "Standard_B2ms"    → "Standard_B2ms"
    """
    return _SKU_GEN_RE.sub("", sku_name)


def _is_arm64_sku(sku: str) -> bool:
    """Return True when *sku* is an ARM64 (Ampere Altra) SKU.

    ARM64 SKUs are identified by 'p' immediately after the family letter:
    Standard_Dps_v5, Standard_Dpds_v5, Standard_Eps_v5, etc.
    """
    return bool(_ARM64_RE.match(sku))


class OfflineSkuCatalog:
    """No-network SKU catalog for offline analysis (e.g., ``analyze`` command).

    All lookup methods return ``None``; the recommendation engine still emits
    findings — it just cannot name a specific target SKU.  Use this class when
    Azure credentials are unavailable (e.g., when running analysis on an
    already-exported JSON file).
    """

    def get(
        self,
        subscription_id: str,
        region: str,
        sku_name: str,
    ) -> None:
        return None

    def find_smaller_sku(
        self,
        subscription_id: str,
        region: str,
        current_sku: str,
        required_vcpus: int,
        required_memory_gb: float,
    ) -> None:
        return None

    def find_larger_sku(
        self,
        subscription_id: str,
        region: str,
        current_sku: str,
        max_scale_factor: float = 2.0,
    ) -> None:
        return None

    def find_newer_generation_sku(
        self,
        subscription_id: str,
        region: str,
        current_sku: str,
    ) -> None:
        return None

    def find_arm64_equivalent_sku(
        self,
        subscription_id: str,
        region: str,
        current_sku: str,
    ) -> None:
        return None
