#!/usr/bin/env python3
# -*- mode: python; indent-tabs-mode: nil; python-indent-level: 4 -*-
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=python
"""
PMD thread discovery for DPDK applications.

Discovers Poll Mode Driver thread TIDs for OVS-DPDK and standalone DPDK
applications (testpmd, l3fwd, etc.) so that perf can target them directly.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys


def discover_ovs_pmd_tids():
    """Discover OVS-DPDK PMD thread TIDs via ovs-appctl.

    Returns a list of dicts with tid, core, numa, and rxq info.
    """
    if not shutil.which("ovs-appctl"):
        return []

    ovs_pid = None
    try:
        result = subprocess.run(
            ["pgrep", "-x", "ovs-vswitchd"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        ovs_pid = int(result.stdout.strip().split("\n")[0])
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return []

    ovs_dir = "/var/run/openvswitch"
    target = f"--target={ovs_dir}/ovs-vswitchd.{ovs_pid}.ctl"

    try:
        result = subprocess.run(
            ["ovs-appctl", target, "dpif-netdev/pmd-rxq-show"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, OSError):
        return []

    return _parse_pmd_rxq_show(result.stdout, ovs_pid)


def _parse_pmd_rxq_show(output, ovs_pid):
    """Parse ovs-appctl dpif-netdev/pmd-rxq-show output.

    Example output format:
        pmd thread numa_id 0 core_id 4:
          isolated : false
          port: dpdk-p0            queue-id:  0 ...
        pmd thread numa_id 0 core_id 6:
          ...
    """
    pmds = []
    current = None
    pmd_header_re = re.compile(
        r"pmd thread numa_id\s+(\d+)\s+core_id\s+(\d+)"
    )

    for line in output.splitlines():
        m = pmd_header_re.search(line)
        if m:
            numa_id = int(m.group(1))
            core_id = int(m.group(2))
            tid = _find_tid_for_pmd_core(ovs_pid, core_id)
            current = {
                "tid": tid,
                "core": core_id,
                "numa": numa_id,
                "process": "ovs-vswitchd",
                "pid": ovs_pid,
                "rxqs": [],
            }
            if tid is not None:
                pmds.append(current)
            continue

        if current is not None and "port:" in line:
            port_match = re.search(r"port:\s+(\S+)\s+queue-id:\s+(\d+)", line)
            if port_match:
                current["rxqs"].append({
                    "port": port_match.group(1),
                    "queue": int(port_match.group(2)),
                })

    return pmds


def _find_tid_for_pmd_core(pid, core_id):
    """Find the TID of an OVS PMD thread pinned to a specific core.

    OVS PMD threads are named 'pmd-cNN/id:MM' where NN is the core.
    """
    task_dir = f"/proc/{pid}/task"
    if not os.path.isdir(task_dir):
        return None

    pmd_pattern = re.compile(rf"pmd-c{core_id}/")

    for tid in os.listdir(task_dir):
        comm_path = os.path.join(task_dir, tid, "comm")
        try:
            with open(comm_path) as f:
                comm = f.read().strip()
            if pmd_pattern.match(comm):
                return int(tid)
        except (OSError, ValueError):
            continue

    return None


def discover_dpdk_process_tids(process_name="dpdk-testpmd"):
    """Discover DPDK lcore worker TIDs by scanning /proc.

    Finds processes matching process_name, then scans their threads
    for lcore-worker-* or dpdk-* patterns.
    """
    results = []

    try:
        pgrep_result = subprocess.run(
            ["pgrep", "-f", process_name],
            capture_output=True, text=True, timeout=5
        )
        if pgrep_result.returncode != 0:
            return []
        pids = [
            int(p) for p in pgrep_result.stdout.strip().split("\n")
            if p.strip()
        ]
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return []

    lcore_re = re.compile(r"^(lcore-worker-\d+|dpdk-\S+|rte_mp_handle)$")

    for pid in pids:
        task_dir = f"/proc/{pid}/task"
        if not os.path.isdir(task_dir):
            continue

        for tid in os.listdir(task_dir):
            comm_path = os.path.join(task_dir, tid, "comm")
            try:
                with open(comm_path) as f:
                    comm = f.read().strip()
                if lcore_re.match(comm) and comm != "rte_mp_handle":
                    results.append({
                        "tid": int(tid),
                        "core": _get_thread_cpu(pid, int(tid)),
                        "numa": None,
                        "process": process_name,
                        "pid": pid,
                        "thread_name": comm,
                    })
            except (OSError, ValueError):
                continue

    return results


def _get_thread_cpu(pid, tid):
    """Get the CPU a thread is currently running on via /proc/stat."""
    stat_path = f"/proc/{pid}/task/{tid}/stat"
    try:
        with open(stat_path) as f:
            fields = f.read().split()
        return int(fields[38]) if len(fields) > 38 else None
    except (OSError, ValueError, IndexError):
        return None


def discover_all(target="auto"):
    """Run discovery based on target specification.

    Returns (tids_list, pmds_detail) where tids_list is the flat list
    of TIDs for perf, and pmds_detail is the full discovery metadata.
    """
    pmds = []

    if target in ("ovs-vswitchd", "auto", "all"):
        ovs_pmds = discover_ovs_pmd_tids()
        if ovs_pmds:
            pmds.extend(ovs_pmds)
            sys.stderr.write(
                f"ebpf-dpdk: discovered {len(ovs_pmds)} OVS PMD thread(s)\n"
            )
            for p in ovs_pmds:
                sys.stderr.write(
                    f"  core {p['core']}: TID {p['tid']} "
                    f"({len(p.get('rxqs', []))} rxq)\n"
                )

        if target == "ovs-vswitchd" and not ovs_pmds:
            sys.stderr.write(
                "ebpf-dpdk: WARNING: no OVS PMD threads found\n"
            )

    if target in ("testpmd", "auto", "all"):
        testpmd_pmds = discover_dpdk_process_tids("dpdk-testpmd")
        if testpmd_pmds:
            pmds.extend(testpmd_pmds)
            sys.stderr.write(
                f"ebpf-dpdk: discovered {len(testpmd_pmds)} "
                f"testpmd lcore thread(s)\n"
            )

    if target == "auto" and not pmds:
        for proc_name in ["ovs-vswitchd", "dpdk-testpmd", "testpmd", "l3fwd"]:
            found = discover_dpdk_process_tids(proc_name)
            if found:
                pmds.extend(found)
                sys.stderr.write(
                    f"ebpf-dpdk: auto-discovered {len(found)} "
                    f"thread(s) from {proc_name}\n"
                )
                break

    tids = [p["tid"] for p in pmds if p.get("tid") is not None]
    return tids, pmds


def main():
    parser = argparse.ArgumentParser(
        description="Discover DPDK PMD thread TIDs for perf profiling"
    )
    parser.add_argument(
        "--target", default="auto",
        choices=["ovs-vswitchd", "testpmd", "all", "auto"],
        help="Which DPDK processes to discover (default: auto)"
    )
    parser.add_argument(
        "--output", default="text",
        choices=["text", "json", "tids"],
        help="Output format (default: text)"
    )
    args = parser.parse_args()

    tids, pmds = discover_all(args.target)

    if args.output == "json":
        json.dump({"tids": tids, "pmds": pmds}, sys.stdout, indent=2)
        print()
    elif args.output == "tids":
        print(",".join(str(t) for t in tids))
    else:
        if not tids:
            print("No PMD threads discovered")
            sys.exit(1)
        print(f"Discovered {len(tids)} PMD thread(s):")
        for p in pmds:
            print(f"  TID {p['tid']} core={p.get('core')} "
                  f"process={p.get('process')}")
        print(f"\nTID list: {','.join(str(t) for t in tids)}")


if __name__ == "__main__":
    main()
