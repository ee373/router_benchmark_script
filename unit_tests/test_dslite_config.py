#!/usr/bin/env python3
"""DS-Lite configuration and wire-rate checks without benchmark hardware."""

import copy
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_dslite_speed as dslite_launcher

from tests.lib.config import DSLITE_TARGETS, load_defaults, load_dslite_target_profiles
from tests.lib.dslite import (
    build_trex_target,
    estimate_wire_rates,
    max_inner_frame_size,
    theoretical_inner_pps,
    udp_payload_bytes,
    validate_dslite_profile,
)
from tests.run_router_bench_dslite_stl_throughput_pps import (
    add_tunnel_capacity_rate_point,
)
from tests.run_router_bench_v4_tcp_scratch_max_sessions import (
    build_server_ip_list,
    planned_session_count,
    session_endpoint_at,
)
from tests.run_router_bench_dslite_astf_sessions_cps import (
    build_server_ip_candidates as build_astf_server_ip_candidates,
)
from tests.run_router_bench_recovery_check import run_astf_recovery
from tests.run_router_bench_dslite_iperf3_latency import build_latency_targets


class DsliteConfigTest(unittest.TestCase):
    def test_profile_loader_matches_compatibility_module(self):
        self.assertEqual(load_dslite_target_profiles(), DSLITE_TARGETS)

    def test_all_profiles_are_valid(self):
        for name, profile in DSLITE_TARGETS.items():
            self.assertIs(validate_dslite_profile(name, profile), profile)
            self.assertEqual(profile["benchmark_mode"], "dslite")
            self.assertEqual(
                profile["supported_tests"],
                [
                    "dslite_stl_throughput",
                    "dslite_tcp_scratch_max_sessions",
                    "dslite_astf_sessions_cps",
                    "dslite_astf_nat_stability",
                    "dslite_l4_latency",
                    "dslite_tcp_source_port_scan",
                ],
            )
            self.assertTrue(profile["supports_high_cps_test"])
            self.assertTrue(profile["supports_max_sessions_test"])

    def test_wan_arp_target_is_not_nat_pool(self):
        profile = copy.deepcopy(DSLITE_TARGETS["i226_1g_dslite"])
        profile["wan_arp_target"] = "192.0.2.1"
        with self.assertRaisesRegex(ValueError, "must not be inside nat_pool_cidr"):
            validate_dslite_profile("invalid", profile)

    def test_server_ipv4_must_be_inside_declared_network_and_not_gateway(self):
        profile = copy.deepcopy(DSLITE_TARGETS["i226_1g_dslite"])
        profile["server_ipv4"] = "203.0.113.10"
        with self.assertRaisesRegex(ValueError, "must be in server_network"):
            validate_dslite_profile("invalid", profile)
        profile["server_ipv4"] = profile["wan_gateway_ip"]
        with self.assertRaisesRegex(ValueError, "must not be the WAN gateway"):
            validate_dslite_profile("invalid", profile)

    def test_trex_aliases_use_link_and_arp_addresses(self):
        profile = DSLITE_TARGETS["i226_1g_dslite"]
        target = build_trex_target(profile)
        self.assertEqual(target["wan_ip"], "198.51.100.254")
        self.assertEqual(target["wan_gw"], "198.51.100.1")
        self.assertEqual(target["router_wan_ip"], "198.51.100.1")
        self.assertEqual(target["server_ip"], "198.51.100.254")
        self.assertEqual(profile["server_network"], "198.51.100.0/24")
        self.assertEqual(profile["server_ipv4"], "198.51.100.254")
        self.assertNotIn("wan_ip", profile)
        self.assertNotIn("wan_gw", profile)

    def test_frame_sizes_include_fcs(self):
        self.assertEqual(max_inner_frame_size(1460), 1478)
        self.assertEqual(udp_payload_bytes(64), 18)
        self.assertEqual(udp_payload_bytes(1478), 1432)

    def test_64_byte_encapsulation_capacity(self):
        pps = theoretical_inner_pps(1.0, 64, 20, 40)
        rates = estimate_wire_rates(pps, 64, 20, 40)
        self.assertAlmostEqual(rates["inner_ipv4_bps"] / 1e9, 84 / 124, places=9)
        self.assertAlmostEqual(rates["outer_line_estimated_bps"], 1e9, places=3)

    def test_1478_byte_encapsulation_capacity(self):
        pps = theoretical_inner_pps(1.0, 1478, 20, 40)
        rates = estimate_wire_rates(pps, 1478, 20, 40)
        self.assertAlmostEqual(rates["inner_ipv4_bps"] / 1e9, 1498 / 1538, places=9)
        self.assertAlmostEqual(rates["outer_line_estimated_bps"], 1e9, places=3)

    def test_default_grid_can_include_frame_specific_tunnel_capacity(self):
        points = add_tunnel_capacity_rate_point(
            [{"label": "470mbps", "rate_mbps": 470.0, "rate_percent": 47.0, "bps_l1": 470e6}],
            1.0,
            64,
            40,
        )
        self.assertEqual(len(points), 2)
        self.assertAlmostEqual(points[-1]["rate_mbps"], 1000.0 * 84 / 124, places=6)

    def test_max_session_server_addresses_descend_without_gateway(self):
        addresses = build_server_ip_list(
            "198.51.100.254",
            68,
            24,
            reserved_ips=("198.51.100.1",),
        )
        self.assertEqual(len(addresses), 68)
        self.assertEqual(addresses[0], "198.51.100.254")
        self.assertEqual(addresses[-1], "198.51.100.187")
        self.assertNotIn("198.51.100.1", addresses)

    def test_astf_server_candidates_match_profile_network(self):
        profile = DSLITE_TARGETS["i226_1g_dslite"]
        addresses = build_astf_server_ip_candidates(profile, 253)
        self.assertEqual(len(addresses), 253)
        self.assertEqual(addresses[0], "198.51.100.2")
        self.assertEqual(addresses[-1], "198.51.100.254")
        self.assertNotIn(profile["wan_gateway_ip"], addresses)
        self.assertEqual(
            build_astf_server_ip_candidates(profile, 4),
            ["198.51.100.251", "198.51.100.252", "198.51.100.253", "198.51.100.254"],
        )
        with self.assertRaisesRegex(ValueError, "exceeds usable"):
            build_astf_server_ip_candidates(profile, 254)

    def test_max_session_plan_reuses_1000_ports_across_destinations(self):
        addresses = build_server_ip_list("198.51.100.254", 68, 24)
        self.assertEqual(planned_session_count(65535, 64536, 68, 68000), 68000)
        self.assertEqual(
            session_endpoint_at(0, 65535, 64536, addresses),
            (65535, "198.51.100.254"),
        )
        self.assertEqual(
            session_endpoint_at(1000, 65535, 64536, addresses),
            (65535, "198.51.100.253"),
        )
        self.assertEqual(
            session_endpoint_at(67999, 65535, 64536, addresses),
            (64536, "198.51.100.187"),
        )

    def test_max_session_defaults_target_68000_sessions(self):
        defaults = load_defaults("dslite_tcp_scratch")
        self.assertEqual(defaults["source_start_port"] - defaults["source_end_port"] + 1, 1000)
        self.assertEqual(defaults["server_ip_count"], 68)
        self.assertEqual(defaults["max_sessions"], 68000)

    def test_launcher_dry_run(self):
        launcher = Path(__file__).resolve().parents[1] / "test_dslite_speed.py"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(launcher),
                    "--target",
                    "i226_1g_dslite",
                    "--run-stl-throughput",
                    "--dry-run",
                    "--duration",
                    "3",
                    "--repeat",
                    "1",
                    "--no-rate-search",
                ],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("run_router_bench_dslite_stl_throughput_pps.py", proc.stdout)
        self.assertNotIn("run_router_bench_dslite_astf_sessions_cps.py", proc.stdout)
        self.assertNotIn("run_router_bench_dslite_tcp_scratch_max_sessions.py", proc.stdout)
        self.assertNotIn("test_basic_speed.py", proc.stdout)

    def test_launcher_astf_cps_dry_run(self):
        launcher = Path(__file__).resolve().parents[1] / "test_dslite_speed.py"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(launcher),
                    "--target",
                    "i226_1g_dslite",
                    "--run-astf-sessions-cps",
                    "--dry-run",
                    "--duration",
                    "3",
                    "--repeat",
                    "1",
                    "--max-cps",
                    "100",
                ],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("run_router_bench_dslite_astf_sessions_cps.py", proc.stdout)
        self.assertNotIn("run_router_bench_dslite_stl_throughput_pps.py", proc.stdout)
        self.assertNotIn("run_router_bench_dslite_tcp_scratch_max_sessions.py", proc.stdout)

    def test_launcher_max_session_dry_run(self):
        launcher = Path(__file__).resolve().parents[1] / "test_dslite_speed.py"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(launcher),
                    "--target",
                    "i226_1g_dslite",
                    "--run-tcp-scratch-max-sessions",
                    "--dry-run",
                    "--max-sessions",
                    "10000",
                ],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("run_router_bench_dslite_tcp_scratch_max_sessions.py", proc.stdout)
        self.assertIn("--max-sessions 10000", proc.stdout)
        self.assertNotIn("run_router_bench_dslite_stl_throughput_pps.py", proc.stdout)
        self.assertNotIn("run_router_bench_dslite_astf_sessions_cps.py", proc.stdout)

    def test_common_lan_source_port_range_is_forwarded_to_scratch(self):
        launcher = Path(__file__).resolve().parents[1] / "test_dslite_speed.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(launcher),
                "--target",
                "i226_1g_dslite",
                "--run-tcp-scratch-max-sessions",
                "--lan-source-port-range",
                "39563-38564",
                "--dry-run",
            ],
            cwd=str(launcher.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        command = next(
            line
            for line in proc.stdout.splitlines()
            if "run_router_bench_dslite_tcp_scratch_max_sessions.py" in line
        )
        self.assertIn("--lan-source-port-range 38564-39563", command)

    def test_launcher_default_dry_run_includes_all_tests(self):
        launcher = Path(__file__).resolve().parents[1] / "test_dslite_speed.py"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(launcher),
                    "--target",
                    "i226_1g_dslite",
                    "--dry-run",
                    "--duration",
                    "3",
                    "--repeat",
                    "1",
                    "--max-cps",
                    "100",
                    "--cps-steps",
                    "10,50,100",
                ],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("run_router_bench_dslite_stl_throughput_pps.py", proc.stdout)
        self.assertIn("run_router_bench_dslite_tcp_scratch_max_sessions.py", proc.stdout)
        self.assertIn("run_router_bench_dslite_astf_sessions_cps.py", proc.stdout)
        self.assertIn("run_router_bench_dslite_iperf3_latency.py", proc.stdout)
        self.assertNotIn("run_router_bench_dslite_astf_nat_stability.py", proc.stdout)
        stl_command = next(
            line
            for line in proc.stdout.splitlines()
            if "run_router_bench_dslite_stl_throughput_pps.py" in line
        )
        self.assertNotIn("--max-cps", stl_command)
        self.assertNotIn("10,50,100", stl_command)

    def test_launcher_astf_nat_stability_dry_run(self):
        launcher = Path(__file__).resolve().parents[1] / "test_dslite_speed.py"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(launcher),
                    "--target",
                    "i226_1g_dslite",
                    "--run-astf-natstability",
                    "--dry-run",
                    "--soak-duration-sec",
                    "60",
                    "--protocols",
                    "tcp,udp",
                ],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("run_router_bench_dslite_astf_nat_stability.py", proc.stdout)
        self.assertIn("--soak-duration-sec 60", proc.stdout)
        self.assertNotIn("run_router_bench_dslite_stl_throughput_pps.py", proc.stdout)
        self.assertNotIn("run_router_bench_dslite_astf_sessions_cps.py", proc.stdout)
        self.assertNotIn("run_router_bench_dslite_tcp_scratch_max_sessions.py", proc.stdout)
        self.assertNotIn("run_router_bench_dslite_iperf3_latency.py", proc.stdout)

    def test_latency_target_uses_dslite_server_and_aftr_gateway(self):
        overrides = type(
            "Overrides",
            (),
            {
                "wan_link_ip": "",
                "wan_gateway_ip": "",
                "wan_arp_target": "",
                "server_ip": "",
                "nat_pool_cidr": "",
            },
        )()
        target = build_latency_targets(overrides)["i226_1g_dslite"]
        self.assertEqual(target["wan_ip"], "198.51.100.254")
        self.assertEqual(target["router_wan_ip"], "198.51.100.1")
        self.assertEqual(target["lan_ip"], "192.168.0.2")

    def test_launcher_l4_latency_dry_run(self):
        launcher = Path(__file__).resolve().parents[1] / "test_dslite_speed.py"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(launcher),
                    "--target",
                    "i226_1g_dslite",
                    "--run-l4-latency",
                    "--dry-run",
                    "--duration",
                    "3",
                    "--load-percentages",
                    "0,50,99",
                    "--l4-transport",
                    "udp",
                ],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("run_router_bench_dslite_iperf3_latency.py", proc.stdout)
        self.assertIn("--load-percentages 0,50,99", proc.stdout)
        self.assertIn("--l4-transport udp", proc.stdout)
        self.assertNotIn("run_router_bench_dslite_stl_throughput_pps.py", proc.stdout)
        self.assertNotIn("run_router_bench_dslite_astf_sessions_cps.py", proc.stdout)
        self.assertNotIn("run_router_bench_dslite_astf_nat_stability.py", proc.stdout)
        self.assertNotIn("run_router_bench_dslite_tcp_scratch_max_sessions.py", proc.stdout)

    def test_launcher_run_all_includes_astf_nat_stability(self):
        launcher = Path(__file__).resolve().parents[1] / "test_dslite_speed.py"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(launcher),
                    "--target",
                    "i226_1g_dslite",
                    "--run-all",
                    "--dry-run",
                    "--soak-duration-sec",
                    "60",
                ],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("run_router_bench_dslite_stl_throughput_pps.py", proc.stdout)
        self.assertIn("run_router_bench_dslite_tcp_scratch_max_sessions.py", proc.stdout)
        self.assertIn("run_router_bench_dslite_astf_sessions_cps.py", proc.stdout)
        self.assertIn("run_router_bench_dslite_astf_nat_stability.py", proc.stdout)
        self.assertIn("run_router_bench_dslite_iperf3_latency.py", proc.stdout)
        self.assertNotIn("run_router_bench_dslite_tcp_source_port_scan.py", proc.stdout)

    def test_source_port_scan_is_explicit_only(self):
        launcher = Path(__file__).resolve().parents[1] / "test_dslite_speed.py"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as cwd:
            default_run = subprocess.run(
                [sys.executable, str(launcher), "--target", "i226_1g_dslite", "--dry-run"],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            explicit_run = subprocess.run(
                [
                    sys.executable,
                    str(launcher),
                    "--target",
                    "i226_1g_dslite",
                    "--run-dslite-source-port-scan",
                    "--source-start-port",
                    "49153",
                    "--source-end-port",
                    "49151",
                    "--dry-run",
                ],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

        self.assertEqual(default_run.returncode, 0, default_run.stderr)
        self.assertNotIn("run_router_bench_dslite_tcp_source_port_scan.py", default_run.stdout)
        self.assertEqual(explicit_run.returncode, 0, explicit_run.stderr)
        self.assertIn("run_router_bench_dslite_tcp_source_port_scan.py", explicit_run.stdout)
        self.assertIn("--source-start-port 49153", explicit_run.stdout)
        self.assertIn("--source-end-port 49151", explicit_run.stdout)
        self.assertNotIn("run_router_bench_dslite_stl_throughput_pps.py", explicit_run.stdout)

    def test_launcher_continues_after_test_and_recovery_failures(self):
        with tempfile.TemporaryDirectory() as out_dir, mock.patch.object(
            sys,
            "argv",
            [
                "test_dslite_speed.py",
                "--target",
                "i226_1g_dslite",
                "--out-dir",
                out_dir,
                "--run-stl-throughput",
                "--run-tcp-scratch-max-sessions",
                "--run-astf-sessions-cps",
            ],
        ), mock.patch.object(
            dslite_launcher,
            "child_long_options",
            return_value={"--repeat"},
        ), mock.patch.object(
            dslite_launcher.subprocess,
            "call",
            side_effect=[1, 1, 0, 0, 0, 0, 0],
        ) as call_mock:
            with self.assertRaises(SystemExit) as exit_context:
                dslite_launcher.main()

        self.assertEqual(exit_context.exception.code, 1)
        self.assertEqual(call_mock.call_count, 7)
        commands = [call.args[0] for call in call_mock.call_args_list]
        self.assertTrue(any("run_router_bench_dslite_stl_throughput_pps.py" in command[1] for command in commands))
        self.assertTrue(any("run_router_bench_dslite_tcp_scratch_max_sessions.py" in command[1] for command in commands))
        astf_commands = [
            command for command in commands
            if "run_router_bench_dslite_astf_sessions_cps.py" in command[1]
        ]
        self.assertEqual(len(astf_commands), 2)
        self.assertTrue(any("--server-ip-count 1" in " ".join(command) for command in astf_commands))
        self.assertTrue(any("--server-ip-count 253" in " ".join(command) for command in astf_commands))
        astf_transition_recovery = [
            command for command in commands
            if "run_router_bench_recovery_check.py" in command[1]
            and "--next-test DS-Lite ASTF short-session TCP CPS (253 server IPs)"
            in " ".join(command)
        ]
        self.assertEqual(len(astf_transition_recovery), 1)
        self.assertIn("--server-ip-count 1", " ".join(astf_transition_recovery[0]))

    def test_dslite_astf_cps_help_without_trex_install(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "tests"
            / "run_router_bench_dslite_astf_sessions_cps.py"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                [sys.executable, str(script), "--help"],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--cps-steps", proc.stdout)
        self.assertIn("--wan-link-ip", proc.stdout)
        self.assertIn("--server-ip-count", proc.stdout)

    def test_dslite_default_astf_cps_uses_one_and_253_server_ips(self):
        launcher = Path(__file__).resolve().parents[1] / "test_dslite_speed.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(launcher),
                "--target",
                "i226_1g_dslite",
                "--dry-run",
            ],
            cwd=str(launcher.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        commands = [
            line for line in proc.stdout.splitlines()
            if "run_router_bench_dslite_astf_sessions_cps.py" in line
        ]
        self.assertEqual(len(commands), 2, proc.stdout)
        self.assertTrue(any("--server-ip-count 1" in line for line in commands))
        self.assertTrue(any("--server-ip-count 253" in line for line in commands))

    def test_dslite_inter_test_recovery_uses_shared_astf_preflight(self):
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
        args = type(
            "RecoveryArgs",
            (),
            {
                "server_ip_count": 4,
                "preflight_success_consecutive": 3,
                "preflight_probe_interval_sec": 2.0,
                "preflight_timeout_sec": 300.0,
                "astf_tcp_port": 80,
                "previous_test": "DS-Lite CPS 4 IP",
                "next_test": "DS-Lite CPS 253 IP",
            },
        )()
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
            result = run_astf_recovery(
                build_trex_target(DSLITE_TARGETS["i226_1g_dslite"]),
                "dslite",
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
            ("198.51.100.251", "198.51.100.254"),
        )
        self.assertEqual(astf_preflight.call_args.kwargs["tcp_port"], 80)
        self.assertEqual(astf_preflight.call_args.kwargs["required_successes"], 3)

    def test_dslite_max_session_help_without_trex_install(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "tests"
            / "run_router_bench_dslite_tcp_scratch_max_sessions.py"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                [sys.executable, str(script), "--help"],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--server-ip-count", proc.stdout)
        self.assertIn("--max-sessions", proc.stdout)

    def test_dslite_astf_nat_stability_help_without_trex_install(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "tests"
            / "run_router_bench_dslite_astf_nat_stability.py"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                [sys.executable, str(script), "--help"],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--soak-duration-sec", proc.stdout)
        self.assertIn("--protocols", proc.stdout)
        self.assertIn("--wan-link-ip", proc.stdout)

    def test_dslite_l4_latency_help_without_trex_install(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "tests"
            / "run_router_bench_dslite_iperf3_latency.py"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                [sys.executable, str(script), "--help"],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--load-percentages", proc.stdout)
        self.assertIn("--l4-transport", proc.stdout)
        self.assertIn("--wan-link-ip", proc.stdout)


    def test_run_all_repeat_excludes_nat_stability(self):
        launcher = Path(__file__).resolve().parents[1] / "test_dslite_speed.py"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(launcher),
                    "--target",
                    "i226_1g_dslite",
                    "--run-all",
                    "--repeat",
                    "2",
                    "--dry-run",
                ],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        nat_line = next(
            line
            for line in proc.stdout.splitlines()
            if "run_router_bench_dslite_astf_nat_stability.py" in line
        )
        self.assertNotIn("--repeat", nat_line)
        supported_lines = [
            line
            for line in proc.stdout.splitlines()
            if "run_router_bench_dslite_" in line
            and "astf_nat_stability" not in line
        ]
        self.assertTrue(supported_lines)
        self.assertTrue(all("--repeat 2" in line for line in supported_lines))


if __name__ == "__main__":
    unittest.main()
