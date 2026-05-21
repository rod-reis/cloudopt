# CloudOpt ΓÇö Recommendation Catalog

> **Audience:** Delivery teams, FastTrack engineers, account leads using CloudOpt outputs with customers.
> **Purpose:** Reference for every recommendation CloudOpt emits ΓÇö what it detects, the rules it applies, the data it needs, and how much trust to place in it.
> **Version:** Generated from code at branch `rod-reis/legendary-succotash` (CloudFit-parity milestone).

---

## 1. How CloudOpt builds a recommendation

Every recommendation ("Finding") in CloudOpt has the same skeleton:

| Field | Meaning |
|---|---|
| **Code** | Stable identifier in the form `CAT-SUB-NNN` (e.g. `RSZ-DWN-001`) |
| **Category** | One of: `rightsize`, `swap`, `decom`, `cleanup`, `quota`, `crr` |
| **Current** | What the resource looks like today (SKU, size, state, ΓÇª) |
| **Proposed** | What CloudOpt recommends as the next step |
| **Rationale** | Plain-English explanation of *why*, including key metric values |
| **Confidence** | `HIGH` / `MEDIUM` / `LOW` (see ┬º2) |
| **Readiness** | `READY` (HIGH) ΓåÆ `LIKELY` (MEDIUM) ΓåÆ `INSUFFICIENT` (LOW) ΓåÆ `DISCOVERY` (candidate flags) |
| **Evidence sources** | Where the data came from (`arm-api`, `platform`, `datadog`, `os-agent`, ΓÇª) |
| **Blockers to high** | What's missing to upgrade the recommendation to HIGH confidence |

All metrics analyzed by CloudOpt are sampled at **PT1H (hourly) granularity** over a **user-configurable lookback window (1ΓÇô90 days, default 30)** via the `--metrics-days` flag.

---

## 2. Confidence model

CloudOpt scores every recommendation with one of three tiers. The rules are deterministic ΓÇö see `src/cloudopt/analyzer/confidence.py`.

| Tier | When it's assigned | What it means for the customer |
|---|---|---|
| **HIGH** | (a) The signal comes from an **authoritative** Azure ARM API (orphan, power state, quota, reservation), **or** (b) An OS-agent or workload-aware enrichment CSV (Datadog / Splunk / Dynatrace / VM Insights) was supplied alongside platform metrics. | Safe to action with normal change-control. |
| **MEDIUM** | The recommendation is **metric-dependent** (rightsize, SKU swap) but only Azure Monitor host-level data is available. The proxy memory metric (`Available Memory Bytes`) can be misleading. | Validate with the workload owner before actioning. |
| **LOW** | Used for **CRR** findings ΓÇö the snapshot collection cannot verify the "ΓëÑ 30 day" duration requirement. | Treat as a starting point for investigation. |

**Authoritative categories** ΓåÆ always HIGH:
- `CLEANUP` (orphaned resources ΓÇö confirmed by ARG)
- `QUOTA` (utilization comes from Quota API)
- `CRR` flagging logic itself (but duration assumption keeps these LOW ΓÇö see ┬º7)
- `DECOM` (power state and idle detection ΓÇö `DCM-IDL-001` becomes MEDIUM if only platform metrics are present)

**Metric-dependent categories** ΓåÆ MEDIUM by default, HIGH with OS/workload-aware enrichment:
- `RIGHTSIZE` (all `RSZ-*` codes)
- `SWAP` (all `SWP-*` codes)

> **Blocker-to-HIGH message** (delivered on every MEDIUM finding):
> *"Supply OS-level agent metrics (`os.cpu.percent` / `os.memory.used_percent`) via the canonical CSV export from Datadog / Splunk / Dynatrace / VM Insights to unlock HIGH confidence."*

---

## 3. Recommendation Catalog (by Category)

### 3.1 RIGHTSIZE ΓÇö keep the workload where it is, change the size

#### `RSZ-DWN-001` ΓÇö Right-size down (underutilized / oversized)
| | |
|---|---|
| **Trigger** | (a) **Underutilized**: avg CPU < 15% **and** memory < 20% over lookback window; or (b) **Oversized**: P95 CPU < 40% (target threshold depends on workload class ΓÇö see below) |
| **Suppression ΓÇö network-bound** | If outbound network utilization ΓëÑ **40 %** of SKU bandwidth, downsize is suppressed. |
| **User-facing classification** | Bursty workloads (CV of hourly CPU ΓëÑ 0.5 AND P95 ΓëÑ 2├ùavg) use the tighter **P95 Γëñ 40 %** target on the new SKU. Steady workloads use the relaxed **P95 Γëñ 80 %**. |
| **VMSS instance-count rule** | For VMSS groups, CloudOpt **prioritizes reducing instance count** over a SKU change (`ceil(total_cpu ├ù headroom / target_pct)`). Only recommends if at least 1 instance can be removed. |
| **Headroom** | Default `1.2├ù` multiplier on observed values when projecting onto the proposed SKU. |
| **Proposed SKU rules** | Same family/generation, strictly cheaper, supports same Accelerated Networking and Premium Storage capability, available in the VM's region. |
| **Confidence** | MEDIUM (platform) ΓåÆ HIGH (with os-agent or workload-aware enrichment) |
| **CloudFit parity** | Γ£à Logic 2 |

#### `RSZ-UPS-001` ΓÇö Right-size up (more CPU/RAM, same family) ΓÇö *registry only, no detector yet*
| | |
|---|---|
| **Status** | Reserved in the taxonomy; detector deferred. |

#### `RSZ-BSF-001` ΓÇö Burstable fit (D/E/F ΓåÆ B-series)
| | |
|---|---|
| **Trigger** | Current SKU in D, E, or F family **and** avg CPU below the B-series baseline for the target vCPU count **and** P95 CPU below 2├ù baseline. |
| **Suppression** | Skip if current SKU has **Accelerated Networking enabled** (B-series does not support AN). |
| **Credit check** | Long-run average credit accrual ΓëÑ credit consumption (simplified per-vCPU model). |
| **Confidence** | MEDIUM ΓåÆ HIGH with enrichment |
| **CloudFit parity** | Γ£à Logic 4 |

#### `RSZ-BSM-001` ΓÇö Burstable misfit (B-series over budget)
| | |
|---|---|
| **Trigger** | Current SKU is B-series **and** avg CPU exceeds the B-series baseline ΓåÆ credits will deplete ΓåÆ throttling risk. |
| **Proposed** | Same-vCPU non-burstable SKU in D/E/F family. |
| **Confidence** | MEDIUM ΓåÆ HIGH with enrichment |
| **CloudFit parity** | Γ£à Logic 4 (inverse direction) |

#### `RSZ-DSK-001` ΓÇö Disk over/undersize ΓÇö *registry only, no detector yet*
| | |
|---|---|
| **Status** | Reserved; CloudOpt does not yet evaluate managed-disk IOPS / size. |

---

### 3.2 SWAP ΓÇö move to a different SKU shape, generation, or architecture

#### `SWP-FAM-001` ΓÇö Family swap (e.g. compute-bound ΓåÆ F-series, memory-bound ΓåÆ E-series)
| | |
|---|---|
| **Trigger** | Sustained CPU pressure with low memory pressure ΓåÆ compute-bound (suggest F-series); or sustained memory pressure with moderate CPU ΓåÆ memory-bound (suggest E-series); or low both with general workload (suggest D/Dasv6). |
| **Confidence** | MEDIUM ΓåÆ HIGH with enrichment. Workload namespace (JVM/.NET/SQL) is added to evidence when available. |

#### `SWP-LFC-001` ΓÇö Lifecycle / retiring SKU
| | |
|---|---|
| **Trigger** | Current SKU is on the legacy / retiring list (e.g. Dv2, Av2, Standard_A/D/G original). |
| **Proposed** | Modern replacement (same shape, current generation). |
| **Confidence** | **HIGH** (lifecycle is authoritative ΓÇö Azure-published retirement) |

#### `SWP-DSK-001` ΓÇö Diskless SKU recommendation
| | |
|---|---|
| **Trigger** | SKU in D/E/F family (v1ΓÇôv5) **and** temp-disk peak IOPS utilization < 5 % **and** temp-disk peak throughput utilization < 5 % over the lookback window. |
| **Suppression** | Skip if no temp-disk telemetry is available (absence Γëá unused). Skip if family not eligible. |
| **Capacity fallback** | When SKU catalog does not expose temp-disk limits, conservative defaults of **3,200 IOPS / 25 MB/s** are used (Standard local SSD). This biases the check toward *fewer* false positives. |
| **Confidence** | MEDIUM ΓåÆ HIGH with enrichment |
| **CloudFit parity** | Γ£à Logic 3 |

#### `SWP-GEN-001` ΓÇö Generation swap (vN ΓåÆ vN+k) ΓÇö *registry only*
| | |
|---|---|
| **Status** | Reserved; SWP-LFC-001 covers explicit retirements today. |

#### `SWP-DST-001` ΓÇö Disk tier swap (Premium ΓåÆ Standard) ΓÇö *registry only* |
| | |
|---|---|
| **Status** | Reserved. |

#### `SWP-ARC-001` ΓÇö Architecture (x64 ΓåÆ ARM64) ΓÇö **candidate, flag-only**
| | |
|---|---|
| **Trigger** | An ARM64 SKU with the same shape exists. |
| **Type** | `CANDIDATE` ΓÇö surfaced for discovery only, never auto-prescribed (requires binary-compatibility validation). |

---

### 3.3 DECOM ΓÇö stop paying for the workload

#### `DCM-IDL-001` ΓÇö Idle running VM
| | |
|---|---|
| **Trigger (all must hold)** | ΓÇó P95 of CPU < **3 %** over lookback window<br>ΓÇó P100 of CPU last **3 days** Γëñ **2 %**<br>ΓÇó Outbound network utilization < **2 %** of SKU bandwidth (only checked when bandwidth catalog data is present) |
| **Suppression** | Skip if VM is not in `running` state (DCM-STP-001 handles stopped). |
| **Confidence** | MEDIUM ΓåÆ HIGH with enrichment |
| **CloudFit parity** | Γ£à Logic 1 |

#### `DCM-STP-001` ΓÇö Stopped-allocated (still billed)
| | |
|---|---|
| **Trigger** | `power_state` is `stopped` or `deallocated` ΓÇö for longer than the configured N days when activity-log lookback is available. |
| **Confidence** | **HIGH** (authoritative ARM signal) |

#### `DCM-DLC-001` ΓÇö Deallocated-stale (opt-in)
| | |
|---|---|
| **Trigger** | Deallocated, lower-environment tag (`dev`/`test`/`qa`), large vCPU count. |
| **Enable** | Requires explicit `--enable-dlc` flag. |
| **Confidence** | HIGH |

#### `DCM-ENV-001` ΓÇö Missing environment tag (opt-in)
| | |
|---|---|
| **Trigger** | VM has no `environment` annotation. |
| **Enable** | Requires explicit `--enable-env-check` flag. |
| **Confidence** | HIGH (factual ΓÇö tag is or isn't there) |

---

### 3.4 CLEANUP ΓÇö orphaned, no-longer-attached resources

All cleanup recommendations are **HIGH confidence** (sourced directly from Azure Resource Graph).

#### `CLN-DSK-001` ΓÇö Unattached managed disk
| | |
|---|---|
| **Trigger** | `managed_by` is empty **and** disk has been unattached for ΓëÑ **30 days** (per `properties.timeCreated`). |
| **Edge case** | If `timeCreated` is null/unparseable, the finding is still emitted but with an *"age unconfirmed"* note. |
| **CloudFit parity** | Γ£à Logic 5 |

#### `CLN-NIC-001` ΓÇö Unattached network interface
| | |
|---|---|
| **Trigger** | `managed_by` is empty. |

#### `CLN-PIP-001` ΓÇö Unassociated public IP
| | |
|---|---|
| **Trigger** | Not bound to any NIC, load balancer, or gateway. |

#### `CLN-SNP-001` ΓÇö Unused snapshot
| | |
|---|---|
| **Trigger** | All snapshots are surfaced for review (no time-based filter in the current build). |

#### `CLN-RGP-001` ΓÇö Empty resource group ΓÇö *registry only*
| | |
|---|---|
| **Status** | Requires complete resource-group list which isn't yet in the `AzureResource` collection model. Deferred. |

---

### 3.5 QUOTA ΓÇö request increases / consolidate / rightsize quota

Thresholds default to: **oversized** < 20 %, **warning** 70ΓÇô85 %, **critical** > 85 %. Window: 30-day max.

| Code | Trigger | Confidence |
|---|---|---|
| **`QTA-OVR-001`** | 30-day max utilization < 20 % **and** quota exceeds Azure default ΓåÆ reduction candidate | HIGH |
| **`QTA-WRN-001`** | 30-day max utilization 70ΓÇô85 % ΓåÆ plan a future quota increase | HIGH |
| **`QTA-CRI-001`** | Utilization > 85 % ΓåÆ request **individual** quota increase | HIGH |
| **`QTA-CRG-001`** | Utilization > 85 % **and** a donor subscription (< 40 % util) exists in the same region/SKU ΓåÆ **groupable** quota consolidation | HIGH |

---

### 3.6 CRR ΓÇö Capacity Reservation Groups

Both CRR findings are **LOW** confidence ΓÇö snapshot collection cannot verify the "ΓëÑ 30 days" requirement.

| Code | Trigger | Confidence |
|---|---|---|
| **`CRR-UNU-001`** | CRG with 0 associated VMs | LOW (duration assumption blocker) |
| **`CRR-UNF-001`** | CRG with `reservedCount > usedCount` | LOW (duration assumption blocker) |

---

## 4. Recommendation Summary Table

| Code | Category | What it does | Default confidence | CloudFit parity |
|---|---|---|---|---|
| RSZ-DWN-001 | rightsize | Smaller SKU when CPU/mem low | MEDIUM | Γ£à Logic 2 |
| RSZ-UPS-001 | rightsize | Larger SKU under sustained pressure | ΓÇö | (reserved) |
| RSZ-BSF-001 | rightsize | D/E/F ΓåÆ B-series when bursty/low | MEDIUM | Γ£à Logic 4 |
| RSZ-BSM-001 | rightsize | B-series ΓåÆ standard when over budget | MEDIUM | Γ£à Logic 4 inv. |
| RSZ-DSK-001 | rightsize | Disk over/undersize | ΓÇö | (reserved) |
| SWP-GEN-001 | swap | Newer generation, same family | ΓÇö | (reserved) |
| SWP-FAM-001 | swap | Family swap by workload profile | MEDIUM | (CloudOpt-only) |
| SWP-LFC-001 | swap | Retiring SKU ΓåÆ modern replacement | HIGH | (CloudOpt-only) |
| SWP-DST-001 | swap | Premium SSD ΓåÆ Standard SSD | ΓÇö | (reserved) |
| SWP-DSK-001 | swap | Diskful ΓåÆ Diskless SKU | MEDIUM | Γ£à Logic 3 |
| SWP-ARC-001 | swap | x64 ΓåÆ ARM64 candidate flag | DISCOVERY | (CloudOpt-only) |
| DCM-IDL-001 | decom | Idle running VM | MEDIUM | Γ£à Logic 1 |
| DCM-STP-001 | decom | Stopped-allocated (still billed) | HIGH | (CloudOpt-only) |
| DCM-DLC-001 | decom | Lower-env oversized (opt-in) | HIGH | (CloudOpt-only) |
| DCM-ENV-001 | decom | Missing env tag (opt-in) | HIGH | (CloudOpt-only) |
| CLN-DSK-001 | cleanup | Unattached disk ΓëÑ 30 days | HIGH | Γ£à Logic 5 |
| CLN-NIC-001 | cleanup | Unattached NIC | HIGH | (CloudOpt-only) |
| CLN-PIP-001 | cleanup | Unassociated public IP | HIGH | (CloudOpt-only) |
| CLN-SNP-001 | cleanup | Snapshot review | HIGH | (CloudOpt-only) |
| CLN-RGP-001 | cleanup | Empty resource group | ΓÇö | (reserved) |
| QTA-OVR-001 | quota | Quota oversized (< 20 % util) | HIGH | (CloudOpt-only) |
| QTA-WRN-001 | quota | Quota warning (70ΓÇô85 %) | HIGH | (CloudOpt-only) |
| QTA-CRI-001 | quota | Quota critical ΓÇö individual | HIGH | (CloudOpt-only) |
| QTA-CRG-001 | quota | Quota critical ΓÇö groupable | HIGH | (CloudOpt-only) |
| CRR-UNU-001 | crr | Capacity Reservation Group unused | LOW | (CloudOpt-only) |
| CRR-UNF-001 | crr | CRG underfilled | LOW | (CloudOpt-only) |

**Implemented & active:** 18 recommendations + 1 candidate.
**Reserved (taxonomy only):** 5 ΓÇö `RSZ-UPS-001`, `RSZ-DSK-001`, `SWP-GEN-001`, `SWP-DST-001`, `CLN-RGP-001`.

---

## 5. Data the engine relies on

| Data source | Used by | Notes |
|---|---|---|
| **Azure Resource Graph (ARG)** | All categories | Inventory, power state, `managed_by`, `properties.timeCreated` |
| **Azure Monitor ΓÇö PT1H** | rightsize, swap (non-lifecycle), decom (idle), diskless | `Percentage CPU`, `Available Memory Bytes`, `Network Out Total`, `Temp Disk Read/Write Operations/Sec`, `Temp Disk Read/Write Bytes/sec` |
| **Compute resource_skus API** | All SKU-comparing detectors | vCPU, memory, **network bandwidth (Mbps)**, **accelerated networking flag**, region availability |
| **Azure Quota API** | quota | 30-day max utilization, Azure default limits |
| **Capacity Reservation Groups API** | crr | reservedCount, usedCount |
| **(Optional) OS-agent CSV** | rightsize, swap | `os.cpu.percent`, `os.memory.used_percent` ΓÇö required to upgrade MEDIUM ΓåÆ HIGH |
| **(Optional) Workload-aware CSV** | swap (family) | `jvm.*`, `dotnet.*`, `sql.*` namespaces ΓÇö adds workload evidence |

---

## 6. How to talk about these with customers

1. **Lead with HIGH-confidence cleanup and quota recommendations.** They are sourced directly from Azure APIs and don't depend on monitoring quality.
2. **For rightsize and swap recommendations, set expectations.** If the customer hasn't supplied an OS-agent or workload-aware CSV, every `RSZ-*` and `SWP-FAM-001` finding will be **MEDIUM** with an explicit blocker message. This is by design ΓÇö Azure's host-level `Available Memory Bytes` proxy is unreliable.
3. **Burstable and diskless are net-new vs. CloudFit feature parity.** Highlight `RSZ-BSF-001` and `SWP-DSK-001` as additional savings opportunities that customers might miss in Azure Advisor.
4. **CRR findings are LOW by design.** They are a starting point for the FinOps team to validate against billing data ΓÇö CloudOpt cannot verify the 30-day duration from a single snapshot.
5. **Discovery-only candidates** (`SWP-ARC-001`) should be framed as "worth investigating" ΓÇö never as a prescribed action.

---

## 7. Known gaps vs. CloudFit (deliberate or deferred)

| CloudFit feature | CloudOpt status | Notes |
|---|---|---|
| 30-minute (PT30M) metric granularity | Currently PT1H | PT30M would 2├ù data volume; PT1H is a deliberate trade-off |
| Service Fabric / AKS reliability-tier awareness in VMSS recs | Not implemented | VMSS instance-count rec is generic |
| Diskless temp-disk *size* utilization check | Not implemented | Azure Monitor doesn't emit temp-disk size-used as a metric ΓÇö IOPS + throughput are used as proxies |
| Per-hour B-series credit simulation | Long-run avg only | Simplified model; conservative |
| App Service Plan orphan detection | Not in scope | Explicitly deferred |
| DDoS Protection Plan orphan detection | Not in scope | Explicitly deferred |

---

*Document generated from source-of-truth code: `src/cloudopt/analyzer/detectors/`, `src/cloudopt/analyzer/taxonomy.py`, `src/cloudopt/analyzer/confidence.py`.*
