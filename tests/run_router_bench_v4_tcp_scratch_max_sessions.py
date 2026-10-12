#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPv4 router TCP maximum session benchmark without TRex.

Topology:
  - LAN namespace: TCP clients bind source ports 65535 down to 1024.
  - WAN namespace: one built-in TCP server listens on the WAN test-host IP.
  - Each established TCP session sends a framed JSON keepalive once per second.
  - The server replies with the same port/sequence metadata and direction=DOWN.

This test intentionally avoids TRex/ASTF so TCP connection lifetime, payload
exchange, and counters are visible in this script.
"""

import argparse
import csv
import datetime as dt
import errno
import heapq
import json
import math
import os
import resource
import selectors
import signal
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from ipaddress import IPv4Address, ip_address, ip_network
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from tests.lib.config import TARGETS  # type: ignore
except Exception:
    from lib.config import TARGETS  # type: ignore

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
        add_consecutive_preflight_arguments,
        run_consecutive_preflight,
        validate_consecutive_preflight_arguments,
    )
    from tests.lib.repeat import (
        DEFAULT_REPEAT,
        argparse_repeat,
        run_repeated_subprocesses,
    )  # type: ignore
    from tests.lib.source_ports import add_lan_source_port_range_argument  # type: ignore
    from tests.lib.network import (  # type: ignore
        DEFAULT_INTERFACE_READY_TIMEOUT_SEC,
        DEFAULT_LAN_NS,
        DEFAULT_LAN_PREFIX,
        DEFAULT_POST_NAMESPACE_SETTLE_SEC,
        DEFAULT_WAN_NS,
        DEFAULT_WAN_PREFIX,
        cleanup_namespaces,
        delete_existing_namespaces,
        make_namespace_arp_refresher,
        merge_target_with_overrides,
        ns_cmd,
        prepare_kernel_interfaces_from_profile,
        prepare_trex_interfaces_from_profile,
        require_command,
        require_profile_interfaces,
        require_root,
        resolve_profile_interfaces,
        run_cmd,
        setup_namespaces,
        verify_namespaces,
        wait_test_interfaces_ready,
    )
except Exception as exc:  # pragma: no cover
    try:
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
            add_consecutive_preflight_arguments,
            run_consecutive_preflight,
            validate_consecutive_preflight_arguments,
        )
        from lib.repeat import (
            DEFAULT_REPEAT,
            argparse_repeat,
            run_repeated_subprocesses,
        )  # type: ignore
        from lib.source_ports import add_lan_source_port_range_argument  # type: ignore
        from lib.network import (  # type: ignore
            DEFAULT_INTERFACE_READY_TIMEOUT_SEC,
            DEFAULT_LAN_NS,
            DEFAULT_LAN_PREFIX,
            DEFAULT_POST_NAMESPACE_SETTLE_SEC,
            DEFAULT_WAN_NS,
            DEFAULT_WAN_PREFIX,
            cleanup_namespaces,
            delete_existing_namespaces,
            make_namespace_arp_refresher,
            merge_target_with_overrides,
            ns_cmd,
            prepare_kernel_interfaces_from_profile,
            prepare_trex_interfaces_from_profile,
            require_command,
            require_profile_interfaces,
            require_root,
            resolve_profile_interfaces,
            run_cmd,
            setup_namespaces,
            verify_namespaces,
            wait_test_interfaces_ready,
        )
    except Exception:
        raise SystemExit(f"ERROR: failed to import shared namespace helpers: {exc}")

FRAME_HEADER = struct.Struct("!I")
_TCP_DEFAULTS = _load_defaults("tcp_scratch")
_NETWORK_DEFAULTS = _load_defaults("network")
MAX_FRAME_BYTES = int(_TCP_DEFAULTS["max_frame_bytes"])
DEFAULT_SOURCE_START_PORT = int(_TCP_DEFAULTS["source_start_port"])
DEFAULT_SOURCE_END_PORT = int(_TCP_DEFAULTS["source_end_port"])
DEFAULT_SESSION_CPS = float(_TCP_DEFAULTS["session_cps"])
DEFAULT_POST_CREATE_HOLD_SEC = float(_TCP_DEFAULTS["post_create_hold_sec"])
DEFAULT_KEEPALIVE_INTERVAL_SEC = float(_TCP_DEFAULTS["keepalive_interval_sec"])
SERVER_TIMESERIES_FILENAME = "tcp_scratch_server_timeseries.csv"
DEFAULT_KEEPALIVE_TIMEOUT_SEC = float(_TCP_DEFAULTS["keepalive_timeout_sec"])
DEFAULT_SERVER_PORT = int(_TCP_DEFAULTS["server_port"])
DEFAULT_CONNECT_TIMEOUT_SEC = float(_TCP_DEFAULTS["connect_timeout_sec"])
DEFAULT_SOCKET_IO_TIMEOUT_SEC = float(_TCP_DEFAULTS["socket_io_timeout_sec"])
DEFAULT_STATS_INTERVAL_SEC = float(_TCP_DEFAULTS["stats_interval_sec"])
DEFAULT_PROGRESS_INTERVAL_SEC = float(_TCP_DEFAULTS["progress_interval_sec"])
DEFAULT_LISTEN_BACKLOG = int(_TCP_DEFAULTS["listen_backlog"])
DEFAULT_SELECT_TIMEOUT_SEC = float(_TCP_DEFAULTS["select_timeout_sec"])
DEFAULT_MAX_CREATE_BATCH = int(_TCP_DEFAULTS["max_create_batch"])
DEFAULT_RLIMIT_NOFILE_MARGIN = int(_TCP_DEFAULTS["rlimit_nofile_margin"])

CLIENT_TIMESERIES_FIELDS = [
    "ts_local",
    "elapsed_sec",
    "phase",
    "source_start_port",
    "source_end_port",
    "next_source_port",
    "server_ip",
    "server_ip_count",
    "current_server_ip",
    "server_port",
    "session_cps",
    "post_create_hold_sec",
    "keepalive_interval_sec",
    "keepalive_timeout_sec",
    "planned_sessions",
    "create_attempts_total",
    "session_create_success_total",
    "session_create_fail_total",
    "session_mid_disconnect_total",
    "current_connecting_sessions",
    "current_active_sessions",
    "active_sessions_delta",
    "create_attempts_delta",
    "create_success_delta",
    "create_fail_delta",
    "mid_disconnect_delta",
    "payload_up_tx_total",
    "payload_down_rx_total",
    "payload_loss_total",
    "payload_message_loss_rate",
    "payload_up_tx_delta",
    "payload_down_rx_delta",
    "payload_loss_delta",
    "payload_message_loss_rate_delta",
    "payload_up_bytes_queued_total",
    "payload_up_bytes_sent_total",
    "payload_down_bytes_rx_total",
    "keepalive_timeout_total",
    "keepalive_timeout_delta",
    "send_error_total",
    "recv_error_total",
    "bind_error_total",
    "connect_error_total",
    "connect_timeout_total",
    "remote_close_total",
    "local_close_total",
]

SESSION_FIELDS = [
    "source_port",
    "server_ip",
    "state",
    "connect_start_elapsed_sec",
    "connect_success_elapsed_sec",
    "close_elapsed_sec",
    "fail_reason",
    "payload_up_tx",
    "payload_down_rx",
    "payload_loss",
    "payload_message_loss_rate",
    "last_seq_tx",
    "last_seq_rx",
    "payload_up_bytes_queued",
    "payload_up_bytes_sent",
    "payload_down_bytes_rx",
    "keepalive_timeout_count",
    "pending_keepalive_seq",
]

SERVER_TIMESERIES_FIELDS = [
    "ts_local",
    "elapsed_sec",
    "bind_ip",
    "server_port",
    "target_session_attempts_total",
    "target_session_attempts_delta",
    "target_session_create_fail_total",
    "target_session_create_fail_delta",
    "accept_total",
    "accept_delta",
    "current_active_sessions",
    "active_sessions_delta",
    "closed_total",
    "closed_delta",
    "payload_up_rx_total",
    "payload_up_rx_delta",
    "payload_down_tx_total",
    "payload_down_tx_delta",
    "payload_up_bytes_rx_total",
    "payload_down_bytes_queued_total",
    "payload_down_bytes_sent_total",
    "parse_error_total",
    "send_error_total",
    "recv_error_total",
]


def build_default_out_dir(target_name, prefix="trex_tcp_scratch_max_sessions"):
    return DEFAULT_RESULT_ROOT / f"{prefix}_{target_name}_{ts_compact()}"


def parse_port(value):
    port = parse_positive_int(value)
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError("port must be in 1..65535")
    return port


def parse_non_negative_int(value):
    try:
        i = int(str(value).replace("_", ""))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if i < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return i


def build_server_ip_list(start_ip, count, prefix_len, reserved_ips=()):
    """Return descending usable IPv4 host addresses beginning at start_ip."""
    count = int(count)
    if count <= 0:
        raise ValueError("server IP count must be positive")
    start = ip_address(start_ip)
    if not isinstance(start, IPv4Address):
        raise ValueError("server IP must be IPv4")
    network = ip_network(f"{start}/{int(prefix_len)}", strict=False)
    reserved = {ip_address(value) for value in reserved_ips if value}
    addresses = []
    candidate_value = int(start)
    first_host = int(network.network_address) + 1
    last_host = int(network.broadcast_address) - 1
    while candidate_value >= first_host and len(addresses) < count:
        candidate = IPv4Address(candidate_value)
        if int(candidate) <= last_host and candidate not in reserved:
            addresses.append(str(candidate))
        candidate_value -= 1
    if len(addresses) != count:
        raise ValueError(
            f"cannot allocate {count} descending server IPs from {start} "
            f"inside {network}; available={len(addresses)}"
        )
    return addresses


def parse_server_ips(value):
    addresses = [item.strip() for item in str(value or "").split(",") if item.strip()]
    if not addresses:
        raise ValueError("server IP list is empty")
    for value in addresses:
        address = ip_address(value)
        if not isinstance(address, IPv4Address):
            raise ValueError(f"server IP must be IPv4: {value}")
    return addresses


def planned_session_count(source_start_port, source_end_port, server_ip_count, max_sessions=0):
    unique_ports = int(source_start_port) - int(source_end_port) + 1
    if unique_ports <= 0:
        raise ValueError("source_start_port must be greater than or equal to source_end_port")
    capacity = unique_ports * int(server_ip_count)
    if int(max_sessions) > 0:
        return min(capacity, int(max_sessions))
    return capacity


def session_endpoint_at(index, source_start_port, source_end_port, server_ips):
    unique_ports = int(source_start_port) - int(source_end_port) + 1
    if unique_ports <= 0:
        raise ValueError("invalid source port range")
    server_index, source_index = divmod(int(index), unique_ports)
    if server_index >= len(server_ips):
        raise IndexError(index)
    return int(source_start_port) - source_index, server_ips[server_index]


def configure_or_verify_server_ips(args, target, server_ips, log_writer):
    """Add server aliases for managed namespaces, or verify caller-managed ones."""
    proc = run_cmd(
        ["ip", "-n", args.wan_ns, "-j", "-4", "addr", "show", "dev", args.wan_iface],
        check=False,
    )
    configured = set()
    if proc.returncode == 0:
        try:
            for interface in json.loads(proc.stdout or "[]"):
                for info in interface.get("addr_info") or []:
                    if info.get("family") == "inet" and info.get("local"):
                        configured.add(str(info["local"]))
        except Exception:
            configured = set()

    missing = [value for value in server_ips if value not in configured]
    if not missing:
        log_writer.writerow([ts_iso(), "wan_server_addresses_ready", f"count={len(server_ips)}"])
        return
    if not args.setup_namespaces:
        raise SystemExit(
            "ERROR: WAN namespace is missing server IPv4 addresses required by "
            f"--server-ip-count: {', '.join(missing[:8])}"
            + (" ..." if len(missing) > 8 else "")
        )
    for value in missing:
        run_cmd(
            [
                "ip",
                "-n",
                args.wan_ns,
                "addr",
                "add",
                f"{value}/{args.wan_prefix}",
                "dev",
                args.wan_iface,
            ]
        )
        log_writer.writerow([ts_iso(), "wan_server_address_add", f"{value}/{args.wan_prefix}"])


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)


def raise_nofile_limit(min_soft):
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except Exception:
        return {"requested": min_soft, "soft_before": "", "hard_before": "", "soft_after": "", "hard_after": ""}
    requested = int(min_soft)
    new_soft = min(max(soft, requested), hard if hard > 0 else requested)
    try:
        if new_soft > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
            soft_after, hard_after = resource.getrlimit(resource.RLIMIT_NOFILE)
        else:
            soft_after, hard_after = soft, hard
    except Exception:
        soft_after, hard_after = soft, hard
    return {
        "requested": requested,
        "soft_before": soft,
        "hard_before": hard,
        "soft_after": soft_after,
        "hard_after": hard_after,
    }


def encode_frame(payload):
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise ValueError(f"payload frame too large: {len(body)} bytes")
    return FRAME_HEADER.pack(len(body)) + body, len(body)


def decode_frames(buffer):
    frames = []
    offset = 0
    total = len(buffer)
    while total - offset >= FRAME_HEADER.size:
        length = FRAME_HEADER.unpack(buffer[offset:offset + FRAME_HEADER.size])[0]
        if length <= 0 or length > MAX_FRAME_BYTES:
            raise ValueError(f"invalid frame length: {length}")
        end = offset + FRAME_HEADER.size + length
        if total < end:
            break
        body = bytes(buffer[offset + FRAME_HEADER.size:end])
        frames.append(json.loads(body.decode("utf-8")))
        offset = end
    if offset:
        del buffer[:offset]
    return frames


@dataclass
class ClientConn:
    source_port: int
    server_ip: str
    sock: socket.socket
    state: str
    connect_start_mono: float
    connect_start_elapsed: float
    connect_success_elapsed: Optional[float] = None
    close_elapsed: Optional[float] = None
    fail_reason: str = ""
    inbuf: bytearray = field(default_factory=bytearray)
    outbuf: bytearray = field(default_factory=bytearray)
    payload_up_tx: int = 0
    payload_down_rx: int = 0
    last_seq_tx: int = 0
    last_seq_rx: int = 0
    payload_up_bytes_queued: int = 0
    payload_up_bytes_sent: int = 0
    payload_down_bytes_rx: int = 0
    next_keepalive_mono: float = 0.0
    last_rx_mono: float = 0.0
    pending_keepalive_seq: int = 0
    pending_keepalive_since_mono: float = 0.0
    keepalive_timeout_count: int = 0


class ClientStats:
    def __init__(self):
        self.create_attempts_total = 0
        self.session_create_success_total = 0
        self.session_create_fail_total = 0
        self.session_mid_disconnect_total = 0
        self.payload_up_tx_total = 0
        self.payload_down_rx_total = 0
        self.payload_up_bytes_queued_total = 0
        self.payload_up_bytes_sent_total = 0
        self.payload_down_bytes_rx_total = 0
        self.keepalive_timeout_total = 0
        self.send_error_total = 0
        self.recv_error_total = 0
        self.bind_error_total = 0
        self.connect_error_total = 0
        self.connect_timeout_total = 0
        self.remote_close_total = 0
        self.local_close_total = 0

    def snapshot(self, active_count, connecting_count):
        payload_loss_total = max(0, self.payload_up_tx_total - self.payload_down_rx_total)
        payload_loss_rate = (payload_loss_total / self.payload_up_tx_total) if self.payload_up_tx_total else 0.0
        return {
            "create_attempts_total": self.create_attempts_total,
            "session_create_success_total": self.session_create_success_total,
            "session_create_fail_total": self.session_create_fail_total,
            "session_mid_disconnect_total": self.session_mid_disconnect_total,
            "current_active_sessions": active_count,
            "current_connecting_sessions": connecting_count,
            "payload_up_tx_total": self.payload_up_tx_total,
            "payload_down_rx_total": self.payload_down_rx_total,
            "payload_loss_total": payload_loss_total,
            "payload_message_loss_rate": payload_loss_rate,
            "payload_up_bytes_queued_total": self.payload_up_bytes_queued_total,
            "payload_up_bytes_sent_total": self.payload_up_bytes_sent_total,
            "payload_down_bytes_rx_total": self.payload_down_bytes_rx_total,
            "keepalive_timeout_total": self.keepalive_timeout_total,
            "send_error_total": self.send_error_total,
            "recv_error_total": self.recv_error_total,
            "bind_error_total": self.bind_error_total,
            "connect_error_total": self.connect_error_total,
            "connect_timeout_total": self.connect_timeout_total,
            "remote_close_total": self.remote_close_total,
            "local_close_total": self.local_close_total,
        }


@dataclass
class ServerConn:
    sock: socket.socket
    peer: Tuple[str, int]
    server_ip: str
    inbuf: bytearray = field(default_factory=bytearray)
    outbuf: bytearray = field(default_factory=bytearray)
    accepted_mono: float = 0.0
    payload_up_rx: int = 0
    payload_down_tx: int = 0
    payload_up_bytes_rx: int = 0
    payload_down_bytes_queued: int = 0
    payload_down_bytes_sent: int = 0


class ServerStats:
    def __init__(self):
        self.accept_total = 0
        self.closed_total = 0
        self.payload_up_rx_total = 0
        self.payload_down_tx_total = 0
        self.payload_up_bytes_rx_total = 0
        self.payload_down_bytes_queued_total = 0
        self.payload_down_bytes_sent_total = 0
        self.parse_error_total = 0
        self.send_error_total = 0
        self.recv_error_total = 0

    def snapshot(self, active_count):
        return {
            "accept_total": self.accept_total,
            "closed_total": self.closed_total,
            "current_active_sessions": active_count,
            "payload_up_rx_total": self.payload_up_rx_total,
            "payload_down_tx_total": self.payload_down_tx_total,
            "payload_up_bytes_rx_total": self.payload_up_bytes_rx_total,
            "payload_down_bytes_queued_total": self.payload_down_bytes_queued_total,
            "payload_down_bytes_sent_total": self.payload_down_bytes_sent_total,
            "parse_error_total": self.parse_error_total,
            "send_error_total": self.send_error_total,
            "recv_error_total": self.recv_error_total,
        }


def safe_unregister(sel, sock):
    try:
        sel.unregister(sock)
    except Exception:
        pass


def safe_close(sock):
    try:
        sock.close()
    except Exception:
        pass


def selector_set(sel, sock, mask, data):
    try:
        sel.modify(sock, mask, data)
    except KeyError:
        sel.register(sock, mask, data)


def monotonic_elapsed(start_mono):
    return time.monotonic() - start_mono


def delta_value(now_snap, prev_snap, key):
    if prev_snap is None:
        return ""
    return now_snap.get(key, 0) - prev_snap.get(key, 0)


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def write_json_atomic(path, data):
    path = Path(path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp_path, path)


def read_json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_last_csv_row(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        with path.open("r", newline="", encoding="utf-8") as f:
            last = None
            for row in csv.DictReader(f):
                last = row
            return last
    except Exception:
        return None


def format_client_progress(row):
    if not row:
        return "TCP scratch max-session progress: waiting for first client statistics row"
    return (
        "TCP scratch max-session "
        f"elapsed={float(row.get('elapsed_sec') or 0.0):.1f}s "
        f"phase={row.get('phase', '')} "
        f"attempt={row.get('create_attempts_total', '')} "
        f"success={row.get('session_create_success_total', '')} "
        f"fail={row.get('session_create_fail_total', '')} "
        f"mid_disc={row.get('session_mid_disconnect_total', '')} "
        f"active={row.get('current_active_sessions', '')} "
        f"payload_loss={float(row.get('payload_message_loss_rate') or 0.0):.6f}"
    )


def resolve_server_timeseries_path(args, out_dir):
    configured_path = getattr(args, "server_timeseries_path", "")
    path = (
        Path(configured_path)
        if configured_path
        else Path(out_dir) / SERVER_TIMESERIES_FILENAME
    )
    ensure_dir(path.parent)
    return path


def run_server(args):
    out_dir = Path(args.out_dir)
    ensure_dir(out_dir)
    timeseries_path = resolve_server_timeseries_path(args, out_dir)
    summary_path = out_dir / "tcp_scratch_server_summary.json"
    ready_path = Path(args.ready_file) if args.ready_file else None
    client_status_path = Path(args.client_status_file) if args.client_status_file else None

    raise_limit = raise_nofile_limit(args.expected_sessions + DEFAULT_RLIMIT_NOFILE_MARGIN)
    stop_requested = {"value": False}

    def _handle_stop(signum, frame):  # noqa: ARG001
        stop_requested["value"] = True

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    sel = selectors.DefaultSelector()
    stats = ServerStats()
    conns: Dict[int, ServerConn] = {}
    start_mono = time.monotonic()
    previous_snapshot = None
    previous_target_attempts = None
    previous_target_create_fail = None

    listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listen_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    listen_sock.bind((args.bind_ip, args.server_port))
    listen_sock.listen(args.listen_backlog)
    listen_sock.setblocking(False)
    sel.register(listen_sock, selectors.EVENT_READ, "listener")

    if ready_path:
        write_json(ready_path, {
            "ready": True,
            "ts_local": ts_iso(),
            "bind_ip": args.bind_ip,
            "server_port": args.server_port,
            "listen_backlog": args.listen_backlog,
            "rlimit_nofile": raise_limit,
        })

    with timeseries_path.open("w", newline="", encoding="utf-8") as f_ts:
        writer = csv.DictWriter(f_ts, fieldnames=SERVER_TIMESERIES_FIELDS)
        writer.writeheader()
        next_stats = time.monotonic()
        try:
            while not stop_requested["value"]:
                events = sel.select(timeout=args.select_timeout_sec)
                for key, mask in events:
                    if key.data == "listener":
                        while True:
                            try:
                                conn_sock, peer = listen_sock.accept()
                            except BlockingIOError:
                                break
                            except OSError:
                                break
                            conn_sock.setblocking(False)
                            try:
                                conn_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                            except OSError:
                                pass
                            conn = ServerConn(
                                sock=conn_sock,
                                peer=peer,
                                server_ip=conn_sock.getsockname()[0],
                                accepted_mono=time.monotonic(),
                            )
                            conns[conn_sock.fileno()] = conn
                            stats.accept_total += 1
                            selector_set(sel, conn_sock, selectors.EVENT_READ, conn)
                    else:
                        conn = key.data
                        if mask & selectors.EVENT_READ:
                            try:
                                data = conn.sock.recv(65536)
                            except BlockingIOError:
                                data = None
                            except OSError:
                                data = b""
                                stats.recv_error_total += 1
                            if data == b"":
                                safe_unregister(sel, conn.sock)
                                conns.pop(conn.sock.fileno(), None)
                                safe_close(conn.sock)
                                stats.closed_total += 1
                                continue
                            if data:
                                conn.inbuf.extend(data)
                                try:
                                    frames = decode_frames(conn.inbuf)
                                except Exception:
                                    stats.parse_error_total += 1
                                    safe_unregister(sel, conn.sock)
                                    conns.pop(conn.sock.fileno(), None)
                                    safe_close(conn.sock)
                                    stats.closed_total += 1
                                    continue
                                for frame in frames:
                                    conn.payload_up_rx += 1
                                    stats.payload_up_rx_total += 1
                                    payload_len = len(json.dumps(frame, separators=(",", ":"), sort_keys=True).encode("utf-8"))
                                    conn.payload_up_bytes_rx += payload_len
                                    stats.payload_up_bytes_rx_total += payload_len
                                    reply = {
                                        "direction": "DOWN",
                                        "source_port": frame.get("source_port"),
                                        "seq": frame.get("seq"),
                                        "client_elapsed_sec": frame.get("elapsed_sec"),
                                        "server_elapsed_sec": monotonic_elapsed(start_mono),
                                        "server_ip": conn.server_ip,
                                        "server_port": args.server_port,
                                    }
                                    try:
                                        encoded, body_len = encode_frame(reply)
                                    except Exception:
                                        stats.parse_error_total += 1
                                        continue
                                    conn.outbuf.extend(encoded)
                                    conn.payload_down_tx += 1
                                    conn.payload_down_bytes_queued += body_len
                                    stats.payload_down_tx_total += 1
                                    stats.payload_down_bytes_queued_total += body_len
                                    selector_set(sel, conn.sock, selectors.EVENT_READ | selectors.EVENT_WRITE, conn)
                        if mask & selectors.EVENT_WRITE and conn.outbuf:
                            try:
                                sent = conn.sock.send(conn.outbuf)
                            except BlockingIOError:
                                sent = 0
                            except OSError:
                                sent = -1
                                stats.send_error_total += 1
                            if sent < 0:
                                safe_unregister(sel, conn.sock)
                                conns.pop(conn.sock.fileno(), None)
                                safe_close(conn.sock)
                                stats.closed_total += 1
                                continue
                            if sent:
                                del conn.outbuf[:sent]
                                conn.payload_down_bytes_sent += sent
                                stats.payload_down_bytes_sent_total += sent
                            if not conn.outbuf:
                                selector_set(sel, conn.sock, selectors.EVENT_READ, conn)

                now = time.monotonic()
                if now >= next_stats:
                    snap = stats.snapshot(active_count=len(conns))
                    status = read_json_file(client_status_path) if client_status_path else {}
                    target_attempts_raw = status.get("create_attempts_total", "")
                    target_create_fail_raw = status.get("session_create_fail_total", "")
                    try:
                        target_attempts = int(target_attempts_raw)
                    except Exception:
                        target_attempts = None
                    try:
                        target_create_fail = int(target_create_fail_raw)
                    except Exception:
                        target_create_fail = None
                    if target_attempts is None:
                        target_attempts_total = ""
                        target_attempts_delta = ""
                    else:
                        target_attempts_total = target_attempts
                        target_attempts_delta = "" if previous_target_attempts is None else target_attempts - previous_target_attempts
                    if target_create_fail is None:
                        target_create_fail_total = ""
                        target_create_fail_delta = ""
                    else:
                        target_create_fail_total = target_create_fail
                        target_create_fail_delta = "" if previous_target_create_fail is None else target_create_fail - previous_target_create_fail
                    row = {
                        "ts_local": ts_iso(),
                        "elapsed_sec": monotonic_elapsed(start_mono),
                        "bind_ip": args.bind_ip,
                        "server_port": args.server_port,
                        "target_session_attempts_total": target_attempts_total,
                        "target_session_attempts_delta": target_attempts_delta,
                        "target_session_create_fail_total": target_create_fail_total,
                        "target_session_create_fail_delta": target_create_fail_delta,
                        **snap,
                        "accept_delta": delta_value(snap, previous_snapshot, "accept_total"),
                        "active_sessions_delta": delta_value(snap, previous_snapshot, "current_active_sessions"),
                        "closed_delta": delta_value(snap, previous_snapshot, "closed_total"),
                        "payload_up_rx_delta": delta_value(snap, previous_snapshot, "payload_up_rx_total"),
                        "payload_down_tx_delta": delta_value(snap, previous_snapshot, "payload_down_tx_total"),
                    }
                    writer.writerow(row)
                    f_ts.flush()
                    previous_snapshot = snap
                    if target_attempts is not None:
                        previous_target_attempts = target_attempts
                    if target_create_fail is not None:
                        previous_target_create_fail = target_create_fail
                    next_stats += args.stats_interval_sec
        finally:
            for conn in list(conns.values()):
                safe_unregister(sel, conn.sock)
                safe_close(conn.sock)
            safe_unregister(sel, listen_sock)
            safe_close(listen_sock)
            sel.close()
            summary = {
                "result": "stopped" if stop_requested["value"] else "done",
                "ts_local": ts_iso(),
                "elapsed_sec": monotonic_elapsed(start_mono),
                "bind_ip": args.bind_ip,
                "server_port": args.server_port,
                "rlimit_nofile": raise_limit,
                **stats.snapshot(active_count=0),
            }
            write_json(summary_path, summary)
    return 0



def recv_exact(sock_obj, size):
    chunks = bytearray()
    while len(chunks) < int(size):
        data = sock_obj.recv(int(size) - len(chunks))
        if not data:
            raise RuntimeError("connection closed during preflight response")
        chunks.extend(data)
    return bytes(chunks)


def run_preflight_client(args):
    started = time.time()
    server_ips = parse_server_ips(args.server_ips) if args.server_ips else [args.server_ip]
    test_server_ips = [server_ips[0]]
    if len(server_ips) > 1:
        test_server_ips.append(server_ips[-1])
    sockets = []
    source_port = int(args.preflight_source_port or 0)
    try:
        for server_ip in test_server_ips:
            sock_obj = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append((server_ip, sock_obj))
            sock_obj.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock_obj.settimeout(float(args.connect_timeout_sec))
            sock_obj.bind((args.client_ip, source_port))
            if source_port == 0:
                source_port = sock_obj.getsockname()[1]
            sock_obj.connect((server_ip, int(args.server_port)))

        replies = []
        for seq, (server_ip, sock_obj) in enumerate(sockets, start=1):
            payload = {
                "direction": "UP",
                "source_port": source_port,
                "seq": seq,
                "elapsed_sec": 0.0,
                "preflight": True,
                "server_ip": server_ip,
            }
            encoded, _body_len = encode_frame(payload)
            sock_obj.sendall(encoded)
            header = recv_exact(sock_obj, FRAME_HEADER.size)
            body_len = FRAME_HEADER.unpack(header)[0]
            if body_len <= 0 or body_len > MAX_FRAME_BYTES:
                raise RuntimeError(f"invalid preflight frame length: {body_len}")
            reply = json.loads(recv_exact(sock_obj, body_len).decode("utf-8"))
            if reply.get("direction") != "DOWN" or int(reply.get("seq", -1)) != seq:
                raise RuntimeError(f"invalid preflight reply from {server_ip}: {reply}")
            if reply.get("server_ip") and reply.get("server_ip") != server_ip:
                raise RuntimeError(
                    f"preflight reply server IP mismatch: expected={server_ip} reply={reply}"
                )
            replies.append(reply)
        print(json.dumps({
            "preflight_ok": 1,
            "elapsed_sec": time.time() - started,
            "client_ip": args.client_ip,
            "source_port": source_port,
            "server_ips": test_server_ips,
            "server_port": args.server_port,
            "same_source_port_multiplexing": len(test_server_ips) > 1,
            "replies": replies,
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({
            "preflight_ok": 0,
            "elapsed_sec": time.time() - started,
            "error": str(exc),
        }, ensure_ascii=False))
        return 1
    finally:
        for _server_ip, sock_obj in sockets:
            try:
                sock_obj.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack("ii", 1, 0),
                )
            except OSError:
                pass
            safe_close(sock_obj)

def run_client(args):
    out_dir = Path(args.out_dir)
    ensure_dir(out_dir)
    timeseries_path = out_dir / "tcp_scratch_max_sessions_timeseries.csv"
    sessions_path = out_dir / "tcp_scratch_max_sessions_sessions.csv"
    summary_path = out_dir / "tcp_scratch_max_sessions_summary.json"

    server_ips = parse_server_ips(args.server_ips) if args.server_ips else [args.server_ip]
    planned_sessions = planned_session_count(
        args.source_start_port,
        args.source_end_port,
        len(server_ips),
        args.max_sessions,
    )
    raise_limit = raise_nofile_limit(planned_sessions + DEFAULT_RLIMIT_NOFILE_MARGIN)

    sel = selectors.DefaultSelector()
    stats = ClientStats()
    conns: Dict[Tuple[int, str], ClientConn] = {}
    completed_sessions: List[dict] = []
    connecting_sessions = set()
    active_sessions = set()
    keepalive_heap: List[Tuple[float, Tuple[int, str]]] = []

    start_mono = time.monotonic()
    next_session_index = 0
    next_create_mono = start_mono
    create_interval = 1.0 / float(args.session_cps)
    creation_complete_mono = None
    previous_snapshot = None
    client_status_path = Path(args.client_status_file) if args.client_status_file else None
    next_progress_print_mono = start_mono

    def elapsed():
        return monotonic_elapsed(start_mono)

    def active_count():
        return len(active_sessions)

    def connecting_count():
        return len(connecting_sessions)

    def session_record(conn: ClientConn):
        payload_loss = max(0, conn.payload_up_tx - conn.payload_down_rx)
        return {
            "source_port": conn.source_port,
            "server_ip": conn.server_ip,
            "state": conn.state,
            "connect_start_elapsed_sec": conn.connect_start_elapsed,
            "connect_success_elapsed_sec": conn.connect_success_elapsed if conn.connect_success_elapsed is not None else "",
            "close_elapsed_sec": conn.close_elapsed if conn.close_elapsed is not None else "",
            "fail_reason": conn.fail_reason,
            "payload_up_tx": conn.payload_up_tx,
            "payload_down_rx": conn.payload_down_rx,
            "payload_loss": payload_loss,
            "payload_message_loss_rate": (payload_loss / conn.payload_up_tx) if conn.payload_up_tx else 0.0,
            "last_seq_tx": conn.last_seq_tx,
            "last_seq_rx": conn.last_seq_rx,
            "payload_up_bytes_queued": conn.payload_up_bytes_queued,
            "payload_up_bytes_sent": conn.payload_up_bytes_sent,
            "payload_down_bytes_rx": conn.payload_down_bytes_rx,
            "keepalive_timeout_count": conn.keepalive_timeout_count,
            "pending_keepalive_seq": conn.pending_keepalive_seq if conn.pending_keepalive_seq and conn.last_seq_rx < conn.pending_keepalive_seq else "",
        }

    def mark_closed(conn: ClientConn, reason: str, count_mid_disconnect: bool):
        fd = conn.sock.fileno()
        session_key = (conn.source_port, conn.server_ip)
        safe_unregister(sel, conn.sock)
        conns.pop(session_key, None)
        connecting_sessions.discard(session_key)
        was_active = session_key in active_sessions
        active_sessions.discard(session_key)
        conn.close_elapsed = elapsed()
        conn.state = "closed"
        conn.fail_reason = reason
        safe_close(conn.sock)
        completed_sessions.append(session_record(conn))
        if count_mid_disconnect and was_active:
            stats.session_mid_disconnect_total += 1
        if reason == "remote_close":
            stats.remote_close_total += 1
        elif reason == "local_close":
            stats.local_close_total += 1
        return fd

    def mark_create_fail(conn: ClientConn, reason: str):
        session_key = (conn.source_port, conn.server_ip)
        safe_unregister(sel, conn.sock)
        conns.pop(session_key, None)
        connecting_sessions.discard(session_key)
        conn.close_elapsed = elapsed()
        conn.state = "failed"
        conn.fail_reason = reason
        safe_close(conn.sock)
        completed_sessions.append(session_record(conn))
        stats.session_create_fail_total += 1

    def queue_keepalive(conn: ClientConn, now: Optional[float] = None):
        if now is None:
            now = time.monotonic()
        conn.last_seq_tx += 1
        payload = {
            "direction": "UP",
            "source_port": conn.source_port,
            "seq": conn.last_seq_tx,
            "elapsed_sec": elapsed(),
            "server_ip": conn.server_ip,
            "server_port": args.server_port,
        }
        encoded, body_len = encode_frame(payload)
        conn.outbuf.extend(encoded)
        conn.payload_up_tx += 1
        conn.payload_up_bytes_queued += body_len
        stats.payload_up_tx_total += 1
        stats.payload_up_bytes_queued_total += body_len
        conn.pending_keepalive_seq = conn.last_seq_tx
        conn.pending_keepalive_since_mono = now
        selector_set(sel, conn.sock, selectors.EVENT_READ | selectors.EVENT_WRITE, conn)

    def mark_connected(conn: ClientConn):
        session_key = (conn.source_port, conn.server_ip)
        connecting_sessions.discard(session_key)
        active_sessions.add(session_key)
        conn.state = "active"
        conn.connect_success_elapsed = elapsed()
        conn.last_rx_mono = time.monotonic()
        stats.session_create_success_total += 1
        selector_set(sel, conn.sock, selectors.EVENT_READ, conn)
        conn.next_keepalive_mono = time.monotonic()
        heapq.heappush(keepalive_heap, (conn.next_keepalive_mono, session_key))

    def create_connection(source_port: int, server_ip: str):
        stats.create_attempts_total += 1
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        conn = ClientConn(
            source_port=source_port,
            server_ip=server_ip,
            sock=sock,
            state="connecting",
            connect_start_mono=time.monotonic(),
            connect_start_elapsed=elapsed(),
        )
        try:
            sock.bind((args.client_ip, source_port))
        except OSError as exc:
            stats.bind_error_total += 1
            conn.fail_reason = f"bind_error:{exc.errno}:{exc.strerror}"
            mark_create_fail(conn, conn.fail_reason)
            return
        try:
            err = sock.connect_ex((server_ip, args.server_port))
        except OSError as exc:
            stats.connect_error_total += 1
            conn.fail_reason = f"connect_error:{exc.errno}:{exc.strerror}"
            mark_create_fail(conn, conn.fail_reason)
            return
        session_key = (source_port, server_ip)
        conns[session_key] = conn
        if err == 0:
            mark_connected(conn)
            return
        if err in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
            connecting_sessions.add(session_key)
            selector_set(sel, sock, selectors.EVENT_WRITE, conn)
            return
        stats.connect_error_total += 1
        conn.fail_reason = f"connect_ex_error:{err}:{os.strerror(err)}"
        mark_create_fail(conn, conn.fail_reason)

    def handle_connect_result(conn: ClientConn):
        try:
            err = conn.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        except OSError as exc:
            stats.connect_error_total += 1
            mark_create_fail(conn, f"so_error_read_failed:{exc.errno}:{exc.strerror}")
            return
        if err == 0:
            mark_connected(conn)
        elif err in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
            return
        else:
            stats.connect_error_total += 1
            mark_create_fail(conn, f"connect_failed:{err}:{os.strerror(err)}")

    def handle_read(conn: ClientConn):
        try:
            data = conn.sock.recv(65536)
        except BlockingIOError:
            return
        except OSError as exc:
            stats.recv_error_total += 1
            mark_closed(conn, f"recv_error:{exc.errno}:{exc.strerror}", count_mid_disconnect=True)
            return
        if data == b"":
            mark_closed(conn, "remote_close", count_mid_disconnect=True)
            return
        conn.inbuf.extend(data)
        try:
            frames = decode_frames(conn.inbuf)
        except Exception as exc:
            stats.recv_error_total += 1
            mark_closed(conn, f"decode_error:{exc}", count_mid_disconnect=True)
            return
        for frame in frames:
            if frame.get("direction") == "DOWN":
                conn.payload_down_rx += 1
                stats.payload_down_rx_total += 1
                try:
                    seq = int(frame.get("seq") or 0)
                except Exception:
                    seq = 0
                conn.last_seq_rx = max(conn.last_seq_rx, seq)
                if conn.pending_keepalive_seq and seq >= conn.pending_keepalive_seq:
                    conn.pending_keepalive_seq = 0
                    conn.pending_keepalive_since_mono = 0.0
                payload_len = len(json.dumps(frame, separators=(",", ":"), sort_keys=True).encode("utf-8"))
                conn.payload_down_bytes_rx += payload_len
                stats.payload_down_bytes_rx_total += payload_len
                conn.last_rx_mono = time.monotonic()

    def handle_write(conn: ClientConn):
        if conn.state == "connecting":
            handle_connect_result(conn)
            return
        if not conn.outbuf:
            selector_set(sel, conn.sock, selectors.EVENT_READ, conn)
            return
        try:
            sent = conn.sock.send(conn.outbuf)
        except BlockingIOError:
            sent = 0
        except OSError as exc:
            stats.send_error_total += 1
            mark_closed(conn, f"send_error:{exc.errno}:{exc.strerror}", count_mid_disconnect=True)
            return
        if sent:
            del conn.outbuf[:sent]
            conn.payload_up_bytes_sent += sent
            stats.payload_up_bytes_sent_total += sent
        if not conn.outbuf:
            selector_set(sel, conn.sock, selectors.EVENT_READ, conn)

    def expire_connect_timeouts(now):
        if not connecting_sessions:
            return
        for session_key in list(connecting_sessions):
            conn = conns.get(session_key)
            if not conn:
                connecting_sessions.discard(session_key)
                continue
            if now - conn.connect_start_mono >= args.connect_timeout_sec:
                stats.connect_timeout_total += 1
                mark_create_fail(conn, "connect_timeout")

    def expire_keepalive_timeouts(now):
        if not active_sessions:
            return
        timeout_sec = float(args.keepalive_timeout_sec)
        for session_key in list(active_sessions):
            conn = conns.get(session_key)
            if not conn or conn.state != "active":
                active_sessions.discard(session_key)
                continue
            if not conn.pending_keepalive_seq:
                continue
            if conn.last_seq_rx >= conn.pending_keepalive_seq:
                conn.pending_keepalive_seq = 0
                conn.pending_keepalive_since_mono = 0.0
                continue
            age = now - conn.pending_keepalive_since_mono
            if age >= timeout_sec:
                conn.keepalive_timeout_count += 1
                stats.keepalive_timeout_total += 1
                mark_closed(
                    conn,
                    f"keepalive_timeout:seq={conn.pending_keepalive_seq}:age_sec={age:.6f}",
                    count_mid_disconnect=True,
                )

    def send_due_keepalives(now):
        while keepalive_heap and keepalive_heap[0][0] <= now:
            due, session_key = heapq.heappop(keepalive_heap)
            conn = conns.get(session_key)
            if not conn or conn.state != "active":
                continue
            if due + 0.001 < conn.next_keepalive_mono:
                continue
            if conn.pending_keepalive_seq and conn.last_seq_rx < conn.pending_keepalive_seq:
                next_check_mono = conn.pending_keepalive_since_mono + args.keepalive_timeout_sec
                heapq.heappush(
                    keepalive_heap,
                    (max(now + 0.001, next_check_mono), session_key),
                )
                continue
            queue_keepalive(conn, now=now)
            conn.next_keepalive_mono = now + args.keepalive_interval_sec
            heapq.heappush(keepalive_heap, (conn.next_keepalive_mono, session_key))

    def phase_name():
        if creation_complete_mono is None:
            return "ramp"
        if time.monotonic() < creation_complete_mono + args.post_create_hold_sec:
            return "hold"
        return "done"

    with timeseries_path.open("w", newline="", encoding="utf-8") as f_ts:
        writer = csv.DictWriter(f_ts, fieldnames=CLIENT_TIMESERIES_FIELDS)
        writer.writeheader()
        next_stats_mono = start_mono
        try:
            while True:
                now = time.monotonic()

                created_this_loop = 0
                while (
                    next_session_index < planned_sessions
                    and now >= next_create_mono
                    and created_this_loop < args.max_create_batch
                ):
                    source_port, server_ip = session_endpoint_at(
                        next_session_index,
                        args.source_start_port,
                        args.source_end_port,
                        server_ips,
                    )
                    create_connection(source_port, server_ip)
                    next_session_index += 1
                    created_this_loop += 1
                    next_create_mono += create_interval

                if next_session_index >= planned_sessions and creation_complete_mono is None:
                    creation_complete_mono = time.monotonic()

                events = sel.select(timeout=args.select_timeout_sec)
                for key, mask in events:
                    conn = key.data
                    if not isinstance(conn, ClientConn):
                        continue
                    if conn.state == "connecting" and (mask & selectors.EVENT_WRITE):
                        handle_connect_result(conn)
                        continue
                    if mask & selectors.EVENT_READ:
                        handle_read(conn)
                    if (conn.source_port, conn.server_ip) in conns and (mask & selectors.EVENT_WRITE):
                        handle_write(conn)

                now = time.monotonic()
                expire_connect_timeouts(now)
                expire_keepalive_timeouts(now)
                send_due_keepalives(now)

                if now >= next_stats_mono:
                    if next_session_index < planned_sessions:
                        next_source_port, current_server_ip = session_endpoint_at(
                            next_session_index,
                            args.source_start_port,
                            args.source_end_port,
                            server_ips,
                        )
                    else:
                        next_source_port, current_server_ip = "", ""
                    snap = stats.snapshot(active_count=active_count(), connecting_count=connecting_count())
                    if previous_snapshot is None:
                        payload_loss_delta = ""
                        payload_loss_rate_delta = ""
                    else:
                        payload_loss_delta_value = snap["payload_loss_total"] - previous_snapshot["payload_loss_total"]
                        payload_up_delta_value = snap["payload_up_tx_total"] - previous_snapshot["payload_up_tx_total"]
                        payload_loss_delta = payload_loss_delta_value
                        payload_loss_rate_delta = (payload_loss_delta_value / payload_up_delta_value) if payload_up_delta_value else 0.0
                    row = {
                        "ts_local": ts_iso(),
                        "elapsed_sec": elapsed(),
                        "phase": phase_name(),
                        "source_start_port": args.source_start_port,
                        "source_end_port": args.source_end_port,
                        "next_source_port": next_source_port,
                        "server_ip": args.server_ip,
                        "server_ip_count": len(server_ips),
                        "current_server_ip": current_server_ip,
                        "server_port": args.server_port,
                        "session_cps": args.session_cps,
                        "post_create_hold_sec": args.post_create_hold_sec,
                        "keepalive_interval_sec": args.keepalive_interval_sec,
                        "keepalive_timeout_sec": args.keepalive_timeout_sec,
                        "planned_sessions": planned_sessions,
                        **snap,
                        "active_sessions_delta": delta_value(snap, previous_snapshot, "current_active_sessions"),
                        "create_attempts_delta": delta_value(snap, previous_snapshot, "create_attempts_total"),
                        "create_success_delta": delta_value(snap, previous_snapshot, "session_create_success_total"),
                        "create_fail_delta": delta_value(snap, previous_snapshot, "session_create_fail_total"),
                        "mid_disconnect_delta": delta_value(snap, previous_snapshot, "session_mid_disconnect_total"),
                        "payload_up_tx_delta": delta_value(snap, previous_snapshot, "payload_up_tx_total"),
                        "payload_down_rx_delta": delta_value(snap, previous_snapshot, "payload_down_rx_total"),
                        "payload_loss_delta": payload_loss_delta,
                        "payload_message_loss_rate_delta": payload_loss_rate_delta,
                        "keepalive_timeout_delta": delta_value(snap, previous_snapshot, "keepalive_timeout_total"),
                    }
                    writer.writerow(row)
                    f_ts.flush()
                    if client_status_path:
                        write_json_atomic(client_status_path, row)
                    previous_snapshot = snap
                    if now >= next_progress_print_mono:
                        print(format_client_progress(row), flush=True)
                        next_progress_print_mono += args.progress_interval_sec
                    next_stats_mono += args.stats_interval_sec

                if creation_complete_mono is not None and time.monotonic() >= creation_complete_mono + args.post_create_hold_sec:
                    break
        finally:
            for conn in list(conns.values()):
                mark_closed(conn, "local_close", count_mid_disconnect=False)
            sel.close()

    with sessions_path.open("w", newline="", encoding="utf-8") as f_sess:
        writer = csv.DictWriter(f_sess, fieldnames=SESSION_FIELDS)
        writer.writeheader()
        for row in sorted(
            completed_sessions,
            key=lambda item: (str(item.get("server_ip") or ""), -int(item.get("source_port") or 0)),
        ):
            writer.writerow(row)

    final_snap = stats.snapshot(active_count=0, connecting_count=0)
    summary = {
        "result": "done",
        "ts_local": ts_iso(),
        "elapsed_sec": elapsed(),
        "server_ip": args.server_ip,
        "server_ips": server_ips,
        "server_ip_count": len(server_ips),
        "server_port": args.server_port,
        "client_ip": args.client_ip,
        "source_start_port": args.source_start_port,
        "source_end_port": args.source_end_port,
        "max_sessions": args.max_sessions,
        "planned_sessions": planned_sessions,
        "session_cps": args.session_cps,
        "post_create_hold_sec": args.post_create_hold_sec,
        "keepalive_interval_sec": args.keepalive_interval_sec,
        "keepalive_timeout_sec": args.keepalive_timeout_sec,
        "rlimit_nofile": raise_limit,
        **final_snap,
    }
    write_json(summary_path, summary)
    return 0


def wait_for_ready_file(path, timeout_sec):
    path = Path(path)
    deadline = time.monotonic() + float(timeout_sec)
    while time.monotonic() < deadline:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if data.get("ready"):
                return data
        time.sleep(0.1)
    raise RuntimeError(f"server did not become ready within {timeout_sec}s: {path}")


def terminate_process(proc, timeout_sec=5.0):
    if proc.poll() is not None:
        return proc.returncode
    proc.terminate()
    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout_sec)
    return proc.returncode


def run_orchestrator(args, targets=None):
    require_root()
    require_command("ip")

    targets = targets or TARGETS
    out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else build_default_out_dir(args.target, getattr(args, "out_prefix", "trex_tcp_scratch_max_sessions"))
    )
    ensure_dir(out_dir)
    details_dir = prepare_details_dir(out_dir)
    if getattr(args, "benchmark_mode", "ipv4") == "dslite":
        target = dict(targets[args.target])
        for argument_name, target_key in (
            ("lan_ip", "lan_ip"),
            ("lan_gw", "lan_gw"),
            ("wan_ip", "wan_ip"),
            ("wan_gw", "wan_gw"),
            ("router_wan_ip", "router_wan_ip"),
        ):
            value = getattr(args, argument_name, "")
            if value:
                target[target_key] = value
    else:
        target = merge_target_with_overrides(dict(targets[args.target]), args)
    server_ip = args.server_ip or target["wan_ip"]
    args.server_ip = server_ip
    server_ips = build_server_ip_list(
        server_ip,
        args.server_ip_count,
        args.wan_prefix,
        reserved_ips=(target.get("router_wan_ip"),),
    )
    unique_source_ports = args.source_start_port - args.source_end_port + 1
    session_capacity = unique_source_ports * len(server_ips)
    if args.max_sessions > session_capacity:
        raise SystemExit(
            f"ERROR: --max-sessions={args.max_sessions} exceeds the configured "
            f"source-port/server-IP capacity of {session_capacity}"
        )
    planned_sessions = planned_session_count(
        args.source_start_port,
        args.source_end_port,
        len(server_ips),
        args.max_sessions,
    )
    if getattr(args, "benchmark_mode", "ipv4") == "dslite":
        b4_session_limit = int(target.get("b4_session_limit") or 0)
        if b4_session_limit <= 0:
            raise SystemExit("ERROR: DS-Lite target must define a positive b4_session_limit")
        if unique_source_ports > b4_session_limit:
            raise SystemExit(
                f"ERROR: unique source ports ({unique_source_ports}) exceed the "
                f"DS-Lite AFTR B4 session limit ({b4_session_limit})"
            )
    ns_log_path = details_dir / "namespace_setup.csv"
    server_timeseries_path = out_dir / SERVER_TIMESERIES_FILENAME
    server_stdout_path = details_dir / "tcp_scratch_server.stdout.log"
    server_stderr_path = details_dir / "tcp_scratch_server.stderr.log"
    client_stdout_path = details_dir / "tcp_scratch_client.stdout.log"
    client_stderr_path = details_dir / "tcp_scratch_client.stderr.log"
    ready_path = details_dir / "tcp_scratch_server_ready.json"
    client_status_path = details_dir / "tcp_scratch_client_status.json"

    with ns_log_path.open("w", newline="", encoding="utf-8") as f_ns:
        ns_log = csv.writer(f_ns)
        ns_log.writerow(["ts_local", "action", "detail"])
        arp_refresher = None
        try:
            if args.setup_namespaces and args.replace_namespaces:
                delete_existing_namespaces(args, ns_log)
            if args.setup_namespaces:
                prepare_kernel_interfaces_from_profile(args, target, ns_log)
            resolve_profile_interfaces(args, target, ns_log)
            if args.setup_namespaces:
                require_profile_interfaces(args, target)

            metadata = {
                "created_at": ts_iso(),
                "target": args.target,
                "description": target.get("description", ""),
                "tool": "python_socket_tcp_scratch",
                "lan_ns": args.lan_ns,
                "wan_ns": args.wan_ns,
                "lan_iface": args.lan_iface,
                "wan_iface": args.wan_iface,
                "lan_ip": target.get("lan_ip"),
                "lan_gw": target.get("lan_gw"),
                "wan_ip": target.get("wan_ip"),
                "router_wan_ip": target.get("router_wan_ip"),
                "server_ip": server_ip,
                "server_ips": server_ips,
                "server_ip_count": len(server_ips),
                "server_port": args.server_port,
                "source_start_port": args.source_start_port,
                "source_end_port": args.source_end_port,
                "unique_source_ports": unique_source_ports,
                "max_sessions": args.max_sessions,
                "planned_sessions": planned_sessions,
                "repeat": args.repeat,
                "session_cps": args.session_cps,
                "post_create_hold_sec": args.post_create_hold_sec,
                "keepalive_interval_sec": args.keepalive_interval_sec,
                "keepalive_timeout_sec": args.keepalive_timeout_sec,
                "stats_interval_sec": args.stats_interval_sec,
                "progress_interval_sec": args.progress_interval_sec,
                "notes": [
                    "LAN client binds source ports from high to low and connects to the configured WAN TCP server addresses.",
                    "Payload messages are length-prefixed JSON frames containing source_port, elapsed_sec, and direction UP/DOWN.",
                    "payload_message_loss_rate is calculated from client queued UP payload messages and received DOWN replies.",
                    "An active session is marked disconnected when a DOWN keepalive reply is not received within keepalive_timeout_sec.",
                ],
            }
            if getattr(args, "benchmark_mode", "ipv4") == "dslite":
                metadata["aftr_port_multiplexing"] = {
                    "b4_session_limit": int(target["b4_session_limit"]),
                    "unique_source_ports": unique_source_ports,
                    "remote_destination_addresses": len(server_ips),
                    "mapping_key": "B4 IPv6, inner IPv4 source, source port, protocol",
                    "destination_in_mapping_key": False,
                }
            write_json(details_dir / "tcp_scratch_max_sessions_metadata.json", metadata)

            print("============================================================")
            print(getattr(args, "benchmark_title", "IPv4 TCP scratch maximum session benchmark"))
            print("============================================================")
            print(f"Target          : {args.target}")
            print(f"Description     : {target.get('description', '')}")
            print("Tool            : built-in Python TCP server/client, ip(iproute2 namespaces)")
            print(f"LAN namespace   : {args.lan_ns}")
            print(f"WAN namespace   : {args.wan_ns}")
            print(f"LAN endpoint    : {target.get('lan_ip')} gw {target.get('lan_gw')}")
            print(
                f"WAN servers     : {server_ips[0]}..{server_ips[-1]} "
                f"({len(server_ips)} addresses), TCP {args.server_port}"
            )
            print(f"Source ports    : {args.source_start_port} down to {args.source_end_port}")
            print(f"Planned sessions: {planned_sessions}")
            print(f"Repeat          : {args.repeat}")
            print(f"Session rate    : {args.session_cps} CPS")
            print(f"Hold after ramp : {args.post_create_hold_sec}s")
            print(f"Keepalive       : every {args.keepalive_interval_sec}s; timeout {args.keepalive_timeout_sec}s")
            print(f"Progress print  : every {args.progress_interval_sec}s")
            print(f"Output dir      : {out_dir}")
            print("============================================================")

            if args.dry_run:
                server_cmd = ns_cmd(args.wan_ns, [sys.executable, str(Path(__file__).resolve()), "--internal-role", "server"])
                client_cmd = ns_cmd(args.lan_ns, [sys.executable, str(Path(__file__).resolve()), "--internal-role", "client"])
                print("[DRY-RUN] server:", " ".join(server_cmd))
                print("[DRY-RUN] client:", " ".join(client_cmd))
                return 0

            if args.setup_namespaces:
                setup_namespaces(args, target, ns_log)
            verify_namespaces(args)
            configure_or_verify_server_ips(args, target, server_ips, ns_log)
            if args.wait_interfaces_ready:
                wait_test_interfaces_ready(args)
            if args.post_namespace_settle_sec > 0:
                time.sleep(args.post_namespace_settle_sec)
            wait_before_test_start(
                args.test_start_delay_sec,
                getattr(args, "benchmark_title", "TCP scratch maximum-session benchmark"),
            )
            arp_refresher = make_namespace_arp_refresher(args, target, details_dir)
            arp_refresher.refresh_now("startup", required=True)
            arp_refresher.start()

            client_ip = args.client_ip or target["lan_ip"]
            server_ips_arg = ",".join(server_ips)
            server_bind_ip = "0.0.0.0" if len(server_ips) > 1 else server_ip

            server_cmd = ns_cmd(args.wan_ns, [
                sys.executable,
                str(Path(__file__).resolve()),
                "--internal-role", "server",
                "--out-dir", str(details_dir),
                "--bind-ip", str(server_bind_ip),
                "--server-ips", server_ips_arg,
                "--server-port", str(args.server_port),
                "--listen-backlog", str(args.listen_backlog),
                "--expected-sessions", str(planned_sessions),
                "--stats-interval-sec", str(args.stats_interval_sec),
                "--select-timeout-sec", str(args.select_timeout_sec),
                "--server-timeseries-path", str(server_timeseries_path),
                "--ready-file", str(ready_path),
                "--client-status-file", str(client_status_path),
            ])
            client_cmd = ns_cmd(args.lan_ns, [
                sys.executable,
                str(Path(__file__).resolve()),
                "--internal-role", "client",
                "--out-dir", str(details_dir),
                "--client-ip", str(client_ip),
                "--server-ip", str(server_ip),
                "--server-ips", server_ips_arg,
                "--server-ip-count", str(len(server_ips)),
                "--server-port", str(args.server_port),
                "--source-start-port", str(args.source_start_port),
                "--source-end-port", str(args.source_end_port),
                "--max-sessions", str(args.max_sessions),
                "--session-cps", str(args.session_cps),
                "--post-create-hold-sec", str(args.post_create_hold_sec),
                "--keepalive-interval-sec", str(args.keepalive_interval_sec),
                "--keepalive-timeout-sec", str(args.keepalive_timeout_sec),
                "--connect-timeout-sec", str(args.connect_timeout_sec),
                "--stats-interval-sec", str(args.stats_interval_sec),
                "--select-timeout-sec", str(args.select_timeout_sec),
                "--max-create-batch", str(args.max_create_batch),
                "--progress-interval-sec", str(args.progress_interval_sec),
                "--client-status-file", str(client_status_path),
            ])

            print("[RUN] WAN TCP server")
            print(" ".join(server_cmd))
            server_stdout = server_stdout_path.open("w", encoding="utf-8")
            server_stderr = server_stderr_path.open("w", encoding="utf-8")
            server_proc = subprocess.Popen(server_cmd, stdout=server_stdout, stderr=server_stderr, text=True)
            try:
                wait_for_ready_file(ready_path, args.server_ready_timeout_sec)
                preflight_stdout_path = details_dir / "tcp_scratch_preflight.json"
                preflight_stderr_path = details_dir / "tcp_scratch_preflight.stderr.log"
                preflight_cmd_base = ns_cmd(args.lan_ns, [
                    sys.executable, str(Path(__file__).resolve()),
                    "--internal-role", "preflight",
                    "--client-ip", str(client_ip),
                    "--server-ip", str(server_ip),
                    "--server-ips", server_ips_arg,
                    "--server-port", str(args.server_port),
                    "--connect-timeout-sec", str(args.connect_timeout_sec),
                ])
                if len(server_ips) > 1 and args.source_end_port > 1:
                    preflight_source_port = (
                        args.source_end_port
                        if args.lan_source_port_range is not None
                        else args.source_end_port - 1
                    )
                    preflight_cmd_base.extend(
                        ["--preflight-source-port", str(preflight_source_port)]
                    )

                def tcp_preflight_probe(attempt):
                    arp_refresher.refresh_now(
                        f"before_tcp_scratch_preflight_attempt_{attempt}",
                        required=True,
                    )
                    attempt_stdout = details_dir / (
                        f"tcp_scratch_preflight_attempt{attempt}.json"
                    )
                    attempt_stderr = details_dir / (
                        f"tcp_scratch_preflight_attempt{attempt}.stderr.log"
                    )
                    with attempt_stdout.open("w", encoding="utf-8") as pf_out, attempt_stderr.open("w", encoding="utf-8") as pf_err:
                        preflight_proc = subprocess.run(
                            preflight_cmd_base,
                            stdout=pf_out,
                            stderr=pf_err,
                            text=True,
                            timeout=float(args.connect_timeout_sec) + 5.0,
                        )
                    if preflight_proc.returncode != 0:
                        raise RuntimeError(
                            "TCP scratch round-trip preflight failed; "
                            f"see {attempt_stdout} and {attempt_stderr}"
                        )
                    return attempt_stdout, attempt_stderr

                preflight_outcome = run_consecutive_preflight(
                    tcp_preflight_probe,
                    phase="tcp_scratch_max_sessions",
                    csv_path=details_dir / "tcp_scratch_preflight_recovery.csv",
                    required_successes=args.preflight_success_consecutive,
                    interval_sec=args.preflight_probe_interval_sec,
                    timeout_sec=args.preflight_timeout_sec,
                )
                final_stdout, final_stderr = preflight_outcome.value
                preflight_stdout_path.write_bytes(final_stdout.read_bytes())
                preflight_stderr_path.write_bytes(final_stderr.read_bytes())

                # The preflight connection must not be included in the measured server counters.
                # Restart the server and clear its output files before launching the real client.
                terminate_process(server_proc)
                server_stdout.close()
                server_stderr.close()
                ready_path.unlink(missing_ok=True)
                client_status_path.unlink(missing_ok=True)
                server_stdout = server_stdout_path.open("w", encoding="utf-8")
                server_stderr = server_stderr_path.open("w", encoding="utf-8")
                server_proc = subprocess.Popen(server_cmd, stdout=server_stdout, stderr=server_stderr, text=True)
                wait_for_ready_file(ready_path, args.server_ready_timeout_sec)

                arp_refresher.refresh_now("before_tcp_scratch_client", required=True)
                print("[RUN] LAN TCP client")
                print(" ".join(client_cmd))
                with client_stdout_path.open("w", encoding="utf-8") as client_stdout, client_stderr_path.open("w", encoding="utf-8") as client_stderr:
                    client_proc = subprocess.Popen(client_cmd, stdout=client_stdout, stderr=client_stderr, text=True)
                    next_progress_mono = time.monotonic()
                    last_progress_line = None
                    while client_proc.poll() is None:
                        now_mono = time.monotonic()
                        if now_mono >= next_progress_mono:
                            row = read_last_csv_row(details_dir / "tcp_scratch_max_sessions_timeseries.csv")
                            line = format_client_progress(row)
                            print(line, flush=True)
                            last_progress_line = line
                            next_progress_mono += args.progress_interval_sec
                        time.sleep(0.2)
                    rc = client_proc.returncode
                    row = read_last_csv_row(details_dir / "tcp_scratch_max_sessions_timeseries.csv")
                    final_line = format_client_progress(row)
                    if final_line != last_progress_line:
                        print(final_line, flush=True)
                if rc != 0:
                    raise SystemExit(f"ERROR: client process failed: exit_code={rc}; see {client_stderr_path}")
            finally:
                terminate_process(server_proc)
                server_stdout.close()
                server_stderr.close()

            combined = {
                "result": "done",
                "client_summary": {},
                "server_summary": {},
            }
            client_summary_path = details_dir / "tcp_scratch_max_sessions_summary.json"
            server_summary_path = details_dir / "tcp_scratch_server_summary.json"
            if client_summary_path.exists():
                combined["client_summary"] = json.loads(client_summary_path.read_text(encoding="utf-8"))
            if server_summary_path.exists():
                combined["server_summary"] = json.loads(server_summary_path.read_text(encoding="utf-8"))
            write_json(details_dir / "tcp_scratch_derived_summary.json", combined)

            with (details_dir / "tcp_scratch_derived_summary.csv").open("w", newline="", encoding="utf-8") as f_sum:
                fields = [
                    "test_name",
                    "result",
                    "target",
                    "planned_sessions",
                    "create_attempts_total",
                    "session_create_success_total",
                    "session_create_fail_total",
                    "session_mid_disconnect_total",
                    "keepalive_timeout_total",
                    "current_active_sessions_end",
                    "payload_up_tx_total",
                    "payload_down_rx_total",
                    "payload_loss_total",
                    "payload_message_loss_rate",
                    "server_accept_total",
                    "server_payload_up_rx_total",
                    "server_payload_down_tx_total",
                    "elapsed_sec",
                ]
                writer = csv.DictWriter(f_sum, fieldnames=fields)
                writer.writeheader()
                cs = combined.get("client_summary") or {}
                ss = combined.get("server_summary") or {}
                writer.writerow({
                    "test_name": getattr(args, "test_name", "tcp_scratch_max_sessions"),
                    "result": combined.get("result"),
                    "target": args.target,
                    "planned_sessions": planned_sessions,
                    "create_attempts_total": cs.get("create_attempts_total", ""),
                    "session_create_success_total": cs.get("session_create_success_total", ""),
                    "session_create_fail_total": cs.get("session_create_fail_total", ""),
                    "session_mid_disconnect_total": cs.get("session_mid_disconnect_total", ""),
                    "keepalive_timeout_total": cs.get("keepalive_timeout_total", ""),
                    "current_active_sessions_end": cs.get("current_active_sessions", ""),
                    "payload_up_tx_total": cs.get("payload_up_tx_total", ""),
                    "payload_down_rx_total": cs.get("payload_down_rx_total", ""),
                    "payload_loss_total": cs.get("payload_loss_total", ""),
                    "payload_message_loss_rate": cs.get("payload_message_loss_rate", ""),
                    "server_accept_total": ss.get("accept_total", ""),
                    "server_payload_up_rx_total": ss.get("payload_up_rx_total", ""),
                    "server_payload_down_tx_total": ss.get("payload_down_tx_total", ""),
                    "elapsed_sec": cs.get("elapsed_sec", ""),
                })

            print("============================================================")
            print("DONE")
            print(f"Output dir: {out_dir}")
            print(f"Summary CSV: {server_timeseries_path}")
            print(f"Details   : {details_dir}")
            print("============================================================")
            return 0
        finally:
            if arp_refresher is not None:
                arp_refresher.stop()
            if args.restore_dpdk_after and prepare_trex_interfaces_from_profile is not None:
                try:
                    prepare_trex_interfaces_from_profile(target, getattr(args, "dpdk_driver", "") or None, ns_log)
                except Exception as exc:
                    ns_log.writerow([ts_iso(), "restore_dpdk_after_warning", str(exc)])
            cleanup_namespaces(args, ns_log)


def build_parser(targets=None, default_target=None, parser_defaults=None, description=None):
    targets = targets or TARGETS
    parser_defaults = dict(parser_defaults or {})
    parser = argparse.ArgumentParser(
        description=description or "Run IPv4 TCP scratch maximum session benchmark without TRex.",
        allow_abbrev=False,
    )
    parser.add_argument("--internal-role", choices=["orchestrator", "server", "client", "preflight"], default="orchestrator", help=argparse.SUPPRESS)

    parser.add_argument(
        "--target",
        choices=targets.keys(),
        default=default_target or _TCP_DEFAULTS["target"],
    )
    parser.add_argument("--out-dir", default="", help="Result directory. Default: /var/trex/trex_tcp_scratch_max_sessions_<target>_<timestamp>/. ")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--repeat",
        type=argparse_repeat,
        default=DEFAULT_REPEAT,
        help="Repeat the complete maximum-session benchmark. Default: 1.",
    )

    parser.add_argument(
        "--source-start-port",
        type=parse_port,
        default=int(parser_defaults.get("source_start_port", DEFAULT_SOURCE_START_PORT)),
    )
    parser.add_argument(
        "--source-end-port",
        type=parse_port,
        default=int(parser_defaults.get("source_end_port", DEFAULT_SOURCE_END_PORT)),
    )
    parser.add_argument(
        "--max-sessions",
        type=parse_non_negative_int,
        default=int(parser_defaults.get("max_sessions", 0)),
        help="Maximum sessions from the source-port/server-IP Cartesian product. Zero uses all combinations.",
    )
    parser.add_argument(
        "--session-cps", "--session-ramp-cps",
        dest="session_cps",
        type=parse_positive_float,
        default=DEFAULT_SESSION_CPS,
        help="Session creation rate in connections per second. --session-ramp-cps is accepted as a launcher-compatible alias.",
    )
    parser.add_argument("--post-create-hold-sec", type=parse_non_negative_float, default=DEFAULT_POST_CREATE_HOLD_SEC)
    parser.add_argument("--keepalive-interval-sec", type=parse_positive_float, default=DEFAULT_KEEPALIVE_INTERVAL_SEC)
    parser.add_argument("--keepalive-timeout-sec", type=parse_positive_float, default=DEFAULT_KEEPALIVE_TIMEOUT_SEC, help="Mark an established session disconnected when a DOWN keepalive reply is not received within this many seconds. Default: 1.0.")
    parser.add_argument("--server-port", type=parse_port, default=DEFAULT_SERVER_PORT)
    parser.add_argument(
        "--server-ip-count",
        type=parse_positive_int,
        default=int(parser_defaults.get("server_ip_count", 1)),
        help="Number of descending WAN server IPv4 addresses beginning at --server-ip.",
    )
    parser.add_argument("--connect-timeout-sec", type=parse_positive_float, default=DEFAULT_CONNECT_TIMEOUT_SEC)
    parser.add_argument("--stats-interval-sec", type=parse_positive_float, default=DEFAULT_STATS_INTERVAL_SEC)
    parser.add_argument("--progress-interval-sec", type=parse_positive_float, default=DEFAULT_PROGRESS_INTERVAL_SEC)
    parser.add_argument("--select-timeout-sec", type=parse_positive_float, default=DEFAULT_SELECT_TIMEOUT_SEC)
    parser.add_argument("--listen-backlog", type=parse_positive_int, default=DEFAULT_LISTEN_BACKLOG)
    parser.add_argument("--server-ready-timeout-sec", type=parse_positive_float, default=_TCP_DEFAULTS["server_ready_timeout_sec"])
    parser.add_argument("--max-create-batch", type=parse_positive_int, default=DEFAULT_MAX_CREATE_BATCH)
    add_consecutive_preflight_arguments(parser)
    add_lan_source_port_range_argument(parser)

    parser.add_argument("--lan-ns", default=DEFAULT_LAN_NS)
    parser.add_argument("--wan-ns", default=DEFAULT_WAN_NS)
    parser.add_argument("--setup-namespaces", dest="setup_namespaces", action="store_true", default=_NETWORK_DEFAULTS["setup_namespaces"])
    parser.add_argument("--no-setup-namespaces", dest="setup_namespaces", action="store_false")
    parser.add_argument("--replace-namespaces", dest="replace_namespaces", action="store_true", default=_NETWORK_DEFAULTS["replace_namespaces"])
    parser.add_argument("--no-replace-namespaces", dest="replace_namespaces", action="store_false")
    parser.add_argument("--keep-namespaces", action="store_true")
    parser.add_argument("--auto-kernel-bind", dest="auto_kernel_bind", action="store_true", default=_NETWORK_DEFAULTS["auto_kernel_bind"])
    parser.add_argument("--no-auto-kernel-bind", dest="auto_kernel_bind", action="store_false")
    parser.add_argument("--restore-dpdk-after", dest="restore_dpdk_after", action="store_true", default=_NETWORK_DEFAULTS["restore_dpdk_after"])
    parser.add_argument("--no-restore-dpdk-after", dest="restore_dpdk_after", action="store_false")
    parser.add_argument("--dpdk-driver", default="")
    parser.add_argument("--post-namespace-settle-sec", type=parse_non_negative_float, default=DEFAULT_POST_NAMESPACE_SETTLE_SEC)
    parser.add_argument("--test-start-delay-sec", type=parse_non_negative_float, default=DEFAULT_TEST_START_DELAY_SEC, help="Seconds to wait after namespace interfaces report link-up and before testing. Default: 15.")
    parser.add_argument("--wait-interfaces-ready", dest="wait_interfaces_ready", action="store_true", default=_NETWORK_DEFAULTS["wait_interfaces_ready"])
    parser.add_argument("--no-wait-interfaces-ready", dest="wait_interfaces_ready", action="store_false")
    parser.add_argument("--interface-ready-timeout-sec", type=parse_positive_float, default=DEFAULT_INTERFACE_READY_TIMEOUT_SEC)

    parser.add_argument("--lan-iface", default="")
    parser.add_argument("--wan-iface", default="")
    parser.add_argument("--lan-prefix", type=parse_positive_int, default=DEFAULT_LAN_PREFIX)
    parser.add_argument("--wan-prefix", type=parse_positive_int, default=DEFAULT_WAN_PREFIX)
    parser.add_argument("--lan-ip", default="")
    parser.add_argument("--lan-gw", default="")
    parser.add_argument("--wan-ip", "--wan-link-ip", dest="wan_ip", default="")
    parser.add_argument("--wan-gw", "--wan-arp-target", dest="wan_gw", default="")
    parser.add_argument(
        "--router-wan-ip",
        "--wan-gateway-ip",
        dest="router_wan_ip",
        default="",
    )
    parser.add_argument("--client-ip", default="", help="LAN client bind IP. Default: target lan_ip.")
    parser.add_argument("--server-ip", default="", help="WAN server bind/connect IP. Default: target wan_ip.")

    parser.add_argument("--bind-ip", default="", help=argparse.SUPPRESS)
    parser.add_argument("--server-ips", default="", help=argparse.SUPPRESS)
    parser.add_argument("--preflight-source-port", type=parse_non_negative_int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--expected-sessions", type=parse_non_negative_int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--ready-file", default="", help=argparse.SUPPRESS)
    parser.add_argument("--client-status-file", default="", help=argparse.SUPPRESS)
    parser.add_argument("--server-timeseries-path", default="", help=argparse.SUPPRESS)
    return parser


def main(
    targets=None,
    default_target=None,
    parser_defaults=None,
    benchmark_mode="ipv4",
    benchmark_title="IPv4 TCP scratch maximum session benchmark",
    test_name="tcp_scratch_max_sessions",
    out_prefix="trex_tcp_scratch_max_sessions",
    description=None,
):
    targets = targets or TARGETS
    parser = build_parser(
        targets=targets,
        default_target=default_target,
        parser_defaults=parser_defaults,
        description=description,
    )
    args = parser.parse_args()
    args.benchmark_mode = benchmark_mode
    args.benchmark_title = benchmark_title
    args.test_name = test_name
    args.out_prefix = out_prefix
    validate_consecutive_preflight_arguments(args)
    if args.lan_source_port_range is not None:
        args.source_end_port, args.source_start_port = args.lan_source_port_range
    if args.source_start_port < args.source_end_port:
        raise SystemExit("ERROR: --source-start-port must be greater than or equal to --source-end-port")
    if args.internal_role == "server":
        if not args.bind_ip:
            raise SystemExit("ERROR: internal server requires --bind-ip")
        return run_server(args)
    if args.internal_role == "client":
        if not args.client_ip or not args.server_ip:
            raise SystemExit("ERROR: internal client requires --client-ip and --server-ip")
        return run_client(args)
    if args.internal_role == "preflight":
        if not args.client_ip or not args.server_ip:
            raise SystemExit("ERROR: internal preflight requires --client-ip and --server-ip")
        return run_preflight_client(args)
    repeated_out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else build_default_out_dir(args.target, args.out_prefix)
    )
    repeated_rc = run_repeated_subprocesses(
        args.repeat,
        repeated_out_dir,
        summary_filenames=(SERVER_TIMESERIES_FILENAME,),
        script_path=sys.argv[0],
        cwd=REPO_DIR,
    )
    if repeated_rc is not None:
        return repeated_rc
    return run_orchestrator(args, targets=targets)


if __name__ == "__main__":
    raise SystemExit(main())
