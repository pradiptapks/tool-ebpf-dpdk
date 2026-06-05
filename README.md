# tool-ebpf-dpdk

eBPF/perf-based CPU profiling and flamegraph generation tool for the [perftool-incubator](https://github.com/perftool-incubator) / [crucible](https://github.com/perftool-incubator/crucible) benchmarking ecosystem.

## Status

**Phase 1: complete** -- perf-based PMD profiling with flamegraph generation.

| Milestone | Status |
|-----------|--------|
| PMD thread auto-discovery (OVS-DPDK + generic DPDK) | Complete |
| Targeted `perf record -t <tid>` on PMD threads | Complete |
| FlameGraph SVG generation (interactive, browser-viewable) | Complete |
| Speedscope-compatible collapsed stacks (.folded) | Complete |
| Traffic-aware filtering (idle period exclusion) | Complete |
| CDM metrics (top function CPU%, sample counts) | Complete |
| Crucible/rickshaw integration (rickshaw.json, workshop.json) | Complete |
| Built-in stack collapser fallback | Complete |
| bpftrace scheduling interference probes | Phase 2 |
| Trial-aware flamegraph splitting (binary search) | Phase 3 |
| Differential flamegraphs (A/B comparison) | Phase 4 |

## Overview

tool-ebpf-dpdk answers the question **"where are DPDK PMD thread CPU cycles going?"** by producing CPU flamegraphs targeted at OVS-DPDK and testpmd Poll Mode Driver threads during crucible benchmark runs.

It fills the diagnostic gap between:
- **tool-dpdk** (telemetry counters -- *what* happened) and
- **tool-ovs** (PMD busy/idle stats -- *how much* is happening)

by showing *where* in the code the cycles are spent:

```
flamegraph reveals:
  51%  dpcls_lookup          ← megaflow classifier (EMC cache thrashing)
  28%  miniflow_extract      ← packet parsing overhead
  11%  conntrack_execute     ← conntrack bottleneck (if enabled)
   5%  netdev_send           ← vhost-user TX
   3%  dp_netdev_upcall      ← flow miss slow path
```

### How It Complements Other Tools

| Question | Tool | Answer |
|----------|------|--------|
| How many packets flowed? | tool-dpdk | 14.2 Mpps, 0 drops |
| How busy are PMD threads? | tool-ovs | 78% busy, 1200 flow misses/sec |
| **Where are PMD cycles spent?** | **tool-ebpf-dpdk** | **51% in dpcls_lookup (flamegraph)** |
| **What preempts PMD threads?** | **tool-ebpf-dpdk** | **ksoftirqd on core 4 (Phase 2)** |

## Usage with Crucible

Add `tool-ebpf-dpdk` to the `tool-params` section of your run file:

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

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `target` | `auto` | Which DPDK processes to profile: `ovs-vswitchd`, `testpmd`, `all`, `auto` |
| `frequency` | `99` | perf sampling frequency in Hz (99 avoids timer aliasing) |
| `call-graph` | `dwarf` | Call graph unwinding mode: `dwarf`, `fp`, `lbr` |
| `perf-extra-opts` | (empty) | Additional options passed to `perf record` |
| `retry-interval` | `10` | Seconds between PMD discovery retries |
| `retry-max` | `30` | Max discovery retry attempts (total wait: up to 5 min) |

### Minimal Example

Profile OVS-DPDK with defaults:

```json
{ "tool": "ebpf-dpdk" }
```

This auto-discovers OVS PMD threads and profiles at 99 Hz with DWARF unwinding.

## Output

After a run, tool-ebpf-dpdk produces these files in the tool data directory:

```
tool-data/profiler/remotehosts-1-ebpf-dpdk-1/ebpf-dpdk/
├── perf.data.xz                              Compressed raw perf data
├── perf-archive.tar                          Symbol archive (cross-machine analysis)
├── flamegraph-ovs-vswitchd-full.svg          Full-run flamegraph (interactive SVG)
├── flamegraph-ovs-vswitchd-active.svg        Traffic-only flamegraph (idle filtered)
├── flamegraph-ovs-vswitchd.folded.xz         Collapsed stacks (speedscope-compatible)
├── pmd-discovery.json                        PMD thread metadata
├── metric-data-0.json.xz                     CDM metrics (top function CPU%)
├── metric-data-0.csv.xz                      CDM metrics (CSV)
└── post-process-data.json                    Rickshaw manifest
```

## Viewing Flamegraphs

### Option A: Open SVG in browser (zero setup)

```bash
firefox /var/lib/crucible/run/latest/run/tool-data/profiler/remotehosts-1-ebpf-dpdk-1/ebpf-dpdk/flamegraph-ovs-vswitchd-full.svg
```

The SVG is interactive -- hover for function names and sample counts, click to zoom.

### Option B: Speedscope (interactive web viewer)

```bash
xzcat .../flamegraph-ovs-vswitchd.folded.xz > /tmp/profile.folded
```

Drag the file to [speedscope.app](https://www.speedscope.app) (runs in-browser, nothing uploaded). Provides three views:
- **Time Order** -- chronological stack timeline
- **Left Heavy** -- traditional flamegraph (largest frames sorted left)
- **Sandwich** -- select any function to see all callers and callees

### Option C: KDAB Hotspot (desktop GUI)

```bash
xzcat .../perf.data.xz > /tmp/perf.data
hotspot /tmp/perf.data
```

Full GUI with per-thread timeline, flamegraph, and top-down/bottom-up views.

### Option D: Netflix FlameScope (perturbation hunting)

```bash
xzcat .../perf.data.xz > /tmp/perf.data
perf script -i /tmp/perf.data --no-inline > /tmp/profile.perf
```

Load in [FlameScope](https://github.com/Netflix/flamescope) for subsecond-offset heatmaps that reveal periodic CPU spikes.

## PMD Thread Discovery

The tool auto-discovers PMD thread TIDs using two methods:

**OVS-DPDK** (primary): parses `ovs-appctl dpif-netdev/pmd-rxq-show` for PMD thread IDs, core assignments, and rx queue mappings.

**Generic DPDK** (fallback): scans `/proc/<pid>/task/*/comm` for threads named `lcore-worker-*` after finding the DPDK process via `pgrep`.

Discovery retries every 10 seconds (up to 5 minutes) to handle the timing gap between tool-start and testpmd/OVS startup. If no PMD threads are found, the tool exits gracefully.

## Supported Scenarios

| Scenario | Mode | Support |
|----------|------|---------|
| OVS-DPDK + testpmd (STL, binary search) | `target=ovs-vswitchd` | Full |
| OVS-DPDK + testpmd (ASTF, binary search) | `target=ovs-vswitchd` | Full |
| OVS-DPDK + testpmd (ASTF, one-shot) | `target=ovs-vswitchd` | Full (best flamegraph quality) |
| Bare-metal testpmd (no OVS) | `target=testpmd` | Full |
| VM testpmd (via QEMU vCPU threads) | `target=testpmd` | Requires host PID namespace |
| Kubernetes OVS-DPDK | `target=ovs-vswitchd` | Requires hostPID |
| Custom DPDK app | `target=auto` | Auto-discovers lcore-worker threads |

## Architecture

```
Collection (profiler engine)         Post-Processing (controller)
────────────────────────────         ────────────────────────────
pmd-discovery.py                     ebpf-dpdk-post-process
  ├─ ovs-appctl pmd-rxq-show          ├─ xz -d perf.data.xz
  └─ /proc scan                        ├─ perf script --no-inline
       │                                ├─ stackcollapse-perf.pl
       ▼                                │     ├─→ flamegraph.pl → SVG
perf record -F 99 -g -t TIDs          │     └─→ .folded.xz (speedscope)
  (runs entire iteration)               ├─ traffic window detection
       │                                │     └─→ active.svg
       ▼                                ├─ top function extraction
ebpf-dpdk-stop                         │     └─→ CDM metrics
  ├─ kill -SIGINT                       └─ post-process-data.json
  ├─ perf archive
  └─ xz --threads=0 perf.data
```

## Dependencies

### Runtime (engine container image)

| Package | Source | Purpose |
|---------|--------|---------|
| `perf` | Distro | CPU profiling |
| `python3` | Distro | PMD discovery, post-processing |
| `xz` | Distro | Compression |
| FlameGraph toolkit | [github.com/brendangregg/FlameGraph](https://github.com/brendangregg/FlameGraph) | SVG generation |

### Post-processing (controller container)

| Dependency | Source | Purpose |
|------------|--------|---------|
| `toolbox.metrics` | `subprojects/core/toolbox` | CDM metric emission |
| `perf` | Distro (in controller image) | `perf script` for stack extraction |

## Documentation

- [Technical Architecture Document](docs/tool-ebpf-dpdk-technical-architecture.md) -- end-to-end design, topology mapping, phased delivery plan, future enhancements

## Roadmap

- **Phase 2**: bpftrace scheduling interference probes (`bpf/pmd_sched.bt`). Off-CPU flamegraphs for PMD preemption analysis.
- **Phase 3**: Trial-aware flamegraph splitting. Generate per-trial flamegraphs during binary search using tool-dpdk telemetry timestamp correlation.
- **Phase 4**: Differential flamegraphs for A/B run comparison. Automated regression detection.

## License

Apache License 2.0 -- see [LICENSE](LICENSE).
