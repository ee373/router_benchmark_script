#!/usr/bin/env python3
"""Tests for MAP-E profiles, port sets, rate calculations, and launcher wiring."""

import copy
from ipaddress import ip_address, ip_network
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from tests.lib.config import MAPE_TARGETS, load_defaults, load_mape_target_profiles
from tests.lib.astf import resolve_astf_cores
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
from tests.run_router_bench_v4_tcp_scratch_max_sessions import (
    build_server_ip_list,
    planned_session_count,
    session_endpoint_at,
)
import test_mape_speed as mape_launcher


_MAPE_SCRATCH_DEFAULTS = load_defaults("mape_tcp_scratch")


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

    def test_mape_inter_test_recovery_uses_shared_astf_preflight(self):
        import types

        from tests import run_router_bench_recovery_check as recovery

        api = types.ModuleType("trex.astf.api")
        client_instance = mock.Mock()
        api.ASTFClient = mock.Mock(return_value=client_instance)
        modules = {
            "trex": types.ModuleType("trex"),
            "trex.astf": types.ModuleType("trex.astf"),
            "trex.astf.api": api,
        }
        args = SimpleNamespace(
            server_ip_count=4,
            astf_tcp_port=80,
            preflight_success_consecutive=3,
            preflight_probe_interval_sec=2.0,
            preflight_timeout_sec=300.0,
            previous_test="MAP-E CPS 4 IP",
            next_test="MAP-E CPS 253 IP",
        )
        expected_result = {"preflight_ok": 1}

        with tempfile.TemporaryDirectory() as out_dir, mock.patch.dict(
            sys.modules, modules
        ), mock.patch.object(recovery, "add_trex_api_path") as add_api_path, mock.patch.object(
            recovery, "prepare_trex_interfaces_from_profile"
        ) as prepare_interfaces, mock.patch.object(
            recovery, "install_trex_cfg"
        ) as install_cfg, mock.patch.object(
            recovery, "resolve_astf_cores", return_value=1
        ), mock.patch.object(
            recovery, "TrexAstfServer"
        ) as trex_server_class, mock.patch.object(
            recovery,
            "run_astf_roundtrip_preflight_consecutive",
            return_value=expected_result,
        ) as astf_preflight:
            server = trex_server_class.return_value
            result = recovery.run_astf_recovery(
                recovery.resolve_target("mape", "i226_1g_mape", "v4"),
                "mape",
                args,
                Path(out_dir),
            )

        self.assertIs(result, expected_result)
        add_api_path.assert_called_once_with()
        prepare_interfaces.assert_called_once()
        install_cfg.assert_called_once()
        server.start.assert_called_once_with()
        server.stop.assert_called_once_with()
        client_instance.connect.assert_called_once_with()
        client_instance.stop.assert_called_once_with()
        client_instance.reset.assert_called_once_with()
        client_instance.disconnect.assert_called_once_with()
        self.assertEqual(
            astf_preflight.call_args.kwargs["server_ip_range"],
            ("203.0.113.2", "203.0.113.5"),
        )
        self.assertEqual(astf_preflight.call_args.kwargs["tcp_port"], 80)
        self.assertEqual(astf_preflight.call_args.kwargs["required_successes"], 3)
        self.assertEqual(astf_preflight.call_args.kwargs["interval_sec"], 2.0)
        self.assertEqual(astf_preflight.call_args.kwargs["timeout_sec"], 300.0)
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
            self.assertEqual(
                profile["supported_tests"],
                [
                    "mape_stl_throughput",
                    "mape_tcp_scratch_max_sessions",
                    "mape_astf_sessions_cps",
                    "mape_astf_nat_stability",
                    "mape_l4_latency",
                ],
            )
        self.assertEqual(resolve_astf_cores(MAPE_TARGETS["x540_10g_mape"]), 1)

    def test_profile_allows_a_conservative_tcp_mss_within_inner_mtu(self):
        profile = copy.deepcopy(MAPE_TARGETS["i226_1g_mape"])
        profile["tcp_mss"] = 1380
        self.assertIs(validate_mape_profile("conservative", profile), profile)

        profile["tcp_mss"] = 1421
        with self.assertRaisesRegex(ValueError, "tcp_mss must be between 1 and 1420"):
            validate_mape_profile("too_large", profile)

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

    def test_scratch_plan_uses_all_psid_ports_and_server_addresses(self):
        profile = MAPE_TARGETS[_MAPE_SCRATCH_DEFAULTS["target"]]
        ports = generate_mape_ports(
            profile["psid"],
            profile["psid_offset"],
            profile["psid_length"],
        )
        network = ip_network(profile["server_network"], strict=True)
        server_ips = build_server_ip_list(
            _MAPE_SCRATCH_DEFAULTS["server_ip"],
            _MAPE_SCRATCH_DEFAULTS["server_ip_count"],
            network.prefixlen,
            reserved_ips=(str(network.network_address + 1),),
        )
        self.assertEqual(len(server_ips), 253)
        self.assertEqual(server_ips[0], "203.0.113.254")
        self.assertEqual(server_ips[-1], "203.0.113.2")
        self.assertNotIn("203.0.113.1", server_ips)
        self.assertTrue(all(ip_address(value) in network for value in server_ips))
        reduced_server_ips = build_server_ip_list(
            _MAPE_SCRATCH_DEFAULTS["server_ip"],
            68,
            network.prefixlen,
            reserved_ips=(str(network.network_address + 1),),
            required_ips=(profile["server_ipv4"],),
        )
        self.assertEqual(len(reduced_server_ips), 68)
        self.assertEqual(reduced_server_ips[0], "203.0.113.254")
        self.assertEqual(reduced_server_ips[-1], profile["server_ipv4"])
        self.assertEqual(
            planned_session_count(
                65535,
                1,
                len(reduced_server_ips),
                source_ports=ports,
            ),
            16320,
        )
        self.assertEqual(
            planned_session_count(65535, 1, len(server_ips), source_ports=ports),
            60720,
        )
        self.assertEqual(
            session_endpoint_at(0, 65535, 1, server_ips, source_ports=ports),
            (4144, "203.0.113.254"),
        )
        self.assertEqual(
            session_endpoint_at(239, 65535, 1, server_ips, source_ports=ports),
            (61503, "203.0.113.254"),
        )
        self.assertEqual(
            session_endpoint_at(240, 65535, 1, server_ips, source_ports=ports),
            (4144, "203.0.113.253"),
        )
        self.assertEqual(
            session_endpoint_at(60719, 65535, 1, server_ips, source_ports=ports),
            (61503, "203.0.113.2"),
        )

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

    def test_launcher_dry_run_selects_mape_tcp_scratch(self):
        launcher = REPO_DIR / "test_mape_speed.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(launcher),
                "--target",
                "i226_1g_mape",
                "--run-tcp-scratch-max-sessions",
                "--dry-run",
            ],
            cwd=str(REPO_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("run_router_bench_mape_tcp_scratch_max_sessions.py", proc.stdout)

    def test_launcher_dry_run_selects_mape_astf_cps(self):
        launcher = REPO_DIR / "test_mape_speed.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(launcher),
                "--target",
                "i226_1g_mape",
                "--run-astf-sessions-cps",
                "--dry-run",
                "--max-cps",
                "10",
            ],
            cwd=str(REPO_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("run_router_bench_mape_astf_sessions_cps.py", proc.stdout)
        self.assertIn("--max-cps 10", proc.stdout)

    def test_launcher_dry_run_selects_mape_l4_latency(self):
        launcher = REPO_DIR / "test_mape_speed.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(launcher),
                "--target",
                "i226_1g_mape",
                "--run-l4-latency",
                "--dry-run",
                "--duration",
                "3",
                "--load-percentages",
                "0,50,99",
                "--l4-transport",
                "tcp",
            ],
            cwd=str(REPO_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("run_router_bench_mape_iperf3_latency.py", proc.stdout)
        self.assertIn("--load-percentages 0,50,99", proc.stdout)
        self.assertIn("--l4-transport tcp", proc.stdout)

    def test_launcher_default_includes_both_mape_astf_cps_ranges(self):
        launcher = REPO_DIR / "test_mape_speed.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(launcher),
                "--target",
                "i226_1g_mape",
                "--dry-run",
            ],
            cwd=str(REPO_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        commands = [
            line for line in proc.stdout.splitlines()
            if "run_router_bench_mape_astf_sessions_cps.py" in line
        ]
        self.assertEqual(len(commands), 2, proc.stdout)
        self.assertTrue(any("--server-ip-count 4" in line for line in commands))
        self.assertTrue(any("--server-ip-count 253" in line for line in commands))
        recovery_commands = [
            line for line in proc.stdout.splitlines()
            if "run_router_bench_recovery_check.py" in line
            and "--previous-test MAP-E ASTF short-session TCP CPS (4 server IPs)" in line
            and "--next-test MAP-E ASTF short-session TCP CPS (253 server IPs)" in line
        ]
        self.assertTrue(recovery_commands, proc.stdout)
        self.assertIn("--server-ip-count 4", recovery_commands[0])

    def test_recovery_astf_args_match_previous_condition_and_overrides(self):
        from test_mape_speed import recovery_astf_args

        self.assertEqual(
            recovery_astf_args(
                {"args": ["--server-ip-count", "4"]},
                ["--server-ip-count", "8", "--astf-tcp-port", "8080"],
            ),
            [
                "--server-ip-count",
                "8",
                "--astf-tcp-port",
                "8080",
            ],
        )

    def test_launcher_run_all_includes_mape_astf_cps(self):
        launcher = REPO_DIR / "test_mape_speed.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(launcher),
                "--target",
                "i226_1g_mape",
                "--run-all",
                "--dry-run",
            ],
            cwd=str(REPO_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        commands = [
            line for line in proc.stdout.splitlines()
            if "run_router_bench_mape_astf_sessions_cps.py" in line
        ]
        self.assertEqual(len(commands), 2, proc.stdout)
        self.assertTrue(any("--server-ip-count 4" in line for line in commands))
        self.assertTrue(any("--server-ip-count 253" in line for line in commands))

    def test_mape_astf_target_uses_external_server_address(self):
        from tests.run_router_bench_mape_astf_sessions_cps import (
            build_server_ip_candidates,
            merge_mape_target_with_overrides,
        )

        target = build_trex_target(MAPE_TARGETS["i226_1g_mape"])
        self.assertEqual(target["wan_ip"], "198.51.100.254")
        self.assertEqual(target["server_ipv4"], "203.0.113.2")
        overridden_target = merge_mape_target_with_overrides(
            MAPE_TARGETS["i226_1g_mape"],
            SimpleNamespace(
                interfaces=None,
                lan_ip="",
                lan_gw="",
                wan_link_ip="",
                wan_gateway_ip="",
                wan_arp_target="",
                server_ipv4="203.0.113.3",
                line_rate_gbps=None,
                cores=None,
            ),
        )
        self.assertEqual(overridden_target["server_ipv4"], "203.0.113.3")
        candidates = build_server_ip_candidates(
            MAPE_TARGETS["i226_1g_mape"],
            server_ip_count=253,
        )
        self.assertEqual(len(candidates), 253)
        self.assertEqual(candidates[0], "203.0.113.2")
        self.assertEqual(candidates[-1], "203.0.113.254")
        self.assertEqual(
            build_server_ip_candidates(
                MAPE_TARGETS["i226_1g_mape"],
                server_ip_count=8,
            ),
            [
                "203.0.113.2",
                "203.0.113.3",
                "203.0.113.4",
                "203.0.113.5",
                "203.0.113.6",
                "203.0.113.7",
                "203.0.113.8",
                "203.0.113.9",
            ],
        )
        self.assertEqual(
            build_server_ip_candidates(
                {**MAPE_TARGETS["i226_1g_mape"], "server_ipv4": "203.0.113.254"},
                server_ip_count=8,
            )[-1],
            "203.0.113.254",
        )
        with self.assertRaisesRegex(ValueError, "must be positive"):
            build_server_ip_candidates(MAPE_TARGETS["i226_1g_mape"], 0)
        with self.assertRaisesRegex(ValueError, "exceeds usable"):
            build_server_ip_candidates(MAPE_TARGETS["i226_1g_mape"], 254)
        with self.assertRaisesRegex(ValueError, "reserved"):
            build_server_ip_candidates(
                {**MAPE_TARGETS["i226_1g_mape"], "server_ipv4": "203.0.113.1"},
                8,
            )

        fake_api = type("FakeApi", (), {})

        class Distribution:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class Generator:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        fake_api.ASTFIPGen = Generator
        fake_api.ASTFIPGenDist = Distribution
        fake_api.ASTFIPGenGlobal = lambda **kwargs: kwargs
        modules = {
            "trex": type(sys)("trex"),
            "trex.astf": type(sys)("trex.astf"),
            "trex.astf.api": fake_api,
        }
        with mock.patch.dict(sys.modules, modules):
            from tests.lib.astf import make_ip_gen

            ip_gen = make_ip_gen(
                target,
                server_ip_range=("203.0.113.2", "203.0.113.9"),
            )
            default_ip_gen = make_ip_gen(target)
            with self.assertRaisesRegex(ValueError, "ordered IPv4 range"):
                make_ip_gen(
                    target,
                    server_ip_range=("203.0.113.9", "203.0.113.2"),
                )

        self.assertEqual(
            ip_gen.kwargs["dist_client"].kwargs["ip_range"],
            ["192.168.0.2", "192.168.0.2"],
        )
        self.assertEqual(
            ip_gen.kwargs["dist_server"].kwargs["ip_range"],
            ["203.0.113.2", "203.0.113.9"],
        )
        self.assertEqual(
            default_ip_gen.kwargs["dist_server"].kwargs["ip_range"],
            ["198.51.100.254", "198.51.100.254"],
        )
    def test_cps_runner_uses_mape_server_for_preflight_and_measurement(self):
        from tests import run_router_bench_v4_astf_sessions_cps as cps_runner

        args = SimpleNamespace(
            cps_steps=[1],
            max_cps=1,
            duration=1.0,
            loss_stop_threshold=0.5,
            session_success_stop_threshold=0.5,
            astf_tcp_port=80,
            preflight_success_consecutive=1,
            preflight_probe_interval_sec=0,
            preflight_timeout_sec=1,
            cps_teardown_timeout=0,
            representative_cps=None,
            stable_success_threshold=0.99,
            stable_loss_threshold=0.01,
            inter_test_sleep=0,
        )
        with tempfile.TemporaryDirectory() as out_dir, mock.patch.object(
            cps_runner,
            "run_astf_roundtrip_preflight_consecutive",
            return_value={},
        ) as preflight, mock.patch.object(
            cps_runner,
            "make_short_session_profile",
            return_value=object(),
        ) as make_profile, mock.patch.object(
            cps_runner,
            "run_astf_profile",
            return_value={},
        ), mock.patch.object(
            cps_runner,
            "calc_astf_metrics",
            return_value={
                "session_success_rate": 1.0,
                "packet_loss_rate": 0.0,
                "throughput_loss_rate": 0.0,
            },
        ), mock.patch.object(
            cps_runner,
            "is_row_stable",
            return_value=True,
        ), mock.patch.object(cps_runner.time, "sleep"):
            cps_runner.run_cps_search(
                client=object(),
                target=build_trex_target(MAPE_TARGETS["i226_1g_mape"]),
                raw_writer=mock.Mock(),
                args=args,
                repeat_index=1,
                out_dir=Path(out_dir),
                astf_server_ip_range=("203.0.113.2", "203.0.113.9"),
            )

        self.assertEqual(
            preflight.call_args.kwargs["server_ip_range"],
            ("203.0.113.2", "203.0.113.9"),
        )
        self.assertEqual(
            make_profile.call_args.kwargs["server_ip_range"],
            ("203.0.113.2", "203.0.113.9"),
        )

    def test_launcher_rejects_ipv4_port_range_for_mape_scratch(self):
        with mock.patch.object(
            sys,
            "argv",
            [
                "test_mape_speed.py",
                "--run-tcp-scratch-max-sessions",
                "--lan-source-port-range",
                "4144-4160",
                "--dry-run",
            ],
        ):
            with self.assertRaisesRegex(SystemExit, "profile PSID source-port set"):
                mape_launcher.main()

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

    def test_scratch_runner_help_hides_contiguous_port_options(self):
        runner = (
            REPO_DIR
            / "tests"
            / "run_router_bench_mape_tcp_scratch_max_sessions.py"
        )
        proc = subprocess.run(
            [sys.executable, str(runner), "--help"],
            cwd=str(REPO_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        help_text = " ".join(proc.stdout.split())
        self.assertIn("203.0.113.254", help_text)
        self.assertNotIn("--source-start-port", help_text)
        self.assertNotIn("--lan-source-port-range", help_text)


if __name__ == "__main__":
    unittest.main()
