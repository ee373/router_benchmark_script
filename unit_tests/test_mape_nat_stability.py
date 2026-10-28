"""MAP-E soak integration and profile regression checks without traffic."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import run_router_bench_mape_astf_nat_stability as mape
from tests import run_router_bench_v4_astf_nat_stability as stability
from tests.lib import astf as astf_lib
from tests.lib.config import load_defaults

ROOT = Path(__file__).resolve().parents[1]


class MapeNatStabilityTests(unittest.TestCase):
    def test_real_parser_uses_profile_mss_and_cli_override(self):
        original_parse = argparse.ArgumentParser.parse_known_args
        for extra, expected in (([], 1380), (["--tcp-mss", "1300"], 1300)):
            parsed = []
            def capture(parser, *args, **kwargs):
                result = original_parse(parser, *args, **kwargs)
                if hasattr(result[0], "tcp_mss"):
                    parsed.append(result[0])
                return result
            with mock.patch.object(sys, "argv", ["runner", *extra]), mock.patch.object(argparse.ArgumentParser, "parse_known_args", capture), mock.patch.object(stability, "require_root", side_effect=RuntimeError("stop before hardware")):
                with self.assertRaisesRegex(RuntimeError, "stop before hardware"):
                    mape.main()
            self.assertEqual(parsed[0].tcp_mss, expected)
            self.assertEqual(parsed[0].protocols, ["tcp", "udp"])
            self.assertEqual(parsed[0].soak_duration_sec, 3600)

    @unittest.skipUnless(Path("/opt/trex/v3.08").is_dir(), "Requires installed TRex API")
    def test_real_trex_profile_serialization_and_existing_targets(self):
        from tests.lib.config import TARGETS, DSLITE_TARGETS, target_for_ip_version
        from tests.lib.dslite import build_trex_target as dslite_target
        stability.add_trex_api_path()
        args = argparse.Namespace(payload_direction="server-to-client",
            payload_chunk_bytes=16384, tcp_mss=1420, tcp_window_kb=1024,
            astf_tcp_port=8080, astf_udp_port=9090, cycle_cps=50,
            cycle_sessions=200, udp_msg_payload_bytes=1200, udp_mtu=1400)
        targets = [mape.build_stability_targets()["i226_1g_mape"],
                   TARGETS["i226_1g"], dslite_target(DSLITE_TARGETS["i226_1g_dslite"]),
                   target_for_ip_version(TARGETS["i226_1g"], "v6")]
        for target in targets:
            for direction in ("server-to-client", "client-to-server", "bidirectional"):
                args.payload_direction = direction
                for protocol in ("tcp", "udp"):
                    with self.subTest(target=target["description"], direction=direction, protocol=protocol):
                        profile = stability.make_nat_stability_profile(protocol, target, args, 1000000)
                        self.assertTrue(profile.to_json())
        udp_preflight_profile = astf_lib.make_udp_roundtrip_profile(
            target=targets[0],
            udp_port=9090,
            cps=5,
            server_ip=targets[0]["astf_server_ipv4"],
            udp_mtu=1400,
        )
        self.assertTrue(udp_preflight_profile.to_json())

    def test_wrapper_profile_mss_and_cli_override_and_restoration(self):
        original = stability.DEFAULT_TCP_MSS
        for extra in ([], ["--tcp-mss", "1300"]):
            def check_settings():
                self.assertEqual(stability.DEFAULT_TCP_MSS, 1380)
                self.assertEqual(stability.METADATA_MODE, "mape_astf_nat_stability")
                self.assertEqual(stability.DEFAULT_OUTPUT_PREFIX, "trex_mape_astf_nat_stability")
                self.assertEqual(stability._NAT_DEFAULTS["target"], "i226_1g_mape")
                self.assertEqual(sys.argv[1:], ["--target", "i226_1g_mape", *extra])
            with mock.patch.object(sys, "argv", ["runner", "--target", "i226_1g_mape", *extra]), mock.patch.object(stability, "main", side_effect=check_settings):
                mape.main()
        self.assertEqual(stability.DEFAULT_TCP_MSS, original)

    def test_tcp_udp_profiles_use_server_without_changing_wan(self):
        target = mape.build_stability_targets()["i226_1g_mape"]
        args = argparse.Namespace(
            payload_direction="bidirectional", payload_chunk_bytes=16384,
            tcp_mss=1300, tcp_window_kb=1024, astf_tcp_port=8080,
            astf_udp_port=9090, cycle_cps=7, cycle_sessions=9,
            udp_msg_payload_bytes=1200, udp_mtu=1400,
        )
        api = mock.MagicMock()
        api.ASTFGlobalInfo.side_effect = lambda: SimpleNamespace(tcp=SimpleNamespace())
        with mock.patch.dict(sys.modules, {"trex.astf.api": api}), mock.patch.object(stability, "make_ip_gen", return_value="ipgen") as ipgen:
            for protocol in ("tcp", "udp"):
                stability.make_nat_stability_profile(protocol, target, args, 4096)
                ipgen.assert_called_with(target, server_ip="203.0.113.2")
                kwargs = api.ASTFTCPClientTemplate.call_args.kwargs
                self.assertEqual(kwargs["port"], 8080 if protocol == "tcp" else 9090)
                self.assertEqual((kwargs["cps"], kwargs["limit"]), (7, 9))
        self.assertEqual(target["wan_ip"], "198.51.100.254")
        self.assertEqual(target["wan_gw"], "198.51.100.1")

    def test_udp_protocol_preflight_uses_map_e_server(self):
        target = mape.build_stability_targets()["i226_1g_mape"]
        args = argparse.Namespace(cycle_cps=50, cycle_sessions=200, mb_per_session=1,
            connect_timeout_sec=1, completion_quiet_sec=2, generation_grace_sec=5,
            sample_interval_sec=1, payload_direction="server-to-client",
            payload_chunk_bytes=16384, tcp_mss=1420, tcp_window_kb=1024,
            astf_tcp_port=80, astf_udp_port=80, udp_msg_payload_bytes=1200, udp_mtu=1400)
        api = mock.MagicMock()
        api.ASTFGlobalInfo.side_effect = lambda: SimpleNamespace(tcp=SimpleNamespace())
        with mock.patch.dict(sys.modules, {"trex.astf.api": api}), mock.patch.object(stability, "make_ip_gen") as ipgen, mock.patch.object(stability, "run_one_cycle", return_value={"cycle_stable": 1, "session_success_rate": 1, "payload_completion_rate": 1}), mock.patch.object(stability, "write_raw_stats"):
            stability.run_protocol_roundtrip_preflight(mock.Mock(), mock.Mock(), args, target, "udp", 1, 1)
            ipgen.assert_called_with(target, server_ip="203.0.113.2")

    def test_map_e_tcp_preflight_uses_shared_astf_consecutive_helper(self):
        target = mape.build_stability_targets()["i226_1g_mape"]
        args = argparse.Namespace(
            soak_duration_sec=1.0,
            preflight_success_consecutive=3,
            preflight_probe_interval_sec=2.0,
            preflight_timeout_sec=300.0,
            cycle_cps=50.0,
            cycle_sessions=200,
            payload_direction="server-to-client",
            mb_per_session=1.0,
            connect_timeout_sec=1.0,
            client_port_min=32768,
            client_port_max=65535,
            inter_cycle_sleep_sec=0.0,
            astf_tcp_port=80,
        )
        expected_preflight = {
            "preflight_ok": 1,
            "preflight_expected_sessions": 10,
            "preflight_session_success_rate": 1.0,
            "preflight_packet_loss_rate": 0.0,
            "preflight_throughput_loss_rate": 0.0,
            "preflight_client_tx_l7_bytes": 10,
            "preflight_server_rx_l7_bytes": 10,
            "preflight_server_tx_l7_bytes": 10,
            "preflight_client_rx_l7_bytes": 10,
            "preflight_attempts": 3,
            "preflight_consecutive_successes": 3,
            "preflight_recovery_elapsed_sec": 12.2,
        }

        def finish_cycle(*unused_args, **unused_kwargs):
            args.soak_duration_sec = 0.0
            return {
                "completion_reason": "completed",
                "cycle_stable": True,
                "session_success_rate": 1.0,
                "payload_completion_rate": 1.0,
                "packet_loss_rate": 0.0,
                "throughput_loss_rate": 0.0,
            }

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            stability, "run_astf_roundtrip_preflight_consecutive",
            return_value=expected_preflight,
        ) as shared_preflight, mock.patch.object(
            stability, "run_consecutive_preflight"
        ) as local_preflight, mock.patch.object(
            stability, "make_nat_stability_profile", return_value=object()
        ), mock.patch.object(
            stability, "run_one_cycle", side_effect=finish_cycle
        ):
            rows = stability.run_protocol_cycles(
                client=mock.Mock(),
                raw_writer=mock.Mock(),
                cycle_writer=mock.Mock(),
                args=args,
                target=target,
                out_dir=Path(directory),
                protocol="tcp",
                test_start=stability.time.time(),
                bytes_per_session=1_000_000,
            )

        shared_preflight.assert_called_once()
        self.assertEqual(
            shared_preflight.call_args.kwargs["target"]["astf_server_ipv4"],
            "203.0.113.2",
        )
        self.assertEqual(shared_preflight.call_args.kwargs["tcp_port"], 80)
        self.assertEqual(shared_preflight.call_args.kwargs["required_successes"], 3)
        self.assertEqual(shared_preflight.call_args.kwargs["interval_sec"], 2.0)
        self.assertEqual(shared_preflight.call_args.kwargs["timeout_sec"], 300.0)
        self.assertEqual(shared_preflight.call_args.kwargs["server_ip"], "203.0.113.2")
        local_preflight.assert_not_called()
        self.assertEqual(rows[0]["preflight_payload_completion_rate"], 1.0)

    def test_udp_roundtrip_preflight_checks_both_payload_directions(self):
        target = mape.build_stability_targets()["i226_1g_mape"]
        api = mock.MagicMock()
        stats = {
            "traffic": {
                "client": {
                    "udps_sndbyte": 10,
                    "udps_rcvbyte": 10,
                    "udps_sndpkt": 10,
                    "udps_rcvpkt": 10,
                },
                "server": {
                    "udps_sndbyte": 10,
                    "udps_rcvbyte": 10,
                    "udps_sndpkt": 10,
                    "udps_rcvpkt": 10,
                },
            },
        }
        with mock.patch.dict(sys.modules, {"trex.astf.api": api}), mock.patch.object(
            astf_lib, "make_ip_gen", return_value="ipgen"
        ) as ipgen, mock.patch.object(
            astf_lib, "run_astf_profile", return_value=stats
        ) as run_profile:
            result = astf_lib.run_astf_udp_roundtrip_preflight(
                client=mock.Mock(),
                target=target,
                raw_writer=mock.Mock(),
                test_name="mape_udp",
                udp_port=9090,
                cps=5,
                duration_sec=2,
                server_ip="203.0.113.2",
                udp_mtu=1400,
            )

        ipgen.assert_called_once_with(
            target,
            server_ip="203.0.113.2",
            server_ip_range=None,
        )
        self.assertEqual(api.ASTFProgram.call_count, 2)
        self.assertEqual(api.ASTFProgram.call_args_list[0].kwargs,
                         {"stream": False, "udp_mtu": 1400})
        self.assertEqual(api.ASTFProgram.call_args_list[1].kwargs,
                         {"stream": False, "udp_mtu": 1400})
        self.assertEqual(api.ASTFProfile.call_args.kwargs["default_ip_gen"], "ipgen")
        self.assertEqual(api.ASTFTCPClientTemplate.call_args.kwargs["port"], 9090)
        self.assertEqual(
            api.ASTFAssociationRule.call_args.kwargs["port"], 9090
        )
        self.assertEqual(run_profile.call_args.kwargs["phase"],
                         "astf_udp_roundtrip_preflight")
        self.assertEqual(result["preflight_expected_sessions"], 10)
        self.assertEqual(result["preflight_established_sessions"], 10)
        self.assertEqual(result["preflight_session_success_rate"], 1.0)
        self.assertEqual(result["preflight_payload_completion_rate"], 1.0)
        self.assertEqual(result["preflight_ok"], 1)

        stats["traffic"]["client"]["udps_rcvbyte"] = 0
        stats["traffic"]["client"]["udps_rcvpkt"] = 0
        with mock.patch.object(astf_lib, "run_astf_profile", return_value=stats):
            with self.assertRaisesRegex(RuntimeError, "UDP round-trip preflight failed"):
                astf_lib.run_astf_udp_roundtrip_preflight(
                    client=mock.Mock(),
                    target=target,
                    raw_writer=mock.Mock(),
                    test_name="mape_udp_missing_reply",
                    udp_port=9090,
                    cps=5,
                    duration_sec=2,
                    server_ip="203.0.113.2",
                    udp_mtu=1400,
                )

    def test_map_e_udp_preflight_uses_shared_astf_consecutive_helper(self):
        target = mape.build_stability_targets()["i226_1g_mape"]
        args = argparse.Namespace(
            soak_duration_sec=1.0,
            preflight_success_consecutive=3,
            preflight_probe_interval_sec=2.0,
            preflight_timeout_sec=300.0,
            cycle_cps=50.0,
            cycle_sessions=200,
            payload_direction="server-to-client",
            mb_per_session=1.0,
            connect_timeout_sec=1.0,
            client_port_min=32768,
            client_port_max=65535,
            inter_cycle_sleep_sec=0.0,
            astf_udp_port=9090,
            udp_mtu=1400,
        )
        expected_preflight = {
            "preflight_ok": 1,
            "preflight_expected_sessions": 10,
            "preflight_session_success_rate": 1.0,
            "preflight_payload_completion_rate": 1.0,
            "preflight_packet_loss_rate": 0.0,
            "preflight_throughput_loss_rate": 0.0,
            "preflight_attempts": 3,
            "preflight_consecutive_successes": 3,
            "preflight_recovery_elapsed_sec": 12.2,
        }

        def finish_cycle(*unused_args, **unused_kwargs):
            args.soak_duration_sec = 0.0
            return {
                "completion_reason": "completed",
                "cycle_stable": True,
                "session_success_rate": 1.0,
                "payload_completion_rate": 1.0,
                "packet_loss_rate": 0.0,
                "throughput_loss_rate": 0.0,
            }

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            stability, "run_astf_udp_roundtrip_preflight_consecutive",
            return_value=expected_preflight,
        ) as shared_preflight, mock.patch.object(
            stability, "run_consecutive_preflight"
        ) as local_preflight, mock.patch.object(
            stability, "make_nat_stability_profile", return_value=object()
        ), mock.patch.object(
            stability, "run_one_cycle", side_effect=finish_cycle
        ):
            rows = stability.run_protocol_cycles(
                client=mock.Mock(),
                raw_writer=mock.Mock(),
                cycle_writer=mock.Mock(),
                args=args,
                target=target,
                out_dir=Path(directory),
                protocol="udp",
                test_start=stability.time.time(),
                bytes_per_session=1_000_000,
            )

        shared_preflight.assert_called_once()
        self.assertEqual(
            shared_preflight.call_args.kwargs["server_ip"], "203.0.113.2"
        )
        self.assertEqual(shared_preflight.call_args.kwargs["udp_port"], 9090)
        self.assertEqual(shared_preflight.call_args.kwargs["udp_mtu"], 1400)
        self.assertEqual(shared_preflight.call_args.kwargs["required_successes"], 3)
        self.assertEqual(shared_preflight.call_args.kwargs["interval_sec"], 2.0)
        self.assertEqual(shared_preflight.call_args.kwargs["timeout_sec"], 300.0)
        local_preflight.assert_not_called()
        self.assertEqual(rows[0]["preflight_payload_completion_rate"], 1.0)

    def test_launcher_selection_and_no_repeat_forwarding(self):
        for selectors, included in (([], False), (["--run-all"], True), (["--run-astf-natstability"], True)):
            proc = subprocess.run([sys.executable, str(ROOT / "test_mape_speed.py"), "--dry-run", "--repeat", "2", *selectors], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            commands = [line for line in proc.stdout.splitlines() if "run_router_bench_mape_astf_nat_stability.py" in line]
            self.assertEqual(bool(commands), included)
            if included:
                self.assertIn("05_mape_astf_nat_stability", commands[0])
                self.assertNotIn("--repeat", commands[0])

    def test_metadata_keeps_map_profile_and_source_port_notice(self):
        defaults = load_defaults("nat_stability")
        args = argparse.Namespace(**defaults, protocols=["tcp", "udp"])
        target = mape.build_stability_targets()["i226_1g_mape"]
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(stability, "METADATA_MODE", "mape_astf_nat_stability"):
            stability.write_metadata(Path(directory), "i226_1g_mape", target, args, 1000000)
            metadata = json.loads((Path(directory) / "metadata.json").read_text())
        self.assertEqual(metadata["mode"], "mape_astf_nat_stability")
        self.assertEqual(metadata["target"]["server_ipv4"], "203.0.113.2")
        self.assertEqual(metadata["target"]["psid"], 3)
        self.assertIn("allocated by TRex internally", metadata["client_source_port_notice"])


if __name__ == "__main__":
    unittest.main()
