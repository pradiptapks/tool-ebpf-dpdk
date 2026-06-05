# tool-ebpf-dpdk — Technical Architecture Document

**eBPF/perf-based CPU profiling and flamegraph generation for OVS-DPDK and DPDK testpmd PMD threads**

| Attribute | Value |
|-----------|-------|
| **Project** | perftool-incubator/tool-ebpf-dpdk |
| **Document Version** | 1.0 |
| **Primary Target** | OVS-DPDK (ovs-vswitchd PMD threads) |
| **Secondary Targets** | dpdk-testpmd, l3fwd, Grout |
| **Phases** | 4 |
| **External Dependencies** | perf, FlameGraph toolkit, bpftrace (Phase 2) |
| **Implementation Status** | Phase 1 complete |
| **Crucible Integration** | Activated via symlink at `subprojects/tools/ebpf-dpdk` |

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement and Motivation](#2-problem-statement-and-motivation)
3. [System Context and Topology](#3-system-context-and-topology)
4. [Crucible Run Lifecycle and Tool Timing](#4-crucible-run-lifecycle-and-tool-timing)
5. [Architecture Overview](#5-architecture-overview)
6. [PMD Thread Discovery](#6-pmd-thread-discovery)
7. [Collection Engine](#7-collection-engine)
8. [Post-Processing Pipeline](#8-post-processing-pipeline)
9. [Binary Search Profiling Strategy](#9-binary-search-profiling-strategy)
10. [Output Artifacts and Visualization](#10-output-artifacts-and-visualization)
11. [CDM Integration](#11-cdm-integration)
12. [Workshop Dependencies and Container Image](#12-workshop-dependencies-and-container-image)
13. [Deployment Topologies](#13-deployment-topologies)
14. [Configuration Reference](#14-configuration-reference)
15. [Upstream eBPF Resources for DPDK](#15-upstream-ebpf-resources-for-dpdk)
16. [Risk Analysis and Mitigations](#16-risk-analysis-and-mitigations)
17. [Phased Delivery Plan](#17-phased-delivery-plan)
18. [Future Enhancements](#18-future-enhancements)

---

## 1. Executive Summary

tool-ebpf-dpdk is a standalone crucible profiling tool that answers the question **"where are DPDK PMD thread CPU cycles going?"** -- a question that existing tools (tool-dpdk, tool-ovs, tool-kernel) cannot answer.

### What exists today

| Tool | What it answers | Blind spot |
|------|----------------|------------|
| **tool-dpdk** | How many packets/bytes flowed? Per-queue distribution? Mempool pressure? | Cannot explain *why* throughput dropped |
| **tool-ovs** | How busy are OVS PMD threads? Flow miss rate? Conntrack stats? | Cannot show *where* in the code cycles are spent |
| **tool-kernel** | System-wide CPU profile via `perf record -a` | Not DPDK-targeted; no flamegraph output; post-processor missing |
| **tool-rt-trace-bpf** | Kernel scheduling events via BCC | Post-processor is a stub; no CDM output |

### What tool-ebpf-dpdk adds

- **Targeted PMD profiling**: `perf record -t <tid>` on specific PMD threads, not system-wide
- **Automatic flamegraph generation**: interactive SVGs produced during post-processing
- **Traffic-aware filtering**: excludes idle pre/post-traffic periods from flamegraphs
- **CDM metrics**: top function CPU percentages indexed into OpenSearch
- **Multiple visualization formats**: SVG (browser), folded stacks (speedscope), raw perf.data (Hotspot)

### Scope

**In Scope:**
- CPU profiling of OVS-DPDK PMD threads via `perf record`
- PMD thread auto-discovery via `ovs-appctl` and `/proc` scan
- Flamegraph SVG + speedscope-compatible collapsed stack generation
- CDM metric emission for top CPU-consuming functions
- Traffic window detection to filter idle profiling periods
- bpftrace-based scheduling interference detection (Phase 2)

**Out of Scope:**
- Modifications to OVS, testpmd, or any DPDK application source code
- Real-time dashboarding (CDM/OpenSearch handles visualization)
- Packet-level tracing (see ByteDance netcap for that use case)
- DPDK telemetry counter collection (that is tool-dpdk's domain)

---

## 2. Problem Statement and Motivation

### 2.1 The Diagnostic Gap

When an OVS-DPDK performance regression occurs during a bench-trafficgen run, the diagnostic workflow today is:

```
Step 1: tool-dpdk shows rx-pps dropped 40%               → WHAT happened
Step 2: tool-ovs shows PMD busy% increased to 95%         → CONFIRMS something is wrong
Step 3: ???                                                → WHY did it happen?
Step 4: Manual SSH to host, install perf, run perf record  → Slow, error-prone, not repeatable
```

tool-ebpf-dpdk fills Step 3 with automated, repeatable CPU profiling:

```
Step 3: tool-ebpf-dpdk flamegraph shows:
        51% dpcls_lookup (megaflow classifier)             → EMC cache thrashing
        28% miniflow_extract (packet parsing)              → Expected overhead
        11% conntrack_execute (if enabled)                 → Conntrack bottleneck
```

### 2.2 Why not add this to tool-kernel?

tool-kernel does `perf record -a` (system-wide). For DPDK:
- System-wide profiling wastes >90% of samples on irrelevant processes
- No PMD-targeted `-t <tid>` recording
- No flamegraph pipeline (missing post-processor, no `perf script` -> SVG flow)
- No DPDK-specific function analysis

### 2.3 Why not add this as a subtool of tool-dpdk?

- **Dependency bloat**: tool-dpdk needs only `python3` + `xz`. Adding perf/bpftrace/FlameGraph would 10x the image build time for ALL users
- **Different data models**: tool-dpdk emits time-series counters; this tool emits stack profiles and SVG artifacts
- **Failure isolation**: bpftrace kernel probe failures should not affect telemetry collection
- **Different privilege requirements**: telemetry is unprivileged; perf requires CAP_PERFMON

---

## 3. System Context and Topology

### 3.1 OVS-DPDK + testpmd VNF Topology

```
┌─────────────────────────────────┐
│  Client Host (nfv-intel-5)      │
│  TRex ASTF/STL traffic gen      │
│  8x NICs (4 port-pairs)         │
│  tool-dpdk instance 1           │
└───────────┬─────────────────────┘
            │ Wire (4-port-pair)
            ▼
┌─────────────────────────────────────────────────────────────────┐
│  Compute Host (nfv-intel-11) — Profiler Engine runs here        │
│                                                                 │
│  ┌────────────────────────────────────────────────────────┐     │
│  │ OVS-DPDK (ovs-vswitchd, DPDK 24.11.3)                  │     │
│  │ PMD threads: pmd-c4, pmd-c6, pmd-c8, pmd-c10           │     │
│  │   ┌──────────────────────────────────────────┐         │     │
│  │   │ NIC rx → flow lookup → action → vhost tx │         │     │
│  │   └──────────────────────────────────────────┘         │     │
│  └──────────────────┬─────────────────────────────────────┘     │
│                     │ vhost-user                                │
│                     ▼                                           │
│  ┌──────────────────────────────────────────────┐               │
│  │ VM 192.168.0.103                             │               │
│  │ testpmd io-forward (DPDK 23.11)              │               │
│  │ 4 queues, 2 queues/PMD, SMT on               │               │
│  │ Devices: 0000:04:00.0, 0000:05:00.0          │               │
│  │ tool-dpdk instance 3                         │               │
│  └──────────────────────────────────────────────┘               │
│                                                                 │
│  Tools running on profiler engine:                              │
│  ├─ tool-sysstat    (CPU/IO/memory)                             │
│  ├─ tool-procstat   (per-process CPU, IRQ)                      │
│  ├─ tool-ovs        (PMD busy%, flows, conntrack)               │
│  ├─ tool-dpdk       (DPDK telemetry counters)                   │
│  └─ tool-ebpf-dpdk  (CPU flamegraphs for PMD threads) ← NEW     │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 Data Layer Mapping

| Layer | What it measures | Tool |
|-------|-----------------|------|
| Wire (L1/L2) | Packets in/out, link status | tool-dpdk (ethdev/stats) |
| NIC extended (L2+) | Per-queue counters, xstats, RSS | tool-dpdk (ethdev/xstats) |
| OVS datapath (L2-L4) | Flow hits/misses, PMD busy%, conntrack | tool-ovs |
| CPU execution (microarch) | **Where cycles go inside PMD code** | **tool-ebpf-dpdk** |
| Scheduling (OS) | PMD preemption, off-CPU events | tool-ebpf-dpdk (Phase 2) |
| System resources | CPU utilization, IRQ, memory | tool-sysstat, tool-procstat |

---

## 4. Crucible Run Lifecycle and Tool Timing

### 4.1 Roadblock Sequence

Tools have no per-phase visibility. They start once before all iterations and stop once after all iterations complete.

```
Roadblock Sequence (per run):
═══════════════════════════════════════════════════════════════
endpoint-deploy-begin/end          Deploy engines
engine-init-begin/end              Initialize engines
get-data-begin/end                 Fetch bench/tool commands
collect-sysinfo-begin/end          Packrat system info

start-tools-begin                  ← tool-ebpf-dpdk starts here
  → ebpf-dpdk-start discovers PMDs, launches perf record
start-tools-end                    All tools confirmed running

setup-bench-begin/end              Load benchmark commands

  ┌─ Per iteration × sample × attempt ─────────────────────┐
  │ infra-start        Client infra (TRex server)          │
  │ server-start       testpmd starts                      │
  │ endpoint-start     Endpoint hooks                      │
  │ client-start       binary-search.py runs all trials    │
  │                    ↑ TRAFFIC FLOWS HERE                │
  │ client-stop        Client finished                     │
  │ server-stop        testpmd stops                       │
  │ infra-stop         Cleanup                             │
  └────────────────────────────────────────────────────────┘

stop-tools-begin                   ← tool-ebpf-dpdk stops here
  → ebpf-dpdk-stop kills perf, compresses perf.data
stop-tools-end                     All tools confirmed stopped

send-data-begin/end                Archive data to controller
endpoint-cleanup-begin/end         Final cleanup
```

### 4.2 Real Timing from Production Run

From run `8ab97461` (ASTF, `search-runtime=20`, `validation-runtime=60`):

| Phase | Duration | Notes |
|-------|----------|-------|
| Tool-start to first traffic | 93s | testpmd startup, TRex init, MAC exchange |
| Active traffic | 805s (13.4 min) | All binary search trials + validation |
| Traffic-end to tool-stop | 134s | Server teardown, data transfer |
| Total tool collection | 1033s (17.2 min) | Entire iteration |
| Idle samples (pre+post) | ~22% of total | Filtered out in post-processing |

### 4.3 Key Constraint

The tool collects for the **entire iteration**. It cannot start/stop per binary-search trial. Traffic window detection happens entirely in post-processing using timestamp analysis.

---

## 5. Architecture Overview

### 5.1 Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                     Collection Phase                            │
│                   (on profiler engine)                          │
│                                                                 │
│  ebpf-dpdk-start                                                │
│       │                                                         │
│       ├─→ pmd-discovery.py                                      │
│       │     ├─ ovs-appctl dpif-netdev/pmd-rxq-show              │
│       │     └─ /proc/<pid>/task/*/comm scan                     │
│       │                                                         │
│       ├─→ perf record -F 99 -g --call-graph dwarf -t TIDs       │
│       │     (runs for entire iteration)                         │
│       │                                                         │
│       └─→ [Phase 2] bpftrace bpf/pmd_sched.bt                   │
│                                                                 │
│  ebpf-dpdk-stop                                                 │
│       ├─→ kill -SIGINT <perf-pid>                               │
│       ├─→ perf archive perf.data                                │
│       └─→ xz --threads=0 perf.data                              │
└─────────────────────┬───────────────────────────────────────────┘
                      │ perf.data.xz transferred to controller
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Post-Processing Phase                         │
│                    (on controller)                              │
│                                                                 │
│  ebpf-dpdk-post-process                                         │
│       │                                                         │
│       ├─→ xz -d perf.data.xz                                    │
│       ├─→ perf script --no-inline                               │
│       ├─→ stackcollapse-perf.pl (or built-in fallback)          │
│       │                                                         │
│       ├─→ flamegraph.pl → flamegraph-{target}-full.svg          │
│       │                                                         │
│       ├─→ Traffic window detection (timestamp density analysis) │
│       │     └─→ flamegraph-{target}-active.svg                  │
│       │                                                         │
│       ├─→ Top function extraction → CDM metrics                 │
│       │     └─→ toolbox.metrics.log_sample()                    │
│       │     └─→ toolbox.metrics.finish_samples()                │
│       │                                                         │
│       └─→ post-process-data.json manifest                       │
└─────────────────────────────────────────────────────────────────┘
```

### 5.2 File Structure

```
tool-ebpf-dpdk/
├── rickshaw.json                  Tool integration manifest
├── workshop.json                  Engine image dependencies
├── CLAUDE.md                      AI development guide
├── LICENSE                        Apache 2.0
├── ebpf-dpdk-start                Collection launcher (Bash)
├── ebpf-dpdk-stop                 Collection stopper (Bash)
├── ebpf-dpdk-post-process         Post-processing pipeline (Python)
├── pmd-discovery.py               PMD thread TID discovery (Python)
├── bpf/
│   └── pmd_sched.bt               Scheduling interference probe (bpftrace)
├── profiles/
│   ├── ovs-dpdk.json              OVS-DPDK PMD target profile
│   ├── testpmd.json               testpmd lcore target profile
│   └── all-dpdk.json              All DPDK threads profile
├── docs/
│   └── tool-ebpf-dpdk-technical-architecture.md  (this document)
└── tests/
    ├── test_pmd_discovery.py      PMD discovery unit tests
    └── test_post_process.py       Post-processing unit tests
```

---

## 6. PMD Thread Discovery

### 6.1 Discovery Methods

`pmd-discovery.py` supports two independent discovery methods:

**Method 1: OVS-DPDK via ovs-appctl**

```
ovs-appctl --target=/var/run/openvswitch/ovs-vswitchd.<pid>.ctl \
    dpif-netdev/pmd-rxq-show
```

Output parsed:
```
pmd thread numa_id 0 core_id 4:
  isolated : false
  port: dpdk-p0            queue-id:  0 (enabled)   pmd usage: 45 %
  port: dpdk-p0            queue-id:  1 (enabled)   pmd usage: 43 %
```

Extracted: core_id → TID mapping via `/proc/<pid>/task/*/comm` matching `pmd-cNN/`.

**Method 2: Generic DPDK via /proc scan**

For testpmd, l3fwd, or any DPDK application:
1. `pgrep -f dpdk-testpmd` → find PID
2. Scan `/proc/<pid>/task/*/comm` for threads named `lcore-worker-*`
3. Return matching TIDs

### 6.2 Target Selection

| `--target` value | Discovery method | Use case |
|------------------|-----------------|----------|
| `ovs-vswitchd` | ovs-appctl | OVS-DPDK compute hosts |
| `testpmd` | /proc scan for `dpdk-testpmd` | Bare-metal testpmd or host PID namespace |
| `all` | Both methods combined | Profile everything on the host |
| `auto` (default) | Try OVS first, fall back to proc scan | Auto-detect whatever is running |

### 6.3 Retry Logic

The start script retries PMD discovery every 10 seconds, up to 30 attempts (5 minutes). This accommodates the ~93-second gap between tool-start and testpmd/OVS startup observed in production runs. If no PMD threads are found, the tool exits gracefully and the post-processor skips processing.

---

## 7. Collection Engine

### 7.1 perf record Configuration

```bash
perf record \
    -F 99 \                    # 99 Hz sampling (avoids timer aliasing)
    -g \                       # Collect call graphs
    --call-graph dwarf \       # DWARF-based unwinding (best for C/C++)
    -t <tid1>,<tid2>,... \     # Target specific PMD thread TIDs
    -o perf.data               # Output file
```

**Why 99 Hz**: Standard practice to avoid frequency aliasing with 100 Hz kernel timer interrupts. At 99 Hz, a 60-second validation trial produces ~5,940 samples per PMD thread — sufficient for statistical accuracy without measurable performance impact.

**Why per-TID**: System-wide `-a` wastes >90% of samples on non-DPDK processes. Per-TID recording focuses 100% of samples on PMD threads.

**Why DWARF unwinding**: OVS-DPDK and testpmd are compiled with frame pointers in some builds but not all. DWARF unwinding works regardless, producing reliable call chains through `dp_netdev_process_rxq_port` → `dpcls_lookup` → etc.

### 7.2 Stop and Compression

On stop:
1. `kill -SIGINT <perf-pid>` — graceful shutdown, flushes ring buffers
2. Wait up to 30 seconds for perf to exit (force kill if hung)
3. `perf archive perf.data` — bundles debug symbols for cross-machine analysis
4. `xz --threads=0 perf.data` — parallel compression (perf.data can be 100+ MB)

---

## 8. Post-Processing Pipeline

### 8.1 Pipeline Stages

```
perf.data.xz
    │
    ▼ xz -dk
perf.data
    │
    ▼ perf script --no-inline
perf-script.txt (timestamped stack traces)
    │
    ├──────────────────────────────────────────┐
    ▼                                          ▼
stackcollapse-perf.pl --tid           parse timestamps
    │                                          │
    ▼                                          ▼
perf.folded (collapsed stacks)         traffic window detection
    │                                          │
    ├─→ flamegraph.pl → full.svg               │
    │                                          │
    ├─→ xz → .folded.xz (speedscope)           │
    │                                          ▼
    ├─→ extract_top_functions()         filter perf-script by time
    │         │                                │
    │         ▼                                ▼
    │   CDM metrics via toolbox         active.folded → active.svg
    │         │
    │         ▼
    │   metric-data-0.json.xz
    │
    └─→ post-process-data.json (manifest)
```

### 8.2 Built-in Fallback

If the FlameGraph toolkit is not available at `/opt/FlameGraph/`, the post-processor uses a built-in Python stack collapser. This produces correct folded stacks but without the SVG flamegraph. The folded output can still be used with speedscope.

### 8.3 Traffic Window Detection

The post-processor detects when traffic was active using perf sample density analysis:

1. Parse timestamps from `perf script` output
2. Bucket samples into 5-second windows
3. Calculate average sample rate
4. Identify contiguous blocks above a density threshold
5. Generate `flamegraph-{target}-active.svg` from only those samples

This filters out the ~93s pre-traffic and ~134s post-traffic idle periods where PMD threads spin in empty poll loops, producing a cleaner flamegraph focused on actual packet processing.

---

## 9. Binary Search Profiling Strategy

### 9.1 STL (Stateless) Mode

| Aspect | Detail |
|--------|--------|
| Traffic pattern | Fixed-rate UDP/L2/L3 frames |
| Trial behavior | Instant rate change between trials |
| Profiling value | Shows function shift as packet rate changes |
| Recommended mode | `one-shot=1` for clean profiling; binary search for regression hunting |

### 9.2 ASTF (Advanced Stateful) Mode

| Aspect | Detail |
|--------|--------|
| Traffic pattern | Real TCP connections (SYN/ACK/data/FIN), CPS-driven |
| Trial behavior | Ramp-up (`astf-ramp-time`) at start of each trial |
| Profiling value | Shows conntrack scaling behavior, TCP state machine overhead |
| Recommended mode | `one-shot=1` for focused analysis; binary search for max-CPS discovery |

### 9.3 one-shot vs Binary Search

| Mode | Flamegraph quality | Use case |
|------|-------------------|----------|
| `one-shot=1` | **Best** — single steady-state window, clean flamegraph | Targeted profiling at a known rate |
| `one-shot=0` (binary search) | Mixed — aggregates all trials at different rates | Discovery + profiling combined |

For binary search mode, the post-processor generates multiple flamegraphs:
- `flamegraph-{target}-full.svg` — entire collection (including idle)
- `flamegraph-{target}-active.svg` — traffic-only (excludes idle)
- Future: `flamegraph-{target}-validation.svg` — validation trial only (Phase 3)

---

## 10. Output Artifacts and Visualization

### 10.1 Generated Files

| File | Format | Always generated | Purpose |
|------|--------|-----------------|---------|
| `perf.data.xz` | Compressed perf data | Yes | Raw data for Hotspot/FlameScope deep analysis |
| `perf-archive.tar` | Symbol archive | Yes | Cross-machine symbol resolution |
| `flamegraph-{target}-full.svg` | Interactive SVG | Yes | Browser-viewable full-run flamegraph |
| `flamegraph-{target}-active.svg` | Interactive SVG | When traffic detected | Traffic-only flamegraph |
| `flamegraph-{target}.folded.xz` | Compressed text | Yes | Speedscope-compatible collapsed stacks |
| `pmd-discovery.json` | JSON | Yes | PMD thread metadata |
| `metric-data-0.json.xz` | CDM metrics | Yes | Top function CPU% for OpenSearch |
| `post-process-data.json` | Manifest | Yes | Rickshaw integration |

### 10.2 Visualization Options

**Option A: SVG in browser (zero setup)**
```bash
firefox /var/lib/crucible/run/latest/run/tool-data/profiler/remotehosts-1-ebpf-dpdk-1/ebpf-dpdk/flamegraph-ovs-vswitchd-full.svg
```

**Option B: Speedscope (interactive web viewer)**
```bash
xzcat .../flamegraph-ovs-vswitchd.folded.xz > /tmp/profile.folded
# Drag to https://www.speedscope.app (no upload, runs in-browser)
# Three views: Time Order, Left Heavy, Sandwich
```

**Option C: KDAB Hotspot (desktop GUI)**
```bash
xzcat .../perf.data.xz > /tmp/perf.data
hotspot /tmp/perf.data
# Per-thread timeline + flamegraph + top-down/bottom-up
```

**Option D: Netflix FlameScope (perturbation hunting)**
```bash
xzcat .../perf.data.xz > /tmp/perf.data
perf script -i /tmp/perf.data --no-inline > /tmp/profile.perf
# Load in FlameScope for subsecond-offset heatmap
```

---

## 11. CDM Integration

### 11.1 Dependency Chain

```
tool-ebpf-dpdk post-processor
        │ imports
        ▼
toolbox.metrics (log_sample, finish_samples)    ← subprojects/core/toolbox
        │ writes
        ▼
metric-data-*.json.xz + metric-data-*.csv.xz
        │ consumed by (automatic)
        ▼
rickshaw-gen-docs.py                             ← subprojects/core/rickshaw
        │ generates
        ▼
OpenSearch NDJSON documents
        │ indexed by (automatic)
        ▼
CommonDataModel/queries/cdmq/add-run.sh          ← subprojects/core/CommonDataModel
```

The tool's only code dependency is `toolbox.metrics`. Everything downstream (rickshaw-gen-docs, CDM indexing) happens automatically.

### 11.2 Emitted Metrics

| Metric | CDM Source | CDM Class | CDM Type | Example Value |
|--------|-----------|-----------|----------|---------------|
| Hottest function CPU% | ebpf-dpdk | utilization | top-function-pct | 51.2 (`dpcls_lookup`) |
| Top-1 function | ebpf-dpdk | utilization | top1-function-pct | 51.2 |
| Top-2 function | ebpf-dpdk | utilization | top2-function-pct | 28.4 |
| Top-3 function | ebpf-dpdk | utilization | top3-function-pct | 11.1 |
| Top-4 function | ebpf-dpdk | utilization | top4-function-pct | 5.3 |
| Top-5 function | ebpf-dpdk | utilization | top5-function-pct | 2.1 |
| Total perf samples | ebpf-dpdk | count | perf-samples | 58212 |
| Active-traffic samples | ebpf-dpdk | count | perf-samples-active | 45100 |

### 11.3 Manifest Format

```json
{
    "rickshaw-bench-metric": { "schema": { "version": "2021.04.12" } },
    "tool": "ebpf-dpdk",
    "primary-period": "measurement",
    "primary-metric": "top-function-pct",
    "periods": [
        { "name": "measurement", "metric-files": "metric-data-0" }
    ]
}
```

---

## 12. Workshop Dependencies and Container Image

### 12.1 Phase 1 (Current)

```json
{
    "requirements": [
        {
            "name": "perf_deps",
            "type": "distro",
            "distro_info": {
                "packages": ["perf", "python3", "xz"]
            }
        },
        {
            "name": "flamegraph",
            "type": "manual",
            "manual_info": {
                "commands": [
                    "git clone --depth 1 https://github.com/brendangregg/FlameGraph.git /opt/FlameGraph"
                ]
            }
        }
    ]
}
```

Image build impact: lightweight (~30 seconds for distro packages + FlameGraph clone).

### 12.2 Phase 2 (bpftrace, future)

Additional requirements via separate userenv:
```json
{
    "name": "bpftrace_deps",
    "type": "distro",
    "distro_info": {
        "packages": ["bpftrace", "kernel-devel", "kernel-headers"]
    }
}
```

### 12.3 Comparison with Other Tools

| Tool | Workshop build time | Key deps |
|------|-------------------|----------|
| tool-dpdk | ~5 seconds | python3, xz |
| tool-ovs | ~10 minutes | OVS 3.5.4 from source |
| tool-kernel | ~20 minutes | Linux kernel tools from source, bcc, trace-cmd |
| **tool-ebpf-dpdk (Phase 1)** | **~30 seconds** | **perf (distro), FlameGraph (git clone)** |

---

## 13. Deployment Topologies

### 13.1 Remotehosts (Your Environment)

```
client: nfv-intel-5   → TRex ASTF/STL (engine: client/1)
profiler: nfv-intel-11 → Tools run here (engine: profiler)
server: 192.168.0.103  → testpmd VM (engine: server/2)
```

tool-ebpf-dpdk runs on the profiler engine (nfv-intel-11), profiling host-side OVS-DPDK PMD threads.

### 13.2 Kubernetes/OpenShift

tool-ebpf-dpdk runs on profiler pods. `perf record` targets OVS-DPDK pods or host processes via host PID namespace access (`hostPID: true`).

### 13.3 VNF vs CNF Considerations

| Aspect | VNF (VM-based testpmd) | CNF (Pod-based testpmd) |
|--------|----------------------|----------------------|
| OVS-DPDK profiling | Profile host-side OVS PMDs | Profile host-side OVS PMDs |
| testpmd profiling | Must target QEMU vCPU threads on host | Direct `/proc` access from profiler pod |
| Socket visibility | `host-mounts: ["/run"]` needed for telemetry | Pod shares host PID/network namespace |

---

## 14. Configuration Reference

### 14.1 Run-file Parameters

```json
{ "tool": "ebpf-dpdk", "params": [
    { "arg": "target",          "val": "ovs-vswitchd" },
    { "arg": "frequency",       "val": "99" },
    { "arg": "call-graph",      "val": "dwarf" },
    { "arg": "perf-extra-opts", "val": "" },
    { "arg": "retry-interval",  "val": "10" },
    { "arg": "retry-max",       "val": "30" }
]}
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `target` | `auto` | Which processes to profile: `ovs-vswitchd`, `testpmd`, `all`, `auto` |
| `frequency` | `99` | perf sampling frequency in Hz |
| `call-graph` | `dwarf` | Call graph mode: `dwarf`, `fp`, `lbr` |
| `perf-extra-opts` | (empty) | Additional `perf record` options |
| `retry-interval` | `10` | Seconds between PMD discovery retries |
| `retry-max` | `30` | Maximum number of discovery retry attempts |

### 14.2 Example Run-file Snippet

```json
"tool-params": [
    { "tool": "sysstat" },
    { "tool": "procstat" },
    { "tool": "ovs", "params": [{ "arg": "interval", "val": "10" }] },
    { "tool": "dpdk", "params": [
        { "arg": "interval", "val": "1" },
        { "arg": "profile", "val": "testpmd" }
    ]},
    { "tool": "ebpf-dpdk", "params": [
        { "arg": "target", "val": "ovs-vswitchd" },
        { "arg": "frequency", "val": "99" }
    ]}
]
```

---

## 15. Upstream eBPF Resources for DPDK

### 15.1 Profiling Tools

| Tool | Approach | Relevance |
|------|----------|-----------|
| **Linux perf** | Hardware PMU sampling with DWARF unwinding | Core of Phase 1 — per-TID PMD profiling |
| **ByteDance netcap** | BCC uprobes on DPDK functions for mbuf packet capture | Complementary — packet-level DPDK tracing |
| **InXpect** (FOSDEM 2026) | Kernel module + eBPF kfunc for direct PMC access | XDP-focused, 71% faster than perf for eBPF programs |
| **DPDK Trace Library** | `RTE_TRACE_POINT_FP` compiled into DPDK, CTF output | Requires DPDK built with `enable_trace_fp=true` |
| **dpdk-top** | TUI for live DPDK telemetry monitoring | Complementary to tool-dpdk for interactive use |

### 15.2 Visualization Tools

| Tool | Type | Best for |
|------|------|----------|
| **FlameGraph** (Brendan Gregg) | Static SVG | Automated pipeline, CI/CD artifacts |
| **Speedscope** | Interactive web | Deep-dive analysis, three view modes |
| **KDAB Hotspot** | Desktop GUI | Per-thread timeline + flamegraph |
| **Netflix FlameScope** | Heatmap + flamegraph | Intermittent performance perturbations |
| **Trace Compass** | Desktop GUI | DPDK native CTF traces |

### 15.3 OVS-DPDK Troubleshooting References

- [Red Hat OVS-DPDK Troubleshooting Guide Ch.12](https://docs.redhat.com/en/documentation/red_hat_openstack_platform/13/html/ovs-dpdk_end_to_end_troubleshooting_guide/troubleshoot_ovs_dpdk_pmd_cpu_usage_with_perf_and_collect_and_send_the_troubleshooting_data) — perf record on PMD threads
- [OVS PMD Performance Metrics](https://www.redhat.com/en/blog/amazing-new-observability-features-open-vswitch) — `dpif-netdev/pmd-perf-show` detailed histograms
- [OVS upstream PMD docs](https://docs.openvswitch.org/en/latest/topics/dpdk/pmd/) — PMD thread configuration and tuning

---

## 16. Risk Analysis and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| PMD threads not discovered (OVS not running at tool-start) | Medium | Retry loop (10s intervals, 30 attempts, 5 min total); graceful exit if not found |
| `perf record` overhead affects PMD performance | Low | 99 Hz sampling adds <0.1% CPU overhead; per-TID avoids system-wide noise |
| Symbol resolution failure (stripped binaries) | Medium | `perf archive` bundles debug info; DWARF unwinding works without frame pointers |
| FlameGraph toolkit not installed in container | Low | Built-in Python stack collapser as fallback; folded stacks still usable with speedscope |
| Large perf.data files (>500 MB) | Medium | xz compression typically achieves 10-20x ratio; 99 Hz rate limits data volume |
| Traffic window detection fails (uniform sample density) | Low | Falls back to full-run flamegraph; user can manually filter via speedscope time view |
| bpftrace probe fails (kernel version mismatch) | Medium | Phase 2 gated behind separate userenv; Phase 1 has no bpftrace dependency |

---

## 17. Phased Delivery Plan

### Phase 1 — perf record flamegraphs (COMPLETE)

- `perf record` targeting OVS-DPDK PMD threads via auto-discovery
- FlameGraph SVG + speedscope folded output
- Traffic-aware filtering (idle period exclusion)
- CDM metrics for top function CPU percentages
- Works with STL, ASTF, binary-search, and one-shot modes

### Phase 2 — bpftrace scheduling probes

- `bpf/pmd_sched.bt`: traces `sched_switch` on PMD cores
- Detects scheduling interference (IRQ storms, kworker preemption)
- Off-CPU flamegraph generation
- Separate userenv with bpftrace + kernel-devel dependencies

### Phase 3 — Trial-aware flamegraph splitting

- Cross-reference perf sample timestamps with tool-dpdk telemetry data
- Detect per-trial boundaries during binary search
- Generate `flamegraph-{target}-validation.svg` for the final validation trial only
- Most valuable for binary search mode where trials run at different rates

### Phase 4 — Differential flamegraphs and integration

- `difffolded.pl` support for A/B run comparison
- Integration with `crucible get metric` for flamegraph artifact retrieval
- Automated regression detection: alert when top function % shifts significantly
- FlameScope-style subsecond heatmap integration for perturbation hunting

---

## 18. Future Enhancements

### 18.1 Short-Term (Phase 2-4)

- **Off-CPU flamegraphs**: Show what replaces PMD threads when they're preempted
- **Validation-only flamegraph**: Most actionable profile during binary search
- **Differential flamegraphs**: "What changed between run A and run B?"
- **Automated regression alerts**: CDM metric-based detection of function hotspot shifts

### 18.2 Medium-Term

- **uprobe-based DPDK function tracing**: Latency histograms for `rte_eth_rx_burst`/`rte_eth_tx_burst`, OVS `dp_netdev_upcall` latency
- **DPDK Native Trace integration**: Consume CTF traces from DPDK's built-in trace framework when `enable_trace_fp=true`
- **netcap (ByteDance) integration**: mbuf-level packet tracing through OVS-DPDK
- **Multi-target profiling**: Profile both OVS-DPDK and testpmd (via QEMU vCPU threads) simultaneously

### 18.3 Long-Term

- **Hardware PMC profiling**: Cache miss rates, branch mispredictions, IPC on PMD cores via `perf stat` or InXpect
- **GPU/DPU offload profiling**: When OVS flow processing is offloaded to SmartNIC, profile the remaining host-side path
- **Continuous profiling**: Always-on low-overhead sampling with periodic flamegraph generation for fleet-wide trend analysis
- **AI-assisted analysis**: Automatic classification of flamegraph patterns (EMC thrashing, conntrack bottleneck, vhost-user saturation)
- **Integration with crucible httpd**: Serve flamegraph SVGs directly from the crucible web UI alongside CDM dashboards
