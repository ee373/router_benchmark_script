#!/usr/bin/env python3

import argparse
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from tests import run_router_bench_v4_astf_nat_stability as stability


class FakeProgram:
    def __init__(self):
        self.commands = []

    def send(self, buf, size=0, fill=None):
        self.commands.append(("send", buf, size, fill))

    def send_msg(self, buf, size=0, fill=None):
        self.commands.append(("send_msg", buf, size, fill))

    def set_var(self, name, value):
        self.commands.append(("set_var", name, value))

    def set_label(self, name):
        self.commands.append(("set_label", name))

    def jmp_nz(self, name, label):
        self.commands.append(("jmp_nz", name, label))


class AstfNatStabilityProfileTests(unittest.TestCase):
    def test_template_group_names_fit_trex_v308_limit(self):
        self.assertLessEqual(len(stability.TCP_TG_NAME), 20)
        self.assertLessEqual(len(stability.UDP_TG_NAME), 20)
        self.assertNotEqual(stability.TCP_TG_NAME, stability.UDP_TG_NAME)

    def test_tcp_virtual_payload_is_exact_and_bounded(self):
        program = FakeProgram()

        stability.add_tcp_send_bytes(program, 1_000_000, 16_384)

        self.assertEqual(len(program.commands), 5)
        full_chunks = next(
            command[2]
            for command in program.commands
            if command[0] == "set_var"
        )
        sends = [command for command in program.commands if command[0] == "send"]
        self.assertEqual(full_chunks, 61)
        self.assertEqual(full_chunks * sends[0][2] + sends[1][2], 1_000_000)

    def test_udp_virtual_payload_is_exact_and_bounded(self):
        program = FakeProgram()

        stability.add_udp_send_messages(program, 1_000_000, 1_200)

        self.assertEqual(len(program.commands), 5)
        full_messages = next(
            command[2]
            for command in program.commands
            if command[0] == "set_var"
        )
        sends = [
            command for command in program.commands if command[0] == "send_msg"
        ]
        self.assertEqual(full_messages, 833)
        self.assertEqual(full_messages * sends[0][2] + sends[1][2], 1_000_000)
        self.assertEqual(stability.udp_packet_count(1_000_000, 1_200), 834)

    def test_protocol_preflight_runs_once_before_all_cycles(self):
        events = []
        completed_cycles = 0
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
        )

        def fake_preflight(*unused_args, **unused_kwargs):
            events.append("preflight")
            return argparse.Namespace(
                value={"preflight_ok": 1},
                attempts=3,
                consecutive_successes=3,
                elapsed_sec=0.1,
            )

        def fake_profile(*unused_args, **unused_kwargs):
            events.append("profile")
            return object()

        def fake_cycle(*unused_args, **unused_kwargs):
            nonlocal completed_cycles
            events.append("cycle")
            completed_cycles += 1
            if completed_cycles == 2:
                args.soak_duration_sec = 0.0
            return {
                "completion_reason": "completed",
                "cycle_stable": True,
                "session_success_rate": 1.0,
                "payload_completion_rate": 1.0,
                "packet_loss_rate": 0.0,
                "throughput_loss_rate": 0.0,
            }

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            stability, "run_consecutive_preflight", side_effect=fake_preflight
        ), mock.patch.object(
            stability, "make_nat_stability_profile", side_effect=fake_profile
        ), mock.patch.object(
            stability, "run_one_cycle", side_effect=fake_cycle
        ):
            stability.run_protocol_cycles(
                client=object(),
                raw_writer=mock.Mock(),
                cycle_writer=mock.Mock(),
                args=args,
                target={},
                out_dir=Path(tmp_dir),
                protocol="tcp",
                test_start=stability.time.time(),
                bytes_per_session=1_000_000,
            )

        self.assertEqual(
            events,
            ["preflight", "profile", "cycle", "profile", "cycle"],
        )


if __name__ == "__main__":
    unittest.main()
