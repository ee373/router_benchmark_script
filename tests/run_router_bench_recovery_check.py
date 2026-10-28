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
from tests.lib.astf import (  # type: ignore
    ASTF_TCP_PORT,
    DEFAULT_ASTF_PREFLIGHT_DURATION_SEC,
    DEFAULT_ASTF_PREFLIGHT_MIN_SUCCESS_RATE,
    DEFAULT_ASTF_PREFLIGHT_CPS,
    TrexAstfServer,
    add_trex_api_path,
    install_trex_cfg,
    prepare_trex_interfaces_from_profile,
    resolve_astf_cores,
    run_astf_roundtrip_preflight_consecutive,
    ts_compact,
)
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
from tests.run_router_bench_mape_astf_sessions_cps import (  # type: ignore
    build_server_ip_candidates as build_mape_server_ip_candidates,
)
from tests.run_router_bench_dslite_astf_sessions_cps import (  # type: ignore
    build_server_ip_candidates as build_dslite_server_ip_candidates,
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


def run_astf_recovery(target, mode, args, out_dir):
    """Use the CPS ASTF canary for MAP-E and DS-Lite inter-test recovery."""
    add_trex_api_path()
    from trex.astf.api import ASTFClient

    if mode == "mape":
        build_server_ip_candidates = build_mape_server_ip_candidates
    else:
        build_server_ip_candidates = build_dslite_server_ip_candidates
    server_ips = build_server_ip_candidates(target, args.server_ip_count)
    server_ip_range = (server_ips[0], server_ips[-1])
    recovery_dir = out_dir / f"{mode}_astf_recovery" / ts_compact()
    recovery_dir.mkdir(parents=True, exist_ok=True)
    prepare_trex_interfaces_from_profile(target, recovery_dir)
    install_trex_cfg(target, recovery_dir, no_install_cfg=False)

    server = TrexAstfServer(cores=resolve_astf_cores(target), out_dir=recovery_dir)
    client = None
    try:
        server.start()
        client = ASTFClient(server="127.0.0.1")
        client.connect()
        with (recovery_dir / "astf_raw.csv").open(
            "w", newline="", encoding="utf-8"
        ) as raw_file:
            raw_writer = csv.writer(raw_file)
            raw_writer.writerow(
                ["ts_local", "elapsed_sec", "phase", "test_name", "metric", "value"]
            )
            result = run_astf_roundtrip_preflight_consecutive(
                client=client,
                target=target,
                raw_writer=raw_writer,
                test_name=f"inter_test_{args.previous_test}_to_{args.next_test}",
                recovery_csv=out_dir / "inter_test_recovery.csv",
                required_successes=args.preflight_success_consecutive,
                interval_sec=args.preflight_probe_interval_sec,
                timeout_sec=args.preflight_timeout_sec,
                tcp_port=args.astf_tcp_port,
                cps=DEFAULT_ASTF_PREFLIGHT_CPS,
                duration_sec=DEFAULT_ASTF_PREFLIGHT_DURATION_SEC,
                minimum_success_rate=DEFAULT_ASTF_PREFLIGHT_MIN_SUCCESS_RATE,
                server_ip_range=server_ip_range,
            )
            raw_file.flush()
        print(
            f"[RECOVERY] {mode.upper()} ASTF preflight passed: "
            f"server_ip_count={len(server_ips)} "
            f"server_range={server_ip_range[0]}-{server_ip_range[1]} "
            f"tcp_port={args.astf_tcp_port}",
            flush=True,
        )
        return result
    finally:
        try:
            if client is not None:
                try:
                    client.stop()
                except Exception:
                    pass
                try:
                    client.reset()
                except Exception:
                    pass
                client.disconnect()
        finally:
            server.stop()


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
    parser.add_argument(
        "--server-ip-count",
        type=int,
        default=1,
        help="Server address count for MAP-E/DS-Lite ASTF recovery preflight.",
    )
    parser.add_argument(
        "--astf-tcp-port",
        type=int,
        default=ASTF_TCP_PORT,
        help="ASTF service port used by the MAP-E/DS-Lite recovery preflight.",
    )
    parser.add_argument(
        "--preflight-success-consecutive",
        type=int,
        default=RECOVERY_DEFAULTS["success_consecutive"],
    )
    parser.add_argument(
        "--preflight-probe-interval-sec",
        type=float,
        default=RECOVERY_DEFAULTS["probe_interval_sec"],
    )
    parser.add_argument(
        "--preflight-timeout-sec",
        type=float,
        default=RECOVERY_DEFAULTS["preflight_timeout_sec"],
    )
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
    if args.server_ip_count <= 0:
        raise SystemExit("ERROR: --server-ip-count must be positive")
    if args.astf_tcp_port <= 0 or args.astf_tcp_port > 65535:
        raise SystemExit("ERROR: --astf-tcp-port must be between 1 and 65535")
    if args.preflight_success_consecutive <= 0:
        raise SystemExit("ERROR: --preflight-success-consecutive must be positive")
    if args.preflight_probe_interval_sec < 0 or args.preflight_timeout_sec < 0:
        raise SystemExit("ERROR: ASTF preflight intervals and timeout must be non-negative")

    require_root()
    target = resolve_target(args.mode, args.target, args.ip_version)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.mode in ("mape", "dslite"):
        run_astf_recovery(target, args.mode, args, out_dir)
        return

    require_command("ip")
    network_args = build_network_args(target, args.mode, args.lan_source_port_range)
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
