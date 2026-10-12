#!/usr/bin/env python3
"""Tests for MAP-E profiles, port sets, rate calculations, and launcher wiring."""

import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from tests.lib.config import MAPE_TARGETS, load_mape_target_profiles
from tests.lib.mape import (
    build_trex_target,
    derive_ce_prefix,
    derive_ea_bits,
    estimate_wire_rates,
    generate_mape_ports,
    is_valid_mape_port,
    max_inner_frame_size,
    theoretical_inner_pps,
    validate_mape_profile,
)
from tests.lib.trex import render_trex_cfg
import test_mape_speed as mape_launcher


class MapeConfigTests(unittest.TestCase):
    def test_launcher_continues_after_test_and_recovery_failures(self):
        script = REPO_DIR / "tests" / "run_router_bench_mape_stl_throughput_pps.py"
        test_plan = [
            {
                "key": f"test_{index}",
                "run_arg": "run_stl_throughput",
                "name": f"MAP-E test {index}",
                "script": script,
                "out_dir_name": f"test_{index}",
                "default": True,
            }
            for index in range(1, 4)
        ]
        with tempfile.TemporaryDirectory() as out_dir, mock.patch.object(
            sys,
            "argv",
            [
                "test_mape_speed.py",
                "--target",
                "i226_1g_mape",
                "--out-dir",
                out_dir,
                "--run-all",
            ],
        ), mock.patch.object(
            mape_launcher,
            "TEST_PLAN",
            test_plan,
        ), mock.patch.object(
            mape_launcher,
            "child_long_options",
            return_value={"--repeat"},
        ), mock.patch.object(
            mape_launcher.subprocess,
            "call",
            side_effect=[1, 1, 0, 0, 0],
        ) as call_mock:
            with self.assertRaises(SystemExit) as exit_context:
                mape_launcher.main()

        self.assertEqual(exit_context.exception.code, 1)
        self.assertEqual(call_mock.call_count, 5)
    def test_profiles_load_and_validate(self):
        self.assertEqual(load_mape_target_profiles(), MAPE_TARGETS)
        self.assertEqual(
            list(MAPE_TARGETS),
            ["i226_1g_mape", "i226_2p5g_mape", "x540_10g_mape"],
        )
        for name, profile in MAPE_TARGETS.items():
            self.assertIs(validate_mape_profile(name, profile), profile)
            self.assertEqual(profile["benchmark_mode"], "mape")
            self.assertIsNone(profile["ce_map_ipv6"])
            self.assertEqual(profile["supported_tests"], ["mape_stl_throughput"])

    def test_trex_uses_ipv4_on_both_measurement_ports(self):
        profile = MAPE_TARGETS["i226_1g_mape"]
        cfg = render_trex_cfg(build_trex_target(profile))
        self.assertIn("port_info:", cfg)
        self.assertIn("ip         : 192.168.0.2", cfg)
        self.assertIn("default_gw : 192.168.0.1", cfg)
        self.assertIn("ip         : 198.51.100.254", cfg)
        self.assertIn("default_gw : 198.51.100.1", cfg)
        self.assertNotIn("2001:db8", cfg)

    def test_rule_ea_bits_and_ce_prefix(self):
        ea_bits = derive_ea_bits("198.18.0.12", "198.18.0.0/24", 3, 8)
        self.assertEqual(ea_bits, 0x0C03)
        self.assertEqual(
            str(derive_ce_prefix("2001:db8:200::/48", ea_bits, 16)),
            "2001:db8:200:c03::/64",
        )

    def test_port_set_matches_psid_3(self):
        ports = generate_mape_ports(psid=3, psid_offset=4, psid_length=8)
        self.assertEqual(len(ports), 240)
        self.assertEqual(ports[:16], list(range(4144, 4160)))
        self.assertEqual(ports[16:32], list(range(8240, 8256)))
        self.assertEqual(ports[-16:], list(range(61488, 61504)))
        self.assertTrue(all(is_valid_mape_port(port) for port in ports))
        self.assertFalse(is_valid_mape_port(48))
        self.assertFalse(is_valid_mape_port(4160))

    def test_mtu_and_outer_wire_rate(self):
        self.assertEqual(max_inner_frame_size(1460), 1478)
        pps = theoretical_inner_pps(1.0, 1478, 20, 40)
        rates = estimate_wire_rates(pps, 1478, 20, 40)
        self.assertAlmostEqual(rates["outer_line_estimated_bps"], 1_000_000_000.0)
        self.assertLess(rates["inner_ipv4_bps"], rates["outer_line_estimated_bps"])

    def test_fixed_ce_map_ipv6_is_rejected(self):
        profile = copy.deepcopy(MAPE_TARGETS["i226_1g_mape"])
        profile["ce_map_ipv6"] = "2001:db8:200:c03::1234"
        with self.assertRaisesRegex(ValueError, "VPP owns MAP encapsulation"):
            validate_mape_profile("invalid", profile)

    def test_launcher_dry_run_selects_only_mape_stl(self):
        launcher = REPO_DIR / "test_mape_speed.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(launcher),
                "--target",
                "i226_1g_mape",
                "--run-stl-throughput",
                "--dry-run",
                "--duration",
                "1",
                "--no-rate-search",
            ],
            cwd=str(REPO_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("run_router_bench_mape_stl_throughput_pps.py", proc.stdout)
        self.assertNotIn("run_router_bench_dslite_", proc.stdout)

    def test_common_lan_source_port_range_is_forwarded_to_mape_stl(self):
        launcher = REPO_DIR / "test_mape_speed.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(launcher),
                "--target",
                "i226_1g_mape",
                "--run-stl-throughput",
                "--lan-source-port-range",
                "38564-39563",
                "--dry-run",
            ],
            cwd=str(REPO_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        command = next(
            line
            for line in proc.stdout.splitlines()
            if "run_router_bench_mape_stl_throughput_pps.py" in line
        )
        self.assertIn("--lan-source-port-range 38564-39563", command)

    def test_runner_help_without_trex_install(self):
        runner = REPO_DIR / "tests" / "run_router_bench_mape_stl_throughput_pps.py"
        proc = subprocess.run(
            [sys.executable, str(runner), "--help"],
            cwd=str(REPO_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("--server-ipv4", proc.stdout)
        self.assertIn("--shared-ipv4", proc.stdout)


if __name__ == "__main__":
    unittest.main()
