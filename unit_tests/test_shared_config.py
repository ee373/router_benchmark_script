#!/usr/bin/env python3
"""Configuration and compatibility checks that do not require benchmark hardware."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

import test_basic_speed as basic_launcher

from tests.lib.config import (
    TARGETS,
    load_defaults,
    load_target_profiles,
    target_for_ip_version,
)
from tests.lib.repeat import DEFAULT_REPEAT
from tests.lib.timing import DEFAULT_TEST_START_DELAY_SEC
from tests.lib.trex import render_trex_cfg, resolve_ipv6_gateway_macs
from tests import run_router_bench_v4_dns_latency as dns_benchmark


class SharedConfigTest(unittest.TestCase):
    def test_basic_launcher_continues_after_test_and_recovery_failures(self):
        with tempfile.TemporaryDirectory() as out_dir, mock.patch.object(
            sys,
            "argv",
            [
                "test_basic_speed.py",
                "--target",
                "i226_1g",
                "--ip-version",
                "v4",
                "--out-dir",
                out_dir,
                "--run-stl-throughput",
                "--run-tcp-scratch-max-sessions",
                "--run-astf-sessions-cps",
            ],
        ), mock.patch.object(
            basic_launcher,
            "child_long_options",
            return_value={"--repeat"},
        ), mock.patch.object(
            basic_launcher.subprocess,
            "call",
            side_effect=[1, 1, 0, 0, 0],
        ) as call_mock:
            with self.assertRaises(SystemExit) as exit_context:
                basic_launcher.main()

        self.assertEqual(exit_context.exception.code, 1)
        self.assertEqual(call_mock.call_count, 5)
        commands = [call.args[0] for call in call_mock.call_args_list]
        self.assertTrue(any("run_router_bench_v4_stl_throughput_pps.py" in command[1] for command in commands))
        self.assertTrue(any("run_router_bench_v4_tcp_scratch_max_sessions.py" in command[1] for command in commands))
        self.assertTrue(any("run_router_bench_v4_astf_sessions_cps.py" in command[1] for command in commands))

    def test_required_default_sections_exist(self):
        for section in (
            "common",
            "trex",
            "launcher",
            "dslite_launcher",
            "dslite_stl",
            "dslite_astf",
            "dslite_nat_stability",
            "dslite_tcp_scratch",
            "dslite_source_port_scan",
            "stl",
            "astf",
            "nat_stability",
            "network",
            "l4_latency",
            "dns",
            "dhcp",
            "tcp_scratch",
        ):
            self.assertIsInstance(load_defaults(section), dict)

    def test_target_profile_loader_matches_compatibility_module(self):
        self.assertEqual(load_target_profiles(), TARGETS)

    def test_i226_profiles_match_current_machine(self):
        for name in ("i226_1g", "i226_2p5g"):
            profile = TARGETS[name]
            self.assertEqual(profile["interfaces"], ["07:00.0", "08:00.0"])
            self.assertEqual(profile["lan_iface"], "enp7s0")
            self.assertEqual(profile["wan_iface"], "enp8s0")

    def test_native_ipv6_profile_mapping(self):
        target = target_for_ip_version(TARGETS["i226_1g"], "v6")
        self.assertEqual(target["lan_ip"], "fd00:1::2")
        self.assertEqual(target["lan_gw"], "fd00:1::1")
        self.assertEqual(target["wan_ip"], "fd00:100::1")
        self.assertEqual(target["wan_gw"], "fd00:100::2")
        self.assertEqual(target["router_wan_ip"], "fd00:100::2")
        self.assertEqual(target["ip_version"], "v6")
        cfg = render_trex_cfg(target)
        self.assertIn("interfaces", cfg)
        self.assertNotIn("port_info", cfg)

    def test_ipv6_nd_does_not_require_port_stack_ipv6_support(self):
        class FakeRecord:
            def __init__(self, src_mac, dst_mac):
                self.src_mac = src_mac
                self.dst_mac = dst_mac

            def is_resolved(self):
                return True

        class FakeServiceIPv6ND:
            def __init__(self, ctx, dst_ip, src_ip, retries):
                self.ctx = ctx
                self.dst_ip = dst_ip
                self.src_ip = src_ip
                self.retries = retries
                self.record = FakeRecord(
                    ctx.src_mac,
                    "02:00:00:00:00:11" if ctx.port == 0 else "02:00:00:00:00:22",
                )

            def get_record(self):
                return self.record

        class FakeServiceContext:
            def __init__(self, port):
                self.port = port
                self.src_mac = (
                    "02:00:00:00:01:01" if port == 0 else "02:00:00:00:01:02"
                )
                self.services = []

            def run(self, services):
                self.services.extend(services)

        class FakeClient:
            def __init__(self):
                self.service_mode_calls = []
                self.contexts = {}

            def conf_ipv6(self, *_args, **_kwargs):
                raise AssertionError("conf_ipv6 must not be called")

            def set_service_mode(self, ports, enabled):
                self.service_mode_calls.append((tuple(ports), enabled))

            def create_service_ctx(self, port):
                context = FakeServiceContext(port)
                self.contexts[port] = context
                return context

        service_module = ModuleType(
            "trex.common.services.trex_service_IPv6ND"
        )
        service_module.ServiceIPv6ND = FakeServiceIPv6ND
        client = FakeClient()
        target = target_for_ip_version(TARGETS["i226_1g"], "v6")

        with mock.patch.dict(
            sys.modules,
            {
                "trex": ModuleType("trex"),
                "trex.common": ModuleType("trex.common"),
                "trex.common.services": ModuleType("trex.common.services"),
                "trex.common.services.trex_service_IPv6ND": service_module,
            },
        ), mock.patch("tests.lib.trex.add_trex_api_path"):
            result = resolve_ipv6_gateway_macs(client, target)

        self.assertEqual(result["lan_src_mac"], "02:00:00:00:01:01")
        self.assertEqual(result["lan_router_mac"], "02:00:00:00:00:11")
        self.assertEqual(result["wan_src_mac"], "02:00:00:00:01:02")
        self.assertEqual(result["wan_router_mac"], "02:00:00:00:00:22")
        self.assertEqual(
            client.service_mode_calls,
            [((0,), True), ((0,), False), ((1,), True), ((1,), False)],
        )
        self.assertEqual(client.contexts[0].services[0].src_ip, "fd00:1::2")
        self.assertEqual(client.contexts[0].services[0].dst_ip, "fd00:1::1")
        self.assertEqual(client.contexts[1].services[0].src_ip, "fd00:100::1")
        self.assertEqual(client.contexts[1].services[0].dst_ip, "fd00:100::2")

    def test_derived_profiles_are_fully_resolved(self):
        profile = TARGETS["x540_10g_loop"]
        self.assertNotIn("extends", profile)
        self.assertEqual(profile["interfaces"], ["01:00.0", "01:00.1"])
        self.assertEqual(profile["supported_tests"], ["l4_latency"])

    def test_start_delay_uses_external_default(self):
        self.assertEqual(
            DEFAULT_TEST_START_DELAY_SEC,
            float(load_defaults("common")["test_start_delay_sec"]),
        )

    def test_repeat_uses_external_default(self):
        self.assertEqual(DEFAULT_REPEAT, int(load_defaults("common")["repeat"]))

    def test_interface_ready_timeout_defaults_to_60_seconds(self):
        self.assertEqual(load_defaults("network")["interface_ready_timeout_sec"], 60.0)
        self.assertEqual(load_defaults("dhcp")["interface_ready_timeout_sec"], 60.0)

    def test_ipv4_nat_stability_is_explicit_only(self):
        test_plan = load_defaults("launcher")["test_plan"]
        nat_stability = next(item for item in test_plan if item["key"] == "astf_nat")
        self.assertFalse(nat_stability["default"])
        self.assertFalse(nat_stability["run_all"])

        launcher = Path(__file__).resolve().parents[1] / "test_basic_speed.py"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as cwd:
            run_all = subprocess.run(
                [
                    sys.executable,
                    str(launcher),
                    "--target",
                    "i226_1g",
                    "--run-all",
                    "--dry-run",
                ],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            explicit = subprocess.run(
                [
                    sys.executable,
                    str(launcher),
                    "--target",
                    "i226_1g",
                    "--run-astf-natstability",
                    "--dry-run",
                ],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

        self.assertEqual(run_all.returncode, 0, run_all.stderr)
        self.assertNotIn("run_router_bench_v4_astf_nat_stability.py", run_all.stdout)
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertIn("run_router_bench_v4_astf_nat_stability.py", explicit.stdout)
        self.assertNotIn("run_router_bench_v4_stl_throughput_pps.py", explicit.stdout)

    def test_ip_version_selection_and_ipv6_stability_policy(self):
        test_plan = load_defaults("launcher")["test_plan"]
        ipv6_stability = next(
            item for item in test_plan if item["key"] == "ipv6_stability"
        )
        self.assertFalse(ipv6_stability["default"])
        self.assertTrue(ipv6_stability["run_all"])

        launcher = Path(__file__).resolve().parents[1] / "test_basic_speed.py"
        commands = {}
        for version, extra in (
            ("dual", []),
            ("v4", ["--ip-version", "v4"]),
            ("v6", ["--ip-version", "v6"]),
            ("v6_all", ["--ip-version", "v6", "--run-all"]),
        ):
            commands[version] = subprocess.run(
                [sys.executable, str(launcher), "--target", "i226_1g", *extra, "--dry-run"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(commands[version].returncode, 0, commands[version].stderr)

        self.assertIn("run_router_bench_v4_stl_throughput_pps.py", commands["dual"].stdout)
        self.assertIn("run_router_bench_v6_stl_throughput_pps.py", commands["dual"].stdout)
        self.assertNotIn("run_router_bench_v6_stl_throughput_pps.py", commands["v4"].stdout)
        self.assertNotIn("run_router_bench_v4_stl_throughput_pps.py", commands["v6"].stdout)
        self.assertNotIn("run_router_bench_v6_astf_routing_stability.py", commands["v6"].stdout)
        self.assertIn("run_router_bench_v6_astf_routing_stability.py", commands["v6_all"].stdout)

    def test_dns_modes_and_launcher_selection(self):
        defaults = load_defaults("dns")
        self.assertEqual(defaults["stability_qps"], 100.0)
        self.assertEqual(
            dns_benchmark.PERFORMANCE_PHASE_ORDER,
            ("cache_table_reuse_size", "cache_rate", "nocache_rate"),
        )
        self.assertEqual(dns_benchmark.CACHE_TABLE_CONSECUTIVE_NG_LIMIT, 3)

        test_plan = load_defaults("launcher")["test_plan"]
        stability = next(item for item in test_plan if item["key"] == "dns_stability")
        self.assertFalse(stability["default"])
        self.assertFalse(stability["run_all"])

        launcher = Path(__file__).resolve().parents[1] / "test_basic_speed.py"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as cwd:
            run_all = subprocess.run(
                [
                    sys.executable,
                    str(launcher),
                    "--target",
                    "i226_1g",
                    "--run-all",
                    "--dry-run",
                ],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            explicit = subprocess.run(
                [
                    sys.executable,
                    str(launcher),
                    "--target",
                    "i226_1g",
                    "--run-dns-stability",
                    "--dns-stability-qps",
                    "250",
                    "--dry-run",
                ],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

        self.assertEqual(run_all.returncode, 0, run_all.stderr)
        self.assertNotIn("run_router_bench_v4_dns_stability.py", run_all.stdout)
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertIn("run_router_bench_v4_dns_stability.py", explicit.stdout)
        self.assertIn("--dns-stability-qps 250", explicit.stdout)
        self.assertNotIn("run_router_bench_v4_dns_latency.py", explicit.stdout)

    def test_cache_rate_reloads_final_table_qnames(self):
        args = SimpleNamespace(
            dns_skip_cache=False,
            dns_cache_preload_count=100,
            dns_domain_suffix="routerbench.example.net",
            dns_table_overflow_rate_pct=50.0,
            duration=1.0,
        )
        table_qnames = ["r100.routerbench.example.net", "r101.routerbench.example.net"]
        loaded_qname_sets = []

        def fake_load(*call_args, **_call_kwargs):
            loaded_qname_sets.append(list(call_args[6]))
            return {"step_status": "OK", "error": ""}

        def fake_row(*_call_args, **_call_kwargs):
            return {
                "success_rate_pct": 100.0,
                "failure_rate_pct": 0.0,
                "rtt_p95_ms": 0.1,
                "rtt_max_ms": 0.2,
                "wan_queries_delta": 0,
                "cache_overflow_rate_pct": 0.0,
            }

        with (
            mock.patch.object(dns_benchmark, "run_client_load_phase_safe", side_effect=fake_load),
            mock.patch.object(dns_benchmark, "count_server_queries", return_value=0),
            mock.patch.object(dns_benchmark, "decorate_summary", side_effect=fake_row),
            mock.patch.object(dns_benchmark, "should_stop", return_value=""),
        ):
            rows = []
            dns_benchmark.run_cache_rate_benchmark(
                rows,
                args,
                {},
                Path("/tmp"),
                Path("/tmp/dns_server_queries.csv"),
                0,
                [10],
                table_cached_qnames=table_qnames,
            )

        self.assertEqual(loaded_qname_sets, [table_qnames, table_qnames])

    def test_cache_rate_selects_100_qnames_from_final_attempt(self):
        table_qnames = [f"r{index}.routerbench.example.net" for index in range(250)]
        selected = dns_benchmark.select_cache_rate_qnames(table_qnames, 220, 100)
        self.assertEqual(selected, table_qnames[:100])

    def test_dns_stability_uses_only_fixed_qps_load(self):
        args = SimpleNamespace(
            dns_skip_table=False,
            dns_skip_cache=False,
            dns_skip_nocache=False,
            dns_skip_table_reuse=False,
            dns_run_delayed_table=False,
            dns_run_nocache_load=False,
            dns_stability_qps=100.0,
        )
        dns_benchmark.configure_benchmark_mode(args, "stability")
        self.assertTrue(args.dns_skip_nocache)
        self.assertTrue(args.dns_run_nocache_load)
        self.assertEqual(args.dns_stability_qps, 100.0)

    def test_arp_helper_supports_direct_execution(self):
        helper = Path(__file__).resolve().parents[1] / "tests" / "lib" / "arp.py"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                [sys.executable, str(helper), "--help"],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("ImportError", proc.stderr)

    def test_common_lan_source_port_range_reaches_supported_basic_tests(self):
        launcher = Path(__file__).resolve().parents[1] / "test_basic_speed.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(launcher),
                "--target",
                "i226_1g",
                "--ip-version",
                "v4",
                "--run-all",
                "--lan-source-port-range",
                "38564-39563",
                "--dry-run",
            ],
            cwd=str(launcher.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        commands = [line for line in proc.stdout.splitlines() if "run_router_bench_" in line]
        self.assertTrue(
            all(
                "--lan-source-port-range 38564-39563" in line
                for line in commands
                if any(
                    name in line
                    for name in (
                        "recovery_check.py",
                        "v4_stl_throughput_pps.py",
                        "v4_tcp_scratch_max_sessions.py",
                        "v4_iperf3_latency.py",
                        "v4_dns_latency.py",
                    )
                )
            )
        )
        self.assertTrue(
            all(
                "--lan-source-port-range" not in line
                for line in commands
                if any(name in line for name in ("astf_sessions_cps.py", "dhcp_scratch.py"))
            )
        )


    def test_repeat_is_forwarded_only_to_supported_run_all_tests(self):
        launcher = Path(__file__).resolve().parents[1] / "test_basic_speed.py"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(launcher),
                    "--target",
                    "i226_1g",
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
        command_lines = [
            line for line in proc.stdout.splitlines()
            if "run_router_bench_" in line
            and "run_router_bench_recovery_check.py" not in line
        ]
        recovery_lines = [
            line for line in proc.stdout.splitlines()
            if "run_router_bench_recovery_check.py" in line
        ]
        self.assertTrue(command_lines)
        self.assertTrue(recovery_lines)
        self.assertTrue(all("--repeat 2" not in line for line in recovery_lines))
        repeat_unsupported = [
            line for line in command_lines
            if "run_router_bench_v6_astf_routing_stability.py" in line
        ]
        repeat_supported = [
            line for line in command_lines
            if "run_router_bench_v6_astf_routing_stability.py" not in line
        ]
        self.assertEqual(len(repeat_unsupported), 1)
        self.assertNotIn("--repeat 2", repeat_unsupported[0])
        self.assertTrue(all("--repeat 2" in line for line in repeat_supported))
        self.assertNotIn("run_router_bench_v4_astf_nat_stability.py", proc.stdout)
        self.assertNotIn("run_router_bench_v4_dns_stability.py", proc.stdout)


if __name__ == "__main__":
    unittest.main()
