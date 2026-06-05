#!/usr/bin/env python3
# -*- mode: python; indent-tabs-mode: nil; python-indent-level: 4 -*-
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=python
"""Unit tests for ebpf-dpdk-post-process"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from importlib.machinery import SourceFileLoader

pp = SourceFileLoader(
    "ebpf_dpdk_post_process",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "ebpf-dpdk-post-process")
).load_module()


class TestTrafficWindowDetection(unittest.TestCase):
    """Test traffic window detection from perf sample timestamps."""

    def test_no_timestamps(self):
        result = pp.detect_traffic_window([])
        self.assertIsNone(result)

    def test_short_duration(self):
        timestamps = [(float(i), i) for i in range(10)]
        result = pp.detect_traffic_window(timestamps)
        self.assertIsNone(result)

    def test_uniform_distribution(self):
        timestamps = [(float(i) * 0.5, i) for i in range(200)]
        result = pp.detect_traffic_window(timestamps)
        self.assertIsNone(result)


class TestCountSamples(unittest.TestCase):
    """Test sample counting in folded files."""

    def test_basic_count(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".folded",
                                         delete=False) as f:
            f.write("a;b;c 100\n")
            f.write("a;b;d 200\n")
            f.write("a;e 50\n")
            fname = f.name
        try:
            total = pp.count_samples_in_folded(fname)
            self.assertEqual(total, 350)
        finally:
            os.unlink(fname)


class TestBuiltinCollapse(unittest.TestCase):
    """Test the built-in stack collapser."""

    PERF_SCRIPT = """\
ovs-vswitchd  1234 [004] 1000.100:     cycles:
	ffffffff deadbeef dpcls_lookup+0x42 (/usr/sbin/ovs-vswitchd)
	ffffffff deadbee0 dp_netdev_process_rxq_port+0x100 (/usr/sbin/ovs-vswitchd)

ovs-vswitchd  1234 [004] 1000.200:     cycles:
	ffffffff deadbeef dpcls_lookup+0x42 (/usr/sbin/ovs-vswitchd)
	ffffffff deadbee0 dp_netdev_process_rxq_port+0x100 (/usr/sbin/ovs-vswitchd)

ovs-vswitchd  1234 [004] 1000.300:     cycles:
	ffffffff deadbee1 emc_processing+0x10 (/usr/sbin/ovs-vswitchd)
	ffffffff deadbee0 dp_netdev_process_rxq_port+0x100 (/usr/sbin/ovs-vswitchd)

"""

    def test_collapse(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".perf",
                                         delete=False) as fin:
            fin.write(self.PERF_SCRIPT)
            script_file = fin.name

        folded_file = script_file + ".folded"
        try:
            result = pp.builtin_collapse(script_file, folded_file)
            self.assertIsNotNone(result)
            with open(folded_file) as f:
                content = f.read()
            self.assertIn("dpcls_lookup", content)
            self.assertIn("emc_processing", content)
        finally:
            os.unlink(script_file)
            if os.path.exists(folded_file):
                os.unlink(folded_file)


if __name__ == "__main__":
    unittest.main()
