# Tool-ebpf-dpdk

## Purpose
Crucible tool for eBPF/perf-based CPU profiling and flamegraph generation targeting OVS-DPDK and DPDK testpmd PMD threads. Produces interactive flamegraph SVGs, speedscope-compatible collapsed stacks, and CDM metrics for top function hotspots.

## Language
- Bash for start/stop wrapper scripts
- Python for PMD discovery, post-processing, and CDM metric emission

## Conventions
- Primary branch is `main`
- Standard Bash modelines and 4-space indentation
- Python code follows 4-space indentation with standard modelines

## Architecture

- `ebpf-dpdk-start` — Bash wrapper that discovers PMD TIDs via `pmd-discovery.py`, launches `perf record` in background
- `ebpf-dpdk-stop` — Bash wrapper that sends SIGINT to perf, runs `perf archive`, compresses output
- `ebpf-dpdk-post-process` — Python post-processor that runs `perf script` -> `stackcollapse-perf.pl` -> `flamegraph.pl` pipeline, extracts top-function CDM metrics
- `pmd-discovery.py` — Reusable PMD thread discovery: OVS via `ovs-appctl dpif-netdev/pmd-rxq-show`, generic DPDK via `/proc` scan

## PMD Discovery

Two independent discovery methods:
1. **OVS-DPDK**: parses `ovs-appctl dpif-netdev/pmd-rxq-show` for PMD thread IDs and core assignments
2. **Generic DPDK**: scans `/proc/<pid>/task/*/comm` for threads named `lcore-worker-*` after finding DPDK processes via `pgrep`

The `--target` parameter selects: `ovs-vswitchd` (default), `testpmd`, `all`, or `auto`.

## Post-Processing

- Generates flamegraph SVGs via Brendan Gregg's FlameGraph toolkit at `/opt/FlameGraph/`
- Produces collapsed stacks (`.folded`) for speedscope interactive analysis
- Extracts top-function CPU percentages as CDM metrics via `toolbox.metrics`
- Detects active traffic windows using perf sample density to filter idle periods

## Deployment Notes

- Runs as a profiler tool on the compute/profiler host (same as tool-ovs and tool-dpdk)
- Requires `CAP_PERFMON` or `CAP_SYS_ADMIN` for `perf record` (provided by crucible's `--privileged` container mode)
- Profiling targets host-side processes only; VM-internal testpmd requires host PID namespace access
- Collection runs for the entire iteration; traffic-window filtering happens in post-processing
