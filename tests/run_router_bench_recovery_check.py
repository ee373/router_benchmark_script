#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TCP recovery gate used between launcher-managed benchmark tests."""

import argparse
import csv
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from tests.lib.common import ts_iso  # type: ignore
from tests.lib.config import (  # type: ignore
    DSLITE_TARGETS,
    MAPE_TARGETS,
    TARGETS,
    load_defaults,
    target_for_ip_version,
)
from tests.lib.dslite import build_trex_target as build_dslite_target  # type: ignore
from tests.lib.dslite import validate_dslite_profile  # type: ignore
from tests.lib.mape import build_trex_target as build_mape_target  # type: ignore
from tests.lib.mape import validate_mape_profile  # type: ignore
from tests.lib.network import (  # type: ignore
    cleanup_namespaces,
    delete_namespace_if_exists,
    prepare_kernel_interfaces_from_profile,
    require_command,
    require_profile_interfaces,
    require_root,
    resolve_profile_interfaces,
    run_cmd,
    setup_namespaces,
    wait_test_interfaces_ready,
)
from tests.lib.source_ports import (  # type: ignore
    add_lan_source_port_range_argument,
    format_lan_source_port_range,
)


RECOVERY_DEFAULTS = load_defaults("recovery")
NETWORK_DEFAULTS = load_defaults("network")
CSV_FIELDS = [
    "ts_local",
    "benchmark_mode",
    "target",
    "ip_version",
    "lan_source_port_range",
    "previous_test",
    "next_test",
    "attempt",
    "status",
    "consecutive_successes",
    "required_consecutive_successes",
    "elapsed_sec",
    "recovery_complete",
    "detail",
]

SERVER_CODE = r"""
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
family = socket.AF_INET6 if ":" in host else socket.AF_INET
server = socket.socket(family, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind((host, port))
server.listen(32)
while True:
    conn, _ = server.accept()
    try:
        conn.sendall(b"R")
    finally:
        conn.close()
"""

CLIENT_CODE = r"""
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
timeout = float(sys.argv[3])
with socket.create_connection((host, port), timeout=timeout) as sock:
    sock.settimeout(timeout)
    if sock.recv(1) != b"R":
        raise RuntimeError("invalid recovery response")
"""


def resolve_target(mode, target_name, ip_version):
    if mode == "basic":
        return target_for_ip_version(TARGETS[target_name], ip_version)
    if mode == "dslite":
        profile = validate_dslite_profile(target_name, DSLITE_TARGETS[target_name])
        return build_dslite_target(profile)
    profile = validate_mape_profile(target_name, MAPE_TARGETS[target_name])
    return build_mape_target(profile)


def target_choices(mode):
    if mode == "basic":
        return TARGETS.keys()
    if mode == "dslite":
        return DSLITE_TARGETS.keys()
    return MAPE_TARGETS.keys()


def build_network_args(target, mode, lan_source_port_range=None):
    is_ipv6 = target.get("ip_version") == "v6"
    return SimpleNamespace(
        lan_ns="rb_recovery_lan",
        wan_ns="rb_recovery_wan",
        lan_iface="",
        wan_iface="",
        lan_prefix=int(target.get("lan_prefix_len", 64 if is_ipv6 else 24)),
        wan_prefix=int(target.get("wan_prefix_len", 64 if is_ipv6 else 24)),
        setup_namespaces=True,
        replace_namespaces=True,
        auto_kernel_bind=True,
        wait_interfaces_ready=True,
        interface_ready_timeout_sec=float(NETWORK_DEFAULTS["interface_ready_timeout_sec"]),
        keep_namespaces=False,
        benchmark_mode=mode,
        lan_source_port_range=lan_source_port_range,
    )


def append_row(csv_path, row):
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_path.exists() or csv_path.stat().st_size == 0
    with csv_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description="Wait for router TCP recovery between benchmark tests.")
    parser.add_argument("--mode", choices=("basic", "dslite", "mape"), required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--ip-version", choices=("v4", "v6"), default="v4")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--previous-test", required=True)
    parser.add_argument("--next-test", required=True)
    parser.add_argument("--probe-interval-sec", type=float, default=RECOVERY_DEFAULTS["probe_interval_sec"])
    parser.add_argument("--success-consecutive", type=int, default=RECOVERY_DEFAULTS["success_consecutive"])
    parser.add_argument("--connect-timeout-sec", type=float, default=RECOVERY_DEFAULTS["connect_timeout_sec"])
    parser.add_argument("--tcp-port", type=int, default=RECOVERY_DEFAULTS["tcp_port"])
    add_lan_source_port_range_argument(parser)
    args = parser.parse_args()

    if args.target not in target_choices(args.mode):
        raise SystemExit(f"ERROR: unknown {args.mode} target: {args.target}")
    if args.mode != "basic" and args.ip_version != "v4":
        raise SystemExit(f"ERROR: {args.mode} recovery supports IPv4 only")
    if args.probe_interval_sec < 0 or args.connect_timeout_sec <= 0:
        raise SystemExit("ERROR: recovery intervals and timeouts must be positive")
    if args.success_consecutive <= 0:
        raise SystemExit("ERROR: --success-consecutive must be positive")
    if args.tcp_port <= 0 or args.tcp_port > 65535:
        raise SystemExit("ERROR: --tcp-port must be between 1 and 65535")

    require_root()
    require_command("ip")
    target = resolve_target(args.mode, args.target, args.ip_version)
    network_args = build_network_args(target, args.mode, args.lan_source_port_range)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    recovery_csv = out_dir / "inter_test_recovery.csv"
    network_csv = out_dir / "inter_test_recovery_network.csv"
    server_log_path = out_dir / "inter_test_recovery_server.log"
    server_proc = None

    with network_csv.open("a", newline="", encoding="utf-8") as network_file:
        network_writer = csv.writer(network_file)
        try:
            for namespace in (
                NETWORK_DEFAULTS["lan_namespace"],
                NETWORK_DEFAULTS["wan_namespace"],
                network_args.lan_ns,
                network_args.wan_ns,
            ):
                delete_namespace_if_exists(namespace, network_writer, "recovery_cleanup_namespace")
            prepare_kernel_interfaces_from_profile(network_args, target, network_writer)
            resolve_profile_interfaces(network_args, target, network_writer)
            require_profile_interfaces(network_args, target)
            setup_namespaces(network_args, target, network_writer)
            wait_test_interfaces_ready(network_args)

            server_ip = target["wan_ip"]
            if args.mode == "mape":
                server_ip = target["server_ipv4"]
                run_cmd([
                    "ip", "-n", network_args.wan_ns, "addr", "add",
                    f"{server_ip}/24", "dev", network_args.wan_iface,
                ])

            server_log = server_log_path.open("a", encoding="utf-8")
            server_proc = subprocess.Popen(
                [
                    "ip", "netns", "exec", network_args.wan_ns,
                    sys.executable, "-u", "-c", SERVER_CODE,
                    server_ip, str(args.tcp_port),
                ],
                stdout=server_log,
                stderr=subprocess.STDOUT,
            )

            started = time.monotonic()
            attempt = 0
            consecutive = 0
            while consecutive < args.success_consecutive:
                time.sleep(args.probe_interval_sec)
                attempt += 1
                if server_proc.poll() is not None:
                    raise RuntimeError(
                        f"recovery server exited with code {server_proc.returncode}; "
                        f"see {server_log_path}"
                    )
                proc = run_cmd(
                    [
                        "ip", "netns", "exec", network_args.lan_ns,
                        sys.executable, "-c", CLIENT_CODE,
                        server_ip, str(args.tcp_port), str(args.connect_timeout_sec),
                    ],
                    check=False,
                    timeout=args.connect_timeout_sec + 2.0,
                )
                elapsed = time.monotonic() - started
                if proc.returncode == 0:
                    consecutive += 1
                    status = "ok"
                    detail = ""
                else:
                    consecutive = 0
                    status = "error"
                    detail = ((proc.stderr or proc.stdout or "").strip())[-1000:]
                complete = int(consecutive >= args.success_consecutive)
                row = {
                    "ts_local": ts_iso(),
                    "benchmark_mode": args.mode,
                    "target": args.target,
                    "ip_version": args.ip_version,
                    "lan_source_port_range": format_lan_source_port_range(args.lan_source_port_range),
                    "previous_test": args.previous_test,
                    "next_test": args.next_test,
                    "attempt": attempt,
                    "status": status,
                    "consecutive_successes": consecutive,
                    "required_consecutive_successes": args.success_consecutive,
                    "elapsed_sec": round(elapsed, 6),
                    "recovery_complete": complete,
                    "detail": detail,
                }
                append_row(recovery_csv, row)
                print(
                    f"[RECOVERY] {args.previous_test} -> {args.next_test}: "
                    f"status={status}, consecutive={consecutive}/{args.success_consecutive}, "
                    f"elapsed={elapsed:.3f}s"
                )
        finally:
            if server_proc is not None:
                server_proc.terminate()
                try:
                    server_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    server_proc.kill()
                    server_proc.wait(timeout=3)
            try:
                server_log.close()
            except (NameError, OSError):
                pass
            cleanup_namespaces(network_args, network_writer)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
