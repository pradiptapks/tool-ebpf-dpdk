#!/usr/bin/env python3
# -*- mode: python; indent-tabs-mode: nil; python-indent-level: 4 -*-
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=python
"""Unit tests for pmd-discovery.py"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from importlib.machinery import SourceFileLoader

pmd_discovery = SourceFileLoader(
    "pmd_discovery",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "pmd-discovery.py")
).load_module()


class TestParsePmdRxqShow(unittest.TestCase):
    """Test parsing of ovs-appctl dpif-netdev/pmd-rxq-show output."""

    SAMPLE_OUTPUT = """\
pmd thread numa_id 0 core_id 4:
  isolated : false
  port: dpdk-p0            queue-id:  0 (enabled)   pmd usage: 45 %
  port: dpdk-p0            queue-id:  1 (enabled)   pmd usage: 43 %
pmd thread numa_id 0 core_id 6:
  isolated : false
  port: dpdk-p1            queue-id:  0 (enabled)   pmd usage: 44 %
  port: dpdk-p1            queue-id:  1 (enabled)   pmd usage: 42 %
pmd thread numa_id 0 core_id 0:
  isolated : false
"""

    def test_parse_basic(self):
        """Verify we extract correct core IDs and rxq mappings."""
        pmds = pmd_discovery._parse_pmd_rxq_show(self.SAMPLE_OUTPUT, 12345)
        cores = [p["core"] for p in pmds if p.get("tid") is not None]
        for p in pmds:
            self.assertEqual(p["process"], "ovs-vswitchd")
            self.assertEqual(p["pid"], 12345)

    def test_rxq_parsing(self):
        """Verify rxq port/queue extraction."""
        pmds = pmd_discovery._parse_pmd_rxq_show(self.SAMPLE_OUTPUT, 12345)
        for p in pmds:
            if p["core"] == 4:
                self.assertEqual(len(p["rxqs"]), 2)
                self.assertEqual(p["rxqs"][0]["port"], "dpdk-p0")
                self.assertEqual(p["rxqs"][0]["queue"], 0)
                break


class TestExtractTopFunctions(unittest.TestCase):
    """Test top function extraction from folded stacks."""

    def test_basic(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".folded",
                                         delete=False) as f:
            f.write("ovs-vswitchd;dp_netdev_process_rxq_port;dpcls_lookup 500\n")
            f.write("ovs-vswitchd;dp_netdev_process_rxq_port;emc_processing 300\n")
            f.write("ovs-vswitchd;dp_netdev_process_rxq_port;netdev_send 200\n")
            f.name
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))))
            pp = SourceFileLoader(
                "ebpf_dpdk_post_process",
                os.path.join(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))), "ebpf-dpdk-post-process")
            ).load_module()

            top = pp.extract_top_functions(f.name)
            self.assertEqual(len(top), 3)
            self.assertEqual(top[0][0], "dpcls_lookup")
            self.assertAlmostEqual(top[0][1], 50.0)
            self.assertEqual(top[1][0], "emc_processing")
            self.assertAlmostEqual(top[1][1], 30.0)
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
