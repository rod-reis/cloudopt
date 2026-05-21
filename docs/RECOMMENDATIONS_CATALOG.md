# CloudOpt -- Recommendation Catalog

> **Audience:** Delivery teams, FastTrack engineers, account leads using CloudOpt outputs with customers.
> **Purpose:** Reference for every recommendation CloudOpt emits -- what it detects, the rules it applies, the data it needs, and how much trust to place in it.
> **Product framing:** CloudOpt is a **Cloud Efficiency** tool focused on **Performance**, **Capacity**, and **Resiliency**. Every recommendation below is framed around fit, headroom, and posture -- not savings. Cost is a tiebreaker among performance-equivalent options, never the goal.

---

## 1. How CloudOpt builds a recommendation

Every recommendation ("Finding") in CloudOpt has the same skeleton:

| Field | Meaning |
|---|---|
| **Code** | Stable identifier in the form `CAT-SUB-NNN` (e.g. `RSZ-DWN-001`) |
| **Category** | One of: `rightsize`, `swap`, `decom`, `cleanup`, `quota`, `crr` |
| **Current** | What the resource looks like today (SKU, size, state, ...) |
| **Proposed** | What CloudOpt recommends as the next step |
| **Rationale** | Plain-English explanation of *why*, including key metric values |
| **Confidence** | `HIGH` / `MEDIUM` / `LOW` (see Section 2) |
| **Readiness** | `READY` (HIGH) -> `LIKELY` (MEDIUM) -> `INSUFFICIENT` (LOW) -> `DISCOVERY` (candidate flags) |
| **Evidence sources** | Where the data came from (`arm-api`, `platform`, `datadog`, `os-agent`, ...) |
| **Blockers to high** | What's missing to upgrade the recommendation to HIGH confidence |

All metrics analyzed by CloudOpt are sampled at **PT1H (hourly) granularity** over a **user-configurable lookback window (1-90 days, default 30)** via the `--metrics-days` flag.

---

## 2. Confidence model

CloudOpt scores every recommendation with a **numeric confidence score (0–100)** and a derived tier. The scoring is deterministic — see `src/cloudopt/analyzer/detectors/__init__.py`.

| Score | Tier | Readiness | What it means |
|---|---|---|---|
| **≥ 80** | **HIGH** | **READY** | Safe to action with normal change control. |
| **50–79** | **MEDIUM** | **LIKELY** | Validate with the workload owner before actioning. |
| **< 50** | **LOW** | **INSUFFICIENT** | Treat as a starting point for investigation. |

**Score formula:**
```
score = base_score
      + memory_quality_bonus   (0–15, based on memory data source quality)
      + coverage_bonus         (0–10, based on metric time-series coverage %)
      + corroboration_bonus    (0–20, 10 per extra data source beyond platform)
      + stability_bonus        (0–5, no recent config change detected)
      - change_impact_penalty  (0–20, high blast-radius proposed changes)
```

**Base scores by category:**

| Category | Base | Notes |
|---|---|---|
| CLEANUP (orphans — ARG) | 90 | Authoritative, no metrics needed |
| QUOTA (Quota API) | 90 | Authoritative, no metrics needed |
| CRR (Capacity Reservation API) | 70 | Snapshot only; duration unverifiable |
| DECOM non-idle (power state) | 90 | Authoritative ARM signal |
| DCM-IDL-001 (idle detection) | 70 | Metric-dependent; can be elevated |
| RSZ-*, SWP-* | 65 | Metric-dependent; elevated by enrichment |
| QTA-OPS-001 | 90 | Authoritative; missing alerts are factual |

**Corroboration sources that boost score (+10 each, max +20):**
- OS-agent or workload-aware CSV (Datadog / Splunk / Dynatrace / VM Insights)
- Workload archetype corroboration (e.g., `bursty` archetype confirming `RSZ-BSF-001`)
- App Insights SLO data (availability p99 ≥ 99.9%)

> **To unlock HIGH confidence on RSZ/SWP findings:** supply `os.cpu.used_percent` and `os.memory.used_percent` via the customer data CSV (see SPEC.md §5). Without OS-agent data, platform-only metrics cap these findings at MEDIUM.

**Dashboard readiness filter reference:**

| Readiness | Score | Suggested action |
|---|---|---|
| READY | ≥ 80 | Schedule with normal change control |
| LIKELY | 50–79 | Validate metrics + workload context first |
| INSUFFICIENT | < 50 | Investigate; collect more data |
| DISCOVERY | n/a | Candidate flag — human evaluation required |

---

## 3. Recommendation Catalog (by Category)

### 3.1 RIGHTSIZE -- keep the workload where it is, change the size

#### `RSZ-DWN-001` -- Right-size down (underutilized / oversized)
| | |
|---|---|
| **Trigger** | (a) **Underutilized**: avg CPU < 15% **and** memory < 20% over lookback window; or (b) **Oversized**: P95 CPU < 40% (target threshold depends on workload class -- see below) |
| **Suppression -- network-bound** | If outbound network utilization >= **40%** of SKU bandwidth, downsize is suppressed. |
| **User-facing classification** | Bursty workloads (CV of hourly CPU >= 0.5 AND P95 >= 2x avg) use the tighter **P95 <= 40%** target on the new SKU. Steady workloads use the relaxed **P95 <= 80%**. |
| **VMSS instance-count rule** | For VMSS groups, CloudOpt **prioritizes reducing instance count** over a SKU change (`ceil(total_cpu * headroom / target_pct)`). Only recommends if at least 1 instance can be removed. |
| **Headroom** | Default `1.2x` multiplier on observed values when projecting onto the proposed SKU. |
| **Proposed SKU rules** | Same family/generation, smallest SKU that preserves the performance headroom targets above, supports same Accelerated Networking and Premium Storage capability, available in the VM's region. (Cost is a tiebreaker among performance-equivalent candidates, not the selector.) |
| **Confidence** | MEDIUM (platform) -> HIGH (with os-agent or workload-aware enrichment) |

#### `RSZ-UPS-001` -- Right-size up (more vCPU / memory, same family)
| | |
|---|---|
| **Trigger** | `os.cpu.used_percent` P95 ≥ 85% **or** `os.memory.used_percent` P95 ≥ 85% sustained over the lookback window. **Gated to HIGH only** — suppressed entirely if OS-agent enrichment data is absent (platform-only metrics are insufficient to safely prescribe an upsize). |
| **Proposed SKU rules** | Same family/generation, smallest SKU that resolves the saturation. Supports same Accelerated Networking and Premium Storage capability, available in the VM's region. |
| **Confidence** | HIGH (OS-agent data required; finding is suppressed at MEDIUM or LOW) |

#### `RSZ-BSF-001` -- Burstable fit (D/E/F -> B-series)
| | |
|---|---|
| **Trigger** | Current SKU in D, E, or F family **and** avg CPU below the B-series baseline for the target vCPU count **and** P95 CPU below 2x baseline. |
| **Suppression** | Skip if current SKU has **Accelerated Networking enabled** (B-series does not support AN). |
| **Credit check** | Long-run average credit accrual >= credit consumption (simplified per-vCPU model). |
| **Confidence** | MEDIUM -> HIGH with enrichment |

#### `RSZ-BSM-001` -- Burstable misfit (B-series over budget)
| | |
|---|---|
| **Trigger** | Current SKU is B-series **and** avg CPU exceeds the B-series baseline -> credits will deplete -> throttling risk. |
| **Proposed** | Same-vCPU non-burstable SKU in D/E/F family. |
| **Confidence** | MEDIUM -> HIGH with enrichment |

#### `RSZ-DSK-001` -- Disk over/undersize
| | |
|---|---|
| **Trigger** | OS disk or data disk is provisioned significantly above or below the IOPS / throughput actually used over the lookback window, based on Azure Monitor disk metrics. |
| **Proposed** | Right-sized disk tier (e.g. P30 → P20) or disk size that covers observed utilization with headroom. |
| **Confidence** | MEDIUM → HIGH with OS-agent disk metrics |

---

### 3.2 SWAP -- move to a different SKU shape, generation, or architecture

#### `SWP-FAM-001` -- Family swap (e.g. compute-bound -> F-series, memory-bound -> E-series)
| | |
|---|---|
| **Trigger** | Sustained CPU pressure with low memory pressure -> compute-bound (suggest F-series); or sustained memory pressure with moderate CPU -> memory-bound (suggest E-series); or low both with general workload (suggest D/Dasv6). |
| **Confidence** | MEDIUM -> HIGH with enrichment. Workload namespace (JVM/.NET/SQL) is added to evidence when available. |

#### `SWP-LFC-001` -- Lifecycle / retiring SKU
| | |
|---|---|
| **Trigger** | Current SKU is on the legacy / retiring list (e.g. Dv2, Av2, Standard_A/D/G original). |
| **Proposed** | Modern replacement (same shape, current generation). |
| **Confidence** | **HIGH** (lifecycle is authoritative -- Azure-published retirement) |

#### `SWP-DSK-001` -- Diskless SKU recommendation
| | |
|---|---|
| **Trigger** | SKU in D/E/F family (v1-v5) **and** temp-disk peak IOPS utilization < 5% **and** temp-disk peak throughput utilization < 5% over the lookback window. |
| **Suppression** | Skip if no temp-disk telemetry is available (absence != unused). Skip if family not eligible. |
| **Capacity fallback** | When SKU catalog does not expose temp-disk limits, conservative defaults of **3,200 IOPS / 25 MB/s** are used (Standard local SSD). This biases the check toward *fewer* false positives. |
| **Confidence** | MEDIUM -> HIGH with enrichment |

#### `SWP-GEN-001` -- Generation swap (vN → vN+k)
| | |
|---|---|
| **Trigger** | Current SKU is ≥ 2 generations behind the latest available in the same family in the VM's region (e.g. D8s_v3 → D8s_v6). Latest generation is determined from the SKU catalog at collection time. |
| **Proposed** | Same vCPU/memory shape, latest generation. |
| **Note** | SWP-LFC-001 handles explicit Azure-published retirements. SWP-GEN-001 fires proactively for non-retiring-but-aging SKUs. |
| **Confidence** | HIGH (catalog data is authoritative) |

#### `SWP-DST-001` -- Disk tier swap (Premium → Standard)
| | |
|---|---|
| **Trigger** | OS or data disk is on Premium SSD **and** observed IOPS + throughput over the lookback window are well within the Standard SSD tier limits for the same disk size. |
| **Suppression** | Skip if the VM's SKU requires Premium Storage (e.g. `M` or `L` series VMs with Premium-required flag). |
| **Confidence** | MEDIUM → HIGH with disk IOPS data from OS agent |

#### `SWP-ARC-001` -- Architecture (x64 -> ARM64) -- **candidate, flag-only**
| | |
|---|---|
| **Trigger** | An ARM64 SKU with the same shape exists. |
| **Type** | `CANDIDATE` -- surfaced for discovery only, never auto-prescribed (requires binary-compatibility validation). |

---

### 3.3 DECOM -- stop paying for the workload

#### `DCM-IDL-001` -- Idle running VM
| | |
|---|---|
| **Trigger (all must hold)** | - P95 of CPU < **3%** over lookback window<br>- P100 of CPU last **3 days** <= **2%**<br>- Outbound network utilization < **2%** of SKU bandwidth (only checked when bandwidth catalog data is present) |
| **Suppression** | Skip if VM is not in `running` state (DCM-STP-001 handles stopped). |
| **Confidence** | MEDIUM -> HIGH with enrichment |

#### `DCM-STP-001` -- Stopped-allocated (still billed)
| | |
|---|---|
| **Trigger** | `power_state` is `stopped` or `deallocated` -- for longer than the configured N days when activity-log lookback is available. |
| **Confidence** | **HIGH** (authoritative ARM signal) |

#### `DCM-DLC-001` -- Deallocated-stale (opt-in)
| | |
|---|---|
| **Trigger** | Deallocated, lower-environment tag (`dev`/`test`/`qa`), large vCPU count. |
| **Enable** | Requires explicit `--enable-dlc` flag. |
| **Confidence** | HIGH |

#### `DCM-ENV-001` -- Missing environment tag (opt-in)
| | |
|---|---|
| **Trigger** | VM has no `environment` annotation. |
| **Enable** | Requires explicit `--enable-env-check` flag. |
| **Confidence** | HIGH (factual -- tag is or isn't there) |

---

### 3.4 CLEANUP -- orphaned, no-longer-attached resources

All cleanup recommendations are **HIGH confidence** (sourced directly from Azure Resource Graph).

#### `CLN-DSK-001` -- Unattached managed disk
| | |
|---|---|
| **Trigger** | `managed_by` is empty **and** disk has been unattached for >= **30 days** (per `properties.timeCreated`). |
| **Edge case** | If `timeCreated` is null/unparseable, the finding is still emitted but with an *"age unconfirmed"* note. |

#### `CLN-NIC-001` -- Unattached network interface
| | |
|---|---|
| **Trigger** | `managed_by` is empty. |

#### `CLN-PIP-001` -- Unassociated public IP
| | |
|---|---|
| **Trigger** | Not bound to any NIC, load balancer, or gateway. |

#### `CLN-SNP-001` -- Unused snapshot
| | |
|---|---|
| **Trigger** | All snapshots are surfaced for review (no time-based filter in the current build). |

#### `CLN-RGP-001` -- Empty resource group
| | |
|---|---|
| **Trigger** | Resource group contains zero resources (VMs, disks, NICs, public IPs, or storage accounts). |
| **Proposed** | Delete the resource group. |
| **Confidence** | **HIGH** (sourced directly from Azure Resource Graph) |

---

### 3.5 QUOTA -- request increases / consolidate / rightsize quota

Thresholds default to: **oversized** < 20%, **warning** 70-85%, **critical** > 85%. Window: 30-day max.

| Code | Trigger | Confidence |
|---|---|---|
| **`QTA-OVR-001`** | 30-day max utilization < 20% **and** quota exceeds Azure default -> reduction candidate | HIGH |
| **`QTA-WRN-001`** | 30-day max utilization 70-85% -> plan a future quota increase | HIGH |
| **`QTA-CRI-001`** | Utilization > 85% -> request **individual** quota increase | HIGH |
| **`QTA-CRG-001`** | Utilization > 85% **and** a donor subscription (< 40% util) exists in the same region/SKU -> **groupable** quota consolidation | HIGH |

#### `QTA-OPS-001` -- Capacity Operations Hygiene
| | |
|---|---|
| **Trigger** | One or more of 5 monitoring sub-checks fails for a subscription. See table below. |
| **Scope** | Emitted **once per subscription**, not per VM. `vm_id` field carries the subscription resource path. |
| **Confidence** | **HIGH / READY** (confidence_score = 90). Missing alerts are a factual, authoritative observation. |
| **Sub-checks** | A: vCPU quota utilization metric alert • B: AllocationFailed/SkuNotAvailable activity-log alert • C: QuotaExceeded activity-log alert • D: CRG utilization alert *(only when CRGs exist)* • E: Service Health alert for Compute |
| **deltas** | `{"subchecks": [{"label": "A: Quota alert", "pass": false, "why": "No metric alert found"}]}` |
| **Prerequisite** | Requires `cloudopt collect` to run with alert collection enabled (on by default). If the scorecard is empty, re-run `cloudopt collect` without `[collect_alerts] false` in the scope file. |

---

### 3.6 CRR -- Capacity Reservation Groups

Both CRR findings are **LOW** confidence -- snapshot collection cannot verify the ">= 30 days" requirement.

| Code | Trigger | Confidence |
|---|---|---|
| **`CRR-UNU-001`** | CRG with 0 associated VMs | LOW (duration assumption blocker) |
| **`CRR-UNF-001`** | CRG with `reservedCount > usedCount` | LOW (duration assumption blocker) |

---

## 4. Recommendation Summary Table

| Code | Category | What it does | Default confidence |
|---|---|---|---|
| RSZ-DWN-001 | rightsize | Smaller SKU when CPU/mem low | MEDIUM |
| RSZ-UPS-001 | rightsize | Larger SKU under sustained pressure | HIGH (OS-agent required) |
| RSZ-BSF-001 | rightsize | D/E/F -> B-series when bursty/low | MEDIUM |
| RSZ-BSM-001 | rightsize | B-series -> standard when over budget | MEDIUM |
| RSZ-DSK-001 | rightsize | Disk over/undersize | MEDIUM |
| SWP-GEN-001 | swap | Newer generation, same family | HIGH |
| SWP-FAM-001 | swap | Family swap by workload profile | MEDIUM |
| SWP-LFC-001 | swap | Retiring SKU -> modern replacement | HIGH |
| SWP-DST-001 | swap | Premium SSD -> Standard SSD | MEDIUM |
| SWP-DSK-001 | swap | Diskful -> Diskless SKU | MEDIUM |
| SWP-ARC-001 | swap | x64 -> ARM64 candidate flag | DISCOVERY |
| DCM-IDL-001 | decom | Idle running VM | MEDIUM |
| DCM-STP-001 | decom | Stopped-allocated (still billed) | HIGH |
| DCM-DLC-001 | decom | Lower-env oversized (opt-in) | HIGH |
| DCM-ENV-001 | decom | Missing env tag (opt-in) | HIGH |
| CLN-DSK-001 | cleanup | Unattached disk >= 30 days | HIGH |
| CLN-NIC-001 | cleanup | Unattached NIC | HIGH |
| CLN-PIP-001 | cleanup | Unassociated public IP | HIGH |
| CLN-SNP-001 | cleanup | Snapshot review | HIGH |
| CLN-RGP-001 | cleanup | Empty resource group | HIGH |
| QTA-OVR-001 | quota | Quota oversized (< 20% util) | HIGH |
| QTA-WRN-001 | quota | Quota warning (70-85%) | HIGH |
| QTA-CRI-001 | quota | Quota critical -- individual | HIGH |
| QTA-CRG-001 | quota | Quota critical -- groupable | HIGH |
| QTA-OPS-001 | quota | Capacity ops hygiene (per subscription) | HIGH |
| CRR-UNU-001 | crr | Capacity Reservation Group unused | LOW |
| CRR-UNF-001 | crr | CRG underfilled | LOW |

**Implemented & active:** 25 recommendations + 1 candidate (26 codes total).

---

## 5. Data the engine relies on

| Data source | Used by | Notes |
|---|---|---|
| **Azure Resource Graph (ARG)** | All categories | Inventory, power state, `managed_by`, `properties.timeCreated` |
| **Azure Monitor -- PT1H** | rightsize, swap (non-lifecycle), decom (idle), diskless | `Percentage CPU`, `Available Memory Bytes`, `Network Out Total`, `Temp Disk Read/Write Operations/Sec`, `Temp Disk Read/Write Bytes/sec` |
| **Compute resource_skus API** | All SKU-comparing detectors | vCPU, memory, **network bandwidth (Mbps)**, **accelerated networking flag**, region availability |
| **Azure Quota API** | quota | 30-day max utilization, Azure default limits |
| **Capacity Reservation Groups API** | crr | reservedCount, usedCount |
| **(Optional) OS-agent CSV** | rightsize, swap | `os.cpu.percent`, `os.memory.used_percent` -- required to upgrade MEDIUM -> HIGH |
| **(Optional) Workload-aware CSV** | swap (family) | `jvm.*`, `dotnet.*`, `sql.*` namespaces -- adds workload evidence |

---

## 6. How to talk about these with customers

CloudOpt is a **Cloud Efficiency** tool. Frame findings around **performance fit, capacity posture, and resiliency**, not savings.

1. **Lead with HIGH-confidence cleanup and quota recommendations.** They are sourced directly from Azure APIs and don't depend on monitoring quality. Cleanup matters because orphans distort capacity planning and operational hygiene; quota matters because exhausted quota blocks deployments and scale events.
2. **For rightsize and swap recommendations, set expectations.** If the customer hasn't supplied an OS-agent or workload-aware CSV, every `RSZ-*` and `SWP-FAM-001` finding will be **MEDIUM** (score 50–79) with an explicit blocker message. This is by design — Azure's host-level `Available Memory Bytes` proxy is unreliable for performance-fit decisions. Supplying the OS-agent CSV unlocks HIGH confidence.
3. **Burstable and diskless surface efficiency gains Azure Advisor's defaults often miss.** `RSZ-BSF-001` matches workloads to a credit model that better fits their performance profile; `SWP-DSK-001` removes a local-disk dependency that isn't being used. Both are about **fit**, not about being cheaper.
4. **CRR findings are LOW by design.** They are a starting point for the capacity-planning conversation — CloudOpt cannot verify the 30-day duration from a single snapshot, so use them to drive investigation, not action.
5. **Discovery-only candidates** (`SWP-ARC-001`) should be framed as "worth investigating" — never as a prescribed action.
6. **Right-size up is a first-class concern.** `RSZ-UPS-001` is framed as protecting performance and resiliency under sustained pressure — not as the opposite of right-size down. It requires OS-agent data to fire.
7. **QTA-OPS-001 is about reducing blast radius.** Missing capacity alerts mean the customer has no early warning before quotas exhaust or allocation fails. Frame this as an operational maturity gap: the tool found the coverage holes; the fix is straightforward and high-leverage.
8. **Use the confidence score histogram.** The dashboard Summary shows score distribution across all findings. A cluster of 65-score findings means the customer hasn't provided OS-agent data yet — use that as a conversation opener to unlock higher-confidence guidance.

---

## 7. Known gaps and deferred work

| Item | Status | Notes |
|---|---|---|
| 30-minute (PT30M) metric granularity | Currently PT1H | PT30M would double data volume; PT1H is a deliberate trade-off |
| Service Fabric / AKS reliability-tier awareness in VMSS recs | Not implemented | VMSS instance-count rec is generic |
| Diskless temp-disk *size* utilization check | Not implemented | Azure Monitor doesn't emit temp-disk size-used as a metric — IOPS + throughput are used as proxies |
| Per-hour B-series credit simulation | Long-run avg only | Simplified model; conservative |
| App Service Plan orphan detection | Not in scope | Deferred |
| RSZ-DSK-001 full disk IOPS analysis | Partial | Current detector uses tier-level thresholds; per-disk IOPS time-series requires AMA or OS agent |

---

*Document generated from source-of-truth code: `src/cloudopt/analyzer/detectors/`, `src/cloudopt/analyzer/taxonomy.py`, `src/cloudopt/analyzer/confidence.py`.*
