#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPv4 NAT latency benchmark using iperf3 plus TCP/UDP request-response, without TRex.

Purpose:
  - Measure idle application-layer RTT using TCP or UDP request-response.
  - Measure application-layer RTT at 0%, 10%, 20%, 30%, 40%, 50%, 60%, 70%, 80%, 90%, 95%, and 99% of target line rate.
  - The 0% phase is a no-load baseline; the remaining phases use iperf3 load through the DUT.

Recommended single-host topology:
  - One Linux host has two physical NICs connected to the router/DUT.
  - The LAN NIC is moved into a LAN network namespace.
  - The WAN NIC is moved into a WAN network namespace.
  - The latency client and iperf3 client execute in the LAN namespace.
  - The latency echo server and iperf3 server execute in the WAN namespace.
  - By default, the script creates and removes these namespaces for each run.
  - When TRex/DPDK has detached the NICs from Linux, the script can rebind
    profile PCI devices back to their kernel drivers before namespace setup.

Why namespaces are used:
  When both LAN and WAN addresses are configured in the same Linux network
  namespace, Linux may deliver packets locally and bypass the router/DUT.
  Separate namespaces force packets to leave the LAN NIC, traverse the DUT,
  and return through the WAN NIC.

Tools used:
  - iperf3: TCP/UDP load generation
  - built-in Python socket request-response helper: TCP/UDP RTT samples
  - ip from iproute2: network namespace setup, enabled by default
"""

import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from tests.lib.config import TARGETS, target_for_ip_version  # type: ignore
except Exception:
    from lib.config import TARGETS, target_for_ip_version  # type: ignore

try:
    from tests.lib.timing import (  # type: ignore
        DEFAULT_TEST_START_DELAY_SEC,
        wait_before_test_start,
    )
except Exception:
    from lib.timing import (  # type: ignore
        DEFAULT_TEST_START_DELAY_SEC,
        wait_before_test_start,
    )

try:
    from tests.lib.common import (  # type: ignore
        DEFAULT_RESULT_ROOT,
        parse_non_negative_float,
        parse_positive_float,
        parse_positive_int,
        ts_compact,
        ts_iso,
    )
    from tests.lib.config import load_defaults as _load_defaults  # type: ignore
    from tests.lib.output import prepare_details_dir  # type: ignore
    from tests.lib.preflight import (  # type: ignore
        ConsecutivePreflightTimeoutError,
        DEFAULT_PREFLIGHT_PROBE_INTERVAL_SEC,
        DEFAULT_PREFLIGHT_SUCCESS_CONSECUTIVE,
        run_consecutive_preflight,
    )
    from tests.lib.repeat import DEFAULT_REPEAT, argparse_repeat  # type: ignore
    from tests.lib.source_ports import add_lan_source_port_range_argument  # type: ignore
    from tests.lib.network import (  # type: ignore
        CommandError,
        DEFAULT_ARP_REFRESH_INTERVAL_SEC,
        DEFAULT_INTERFACE_READY_TIMEOUT_SEC,
        DEFAULT_LAN_NS,
        DEFAULT_LAN_PREFIX,
        DEFAULT_POST_NAMESPACE_SETTLE_SEC,
        DEFAULT_WAN_NS,
        DEFAULT_WAN_PREFIX,
        cleanup_namespaces,
        delete_existing_namespaces,
        delete_namespace_if_exists,
        make_namespace_neighbor_refresher,
        merge_target_with_overrides,
        namespace_exists,
        ns_cmd,
        prepare_kernel_interfaces_from_profile,
        prepare_trex_interfaces_from_profile,
        require_command,
        require_profile_interfaces,
        require_root,
        resolve_profile_interfaces,
        root_iface_exists,
        run_cmd,
        setup_namespaces,
        verify_namespaces,
        wait_namespace_interface_ready,
        wait_test_interfaces_ready,
        bind_pci_to_kernel_driver_if_needed,
        direct_profile_iface_entry,
        namespace_iface_exists,
        normalize_pci_bdf,
        ns_prefix,
        pci_current_driver,
        pci_device_path,
        pci_netdev_names,
        pci_netdev_names_for_profile_side,
        pci_unbind_current_driver,
        profile_interface_entry,
        profile_kernel_driver_entry,
        profile_pci_interface_entry,
        profile_port_index,
        resolve_linux_iface_from_profile_value,
        resolve_side_interface,
        sysfs_write,
        wait_for_pci_netdev,
    )
    from tests.lib.arp import NamespaceArpRefresher  # type: ignore
except Exception:
    from lib.common import (  # type: ignore
        DEFAULT_RESULT_ROOT,
        parse_non_negative_float,
        parse_positive_float,
        parse_positive_int,
        ts_compact,
        ts_iso,
    )
    from lib.config import load_defaults as _load_defaults  # type: ignore
    from lib.output import prepare_details_dir  # type: ignore
    from lib.preflight import (  # type: ignore
        ConsecutivePreflightTimeoutError,
        DEFAULT_PREFLIGHT_PROBE_INTERVAL_SEC,
        DEFAULT_PREFLIGHT_SUCCESS_CONSECUTIVE,
        run_consecutive_preflight,
    )
    from lib.repeat import DEFAULT_REPEAT, argparse_repeat  # type: ignore
    from lib.source_ports import add_lan_source_port_range_argument  # type: ignore
    from lib.network import (  # type: ignore
        CommandError,
        DEFAULT_ARP_REFRESH_INTERVAL_SEC,
        DEFAULT_INTERFACE_READY_TIMEOUT_SEC,
        DEFAULT_LAN_NS,
        DEFAULT_LAN_PREFIX,
        DEFAULT_POST_NAMESPACE_SETTLE_SEC,
        DEFAULT_WAN_NS,
        DEFAULT_WAN_PREFIX,
        cleanup_namespaces,
        delete_existing_namespaces,
        delete_namespace_if_exists,
        make_namespace_neighbor_refresher,
        merge_target_with_overrides,
        namespace_exists,
        ns_cmd,
        prepare_kernel_interfaces_from_profile,
        prepare_trex_interfaces_from_profile,
        require_command,
        require_profile_interfaces,
        require_root,
        resolve_profile_interfaces,
        root_iface_exists,
        run_cmd,
        setup_namespaces,
        verify_namespaces,
        wait_namespace_interface_ready,
        wait_test_interfaces_ready,
        bind_pci_to_kernel_driver_if_needed,
        direct_profile_iface_entry,
        namespace_iface_exists,
        normalize_pci_bdf,
        ns_prefix,
        pci_current_driver,
        pci_device_path,
        pci_netdev_names,
        pci_netdev_names_for_profile_side,
        pci_unbind_current_driver,
        profile_interface_entry,
        profile_kernel_driver_entry,
        profile_pci_interface_entry,
        profile_port_index,
        resolve_linux_iface_from_profile_value,
        resolve_side_interface,
        sysfs_write,
        wait_for_pci_netdev,
    )
    from lib.arp import NamespaceArpRefresher  # type: ignore


_L4_DEFAULTS = _load_defaults("l4_latency")
_NETWORK_DEFAULTS = _load_defaults("network")
BENCHMARK_DESCRIPTION = "Run IPv4 NAT latency benchmark using iperf3 plus TCP/UDP request-response, without TRex."
BENCHMARK_TITLE = "IPv4 NAT latency benchmark using iperf3 + TCP/UDP request-response"
DEFAULT_OUTPUT_PREFIX = "iperf3_l4rr_latency"
METADATA_MODE = "iperf3_l4rr_latency"
METADATA_NOTES_EXTRA = []
DEFAULT_TCP_MSS = None
SOURCE_PORTS = ()
_SOURCE_PORT_INDEX = 0
DEFAULT_LATENCY_DURATION_SEC = float(_L4_DEFAULTS["duration_sec"])
DEFAULT_LATENCY_INTERVAL_SEC = float(_L4_DEFAULTS["interval_sec"])
DEFAULT_LATENCY_PAYLOAD_SIZE_BYTES = int(_L4_DEFAULTS["payload_size_bytes"])
DEFAULT_LATENCY_TIMEOUT_SEC = float(_L4_DEFAULTS["timeout_sec"])
DEFAULT_LATENCY_CONNECT_TIMEOUT_SEC = float(_L4_DEFAULTS["connect_timeout_sec"])
DEFAULT_LOAD_WARMUP_SEC = float(_L4_DEFAULTS["load_warmup_sec"])
DEFAULT_LOAD_COOLDOWN_SEC = float(_L4_DEFAULTS["load_cooldown_sec"])
DEFAULT_L4RR_PREFLIGHT_TIMEOUT_SEC = float(_L4_DEFAULTS["preflight_timeout_sec"])
DEFAULT_L4RR_PREFLIGHT_DURATION_SEC = float(_L4_DEFAULTS["preflight_duration_sec"])
DEFAULT_L4RR_PREFLIGHT_INTERVAL_SEC = float(_L4_DEFAULTS["preflight_interval_sec"])
DEFAULT_LOAD_PERCENTAGES = tuple(_L4_DEFAULTS["load_percentages"])
DEFAULT_IPERF3_PORT = int(_L4_DEFAULTS["iperf3_port"])
DEFAULT_LATENCY_PORT = int(_L4_DEFAULTS["latency_port"])
L4RR_HEADER = struct.Struct("!Qd")

SAMPLE_CSV_FIELDS = [
    "phase",
    "sample_index",
    "seq",
    "latency_protocol",
    "unix_ts_start",
    "unix_ts_end",
    "rtt_ms",
    "payload_bytes",
    "reply_bytes",
    "result",
    "error",
]

SUMMARY_FIELDS = [
    "phase",
    "test_name",
    "result",
    "tool_latency",
    "tool_load",
    "target_name",
    "lan_ns",
    "wan_ns",
    "lan_iface",
    "wan_iface",
    "lan_ip",
    "lan_gw",
    "wan_ip",
    "wan_interface_ip",
    "wan_gw",
    "router_wan_ip",
    "latency_protocol",
    "latency_port",
    "latency_interval_sec",
    "latency_payload_size_bytes",
    "latency_timeout_sec",
    "latency_tcp_mss",
    "latency_tcp_mss_effective",
    "load_protocol",
    "load_direction",
    "load_rate",
    "load_percent",
    "computed_load_bps",
    "iperf3_parallel",
    "iperf3_tcp_mss",
    "duration_sec",
    "samples_requested",
    "samples_received",
    "timeout_count",
    "error_count",
    "request_loss_pct",
    "rtt_min_ms",
    "rtt_avg_ms",
    "rtt_p50_ms",
    "rtt_p90_ms",
    "rtt_p95_ms",
    "rtt_p99_ms",
    "rtt_max_ms",
    "rtt_mdev_ms",
    "iperf3_bits_per_second",
    "iperf3_retransmits",
    "iperf3_jitter_ms",
    "iperf3_lost_packets",
    "iperf3_packets",
    "iperf3_lost_percent",
    "error",
]


def parse_load_rate(value):
    text = str(value).strip()
    if not text:
        raise argparse.ArgumentTypeError("load rate must not be empty")
    return text


def parse_load_percentages(value):
    text = str(value).strip()
    if not text:
        raise argparse.ArgumentTypeError("load percentages must not be empty")
    percentages = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            pct = float(item.replace("_", ""))
        except ValueError as exc:
            raise argparse.ArgumentTypeError("load percentages must be comma-separated numbers") from exc
        if pct < 0 or pct >= 100:
            raise argparse.ArgumentTypeError("load percentages must be greater than or equal to 0 and less than 100")
        percentages.append(pct)
    if not percentages:
        raise argparse.ArgumentTypeError("load percentages must contain at least one value")
    return percentages


def percent_label(percent):
    pct = float(percent)
    if pct.is_integer():
        return str(int(pct))
    return (f"{pct:g}").replace(".", "_")


def phase_for_load_percent(percent):
    pct = float(percent)
    if math.isclose(pct, 0.0, abs_tol=1e-12):
        return "idle"
    return f"load_{percent_label(percent)}pct"


def split_load_percentages(percentages):
    idle_requested = False
    loaded = []
    seen = set()
    for percent in percentages:
        pct = float(percent)
        key = percent_label(pct)
        if key in seen:
            continue
        seen.add(key)
        if math.isclose(pct, 0.0, abs_tol=1e-12):
            idle_requested = True
        else:
            loaded.append(pct)
    return idle_requested, loaded


def compute_load_bps(target, percent):
    line_rate_gbps = float(target.get("line_rate_gbps") or 0.0)
    if line_rate_gbps <= 0:
        raise SystemExit("ERROR: target profile must define positive line_rate_gbps for percentage-based iperf3 load")
    return int(round(line_rate_gbps * 1_000_000_000 * float(percent) / 100.0))


def effective_load_rate(args, target, percent):
    if args.load_rate and args.load_rate.lower() != "auto":
        return args.load_rate, ""
    bps = compute_load_bps(target, percent)
    return str(bps), bps


def percentile(values, pct):
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return ""
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * (float(pct) / 100.0)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def mean(values):
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else ""


def mdev(values):
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return ""
    avg = sum(vals) / len(vals)
    return math.sqrt(sum((v - avg) ** 2 for v in vals) / len(vals))


def make_payload(seq, payload_size):
    if payload_size < L4RR_HEADER.size:
        raise ValueError(f"payload size must be >= {L4RR_HEADER.size}")
    header = L4RR_HEADER.pack(int(seq), time.time())
    pad_len = payload_size - len(header)
    return header + (b"x" * pad_len)


def extract_seq(payload):
    if len(payload) < L4RR_HEADER.size:
        return None
    try:
        return L4RR_HEADER.unpack(payload[:L4RR_HEADER.size])[0]
    except Exception:
        return None


def recv_exact(sock_obj, size):
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = sock_obj.recv(remaining)
        if not chunk:
            raise ConnectionError("peer closed TCP connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def socket_family(address):
    return socket.AF_INET6 if ":" in str(address) else socket.AF_INET


def set_tcp_mss(sock_obj, tcp_mss):
    if tcp_mss:
        sock_obj.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, int(tcp_mss))


def next_source_port(count=1):
    """Choose a configured source port and, when needed, a consecutive block."""
    candidates = ordered_source_ports(count, advance=True)
    return candidates[0] if candidates else None


def ordered_source_ports(count=1, *, advance=False, advance_step=1):
    global _SOURCE_PORT_INDEX
    if not SOURCE_PORTS:
        return ()
    count = int(count)
    if count <= 0:
        raise ValueError("source-port count must be positive")
    valid_ports = {int(port) for port in SOURCE_PORTS}
    candidates = [
        int(port)
        for port in SOURCE_PORTS
        if all(int(port) + offset in valid_ports for offset in range(count))
    ]
    if not candidates:
        raise ValueError(
            f"configured source-port set has no {count}-port consecutive range"
        )

    start_index = next(
        (index for index, port in enumerate(candidates) if port >= 32768),
        0,
    )
    if advance:
        start_index = (
            start_index + _SOURCE_PORT_INDEX
        ) % len(candidates)
        _SOURCE_PORT_INDEX += int(advance_step)
    return tuple(candidates[start_index:] + candidates[:start_index])


def l4rr_tcp_server(bind_ip, port, tcp_mss=0):
    srv = socket.socket(socket_family(bind_ip), socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    set_tcp_mss(srv, tcp_mss)
    srv.bind((bind_ip, int(port)))
    srv.listen(32)
    srv.settimeout(0.5)
    print(f"l4rr TCP server listening on {bind_ip}:{port}", file=sys.stderr, flush=True)
    while True:
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        except KeyboardInterrupt:
            break
        set_tcp_mss(conn, tcp_mss)
        conn.settimeout(1.0)
        with conn:
            print(f"accepted TCP client {addr}", file=sys.stderr, flush=True)
            while True:
                try:
                    data = conn.recv(65535)
                    if not data:
                        break
                    conn.sendall(data)
                except socket.timeout:
                    continue
                except (ConnectionError, OSError):
                    break
    srv.close()


def l4rr_udp_server(bind_ip, port):
    srv = socket.socket(socket_family(bind_ip), socket.SOCK_DGRAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind_ip, int(port)))
    srv.settimeout(0.5)
    print(f"l4rr UDP server listening on {bind_ip}:{port}", file=sys.stderr, flush=True)
    while True:
        try:
            data, addr = srv.recvfrom(65535)
        except socket.timeout:
            continue
        except KeyboardInterrupt:
            break
        except OSError:
            break
        if data:
            try:
                srv.sendto(data, addr)
            except OSError as exc:
                print(f"UDP sendto failed for {addr}: {exc}", file=sys.stderr, flush=True)
    srv.close()


def connect_tcp_with_retry(
    server_ip, port, source_ip, connect_timeout_sec, retry_interval_sec=0.1,
    tcp_mss=0, source_port=None, source_ports=(),
):
    deadline = time.monotonic() + float(connect_timeout_sec)
    last_exc = None
    attempt = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if last_exc is not None:
                raise TimeoutError(f"TCP connect retry timeout: {last_exc}")
            raise TimeoutError("TCP connect retry timeout")
        sock_obj = socket.socket(socket_family(server_ip), socket.SOCK_STREAM)
        try:
            set_tcp_mss(sock_obj, tcp_mss)
            candidate_port = (
                source_ports[attempt % len(source_ports)]
                if source_ports
                else source_port
            )
            if source_ip or candidate_port:
                sock_obj.bind(
                    (source_ip or "0.0.0.0", int(candidate_port or 0))
                )
            sock_obj.settimeout(min(0.5, remaining))
            sock_obj.connect((server_ip, int(port)))
            return sock_obj
        except OSError as exc:
            last_exc = exc
            try:
                sock_obj.close()
            except Exception:
                pass
            attempt += 1
            time.sleep(
                min(
                    float(retry_interval_sec),
                    max(0.0, deadline - time.monotonic()),
                )
            )


def l4rr_client_tcp(
    server_ip, port, source_ip, duration_sec, interval_sec, payload_size,
    timeout_sec, connect_timeout_sec, tcp_mss=0, source_port=None,
    source_ports=(),
):
    samples = []
    requested = int(math.ceil(float(duration_sec) / float(interval_sec)))
    sock_obj = connect_tcp_with_retry(
        server_ip,
        port,
        source_ip,
        connect_timeout_sec,
        tcp_mss=tcp_mss,
        source_port=source_port,
        source_ports=source_ports,
    )
    actual_source_port = sock_obj.getsockname()[1]
    sock_obj.settimeout(float(timeout_sec))
    try:
        for seq in range(1, requested + 1):
            payload = make_payload(seq, payload_size)
            start_wall = time.time()
            start_perf = time.perf_counter()
            row = {
                "sample_index": seq,
                "seq": seq,
                "unix_ts_start": start_wall,
                "unix_ts_end": "",
                "rtt_ms": "",
                "payload_bytes": payload_size,
                "reply_bytes": 0,
                "result": "FAIL",
                "error": "",
            }
            try:
                sock_obj.sendall(payload)
                reply = recv_exact(sock_obj, payload_size)
                end_perf = time.perf_counter()
                row["unix_ts_end"] = time.time()
                row["reply_bytes"] = len(reply)
                if extract_seq(reply) != seq:
                    row["error"] = "unexpected TCP reply sequence"
                else:
                    row["rtt_ms"] = (end_perf - start_perf) * 1000.0
                    row["result"] = "PASS"
            except socket.timeout:
                row["unix_ts_end"] = time.time()
                row["error"] = "timeout"
            except Exception as exc:
                row["unix_ts_end"] = time.time()
                row["error"] = str(exc)
            samples.append(row)
            sleep_until = start_wall + float(interval_sec)
            delay = sleep_until - time.time()
            if delay > 0:
                time.sleep(delay)
    finally:
        try:
            effective_mss = sock_obj.getsockopt(
                socket.IPPROTO_TCP, socket.TCP_MAXSEG
            )
        finally:
            sock_obj.close()
    return samples, requested, effective_mss, actual_source_port


def l4rr_client_udp(
    server_ip, port, source_ip, duration_sec, interval_sec, payload_size,
    timeout_sec, source_port=None,
):
    samples = []
    requested = int(math.ceil(float(duration_sec) / float(interval_sec)))
    sock_obj = socket.socket(socket_family(server_ip), socket.SOCK_DGRAM)
    if source_ip or source_port:
        sock_obj.bind((source_ip or "0.0.0.0", int(source_port or 0)))
    actual_source_port = sock_obj.getsockname()[1]
    sock_obj.settimeout(float(timeout_sec))
    try:
        for seq in range(1, requested + 1):
            payload = make_payload(seq, payload_size)
            start_wall = time.time()
            start_perf = time.perf_counter()
            deadline = start_perf + float(timeout_sec)
            row = {
                "sample_index": seq,
                "seq": seq,
                "unix_ts_start": start_wall,
                "unix_ts_end": "",
                "rtt_ms": "",
                "payload_bytes": payload_size,
                "reply_bytes": 0,
                "result": "FAIL",
                "error": "",
            }
            try:
                sock_obj.sendto(payload, (server_ip, int(port)))
                while True:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        raise socket.timeout()
                    sock_obj.settimeout(remaining)
                    reply, _addr = sock_obj.recvfrom(max(65535, payload_size))
                    if extract_seq(reply) != seq:
                        continue
                    end_perf = time.perf_counter()
                    row["unix_ts_end"] = time.time()
                    row["reply_bytes"] = len(reply)
                    row["rtt_ms"] = (end_perf - start_perf) * 1000.0
                    row["result"] = "PASS"
                    break
            except socket.timeout:
                row["unix_ts_end"] = time.time()
                row["error"] = "timeout"
            except Exception as exc:
                row["unix_ts_end"] = time.time()
                row["error"] = str(exc)
            samples.append(row)
            sleep_until = start_wall + float(interval_sec)
            delay = sleep_until - time.time()
            if delay > 0:
                time.sleep(delay)
    finally:
        sock_obj.close()
    return samples, requested, actual_source_port


def run_internal_l4rr_server(argv):
    parser = argparse.ArgumentParser(description="Internal TCP/UDP echo server for router benchmark.")
    parser.add_argument("--protocol", choices=["tcp", "udp"], required=True)
    parser.add_argument("--bind-ip", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--tcp-mss", type=parse_positive_int, default=0)
    args = parser.parse_args(argv)
    if args.protocol == "tcp":
        l4rr_tcp_server(args.bind_ip, args.port, args.tcp_mss)
    else:
        l4rr_udp_server(args.bind_ip, args.port)


def run_internal_l4rr_client(argv):
    parser = argparse.ArgumentParser(description="Internal TCP/UDP request-response client for router benchmark.")
    parser.add_argument("--protocol", choices=["tcp", "udp"], required=True)
    parser.add_argument("--server-ip", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--source-ip", default="")
    parser.add_argument("--duration-sec", type=float, required=True)
    parser.add_argument("--interval-sec", type=float, required=True)
    parser.add_argument("--payload-size-bytes", type=int, required=True)
    parser.add_argument("--timeout-sec", type=float, required=True)
    parser.add_argument("--connect-timeout-sec", type=float, default=DEFAULT_LATENCY_CONNECT_TIMEOUT_SEC)
    parser.add_argument("--tcp-mss", type=parse_positive_int, default=0)
    parser.add_argument("--source-port", type=parse_positive_int, default=0)
    parser.add_argument("--source-ports", default="")
    args = parser.parse_args(argv)
    source_ports = tuple(
        int(port) for port in args.source_ports.split(",") if port
    )
    started = time.time()
    effective_mss = ""
    actual_source_port = args.source_port or ""
    try:
        if args.protocol == "tcp":
            samples, requested, effective_mss, actual_source_port = l4rr_client_tcp(
                args.server_ip, args.port, args.source_ip, args.duration_sec,
                args.interval_sec, args.payload_size_bytes, args.timeout_sec,
                args.connect_timeout_sec, args.tcp_mss, args.source_port or None,
                source_ports,
            )
        else:
            samples, requested, actual_source_port = l4rr_client_udp(
                args.server_ip, args.port, args.source_ip, args.duration_sec,
                args.interval_sec, args.payload_size_bytes, args.timeout_sec,
                args.source_port or None,
            )
        result = "PASS" if any(sample.get("result") == "PASS" for sample in samples) else "FAIL"
        output = {
            "result": result,
            "protocol": args.protocol,
            "server_ip": args.server_ip,
            "source_ip": args.source_ip,
            "port": args.port,
            "source_port": actual_source_port,
            "duration_sec_requested": args.duration_sec,
            "duration_sec_elapsed": time.time() - started,
            "interval_sec": args.interval_sec,
            "payload_size_bytes": args.payload_size_bytes,
            "timeout_sec": args.timeout_sec,
            "tcp_mss": args.tcp_mss if args.protocol == "tcp" else "",
            "tcp_mss_effective": effective_mss,
            "samples_requested": requested,
            "samples": samples,
        }
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))
        return 0 if result == "PASS" else 1
    except Exception as exc:
        output = {
            "result": "FAIL",
            "protocol": args.protocol,
            "server_ip": args.server_ip,
            "source_ip": args.source_ip,
            "port": args.port,
            "source_port": actual_source_port,
            "duration_sec_requested": args.duration_sec,
            "duration_sec_elapsed": time.time() - started,
            "interval_sec": args.interval_sec,
            "payload_size_bytes": args.payload_size_bytes,
            "timeout_sec": args.timeout_sec,
            "tcp_mss": args.tcp_mss if args.protocol == "tcp" else "",
            "tcp_mss_effective": effective_mss,
            "samples_requested": int(math.ceil(float(args.duration_sec) / float(args.interval_sec))),
            "samples": [],
            "error": str(exc),
        }
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))
        return 1


def wait_for_process_exit(proc, timeout_sec, name):
    try:
        return proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGINT)
        try:
            return proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.communicate(timeout=5)


def start_l4rr_server(args, target, out_dir, phase):
    server_stdout_path = out_dir / f"l4rr_server_{phase}.stdout.log"
    server_stderr_path = out_dir / f"l4rr_server_{phase}.stderr.log"
    cmd = ns_cmd(args.wan_ns, [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_l4rr-server",
        "--protocol", args.latency_protocol,
        "--bind-ip", target["wan_ip"],
        "--port", str(args.latency_port),
    ])
    if args.latency_protocol == "tcp" and args.tcp_mss:
        cmd.extend(["--tcp-mss", str(args.tcp_mss)])
    (out_dir / f"l4rr_server_{phase}.cmd.txt").write_text(
        " ".join(shlex.quote(str(x)) for x in cmd) + "\n",
        encoding="utf-8",
    )
    stdout = open(server_stdout_path, "w", encoding="utf-8")
    stderr = open(server_stderr_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, text=True)
    return proc, stdout, stderr


def build_l4rr_client_command(args, target, duration_sec=None, interval_sec=None):
    if duration_sec is None:
        duration_sec = args.latency_duration_sec
    if interval_sec is None:
        interval_sec = args.latency_interval_sec
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_l4rr-client",
        "--protocol", args.latency_protocol,
        "--server-ip", target["wan_ip"],
        "--port", str(args.latency_port),
        "--source-ip", target["lan_ip"],
        "--duration-sec", str(duration_sec),
        "--interval-sec", str(interval_sec),
        "--payload-size-bytes", str(args.latency_payload_size_bytes),
        "--timeout-sec", str(args.latency_timeout_sec),
        "--connect-timeout-sec", str(args.latency_connect_timeout_sec),
    ]
    if args.latency_protocol == "tcp" and args.tcp_mss:
        cmd.extend(["--tcp-mss", str(args.tcp_mss)])
    source_ports = ordered_source_ports(
        advance=True,
        advance_step=4 if args.latency_protocol == "tcp" else 1,
    )
    if source_ports:
        if args.latency_protocol == "tcp":
            cmd.extend([
                "--source-ports",
                ",".join(str(port) for port in source_ports),
            ])
        else:
            cmd.extend(["--source-port", str(source_ports[0])])
    return ns_cmd(args.lan_ns, cmd)


def parse_l4rr_json(path):
    if not path.exists() or path.stat().st_size == 0:
        return {"samples": [], "samples_requested": 0, "error": "empty l4rr client output"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"samples": [], "samples_requested": 0, "error": f"failed to parse l4rr JSON: {exc}"}


def ensure_process_running(proc, name):
    if proc.poll() is not None:
        raise RuntimeError(f"{name} exited before the measurement client started; exit_code={proc.returncode}")


def l4rr_client_result_ok(json_path):
    data = parse_l4rr_json(json_path)
    samples = data.get("samples", []) if isinstance(data, dict) else []
    if not isinstance(samples, list):
        return False
    return bool(samples) and all(sample.get("result") == "PASS" for sample in samples)


def run_l4rr_preflight(args, target, out_dir, phase, neighbor_refresher=None):
    if not args.l4rr_preflight:
        return

    def probe(attempt):
        if (
            neighbor_refresher is not None
            and target.get("benchmark_mode") == "mape"
        ):
            neighbor_refresher.refresh_now(
                f"l4rr_preflight_{phase}_attempt_{attempt}",
                required=True,
            )
        preflight_json = out_dir / f"l4rr_preflight_{phase}_attempt{attempt}.json"
        preflight_stderr = out_dir / f"l4rr_preflight_{phase}_attempt{attempt}.stderr.log"
        cmd = build_l4rr_client_command(
            args,
            target,
            duration_sec=args.l4rr_preflight_duration_sec,
            interval_sec=args.l4rr_preflight_interval_sec,
        )
        (out_dir / f"l4rr_preflight_{phase}_attempt{attempt}.cmd.txt").write_text(
            " ".join(shlex.quote(str(x)) for x in cmd) + "\n",
            encoding="utf-8",
        )
        with open(preflight_json, "w", encoding="utf-8") as out_f, open(preflight_stderr, "w", encoding="utf-8") as err_f:
            proc = subprocess.Popen(cmd, stdout=out_f, stderr=err_f, text=True)
            try:
                wait_for_process_exit(
                    proc,
                    args.l4rr_preflight_duration_sec + args.latency_timeout_sec + args.latency_connect_timeout_sec + 5.0,
                    "l4rr preflight client",
                )
            except Exception as exc:
                raise RuntimeError(str(exc)) from exc
        if l4rr_client_result_ok(preflight_json):
            return preflight_json
        data = parse_l4rr_json(preflight_json)
        last_error = "preflight failed"
        if isinstance(data, dict):
            last_error = str(data.get("error") or data.get("result") or "preflight failed")
        raise RuntimeError(last_error)

    try:
        return run_consecutive_preflight(
            probe,
            phase=phase,
            csv_path=out_dir / "l4rr_preflight_recovery.csv",
            required_successes=args.preflight_success_consecutive,
            interval_sec=args.preflight_probe_interval_sec,
            timeout_sec=args.l4rr_preflight_timeout_sec,
        )
    except ConsecutivePreflightTimeoutError as exc:
        message = (
            f"L4 request-response preflight failed before phase {phase}. "
            f"protocol={args.latency_protocol} "
            f"server={target['wan_ip']}:{args.latency_port} "
            f"source={target['lan_ip']} detail={exc}"
        )
        if args.require_l4rr_preflight:
            raise RuntimeError(message) from exc
        print(f"WARNING: {message}", file=sys.stderr, flush=True)
        return None


def run_l4rr_phase(
    phase, args, target, out_dir, *, tool_load="none", load_percent="",
    computed_load_bps="", neighbor_refresher=None,
):
    server_proc, server_stdout, server_stderr = start_l4rr_server(args, target, out_dir, phase)
    client_json = out_dir / f"l4rr_client_{phase}.json"
    client_stderr = out_dir / f"l4rr_client_{phase}.stderr.log"
    proc = None
    try:
        time.sleep(args.latency_server_start_delay_sec)
        ensure_process_running(server_proc, "l4rr server")
        run_l4rr_preflight(
            args,
            target,
            out_dir,
            phase,
            neighbor_refresher=neighbor_refresher,
        )
        client_cmd = build_l4rr_client_command(args, target)
        (out_dir / f"l4rr_client_{phase}.cmd.txt").write_text(
            " ".join(shlex.quote(str(x)) for x in client_cmd) + "\n",
            encoding="utf-8",
        )
        with open(client_json, "w", encoding="utf-8") as out_f, open(client_stderr, "w", encoding="utf-8") as err_f:
            proc = subprocess.Popen(client_cmd, stdout=out_f, stderr=err_f, text=True)
            wait_for_process_exit(proc, args.latency_duration_sec + args.latency_timeout_sec + args.latency_connect_timeout_sec + 10.0, "l4rr client")
    finally:
        if server_proc.poll() is None:
            server_proc.terminate()
            wait_for_process_exit(server_proc, 5.0, "l4rr server")
        for handle in (server_stdout, server_stderr):
            try:
                handle.close()
            except Exception:
                pass

    data = parse_l4rr_json(client_json)
    samples = data.get("samples", []) if isinstance(data, dict) else []
    if not isinstance(samples, list):
        samples = []
    sample_csv = out_dir / f"latency_{phase}_samples.csv"
    with open(sample_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SAMPLE_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for sample in samples:
            row = dict(sample)
            row.update({"phase": phase, "latency_protocol": args.latency_protocol})
            writer.writerow(row)

    requested = int(data.get("samples_requested") or int(math.ceil(args.latency_duration_sec / args.latency_interval_sec))) if isinstance(data, dict) else 0
    ok_samples = [sample for sample in samples if sample.get("result") == "PASS" and sample.get("rtt_ms") not in ("", None)]
    timeout_count = sum(1 for sample in samples if sample.get("error") == "timeout")
    error_count = sum(1 for sample in samples if sample.get("result") != "PASS" and sample.get("error") != "timeout")
    rtts = [float(sample["rtt_ms"]) for sample in ok_samples]
    request_loss_pct = ((requested - len(ok_samples)) / requested * 100.0) if requested else ""
    error_text = ""
    if isinstance(data, dict) and data.get("error"):
        error_text = str(data.get("error"))
    if proc and proc.returncode not in (0, None) and ok_samples:
        error_text = (error_text + "; " if error_text else "") + f"l4rr client returned exit code {proc.returncode}"

    summary = {
        "phase": phase,
        "test_name": f"iperf3_l4rr_latency_{phase}",
        "result": "PASS" if ok_samples else "FAIL",
        "tool_latency": f"built-in Python socket {args.latency_protocol.upper()} request-response",
        "tool_load": tool_load,
        "target_name": args.target,
        "lan_ns": args.lan_ns,
        "wan_ns": args.wan_ns,
        "lan_iface": args.lan_iface,
        "wan_iface": args.wan_iface,
        "lan_ip": target["lan_ip"],
        "lan_gw": target["lan_gw"],
        "wan_ip": target["wan_ip"],
        "wan_interface_ip": target.get("wan_iface_ip", target["wan_ip"]),
        "wan_gw": target["wan_gw"],
        "router_wan_ip": target["router_wan_ip"],
        "latency_protocol": args.latency_protocol,
        "latency_port": args.latency_port,
        "latency_interval_sec": args.latency_interval_sec,
        "latency_payload_size_bytes": args.latency_payload_size_bytes,
        "latency_timeout_sec": args.latency_timeout_sec,
        "latency_tcp_mss": (
            args.tcp_mss if args.latency_protocol == "tcp" else ""
        ),
        "latency_tcp_mss_effective": (
            data.get("tcp_mss_effective", "")
            if args.latency_protocol == "tcp" and isinstance(data, dict)
            else ""
        ),
        "load_protocol": "none",
        "load_direction": "none",
        "load_rate": "none",
        "load_percent": load_percent,
        "computed_load_bps": computed_load_bps,
        "iperf3_parallel": "",
        "iperf3_tcp_mss": "",
        "duration_sec": data.get("duration_sec_elapsed", args.latency_duration_sec) if isinstance(data, dict) else args.latency_duration_sec,
        "samples_requested": requested,
        "samples_received": len(ok_samples),
        "timeout_count": timeout_count,
        "error_count": error_count,
        "request_loss_pct": request_loss_pct,
        "rtt_min_ms": min(rtts) if rtts else "",
        "rtt_avg_ms": mean(rtts),
        "rtt_p50_ms": percentile(rtts, 50),
        "rtt_p90_ms": percentile(rtts, 90),
        "rtt_p95_ms": percentile(rtts, 95),
        "rtt_p99_ms": percentile(rtts, 99),
        "rtt_max_ms": max(rtts) if rtts else "",
        "rtt_mdev_ms": mdev(rtts),
        "error": error_text if error_text else ("" if ok_samples else "no successful TCP/UDP request-response samples"),
    }
    return summary


def start_iperf3_server(args, target, out_dir, phase):
    server_json = out_dir / f"iperf3_server_{phase}.json"
    server_err = out_dir / f"iperf3_server_{phase}.stderr.log"
    cmd = ns_cmd(args.wan_ns, [
        "iperf3",
        *(["-6"] if target.get("ip_version") == "v6" else []),
        "-s",
        "-1",
        "-B", target["wan_ip"],
        "-p", str(args.iperf3_port),
        "--json",
    ])
    (out_dir / f"iperf3_server_{phase}.cmd.txt").write_text(
        " ".join(shlex.quote(str(x)) for x in cmd) + "\n",
        encoding="utf-8",
    )
    stdout = open(server_json, "w", encoding="utf-8")
    stderr = open(server_err, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, text=True)
    return proc, stdout, stderr, server_json


def build_iperf3_client_command(args, target, duration_sec, load_rate):
    cmd = [
        "iperf3",
        *(["-6"] if target.get("ip_version") == "v6" else []),
        "-c", target["wan_ip"],
        "-B", target["lan_ip"],
        "-p", str(args.iperf3_port),
        "-t", str(int(math.ceil(duration_sec))),
        "-P", str(args.iperf3_parallel),
        "--json",
    ]
    if args.load_protocol == "udp":
        cmd.append("-u")
        cmd.extend(["-b", str(load_rate)])
    else:
        cmd.extend(["-b", str(load_rate)])

    if args.load_direction == "download":
        cmd.append("-R")
    elif args.load_direction == "bidirectional":
        cmd.append("--bidir")

    if args.iperf3_extra_args:
        extra_args = shlex.split(args.iperf3_extra_args)
        if SOURCE_PORTS and any(
            item == "--cport" or item.startswith("--cport=")
            for item in extra_args
        ):
            raise ValueError(
                "--iperf3-extra-args must not override profile source-port selection"
            )
        if args.load_protocol == "tcp" and args.tcp_mss and any(
            item in ("-M", "--set-mss")
            or item.startswith("--set-mss=")
            or (item.startswith("-M") and item != "-M")
            for item in extra_args
        ):
            raise ValueError(
                "--iperf3-extra-args must not override the configured TCP MSS"
            )
        cmd.extend(extra_args)
    if args.load_protocol == "tcp" and args.tcp_mss:
        cmd.extend(["-M", str(args.tcp_mss)])
    source_port = next_source_port(args.iperf3_parallel)
    if source_port is not None:
        cmd.extend(["--cport", str(source_port)])
    return ns_cmd(args.lan_ns, cmd)


def parse_iperf3_json(path):
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    end = data.get("end") if isinstance(data, dict) else {}
    if not isinstance(end, dict):
        return {}

    result = {
        "iperf3_bits_per_second": "",
        "iperf3_retransmits": "",
        "iperf3_jitter_ms": "",
        "iperf3_lost_packets": "",
        "iperf3_packets": "",
        "iperf3_lost_percent": "",
    }

    sum_sent = end.get("sum_sent") if isinstance(end.get("sum_sent"), dict) else {}
    sum_received = end.get("sum_received") if isinstance(end.get("sum_received"), dict) else {}
    candidate = sum_received or sum_sent
    if candidate:
        result["iperf3_bits_per_second"] = candidate.get("bits_per_second", "")
    if sum_sent:
        result["iperf3_retransmits"] = sum_sent.get("retransmits", "")

    udp_sum = end.get("sum") if isinstance(end.get("sum"), dict) else {}
    if udp_sum:
        result["iperf3_bits_per_second"] = udp_sum.get("bits_per_second", result["iperf3_bits_per_second"])
        result["iperf3_jitter_ms"] = udp_sum.get("jitter_ms", "")
        result["iperf3_lost_packets"] = udp_sum.get("lost_packets", "")
        result["iperf3_packets"] = udp_sum.get("packets", "")
        result["iperf3_lost_percent"] = udp_sum.get("lost_percent", "")

    bidir_bits = []
    for key in ("sum_sent_bidir", "sum_received_bidir"):
        item = end.get(key)
        if isinstance(item, dict) and item.get("bits_per_second") is not None:
            bidir_bits.append(float(item.get("bits_per_second") or 0.0))
    if bidir_bits:
        result["iperf3_bits_per_second"] = sum(bidir_bits)
    return result


def build_l4rr_summary_from_json(phase, args, target, out_dir, data, proc_returncode=None, *, tool_load="none", load_rate="none", load_percent="", computed_load_bps=""):
    samples = data.get("samples", []) if isinstance(data, dict) else []
    if not isinstance(samples, list):
        samples = []
    sample_csv = out_dir / f"latency_{phase}_samples.csv"
    with open(sample_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SAMPLE_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for sample in samples:
            row = dict(sample)
            row.update({"phase": phase, "latency_protocol": args.latency_protocol})
            writer.writerow(row)

    requested = int(data.get("samples_requested") or int(math.ceil(args.latency_duration_sec / args.latency_interval_sec))) if isinstance(data, dict) else 0
    ok_samples = [sample for sample in samples if sample.get("result") == "PASS" and sample.get("rtt_ms") not in ("", None)]
    timeout_count = sum(1 for sample in samples if sample.get("error") == "timeout")
    error_count = sum(1 for sample in samples if sample.get("result") != "PASS" and sample.get("error") != "timeout")
    rtts = [float(sample["rtt_ms"]) for sample in ok_samples]
    request_loss_pct = ((requested - len(ok_samples)) / requested * 100.0) if requested else ""
    error_text = ""
    if isinstance(data, dict) and data.get("error"):
        error_text = str(data.get("error"))
    if proc_returncode not in (0, None) and ok_samples:
        error_text = (error_text + "; " if error_text else "") + f"l4rr client returned exit code {proc_returncode}"

    is_idle = phase == "idle"
    summary = {
        "phase": phase,
        "test_name": f"iperf3_l4rr_latency_{phase}",
        "result": "PASS" if ok_samples else "FAIL",
        "tool_latency": f"built-in Python socket {args.latency_protocol.upper()} request-response",
        "tool_load": tool_load,
        "target_name": args.target,
        "lan_ns": args.lan_ns,
        "wan_ns": args.wan_ns,
        "lan_iface": args.lan_iface,
        "wan_iface": args.wan_iface,
        "lan_ip": target["lan_ip"],
        "lan_gw": target["lan_gw"],
        "wan_ip": target["wan_ip"],
        "wan_interface_ip": target.get("wan_iface_ip", target["wan_ip"]),
        "wan_gw": target["wan_gw"],
        "router_wan_ip": target["router_wan_ip"],
        "latency_protocol": args.latency_protocol,
        "latency_port": args.latency_port,
        "latency_interval_sec": args.latency_interval_sec,
        "latency_payload_size_bytes": args.latency_payload_size_bytes,
        "latency_timeout_sec": args.latency_timeout_sec,
        "latency_tcp_mss": (
            args.tcp_mss if args.latency_protocol == "tcp" else ""
        ),
        "latency_tcp_mss_effective": (
            data.get("tcp_mss_effective", "")
            if args.latency_protocol == "tcp" and isinstance(data, dict)
            else ""
        ),
        "load_protocol": "none" if is_idle else args.load_protocol,
        "load_direction": "none" if is_idle else args.load_direction,
        "load_rate": "none" if is_idle else load_rate,
        "load_percent": load_percent if load_percent != "" else (0 if is_idle else ""),
        "computed_load_bps": computed_load_bps if computed_load_bps != "" else (0 if is_idle else ""),
        "iperf3_parallel": "" if is_idle else args.iperf3_parallel,
        "iperf3_tcp_mss": (
            args.tcp_mss
            if not is_idle and args.load_protocol == "tcp"
            else ""
        ),
        "duration_sec": data.get("duration_sec_elapsed", args.latency_duration_sec) if isinstance(data, dict) else args.latency_duration_sec,
        "samples_requested": requested,
        "samples_received": len(ok_samples),
        "timeout_count": timeout_count,
        "error_count": error_count,
        "request_loss_pct": request_loss_pct,
        "rtt_min_ms": min(rtts) if rtts else "",
        "rtt_avg_ms": mean(rtts),
        "rtt_p50_ms": percentile(rtts, 50),
        "rtt_p90_ms": percentile(rtts, 90),
        "rtt_p95_ms": percentile(rtts, 95),
        "rtt_p99_ms": percentile(rtts, 99),
        "rtt_max_ms": max(rtts) if rtts else "",
        "rtt_mdev_ms": mdev(rtts),
        "error": error_text if error_text else ("" if ok_samples else "no successful TCP/UDP request-response samples"),
    }
    return summary


def run_loaded_phase(
    phase, args, target, out_dir, *, load_rate, load_percent,
    computed_load_bps, neighbor_refresher=None,
):
    l4_server_proc, l4_server_stdout, l4_server_stderr = start_l4rr_server(args, target, out_dir, phase)
    iperf_server_proc, iperf_server_stdout, iperf_server_stderr, _server_json = start_iperf3_server(args, target, out_dir, phase)
    client_json = out_dir / f"iperf3_client_{phase}.json"
    client_err = out_dir / f"iperf3_client_{phase}.stderr.log"
    iperf_client_stdout = None
    iperf_client_stderr = None
    iperf_client_proc = None
    summary = None
    try:
        time.sleep(max(args.latency_server_start_delay_sec, args.iperf3_server_start_delay_sec))
        ensure_process_running(l4_server_proc, "l4rr server")
        ensure_process_running(iperf_server_proc, "iperf3 server")
        run_l4rr_preflight(
            args,
            target,
            out_dir,
            phase,
            neighbor_refresher=neighbor_refresher,
        )
        load_duration = args.latency_duration_sec + args.load_warmup_sec + args.load_cooldown_sec
        client_cmd = build_iperf3_client_command(args, target, load_duration, load_rate)
        (out_dir / f"iperf3_client_{phase}.cmd.txt").write_text(
            " ".join(shlex.quote(str(x)) for x in client_cmd) + "\n",
            encoding="utf-8",
        )
        iperf_client_stdout = open(client_json, "w", encoding="utf-8")
        iperf_client_stderr = open(client_err, "w", encoding="utf-8")
        iperf_client_proc = subprocess.Popen(client_cmd, stdout=iperf_client_stdout, stderr=iperf_client_stderr, text=True)
        time.sleep(args.load_warmup_sec)

        l4_client_json = out_dir / f"l4rr_client_{phase}.json"
        l4_client_stderr = out_dir / f"l4rr_client_{phase}.stderr.log"
        l4_client_cmd = build_l4rr_client_command(args, target)
        (out_dir / f"l4rr_client_{phase}.cmd.txt").write_text(
            " ".join(shlex.quote(str(x)) for x in l4_client_cmd) + "\n",
            encoding="utf-8",
        )
        with open(l4_client_json, "w", encoding="utf-8") as out_f, open(l4_client_stderr, "w", encoding="utf-8") as err_f:
            l4_client_proc = subprocess.Popen(l4_client_cmd, stdout=out_f, stderr=err_f, text=True)
            wait_for_process_exit(l4_client_proc, args.latency_duration_sec + args.latency_timeout_sec + args.latency_connect_timeout_sec + 10.0, "l4rr client")

        data = parse_l4rr_json(l4_client_json)
        summary = build_l4rr_summary_from_json(
            phase,
            args,
            target,
            out_dir,
            data,
            l4_client_proc.returncode,
            tool_load="iperf3",
            load_rate=load_rate,
            load_percent=load_percent,
            computed_load_bps=computed_load_bps,
        )
        wait_for_process_exit(iperf_client_proc, args.load_cooldown_sec + 15.0, "iperf3 client")
        wait_for_process_exit(iperf_server_proc, 10.0, "iperf3 server")
    finally:
        if iperf_client_proc and iperf_client_proc.poll() is None:
            iperf_client_proc.terminate()
            wait_for_process_exit(iperf_client_proc, 5.0, "iperf3 client")
        if iperf_server_proc.poll() is None:
            iperf_server_proc.terminate()
            wait_for_process_exit(iperf_server_proc, 5.0, "iperf3 server")
        if l4_server_proc.poll() is None:
            l4_server_proc.terminate()
            wait_for_process_exit(l4_server_proc, 5.0, "l4rr server")
        for handle in (iperf_client_stdout, iperf_client_stderr, iperf_server_stdout, iperf_server_stderr, l4_server_stdout, l4_server_stderr):
            try:
                if handle:
                    handle.close()
            except Exception:
                pass

    if summary is None:
        summary = {
            "phase": phase,
            "test_name": f"iperf3_l4rr_latency_{phase}",
            "result": "FAIL",
            "tool_latency": f"built-in Python socket {args.latency_protocol.upper()} request-response",
            "tool_load": "iperf3",
            "load_rate": load_rate,
            "load_percent": load_percent,
            "computed_load_bps": computed_load_bps,
            "error": "loaded phase failed before latency summary was created",
        }
    iperf3_metrics = parse_iperf3_json(client_json)
    summary.update(iperf3_metrics)
    if iperf_client_proc and iperf_client_proc.returncode not in (0, None):
        summary["result"] = "FAIL"
        current_error = summary.get("error") or ""
        summary["error"] = (current_error + "; " if current_error else "") + f"iperf3 client returned exit code {iperf_client_proc.returncode}"
    return summary


def write_summary_csv(path, rows):
    fields = list(SUMMARY_FIELDS)
    for row in rows:
        for key in row.keys():
            if key not in fields:
                fields.append(key)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def make_delta_row(idle_row, loaded_row, args, target):
    def as_float(value):
        try:
            if value == "" or value is None:
                return None
            return float(value)
        except Exception:
            return None

    idle_avg = as_float(idle_row.get("rtt_avg_ms")) if idle_row else None
    loaded_avg = as_float(loaded_row.get("rtt_avg_ms")) if loaded_row else None
    idle_p95 = as_float(idle_row.get("rtt_p95_ms")) if idle_row else None
    loaded_p95 = as_float(loaded_row.get("rtt_p95_ms")) if loaded_row else None
    idle_p99 = as_float(idle_row.get("rtt_p99_ms")) if idle_row else None
    loaded_p99 = as_float(loaded_row.get("rtt_p99_ms")) if loaded_row else None
    loaded_phase = loaded_row.get("phase", "loaded") if loaded_row else "loaded"

    row = {
        "phase": f"delta_vs_idle_{loaded_phase}",
        "test_name": f"iperf3_l4rr_latency_delta_vs_idle_{loaded_phase}",
        "result": "PASS" if idle_row and loaded_row and idle_row.get("result") == "PASS" and loaded_row.get("result") == "PASS" else "FAIL",
        "tool_latency": f"built-in Python socket {args.latency_protocol.upper()} request-response",
        "tool_load": "iperf3",
        "target_name": args.target,
        "lan_ns": args.lan_ns,
        "wan_ns": args.wan_ns,
        "lan_iface": args.lan_iface,
        "wan_iface": args.wan_iface,
        "lan_ip": target["lan_ip"],
        "lan_gw": target["lan_gw"],
        "wan_ip": target["wan_ip"],
        "wan_interface_ip": target.get("wan_iface_ip", target["wan_ip"]),
        "wan_gw": target["wan_gw"],
        "router_wan_ip": target["router_wan_ip"],
        "latency_protocol": args.latency_protocol,
        "latency_port": args.latency_port,
        "latency_interval_sec": args.latency_interval_sec,
        "latency_payload_size_bytes": args.latency_payload_size_bytes,
        "latency_timeout_sec": args.latency_timeout_sec,
        "latency_tcp_mss": (
            args.tcp_mss if args.latency_protocol == "tcp" else ""
        ),
        "latency_tcp_mss_effective": (
            loaded_row.get("latency_tcp_mss_effective", "")
            if loaded_row
            else ""
        ),
        "load_protocol": loaded_row.get("load_protocol", args.load_protocol) if loaded_row else args.load_protocol,
        "load_direction": loaded_row.get("load_direction", args.load_direction) if loaded_row else args.load_direction,
        "load_rate": loaded_row.get("load_rate", "") if loaded_row else "",
        "load_percent": loaded_row.get("load_percent", "") if loaded_row else "",
        "computed_load_bps": loaded_row.get("computed_load_bps", "") if loaded_row else "",
        "iperf3_parallel": args.iperf3_parallel,
        "iperf3_tcp_mss": (
            loaded_row.get("iperf3_tcp_mss", "")
            if loaded_row
            else (args.tcp_mss if args.load_protocol == "tcp" else "")
        ),
        "duration_sec": args.latency_duration_sec,
        "idle_rtt_avg_ms": idle_avg if idle_avg is not None else "",
        "loaded_rtt_avg_ms": loaded_avg if loaded_avg is not None else "",
        "delta_rtt_avg_ms": (loaded_avg - idle_avg) if idle_avg is not None and loaded_avg is not None else "",
        "idle_rtt_p95_ms": idle_p95 if idle_p95 is not None else "",
        "loaded_rtt_p95_ms": loaded_p95 if loaded_p95 is not None else "",
        "delta_rtt_p95_ms": (loaded_p95 - idle_p95) if idle_p95 is not None and loaded_p95 is not None else "",
        "idle_rtt_p99_ms": idle_p99 if idle_p99 is not None else "",
        "loaded_rtt_p99_ms": loaded_p99 if loaded_p99 is not None else "",
        "delta_rtt_p99_ms": (loaded_p99 - idle_p99) if idle_p99 is not None and loaded_p99 is not None else "",
    }
    if loaded_row:
        for key in (
            "iperf3_bits_per_second",
            "iperf3_retransmits",
            "iperf3_jitter_ms",
            "iperf3_lost_packets",
            "iperf3_packets",
            "iperf3_lost_percent",
        ):
            row[key] = loaded_row.get(key, "")
    return row


def write_metadata(out_dir, args, target):
    notes = [
        "TRex tests are not replaced by this script; this is an additional latency benchmark.",
        "Separate network namespaces are enabled by default so that Linux does not deliver LAN-to-WAN traffic locally on the test host.",
        "Existing benchmark namespaces are replaced by default; this makes launcher execution after TRex ASTF safe against stale namespace state.",
        "When Linux netdevs are missing after TRex/DPDK use, profile PCI devices are rebound to kernel drivers by default before namespace setup.",
        "Linux interface names are read from target profile lan_iface/wan_iface. PCI BDF auto-resolution is a fallback only for profiles that omit those fields.",
        "Latency is measured with TCP or UDP request-response RTT while iperf3 generates TCP or UDP load. The default L4 latency mode uses UDP for both latency request-response and iperf3 load.",
        "Default phases are 0%, 10%, 20%, 30%, 40%, 50%, 60%, 70%, 80%, 90%, 95%, and 99%. The 0% phase is recorded as idle. Percent-based load is calculated from target profile line_rate_gbps.",
        "ICMP ping is not used for latency measurement in this script.",
    ]
    if args.tcp_mss:
        notes.append(
            f"TCP MSS {args.tcp_mss} is applied to TCP request-response sockets and TCP iperf3 load."
        )
    notes.extend(METADATA_NOTES_EXTRA)
    metadata = {
        "created_at": ts_iso(),
        "mode": METADATA_MODE,
        "target_name": args.target,
        "target": target,
        "args": vars(args),
        "tools": {
            "load_generation": "iperf3",
            "latency_measurement": f"built-in Python socket {args.latency_protocol.upper()} request-response",
            "namespace_setup": "ip (iproute2)",
            "kernel_rebind": "modprobe + sysfs PCI driver bind/unbind when needed",
        },
        "notes": notes,
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=BENCHMARK_DESCRIPTION)
    parser.add_argument("--ip-version", choices=("v4", "v6"), default="v4", help=argparse.SUPPRESS)
    parser.add_argument("--target", choices=TARGETS.keys(), default=_L4_DEFAULTS["target"])
    parser.add_argument("--out-dir", default="")
    parser.add_argument(
        "--repeat",
        type=argparse_repeat,
        default=DEFAULT_REPEAT,
        help="Repeat the complete idle and loaded latency measurement sequence. Default: 1.",
    )

    parser.add_argument("--lan-iface", default="", help="Linux interface name connected to the DUT LAN side. Default: read lan_iface from --target profile. PCI BDF auto-resolution is fallback only when the profile omits lan_iface.")
    parser.add_argument("--wan-iface", default="", help="Linux interface name connected to the DUT WAN side. Default: read wan_iface from --target profile. PCI BDF auto-resolution is fallback only when the profile omits wan_iface.")
    parser.add_argument("--lan-ns", default=DEFAULT_LAN_NS, help="LAN network namespace name. Use root only for preconfigured non-namespace tests.")
    parser.add_argument("--wan-ns", default=DEFAULT_WAN_NS, help="WAN network namespace name. Use root only for preconfigured non-namespace tests.")
    parser.add_argument("--setup-namespaces", dest="setup_namespaces", action="store_true", default=_NETWORK_DEFAULTS["setup_namespaces"], help="Create namespaces, move LAN/WAN interfaces into them, assign addresses, and set routes. Enabled by default for this test; kept for compatibility.")
    parser.add_argument("--no-setup-namespaces", dest="setup_namespaces", action="store_false", help="Do not create or configure namespaces. Use only for a preconfigured topology.")
    parser.add_argument("--replace-namespaces", dest="replace_namespaces", action="store_true", default=_NETWORK_DEFAULTS["replace_namespaces"], help="Delete existing --lan-ns/--wan-ns before setup. Enabled by default for this test; kept for compatibility.")
    parser.add_argument("--no-replace-namespaces", dest="replace_namespaces", action="store_false", help="Fail instead of deleting existing --lan-ns/--wan-ns.")
    parser.add_argument("--keep-namespaces", action="store_true", help="Leave namespaces and interfaces in place after the test.")
    parser.add_argument("--auto-kernel-bind", dest="auto_kernel_bind", action="store_true", default=_NETWORK_DEFAULTS["auto_kernel_bind"], help="Before namespace setup, bind profile PCI devices back to their kernel drivers when Linux netdevs are missing. Enabled by default.")
    parser.add_argument("--no-auto-kernel-bind", dest="auto_kernel_bind", action="store_false", help="Do not attempt PCI driver rebinding before namespace setup.")
    parser.add_argument("--restore-dpdk-after", dest="restore_dpdk_after", action="store_true", default=_NETWORK_DEFAULTS["restore_dpdk_after"], help="After the L4 test, delete namespaces and bind profile PCI devices back to the profile DPDK driver. Enabled by default.")
    parser.add_argument("--no-restore-dpdk-after", dest="restore_dpdk_after", action="store_false", help="Leave the NICs on their kernel drivers after the L4 test.")
    parser.add_argument("--dpdk-driver", default="", help="Override the DPDK driver used when --restore-dpdk-after is enabled.")
    parser.add_argument("--lan-prefix", type=parse_positive_int, default=DEFAULT_LAN_PREFIX)
    parser.add_argument("--wan-prefix", type=parse_positive_int, default=DEFAULT_WAN_PREFIX)

    parser.add_argument("--lan-ip", default="")
    parser.add_argument("--lan-gw", default="")
    parser.add_argument("--wan-ip", default="")
    parser.add_argument("--wan-gw", default="")
    parser.add_argument("--router-wan-ip", default="")
    add_lan_source_port_range_argument(parser)

    parser.add_argument("--latency-duration-sec", "--duration", dest="latency_duration_sec", type=parse_positive_float, default=DEFAULT_LATENCY_DURATION_SEC)
    parser.add_argument("--l4-transport", choices=["tcp", "udp"], default="", help="Set both --latency-protocol and --load-protocol unless either option is explicitly specified. Default behavior uses UDP for both latency and load.")
    parser.add_argument("--latency-protocol", choices=["tcp", "udp"], default=_L4_DEFAULTS["latency_protocol"], help="Protocol used by the latency request-response measurement. Default: udp.")
    parser.add_argument("--latency-port", type=parse_positive_int, default=DEFAULT_LATENCY_PORT)
    parser.add_argument("--latency-interval-sec", dest="latency_interval_sec", type=parse_positive_float, default=DEFAULT_LATENCY_INTERVAL_SEC)
    parser.add_argument("--ping-interval-sec", dest="latency_interval_sec", type=parse_positive_float, help=argparse.SUPPRESS)
    parser.add_argument("--latency-payload-size-bytes", dest="latency_payload_size_bytes", type=parse_positive_int, default=DEFAULT_LATENCY_PAYLOAD_SIZE_BYTES)
    parser.add_argument("--ping-size-bytes", dest="latency_payload_size_bytes", type=parse_positive_int, help=argparse.SUPPRESS)
    parser.add_argument("--latency-timeout-sec", type=parse_positive_float, default=DEFAULT_LATENCY_TIMEOUT_SEC)
    parser.add_argument(
        "--tcp-mss",
        type=parse_positive_int,
        default=DEFAULT_TCP_MSS,
        help="Set TCP MSS for TCP request-response sockets and TCP iperf3 load. Default: unset.",
    )
    parser.add_argument("--latency-connect-timeout-sec", type=parse_positive_float, default=DEFAULT_LATENCY_CONNECT_TIMEOUT_SEC)
    parser.add_argument("--latency-server-start-delay-sec", type=parse_non_negative_float, default=_L4_DEFAULTS["latency_server_start_delay_sec"])
    parser.add_argument("--l4rr-preflight", dest="l4rr_preflight", action="store_true", default=_L4_DEFAULTS["preflight"], help="Run a short TCP/UDP request-response preflight before each measured phase. Enabled by default.")
    parser.add_argument("--no-l4rr-preflight", dest="l4rr_preflight", action="store_false", help="Disable L4 request-response preflight.")
    parser.add_argument("--require-l4rr-preflight", dest="require_l4rr_preflight", action="store_true", default=_L4_DEFAULTS["require_preflight"], help="Fail the phase when preflight cannot pass. Enabled by default.")
    parser.add_argument("--no-require-l4rr-preflight", dest="require_l4rr_preflight", action="store_false", help="Continue even when preflight fails.")
    parser.add_argument("--l4rr-preflight-timeout-sec", type=parse_positive_float, default=DEFAULT_L4RR_PREFLIGHT_TIMEOUT_SEC)
    parser.add_argument("--l4rr-preflight-duration-sec", type=parse_positive_float, default=DEFAULT_L4RR_PREFLIGHT_DURATION_SEC)
    parser.add_argument("--l4rr-preflight-interval-sec", type=parse_positive_float, default=DEFAULT_L4RR_PREFLIGHT_INTERVAL_SEC)
    parser.add_argument(
        "--preflight-success-consecutive",
        type=parse_positive_int,
        default=DEFAULT_PREFLIGHT_SUCCESS_CONSECUTIVE,
        help="Required consecutive successful L4 preflight executions. Default: 3.",
    )
    parser.add_argument(
        "--preflight-probe-interval-sec",
        type=parse_non_negative_float,
        default=DEFAULT_PREFLIGHT_PROBE_INTERVAL_SEC,
        help="Seconds between L4 preflight executions. Default: 2.",
    )
    parser.add_argument("--post-namespace-settle-sec", type=parse_non_negative_float, default=DEFAULT_POST_NAMESPACE_SETTLE_SEC)
    parser.add_argument("--test-start-delay-sec", type=parse_non_negative_float, default=DEFAULT_TEST_START_DELAY_SEC, help="Seconds to wait after namespace interfaces report link-up and before testing. Default: 15.")
    parser.add_argument("--wait-interfaces-ready", dest="wait_interfaces_ready", action="store_true", default=_NETWORK_DEFAULTS["wait_interfaces_ready"], help="Wait until namespace interfaces report UP/LOWER_UP. Enabled by default.")
    parser.add_argument("--no-wait-interfaces-ready", dest="wait_interfaces_ready", action="store_false", help="Do not wait for namespace interface readiness.")
    parser.add_argument("--interface-ready-timeout-sec", type=parse_positive_float, default=DEFAULT_INTERFACE_READY_TIMEOUT_SEC)
    parser.add_argument("--skip-idle", action="store_true", help="Skip no-load latency measurement.")
    parser.add_argument("--skip-loaded", action="store_true", help="Skip latency measurement under iperf3 load.")

    parser.add_argument("--iperf3-port", type=parse_positive_int, default=DEFAULT_IPERF3_PORT)
    parser.add_argument("--iperf3-parallel", type=parse_positive_int, default=_L4_DEFAULTS["iperf3_parallel"])
    parser.add_argument("--iperf3-server-start-delay-sec", type=parse_non_negative_float, default=_L4_DEFAULTS["iperf3_server_start_delay_sec"])
    parser.add_argument("--iperf3-extra-args", default="", help="Additional arguments appended to iperf3 client command. Quote as one string.")
    parser.add_argument("--load-protocol", choices=["tcp", "udp"], default=_L4_DEFAULTS["load_protocol"])
    parser.add_argument("--load-direction", choices=["upload", "download", "bidirectional"], default=_L4_DEFAULTS["load_direction"])
    parser.add_argument("--load-percentages", type=parse_load_percentages, default=list(DEFAULT_LOAD_PERCENTAGES), help="Comma-separated load percentages based on target line_rate_gbps. 0 means no-load baseline. Default: 0,10,20,30,40,50,60,70,80,90,95,99.")
    parser.add_argument("--load-rate", type=parse_load_rate, default=_L4_DEFAULTS["load_rate"], help="Compatibility override for iperf3 -b. Default auto computes each loaded phase from --load-percentages.")
    parser.add_argument("--load-warmup-sec", type=parse_non_negative_float, default=DEFAULT_LOAD_WARMUP_SEC)
    parser.add_argument("--load-cooldown-sec", type=parse_non_negative_float, default=DEFAULT_LOAD_COOLDOWN_SEC)

    argv_for_explicit_check = sys.argv[1:]
    latency_protocol_explicit = any(a == "--latency-protocol" or a.startswith("--latency-protocol=") for a in argv_for_explicit_check)
    load_protocol_explicit = any(a == "--load-protocol" or a.startswith("--load-protocol=") for a in argv_for_explicit_check)

    args, unknown_args = parser.parse_known_args()
    if args.l4_transport:
        if not latency_protocol_explicit:
            args.latency_protocol = args.l4_transport
        if not load_protocol_explicit:
            args.load_protocol = args.l4_transport
    if unknown_args:
        print(f"WARNING: ignored unsupported options for iperf3 L4 latency script: {' '.join(unknown_args)}")

    if args.skip_idle and args.skip_loaded:
        raise SystemExit("ERROR: both --skip-idle and --skip-loaded were specified; no measurement remains")
    max_prefix = 128 if args.ip_version == "v6" else 32
    if args.lan_prefix > max_prefix or args.wan_prefix > max_prefix:
        raise SystemExit(f"ERROR: --lan-prefix and --wan-prefix must be 1..{max_prefix}")
    if args.load_rate.lower() == "auto" and not args.load_percentages:
        raise SystemExit("ERROR: --load-percentages must contain at least one value")
    if args.latency_payload_size_bytes < L4RR_HEADER.size:
        raise SystemExit(f"ERROR: --latency-payload-size-bytes must be >= {L4RR_HEADER.size}")
    if args.latency_port == args.iperf3_port:
        raise SystemExit("ERROR: --latency-port and --iperf3-port must be different")

    require_root()
    if not args.skip_loaded:
        require_command("iperf3")
    require_command("ip")
    require_command(sys.executable)
    if args.ip_version == "v6":
        require_command("ping")

    target = merge_target_with_overrides(
        target_for_ip_version(TARGETS[args.target], args.ip_version),
        args,
    )
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_RESULT_ROOT / f"{DEFAULT_OUTPUT_PREFIX}_{args.target}_{ts_compact()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    details_dir = prepare_details_dir(out_dir)

    rows = []
    ns_log_path = details_dir / "namespace_setup.csv"
    with open(ns_log_path, "w", newline="") as ns_log_file:
        ns_log = csv.writer(ns_log_file)
        ns_log.writerow(["ts_local", "action", "detail"])

        if args.setup_namespaces and args.replace_namespaces:
            delete_existing_namespaces(args, ns_log)
        if args.setup_namespaces:
            prepare_kernel_interfaces_from_profile(args, target, ns_log)
        resolve_profile_interfaces(args, target, ns_log)
        if args.setup_namespaces:
            require_profile_interfaces(args, target)

        write_metadata(details_dir, args, target)

        print("============================================================")
        print(BENCHMARK_TITLE)
        print("============================================================")
        print(f"Target          : {args.target}")
        print(f"Description     : {target['description']}")
        print(f"Topology        : {target.get('topology', 'router_nat')}")
        print(f"Tools           : iperf3(load), Python socket {args.latency_protocol.upper()} request-response(latency), ip(iproute2 namespaces)")
        print(f"LAN namespace   : {args.lan_ns}")
        print(f"WAN namespace   : {args.wan_ns}")
        print(f"LAN interface   : {args.lan_iface or '(preconfigured)'}")
        print(f"WAN interface   : {args.wan_iface or '(preconfigured)'}")
        print(f"Namespace setup : {'enabled' if args.setup_namespaces else 'disabled'} replace_existing={'yes' if args.replace_namespaces else 'no'} keep={'yes' if args.keep_namespaces else 'no'}")
        print(f"Kernel bind     : {'auto' if args.auto_kernel_bind else 'disabled'}")
        print(f"LAN endpoint    : {target['lan_ip']} gw {target['lan_gw']}")
        print(f"WAN endpoint    : {target['wan_ip']} gw {target['router_wan_ip']}")
        if target.get("wan_iface_ip") and target["wan_iface_ip"] != target["wan_ip"]:
            print(f"WAN iface IP    : {target['wan_iface_ip']}")
        if args.tcp_mss:
            print(f"TCP MSS         : {args.tcp_mss} (TCP request-response and TCP iperf3)")
        print(f"Latency protocol: {args.latency_protocol.upper()} port={args.latency_port}")
        print(f"Latency duration: {args.latency_duration_sec:g}s")
        print(f"Latency interval: {args.latency_interval_sec:g}s")
        print(f"Repeat          : {args.repeat}")
        print(f"Load            : {args.load_protocol} {args.load_direction} percentages={','.join(percent_label(p) for p in args.load_percentages)} rate_override={args.load_rate} parallel={args.iperf3_parallel}")
        print(f"Output          : {out_dir}")
        print("============================================================")

        neighbor_refresher = None
        try:
            if args.setup_namespaces:
                setup_namespaces(args, target, ns_log)
            verify_namespaces(args)
            wait_test_interfaces_ready(args)
            if args.post_namespace_settle_sec > 0:
                time.sleep(args.post_namespace_settle_sec)
            wait_before_test_start(args.test_start_delay_sec, "L4 latency benchmark")
            neighbor_refresher = make_namespace_neighbor_refresher(args, target, details_dir)
            neighbor_refresher.refresh_now("startup", required=True)
            neighbor_refresher.start()

            idle_requested, loaded_percentages = split_load_percentages(args.load_percentages)
            for repeat_index in range(1, args.repeat + 1):
                repeat_out_dir = (
                    details_dir
                    if args.repeat == 1
                    else details_dir / f"repeat_{repeat_index:03d}"
                )
                repeat_out_dir.mkdir(parents=True, exist_ok=True)
                if args.repeat > 1:
                    print(
                        f"=== latency repeat {repeat_index}/{args.repeat}: "
                        f"output={repeat_out_dir} ===",
                        flush=True,
                    )

                idle_row = None
                if idle_requested and not args.skip_idle:
                    print("--- idle TCP/UDP request-response latency start: load_percent=0 ---", flush=True)
                    neighbor_refresher.refresh_now(
                        f"repeat_{repeat_index:03d}_before_idle",
                        required=True,
                    )
                    idle_row = run_l4rr_phase(
                        "idle",
                        args,
                        target,
                        repeat_out_dir,
                        tool_load="none",
                        load_percent=0,
                        computed_load_bps=0,
                        neighbor_refresher=neighbor_refresher,
                    )
                    idle_row["repeat"] = repeat_index
                    rows.append(idle_row)
                    print(
                        f"idle latency: avg={idle_row.get('rtt_avg_ms')}ms "
                        f"p95={idle_row.get('rtt_p95_ms')}ms loss={idle_row.get('request_loss_pct')}%",
                        flush=True,
                    )
                elif not idle_requested:
                    print("--- idle phase skipped: 0 is not present in --load-percentages ---", flush=True)
                else:
                    print("--- idle phase skipped by --skip-idle ---", flush=True)

                loaded_rows = []
                if not args.skip_loaded:
                    for load_percent in loaded_percentages:
                        phase = phase_for_load_percent(load_percent)
                        load_rate, computed_load_bps = effective_load_rate(args, target, load_percent)
                        print(f"--- {phase} TCP/UDP request-response latency start: iperf3_rate={load_rate} ---", flush=True)
                        neighbor_refresher.refresh_now(
                            f"repeat_{repeat_index:03d}_before_{phase}",
                            required=True,
                        )
                        loaded_row = run_loaded_phase(
                            phase,
                            args,
                            target,
                            repeat_out_dir,
                            load_rate=load_rate,
                            load_percent=load_percent,
                            computed_load_bps=computed_load_bps,
                            neighbor_refresher=neighbor_refresher,
                        )
                        loaded_row["repeat"] = repeat_index
                        rows.append(loaded_row)
                        loaded_rows.append(loaded_row)
                        print(
                            f"{phase} latency: avg={loaded_row.get('rtt_avg_ms')}ms "
                            f"p95={loaded_row.get('rtt_p95_ms')}ms loss={loaded_row.get('request_loss_pct')}% "
                            f"iperf3_bps={loaded_row.get('iperf3_bits_per_second')}",
                            flush=True,
                        )
                elif loaded_percentages:
                    print("--- loaded phases skipped by --skip-loaded ---", flush=True)

                if idle_row:
                    for loaded_row in loaded_rows:
                        delta_row = make_delta_row(idle_row, loaded_row, args, target)
                        delta_row["repeat"] = repeat_index
                        rows.append(delta_row)
        finally:
            if neighbor_refresher is not None:
                neighbor_refresher.stop()
            cleanup_namespaces(args, ns_log)
            if getattr(args, "restore_dpdk_after", True):
                if prepare_trex_interfaces_from_profile is None:
                    ns_log.writerow([ts_iso(), "restore_dpdk_after_unavailable", "prepare_trex_interfaces_from_profile import failed"])
                else:
                    try:
                        prep_log = prepare_trex_interfaces_from_profile(
                            target,
                            details_dir,
                            auto_dpdk_bind=True,
                            dpdk_driver_override=getattr(args, "dpdk_driver", ""),
                        )
                        ns_log.writerow([ts_iso(), "restore_dpdk_after_done", str(prep_log)])
                    except Exception as exc:
                        ns_log.writerow([ts_iso(), "restore_dpdk_after_warning", str(exc)])
                        print(f"WARNING: failed to restore DPDK driver state after L4 latency: {exc}", flush=True)

    summary_csv = out_dir / "summary_all.csv"
    write_summary_csv(summary_csv, rows)

    print("============================================================")
    print("DONE")
    print("============================================================")
    print(f"Result directory : {out_dir}")
    print(f"Summary CSV      : {summary_csv}")
    print(f"Details          : {details_dir}")
    if args.repeat == 1:
        idle_requested, loaded_percentages = split_load_percentages(args.load_percentages)
        if idle_requested and not args.skip_idle:
            print(f"Idle samples CSV : {details_dir / 'latency_idle_samples.csv'}")
        for load_percent in loaded_percentages:
            phase = phase_for_load_percent(load_percent)
            print(f"{phase} samples CSV: {details_dir / f'latency_{phase}_samples.csv'}")
            print(f"{phase} iperf3 JSON : {details_dir / f'iperf3_client_{phase}.json'}")
    else:
        print(f"Per-repeat data  : {details_dir / 'repeat_NNN'}")
    print(f"Metadata         : {details_dir / 'metadata.json'}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--_l4rr-server":
        run_internal_l4rr_server(sys.argv[2:])
    elif len(sys.argv) >= 2 and sys.argv[1] == "--_l4rr-client":
        raise SystemExit(run_internal_l4rr_client(sys.argv[2:]))
    else:
        main()
