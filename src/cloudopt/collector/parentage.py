"""Resolve managed-compute parentage for VMSS resource IDs via Resource Graph.

Given a set of VMSS resource IDs, this module issues Resource Graph queries to
detect whether each VMSS is owned by AKS, AVD, Databricks, Azure Batch, AML,
ARO, or HDInsight (SPEC §7.7).  Returns a mapping that the CLI wires into each
``VmInventory.parent_service_type / parent_service_id / …`` field.

Detection priority (highest first):
  1. AKS — managed RG pattern ``MC_*`` or tag ``aks-managed-cluster-name``
  2. AVD — VMSS referenced by a DesktopVirtualization/hostPool session host
  3. Databricks — VMSS in an RG with tag ``Vendor=Databricks`` or ``databricks-rg-*``
  4. Azure Batch — VMSS owned by Batch/batchAccounts/pools
  5. AML — VMSS owned by MachineLearningServices/workspaces/computes
  6. ARO — VMSS tagged ``kubernetes.io_cluster.<name>=owned`` + ARO cluster present
  7. HDInsight — VMSS owned by HDInsight/clusters
  8. Standalone VMSS — no owning service detected
"""

from __future__ import annotations

import logging
from typing import Any

from azure.core.exceptions import HttpResponseError
from azure.identity import DefaultAzureCredential
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions

from cloudopt.collector.throttle import ThrottleManager
from cloudopt.models import ParentServiceType

logger = logging.getLogger(__name__)

_MAX_SUBS_PER_QUERY = 10
_MAX_VMSS_PER_BATCH = 100

# Result type: vmss_resource_id → (service_type, service_id, service_name, pool_name)
ParentageResult = dict[str, tuple[ParentServiceType, str | None, str | None, str | None]]


def resolve_parent_services(
    credential: DefaultAzureCredential,
    subscriptions: list[Any],  # list[SubscriptionInfo] from collector.auth
    vmss_ids: list[str],
    throttle: ThrottleManager | None = None,
) -> ParentageResult:
    """Resolve managed-compute parentage for the given VMSS resource IDs.

    Args:
        credential: Azure credential for Resource Graph.
        subscriptions: Subscription list used to scope the queries.
        vmss_ids: Full resource IDs of VMSS objects to classify.
        throttle: Optional shared ThrottleManager (rate limiting).

    Returns:
        Mapping from vmss_id (lowercase) to
        ``(ParentServiceType, service_resource_id, service_name, pool_name)``.
        VMSS IDs absent from the result are Standalone VMs (no VMSS).
    """
    if not vmss_ids:
        return {}

    client = ResourceGraphClient(credential)
    sub_ids = [s.subscription_id for s in subscriptions]
    result: ParentageResult = {}

    # Normalise vmss_ids to lowercase for case-insensitive matching
    normalised = {vid.lower(): vid for vid in vmss_ids}
    remaining = set(normalised.keys())

    # Detection runs in priority order; once a VMSS is classified it is removed
    # from ``remaining`` so it won't be reclassified by a lower-priority detector.
    detectors = [
        _detect_aks,
        _detect_avd,
        _detect_databricks,
        _detect_batch,
        _detect_aml,
        _detect_aro,
        _detect_hdinsight,
    ]

    for detector in detectors:
        if not remaining:
            break
        try:
            batch_result = detector(
                client, sub_ids, list(remaining), throttle
            )
            for vmss_lower, entry in batch_result.items():
                result[normalised[vmss_lower]] = entry
                remaining.discard(vmss_lower)
        except Exception as exc:
            logger.warning("Parentage detector %s failed: %s", detector.__name__, exc)

    # Remaining VMSS IDs are Standalone VMSS (no managed-service owner found)
    for vmss_lower in remaining:
        result[normalised[vmss_lower]] = (
            ParentServiceType.STANDALONE_VMSS, None, None, None
        )

    return result


# ---------------------------------------------------------------------------
# Detector helpers
# ---------------------------------------------------------------------------

def _run_query(
    client: ResourceGraphClient,
    sub_ids: list[str],
    query: str,
    throttle: ThrottleManager | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(0, len(sub_ids), _MAX_SUBS_PER_QUERY):
        batch = sub_ids[i : i + _MAX_SUBS_PER_QUERY]
        if throttle:
            throttle.acquire(1)
        try:
            req = QueryRequest(
                subscriptions=batch,
                query=query,
                options=QueryRequestOptions(result_format="objectArray"),
            )
            resp = client.resources(req)
            rows.extend(resp.data or [])
        except HttpResponseError as exc:
            logger.warning("Resource Graph query failed: %s", exc)
    return rows


def _detect_aks(
    client: ResourceGraphClient,
    sub_ids: list[str],
    vmss_lower: list[str],
    throttle: ThrottleManager | None,
) -> ParentageResult:
    """AKS: VMSS in the AKS-managed node RG or tagged aks-managed-cluster-name.

    ARG join predicates only support 'and', not 'or', so the two detection
    paths (managed-RG and tag-based) are issued as separate queries and merged
    in Python.
    """
    vmss_set = set(vmss_lower)  # O(1) membership checks

    # --- path 1: VMSS lives in the cluster's managed node resource group ----
    query_rg = """
Resources
| where type =~ 'microsoft.containerservice/managedclusters'
| project clusterId = id, clusterName = name,
          managedRG = tolower(tostring(properties.nodeResourceGroup))
| join kind=inner (
    Resources
    | where type =~ 'microsoft.compute/virtualmachinescalesets'
    | project vmssId = tolower(id), vmssRG = tolower(resourceGroup),
              poolName = tostring(tags['aks-nodepool-name'])
) on $left.managedRG == $right.vmssRG
| project vmssId, clusterId, clusterName,
          poolName = iff(isempty(poolName), '', poolName)
"""

    # --- path 2: VMSS carries the aks-managed-cluster-name tag --------------
    query_tag = """
Resources
| where type =~ 'microsoft.containerservice/managedclusters'
| project clusterId = id, clusterName = name,
          clusterNameLower = tolower(name)
| join kind=inner (
    Resources
    | where type =~ 'microsoft.compute/virtualmachinescalesets'
    | where isnotempty(tags['aks-managed-cluster-name'])
    | project vmssId = tolower(id),
              poolName = tostring(tags['aks-nodepool-name']),
              clusterTag = tolower(tostring(tags['aks-managed-cluster-name']))
) on $left.clusterNameLower == $right.clusterTag
| project vmssId, clusterId, clusterName,
          poolName = iff(isempty(poolName), '', poolName)
"""

    result: ParentageResult = {}
    for query in (query_rg, query_tag):
        rows = _run_query(client, sub_ids, query, throttle)
        for row in rows:
            vmss_id = (row.get("vmssId") or "").lower()
            if vmss_id in vmss_set and vmss_id not in result:
                result[vmss_id] = (
                    ParentServiceType.AKS,
                    row.get("clusterId") or "",
                    row.get("clusterName") or "",
                    row.get("poolName") or None,
                )
    return result


def _detect_avd(
    client: ResourceGraphClient,
    sub_ids: list[str],
    vmss_lower: list[str],
    throttle: ThrottleManager | None,
) -> ParentageResult:
    """AVD: VMSS referenced by a DesktopVirtualization/hostPool session host."""
    query = """
Resources
| where type =~ 'microsoft.desktopvirtualization/hostpools'
| project hostPoolId = id, hostPoolName = name
| join kind=inner (
    Resources
    | where type =~ 'microsoft.compute/virtualmachinescalesets'
    | where isnotempty(tags['WVD-HostPool'])
    | project vmssId = tolower(id),
              hostPoolTag = tostring(tags['WVD-HostPool'])
) on $left.hostPoolName == $right.hostPoolTag
| project vmssId, hostPoolId, hostPoolName
"""
    rows = _run_query(client, sub_ids, query, throttle)
    result: ParentageResult = {}
    for row in rows:
        vmss_id = (row.get("vmssId") or "").lower()
        if vmss_id in vmss_lower:
            hp_id: str = row.get("hostPoolId") or ""
            hp_name: str = row.get("hostPoolName") or ""
            result[vmss_id] = (
                ParentServiceType.AVD, hp_id, hp_name, None
            )
    return result


def _detect_databricks(
    client: ResourceGraphClient,
    sub_ids: list[str],
    vmss_lower: list[str],
    throttle: ThrottleManager | None,
) -> ParentageResult:
    """Databricks: VMSS in RG with Vendor=Databricks tag or databricks-rg-* name."""
    query = """
Resources
| where type =~ 'microsoft.compute/virtualmachinescalesets'
| where tostring(tags['Vendor']) =~ 'Databricks'
      or resourceGroup startswith 'databricks-rg-'
| project vmssId = tolower(id),
          workspaceName = tostring(tags['DatabricksInstancePoolName']),
          poolName = tostring(tags['ClusterName']),
          workspaceId = tostring(tags['DatabricksInstanceGroupId'])
"""
    rows = _run_query(client, sub_ids, query, throttle)
    result: ParentageResult = {}
    for row in rows:
        vmss_id = (row.get("vmssId") or "").lower()
        if vmss_id in vmss_lower:
            ws_name: str = row.get("workspaceName") or ""
            pool_name: str | None = row.get("poolName") or None
            result[vmss_id] = (
                ParentServiceType.DATABRICKS, None, ws_name or None, pool_name
            )
    return result


def _detect_batch(
    client: ResourceGraphClient,
    sub_ids: list[str],
    vmss_lower: list[str],
    throttle: ThrottleManager | None,
) -> ParentageResult:
    """Azure Batch: VMSS owned by Batch/batchAccounts/pools."""
    query = """
Resources
| where type =~ 'microsoft.batch/batchaccounts/pools'
| extend vmssRef = tolower(tostring(properties.networkConfiguration.subnetId))
| project poolId = id, poolName = name,
          accountName = split(id, '/')[8],
          vmssRef
| join kind=inner (
    Resources
    | where type =~ 'microsoft.compute/virtualmachinescalesets'
    | project vmssId = tolower(id)
) on $left.vmssRef == $right.vmssId
| project vmssId, poolId, poolName = tostring(accountName)
"""
    rows = _run_query(client, sub_ids, query, throttle)
    result: ParentageResult = {}
    for row in rows:
        vmss_id = (row.get("vmssId") or "").lower()
        if vmss_id in vmss_lower:
            pool_id: str = row.get("poolId") or ""
            pool_name_val: str | None = row.get("poolName") or None
            result[vmss_id] = (
                ParentServiceType.AZURE_BATCH, pool_id, pool_name_val, None
            )
    return result


def _detect_aml(
    client: ResourceGraphClient,
    sub_ids: list[str],
    vmss_lower: list[str],
    throttle: ThrottleManager | None,
) -> ParentageResult:
    """AML: VMSS owned by MachineLearningServices/workspaces/computes."""
    query = """
Resources
| where type =~ 'microsoft.machinelearningservices/workspaces/computes'
| extend vmssId = tolower(tostring(properties.computeLocation))
| project computeId = id, computeName = name,
          workspaceName = split(id, '/')[8],
          vmssId
| join kind=inner (
    Resources
    | where type =~ 'microsoft.compute/virtualmachinescalesets'
    | project vmssId = tolower(id)
) on vmssId
| project vmssId, computeId, computeName = tostring(workspaceName)
"""
    rows = _run_query(client, sub_ids, query, throttle)
    result: ParentageResult = {}
    for row in rows:
        vmss_id = (row.get("vmssId") or "").lower()
        if vmss_id in vmss_lower:
            compute_id: str = row.get("computeId") or ""
            ws_name_aml: str | None = row.get("computeName") or None
            result[vmss_id] = (
                ParentServiceType.AML, compute_id, ws_name_aml, None
            )
    return result


def _detect_aro(
    client: ResourceGraphClient,
    sub_ids: list[str],
    vmss_lower: list[str],
    throttle: ThrottleManager | None,
) -> ParentageResult:
    """ARO: VMSS tagged kubernetes.io_cluster.<name>=owned + ARO cluster present."""
    query = """
Resources
| where type =~ 'microsoft.compute/virtualmachinescalesets'
| mv-expand tags
| where tags.key startswith 'kubernetes.io/cluster/'
      and tags.value =~ 'owned'
| extend aroClusterName = substring(tags.key, strlen('kubernetes.io/cluster/'))
| project vmssId = tolower(id), aroClusterName
| join kind=leftouter (
    Resources
    | where type =~ 'microsoft.redhatopenshift/openshiftclusters'
    | project aroId = id, aroName = name
) on $left.aroClusterName == $right.aroName
| project vmssId, aroId, aroName
"""
    rows = _run_query(client, sub_ids, query, throttle)
    result: ParentageResult = {}
    for row in rows:
        vmss_id = (row.get("vmssId") or "").lower()
        if vmss_id in vmss_lower:
            aro_id: str = row.get("aroId") or ""
            aro_name: str | None = row.get("aroName") or None
            result[vmss_id] = (
                ParentServiceType.ARO, aro_id or None, aro_name, None
            )
    return result


def _detect_hdinsight(
    client: ResourceGraphClient,
    sub_ids: list[str],
    vmss_lower: list[str],
    throttle: ThrottleManager | None,
) -> ParentageResult:
    """HDInsight: VMSS owned by HDInsight/clusters (via managed RG pattern)."""
    query = """
Resources
| where type =~ 'microsoft.hdinsight/clusters'
| project clusterId = id, clusterName = name,
          managedRG = tolower(tostring(properties.computeIsolationProperties))
| join kind=inner (
    Resources
    | where type =~ 'microsoft.compute/virtualmachinescalesets'
    | where tostring(tags['HDInsight-cluster-name']) != ''
    | project vmssId = tolower(id),
              hdiTag = tostring(tags['HDInsight-cluster-name']),
              nodeGroup = tostring(tags['HDInsight-role'])
) on $left.clusterName == $right.hdiTag
| project vmssId, clusterId, clusterName, nodeGroup
"""
    rows = _run_query(client, sub_ids, query, throttle)
    result: ParentageResult = {}
    for row in rows:
        vmss_id = (row.get("vmssId") or "").lower()
        if vmss_id in vmss_lower:
            cluster_id_hdi: str = row.get("clusterId") or ""
            cluster_name_hdi: str | None = row.get("clusterName") or None
            node_group: str | None = row.get("nodeGroup") or None
            result[vmss_id] = (
                ParentServiceType.HDINSIGHT, cluster_id_hdi or None, cluster_name_hdi, node_group
            )
    return result


def apply_parentage(
    vms: list[Any],  # list[VmInventory]
    parentage: ParentageResult,
) -> list[Any]:
    """Return new VmInventory objects with parentage fields applied.

    VMs whose ``vmss_id`` is not in *parentage* are left as-is
    (they remain ``Standalone``).
    """
    from cloudopt.models import ParentServiceType as PST

    updated: list[Any] = []
    for vm in vms:
        vmss_id = getattr(vm, "vmss_id", None)
        if vmss_id and vmss_id in parentage:
            svc_type, svc_id, svc_name, pool_name = parentage[vmss_id]
            updated.append(vm.model_copy(update={
                "parent_service_type": svc_type,
                "parent_service_id": svc_id,
                "parent_service_name": svc_name,
                "parent_pool_name": pool_name,
            }))
        elif vmss_id:
            # Has a VMSS ID but wasn't classified by any detector — already
            # Standalone VMSS by default, but set explicitly to be clear.
            updated.append(vm.model_copy(update={
                "parent_service_type": PST.STANDALONE_VMSS,
            }))
        else:
            updated.append(vm)
    return updated
