"""Tests for MAP-E L4 latency target setup and TCP MSS handling."""

import argparse
import csv
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from tests import run_router_bench_mape_iperf3_latency as mape_latency
from tests import run_router_bench_v4_iperf3_latency as latency
from tests.lib import network
from tests.lib.mape import generate_mape_ports


class MapeL4LatencyTests(unittest.TestCase):
    def test_targets_separate_map_e_server_and_wan_link_addresses(self):
        target = mape_latency.build_latency_targets()["i226_1g_mape"]
        self.assertEqual(target["wan_ip"], "203.0.113.2")
        self.assertEqual(target["wan_iface_ip"], "198.51.100.254")
        self.assertEqual(target["wan_gw"], "198.51.100.1")
        self.assertEqual(target["router_wan_ip"], "198.51.100.1")

    def test_wrapper_uses_profile_mss_default(self):
        original_mss = latency.DEFAULT_TCP_MSS
        original_source_ports = latency.SOURCE_PORTS
        original_target = latency._L4_DEFAULTS["target"]
        try:
            def check_shared_runner():
                self.assertEqual(latency.DEFAULT_TCP_MSS, 1380)
                self.assertEqual(
                    latency.SOURCE_PORTS,
                    tuple(generate_mape_ports(3, 4, 8)),
                )
                self.assertEqual(latency._L4_DEFAULTS["target"], "i226_1g_mape")

            with mock.patch.object(
                sys, "argv", ["runner", "--target", "i226_1g_mape"]
            ), mock.patch.object(
                latency, "main", side_effect=check_shared_runner
            ) as shared_main:
                mape_latency.main()
            self.assertEqual(latency.DEFAULT_TCP_MSS, original_mss)
            self.assertEqual(latency.SOURCE_PORTS, original_source_ports)
            self.assertEqual(latency._L4_DEFAULTS["target"], original_target)
            shared_main.assert_called_once_with()
        finally:
            latency.DEFAULT_TCP_MSS = original_mss
            latency.SOURCE_PORTS = original_source_ports

    def test_profile_source_ports_are_used_for_latency_and_iperf_clients(self):
        original_ports = latency.SOURCE_PORTS
        original_index = latency._SOURCE_PORT_INDEX
        latency.SOURCE_PORTS = (4144, 4145, 8240, 8241)
        latency._SOURCE_PORT_INDEX = 0
        args = argparse.Namespace(
            wan_ns="rb_wan",
            lan_ns="rb_lan",
            latency_protocol="tcp",
            latency_port=5501,
            tcp_mss=1380,
            latency_duration_sec=1.0,
            latency_interval_sec=0.2,
            latency_payload_size_bytes=64,
            latency_timeout_sec=1.0,
            latency_connect_timeout_sec=2.0,
            load_protocol="tcp",
            load_direction="download",
            iperf3_port=5201,
            iperf3_parallel=2,
            iperf3_extra_args="",
        )
        target = {
            "wan_ip": "203.0.113.2",
            "lan_ip": "192.168.0.2",
            "ip_version": "v4",
        }
        try:
            client = latency.build_l4rr_client_command(args, target)
            source_ports = {
                int(port)
                for port in client[client.index("--source-ports") + 1].split(",")
            }
            self.assertEqual(source_ports, {4144, 4145, 8240, 8241})

            iperf_client = latency.build_iperf3_client_command(
                args, target, 1.0, "100M"
            )
            cport = int(iperf_client[iperf_client.index("--cport") + 1])
            self.assertIn(cport, {4144, 8240})
            self.assertIn(cport + 1, {4145, 8241})
        finally:
            latency.SOURCE_PORTS = original_ports
            latency._SOURCE_PORT_INDEX = original_index

    def test_namespace_setup_assigns_server_as_secondary_host_address(self):
        args = argparse.Namespace(
            lan_ns="rb_lan",
            wan_ns="rb_wan",
            replace_namespaces=True,
            lan_iface="lan0",
            wan_iface="wan0",
            lan_prefix=24,
            wan_prefix=24,
            lan_source_port_range=None,
        )
        target = {
            "lan_ip": "192.168.0.2",
            "lan_gw": "192.168.0.1",
            "wan_ip": "203.0.113.2",
            "wan_iface_ip": "198.51.100.254",
            "router_wan_ip": "198.51.100.1",
        }
        calls = []
        with mock.patch.object(network, "namespace_exists", return_value=False), mock.patch.object(
            network, "run_cmd", side_effect=lambda command: calls.append(command)
        ):
            network.setup_namespaces(args, target, csv.writer(StringIO()))

        self.assertIn(
            ["ip", "-n", "rb_wan", "addr", "add", "198.51.100.254/24", "dev", "wan0"],
            calls,
        )
        self.assertIn(
            ["ip", "-n", "rb_wan", "addr", "add", "203.0.113.2/32", "dev", "wan0"],
            calls,
        )
        self.assertIn(
            [
                "ip", "-n", "rb_wan", "-4", "route", "replace", "default",
                "via", "198.51.100.1", "dev", "wan0",
            ],
            calls,
        )

    def test_tcp_mss_is_applied_to_echo_and_iperf_commands(self):
        args = argparse.Namespace(
            wan_ns="rb_wan",
            lan_ns="rb_lan",
            latency_protocol="tcp",
            latency_port=5501,
            tcp_mss=1380,
            latency_duration_sec=1.0,
            latency_interval_sec=0.2,
            latency_payload_size_bytes=64,
            latency_timeout_sec=1.0,
            latency_connect_timeout_sec=2.0,
            load_protocol="tcp",
            load_direction="download",
            iperf3_port=5201,
            iperf3_parallel=1,
            iperf3_extra_args="",
        )
        target = {
            "wan_ip": "203.0.113.2",
            "lan_ip": "192.168.0.2",
            "ip_version": "v4",
        }
        client = latency.build_l4rr_client_command(args, target)
        self.assertEqual(client[client.index("--tcp-mss") + 1], "1380")

        with tempfile.TemporaryDirectory() as out_dir, mock.patch.object(
            latency.subprocess, "Popen"
        ) as popen:
            _proc, stdout, stderr = latency.start_l4rr_server(
                args, target, Path(out_dir), "load_50pct"
            )
            try:
                server = popen.call_args.args[0]
                self.assertEqual(server[server.index("--tcp-mss") + 1], "1380")
            finally:
                stdout.close()
                stderr.close()

        tcp_load = latency.build_iperf3_client_command(
            args, target, 1.0, "100M"
        )
        self.assertEqual(tcp_load[tcp_load.index("-M") + 1], "1380")

        with tempfile.TemporaryDirectory() as out_dir, mock.patch.object(
            latency.subprocess, "Popen"
        ) as popen:
            _proc, stdout, stderr, _json_path = latency.start_iperf3_server(
                args, target, Path(out_dir), "load_50pct"
            )
            try:
                server = popen.call_args.args[0]
                self.assertNotIn("-M", server)
            finally:
                stdout.close()
                stderr.close()

        args.load_protocol = "udp"
        udp_load = latency.build_iperf3_client_command(
            args, target, 1.0, "100M"
        )
        self.assertNotIn("-M", udp_load)

    def test_tcp_mss_socket_option_is_applied(self):
        sock_obj = mock.Mock()
        latency.set_tcp_mss(sock_obj, 1380)
        sock_obj.setsockopt.assert_called_once_with(
            latency.socket.IPPROTO_TCP, latency.socket.TCP_MAXSEG, 1380
        )

    def test_tcp_client_binds_selected_map_e_source_port(self):
        sock_obj = mock.Mock()
        with mock.patch.object(latency.socket, "socket", return_value=sock_obj):
            result = latency.connect_tcp_with_retry(
                "203.0.113.2",
                5501,
                "192.168.0.2",
                1.0,
                source_port=4144,
            )

        self.assertIs(result, sock_obj)
        sock_obj.bind.assert_called_once_with(("192.168.0.2", 4144))

    def test_tcp_retry_tries_profile_ports_until_a_connection_succeeds(self):
        sockets = [mock.Mock() for _ in range(3)]
        sockets[0].connect.side_effect = TimeoutError("unavailable")
        sockets[1].connect.side_effect = TimeoutError("unavailable")
        with mock.patch.object(
            latency.socket, "socket", side_effect=sockets
        ), mock.patch.object(latency.time, "sleep") as sleep:
            connected = latency.connect_tcp_with_retry(
                "203.0.113.2",
                5501,
                "192.168.0.2",
                1.0,
                source_ports=(4144, 4145, 8240),
            )

        self.assertIs(connected, sockets[2])
        self.assertEqual(
            [sock.settimeout.call_args.args[0] for sock in sockets],
            [0.5, 0.5, 0.5],
        )
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(
            [sock.bind.call_args.args[0] for sock in sockets],
            [
                ("192.168.0.2", 4144),
                ("192.168.0.2", 4145),
                ("192.168.0.2", 8240),
            ],
        )

    def test_l4rr_preflight_refreshes_neighbors_before_each_probe(self):
        args = argparse.Namespace(
            l4rr_preflight=True,
            latency_protocol="tcp",
            l4rr_preflight_duration_sec=0.6,
            l4rr_preflight_interval_sec=0.1,
            latency_timeout_sec=1.0,
            latency_connect_timeout_sec=2.0,
            preflight_success_consecutive=3,
            preflight_probe_interval_sec=2.0,
            l4rr_preflight_timeout_sec=30.0,
            require_l4rr_preflight=True,
        )
        refresher = mock.Mock()

        def write_successful_client_result(command, stdout, stderr, text):
            stdout.write('{"samples":[{"result":"PASS"}]}')
            return mock.Mock()

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            latency, "build_l4rr_client_command", return_value=["client"]
        ), mock.patch.object(
            latency.subprocess, "Popen", side_effect=write_successful_client_result
        ), mock.patch.object(
            latency, "wait_for_process_exit"
        ), mock.patch.object(
            latency,
            "run_consecutive_preflight",
            side_effect=lambda probe, **kwargs: probe(1),
        ):
            result = latency.run_l4rr_preflight(
                args,
                {
                    "wan_ip": "203.0.113.2",
                    "lan_ip": "192.168.0.2",
                    "benchmark_mode": "mape",
                },
                Path(temp_dir),
                "idle",
                neighbor_refresher=refresher,
            )

        self.assertEqual(
            result,
            Path(temp_dir) / "l4rr_preflight_idle_attempt1.json",
        )
        refresher.refresh_now.assert_called_once_with(
            "l4rr_preflight_idle_attempt_1",
            required=True,
        )


if __name__ == "__main__":
    unittest.main()
