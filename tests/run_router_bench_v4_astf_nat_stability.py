#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRex IPv4 NAT ASTF stability soak test.

Port mapping:
  port 0 = LAN/client side
  port 1 = WAN/server side

Default test pattern:
  - Run TCP cycles, then UDP cycles.
  - Each cycle creates 200 ASTF flows at 50 CPS.
  - Each flow transfers 1 MB by default.
  - Each flow is explicitly completed/closed.
  - When a cycle has completed, wait 1 second and start the next cycle.
  - Continue for the configured soak duration for each protocol.

Notes:
  - ASTF UDP is implemented with ASTFProgram(stream=False), send_msg(),
    recv_msg(), and close_msg().
  - TRex ASTF allocates client source ports internally.  The requested client
    source-port range is recorded in metadata and CSV, but this script cannot
    force a deterministic client source-port round-robin through the public
    ASTF Python profile API.
"""

import argparse
import csv
import datetime as dt
import json
import math
import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from tests.lib.config import TARGETS, target_for_ip_version  # type: ignore
    from tests.lib.output import prepare_details_dir  # type: ignore
except Exception:
    from lib.config import TARGETS, target_for_ip_version  # type: ignore
    from lib.output import prepare_details_dir  # type: ignore

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
    from tests.lib.common import DEFAULT_RESULT_ROOT  # type: ignore
    from tests.lib.config import load_defaults as _load_defaults  # type: ignore
    from tests.lib.preflight import (  # type: ignore
        ConsecutivePreflightTimeoutError,
        add_consecutive_preflight_arguments,
        run_consecutive_preflight,
        validate_consecutive_preflight_arguments,
    )
except Exception:
    from lib.common import DEFAULT_RESULT_ROOT  # type: ignore
    from lib.config import load_defaults as _load_defaults  # type: ignore
    from lib.preflight import (  # type: ignore
        ConsecutivePreflightTimeoutError,
        add_consecutive_preflight_arguments,
        run_consecutive_preflight,
        validate_consecutive_preflight_arguments,
    )

try:
    from tests.lib.astf import (  # type: ignore
        ASTF_TCP_PORT,
        RAW_INTERVAL_SEC,
        TrexAstfServer,
        add_trex_api_path,
        calc_astf_metrics,
        clamp_rate,
        first_number,
        install_trex_cfg,
        json_cell,
        enable_ipv6_global_info,
        make_ip_gen,
        make_ipv6_ip_gen,
        merge_target_with_overrides,
        parse_interfaces,
        prepare_trex_interfaces_from_profile,
        require_root,
        resolve_astf_cores,
        resolve_ipv6_gateway_macs,
        ts_compact,
        ts_iso,
        try_arp,
        write_raw_stats,
    )
except Exception:
    from lib.astf import (  # type: ignore
        ASTF_TCP_PORT,
        RAW_INTERVAL_SEC,
        TrexAstfServer,
        add_trex_api_path,
        calc_astf_metrics,
        clamp_rate,
        first_number,
        install_trex_cfg,
        json_cell,
        enable_ipv6_global_info,
        make_ip_gen,
        make_ipv6_ip_gen,
        merge_target_with_overrides,
        parse_interfaces,
        prepare_trex_interfaces_from_profile,
        require_root,
        resolve_astf_cores,
        resolve_ipv6_gateway_macs,
        ts_compact,
        ts_iso,
        try_arp,
        write_raw_stats,
    )


_NAT_DEFAULTS = _load_defaults("nat_stability")
BENCHMARK_DESCRIPTION = "Run TRex ASTF IPv4 NAT stability soak test."
BENCHMARK_TITLE = "TRex ASTF IPv4 NAT stability benchmark"
DEFAULT_OUTPUT_PREFIX = "trex_astf_nat_stability"
METADATA_MODE = "astf_nat_stability"
STABILITY_SLUG = "astf_nat_stability"
STABILITY_LABEL = "ASTF NAT stability"
TCP_TG_NAME = "nat_stability_tcp"
UDP_TG_NAME = "nat_stability_udp"
DEFAULT_SOAK_DURATION_SEC = float(_NAT_DEFAULTS["soak_duration_sec"])
DEFAULT_CYCLE_CPS = float(_NAT_DEFAULTS["cycle_cps"])
DEFAULT_CYCLE_SESSIONS = int(_NAT_DEFAULTS["cycle_sessions"])
DEFAULT_MB_PER_SESSION = float(_NAT_DEFAULTS["mb_per_session"])
DEFAULT_INTER_CYCLE_SLEEP_SEC = float(_NAT_DEFAULTS["inter_cycle_sleep_sec"])
DEFAULT_COMPLETION_QUIET_SEC = float(_NAT_DEFAULTS["completion_quiet_sec"])
DEFAULT_GENERATION_GRACE_SEC = float(_NAT_DEFAULTS["generation_grace_sec"])
DEFAULT_CONNECT_TIMEOUT_SEC = float(_NAT_DEFAULTS["connect_timeout_sec"])
DEFAULT_PAYLOAD_DIRECTION = _NAT_DEFAULTS["payload_direction"]
DEFAULT_CHUNK_BYTES = int(_NAT_DEFAULTS["chunk_bytes"])
DEFAULT_UDP_MSG_PAYLOAD_BYTES = int(_NAT_DEFAULTS["udp_msg_payload_bytes"])
DEFAULT_UDP_MTU = int(_NAT_DEFAULTS["udp_mtu"])
DEFAULT_TCP_MSS = int(_NAT_DEFAULTS["tcp_mss"])
DEFAULT_TCP_WINDOW_KB = int(_NAT_DEFAULTS["tcp_window_kb"])
DEFAULT_STABLE_SUCCESS_THRESHOLD = float(_NAT_DEFAULTS["stable_success_threshold"])
DEFAULT_STABLE_LOSS_THRESHOLD = float(_NAT_DEFAULTS["stable_loss_threshold"])
DEFAULT_PAYLOAD_COMPLETION_THRESHOLD = float(_NAT_DEFAULTS["payload_completion_threshold"])
DEFAULT_CLIENT_PORT_MIN = int(_NAT_DEFAULTS["client_port_min"])
DEFAULT_CLIENT_PORT_MAX = int(_NAT_DEFAULTS["client_port_max"])

CYCLE_CSV_FIELDS = [
    "ts_local_start",
    "ts_local_end",
    "cycle_index",
    "protocol",
    "test_name",
    "preflight_ok",
    "preflight_session_success_rate",
    "preflight_payload_completion_rate",
    "preflight_packet_loss_rate",
    "preflight_throughput_loss_rate",
    "test_elapsed_total_sec",
    "protocol_elapsed_sec",
    "cycle_elapsed_sec",
    "cycle_hard_timeout_sec",
    "connect_timeout_sec",
    "completion_reason",
    "cycle_completed",
    "cycle_timeout",
    "cycle_stable",
    "error",
    "requested_cps",
    "requested_sessions",
    "session_generation_duration_sec",
    "inter_cycle_sleep_sec",
    "payload_direction",
    "mb_per_session",
    "bytes_per_session",
    "expected_payload_bytes",
    "actual_payload_rx_bytes",
    "payload_completion_rate",
    "payload_shortfall_bytes",
    "stable_success_threshold",
    "stable_loss_threshold",
    "payload_completion_threshold",
    "communication_outage_suspected",
    "communication_outage",
    "communication_outage_reason",
    "client_port_min_requested",
    "client_port_max_requested",
    "client_source_port_mode",
    "server_port",
    "udp_msg_payload_bytes",
    "udp_expected_rx_msgs",
    "udp_actual_rx_msgs",
    "udp_message_completion_rate",
    "client_connects",
    "server_accepts",
    "established_sessions",
    "failed_or_unestablished_sessions",
    "session_success_rate",
    "session_failure_rate",
    "client_closed_sessions",
    "server_closed_sessions",
    "closed_sessions",
    "estimated_active_sessions",
    "active_sessions_source",
    "paired_trex_m_est_flows",
    "client_trex_m_est_flows",
    "server_trex_m_est_flows",
    "global_trex_active_flows",
    "global_trex_open_flows",
    "global_trex_active_sockets",
    "global_trex_socket_util",
    "client_tcps_drops",
    "server_tcps_drops",
    "client_tcps_conndrops",
    "server_tcps_conndrops",
    "client_tcps_timeoutdrop",
    "server_tcps_timeoutdrop",
    "client_tcps_rexmttimeo",
    "server_tcps_rexmttimeo",
    "client_tx_l7_bytes",
    "server_rx_l7_bytes",
    "server_tx_l7_bytes",
    "client_rx_l7_bytes",
    "tx_l7_bytes",
    "rx_l7_bytes",
    "l7_loss_bytes",
    "throughput_loss_rate",
    "client_tx_tcp_packets",
    "server_rx_tcp_packets",
    "server_tx_tcp_packets",
    "client_rx_tcp_packets",
    "tx_tcp_packets",
    "rx_tcp_packets",
    "packet_loss_packets",
    "packet_loss_rate",
    "client_udps_sndpkt",
    "server_udps_rcvpkt",
    "server_udps_sndpkt",
    "client_udps_rcvpkt",
    "client_udps_sndbyte",
    "server_udps_rcvbyte",
    "server_udps_sndbyte",
    "client_udps_rcvbyte",
    "udp_tx_packets",
    "udp_rx_packets",
    "udp_packet_loss_packets",
    "udp_packet_loss_rate",
]


def seconds_text(value):
    return f"{float(value):.3f}s"


def parse_positive_float(value):
    try:
        f = float(str(value).replace("_", ""))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive number") from exc
    if f <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return f


def parse_non_negative_float(value):
    try:
        f = float(str(value).replace("_", ""))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a non-negative number") from exc
    if f < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return f


def parse_positive_int(value):
    try:
        i = int(str(value).replace("_", ""))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer") from exc
    if i <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return i


def parse_protocols(value):
    parts = [p.strip().lower() for p in str(value).split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("protocol list must not be empty")
    invalid = [p for p in parts if p not in ("tcp", "udp")]
    if invalid:
        raise argparse.ArgumentTypeError("protocols must be tcp, udp, or tcp,udp")
    ordered = []
    for p in parts:
        if p not in ordered:
            ordered.append(p)
    return ordered


def resolve_bytes_per_session(args):
    if getattr(args, "payload_bytes_per_session", None) is not None:
        return int(args.payload_bytes_per_session)
    return int(round(float(args.mb_per_session) * 1_000_000.0))


def update_payload_size_args(args):
    bytes_per_session = resolve_bytes_per_session(args)
    args.bytes_per_session = int(bytes_per_session)
    args.mb_per_session = float(bytes_per_session) / 1_000_000.0
    return bytes_per_session


def payload_marker(direction):
    if direction == "server-to-client":
        return b"GET /nat-stability HTTP/1.1\r\nHost: astf-nat-stability\r\nConnection: close\r\n\r\n"
    if direction == "client-to-server":
        return b"S"
    return b"BIDIR-OK"


def add_tcp_send_bytes(program, bytes_total, chunk_bytes):
    """Add a bounded ASTF program that sends exactly ``bytes_total`` bytes.

    ASTFProgram.send_chunk() expands a large transfer into one program command
    per chunk and embeds the complete payload in the profile.  A 1 MB transfer
    therefore creates dozens of commands before the preflight can even start.
    Use TRex's virtual ``size``/``fill`` buffer with an ASTF loop instead so the
    profile stays small regardless of the configured payload size.
    """
    bytes_total = int(bytes_total)
    chunk_bytes = int(chunk_bytes)
    if bytes_total <= 0:
        raise ValueError("bytes_total must be positive")
    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")

    full_chunks, remainder = divmod(bytes_total, chunk_bytes)
    fill = b"N"
    if full_chunks == 1:
        program.send(fill, size=chunk_bytes, fill=fill)
    elif full_chunks > 1:
        program.set_var("nat_payload_chunks", full_chunks)
        program.set_label("nat_payload_chunk:")
        program.send(fill, size=chunk_bytes, fill=fill)
        program.jmp_nz("nat_payload_chunks", "nat_payload_chunk:")
    if remainder:
        program.send(fill, size=remainder, fill=fill)


def apply_tcp_info(info, args):
    info.tcp.mss = int(args.tcp_mss)
    info.tcp.initwnd = 2
    info.tcp.no_delay = 0
    info.tcp.no_delay_counter = 2 * int(args.tcp_mss)
    info.tcp.rxbufsize = int(args.tcp_window_kb) * 1024
    info.tcp.txbufsize = int(args.tcp_window_kb) * 1024


def udp_packet_count(bytes_per_session, udp_msg_payload_bytes):
    return max(1, int(math.ceil(float(bytes_per_session) / float(udp_msg_payload_bytes))))


def add_udp_send_messages(program, bytes_total, msg_payload_bytes):
    bytes_total = int(bytes_total)
    msg_payload_bytes = int(msg_payload_bytes)
    if bytes_total <= 0:
        raise ValueError("bytes_total must be positive")
    if msg_payload_bytes <= 0:
        raise ValueError("msg_payload_bytes must be positive")

    full_messages, remainder = divmod(bytes_total, msg_payload_bytes)
    fill = b"U"
    if full_messages == 1:
        program.send_msg(fill, size=msg_payload_bytes, fill=fill)
    elif full_messages > 1:
        program.set_var("nat_udp_messages", full_messages)
        program.set_label("nat_udp_message:")
        program.send_msg(fill, size=msg_payload_bytes, fill=fill)
        program.jmp_nz("nat_udp_messages", "nat_udp_message:")
    if remainder:
        program.send_msg(fill, size=remainder, fill=fill)


def make_tcp_nat_stability_profile(target, args, bytes_per_session):
    from trex.astf.api import ASTFGlobalInfo, ASTFProfile, ASTFProgram, ASTFTemplate, ASTFTCPClientTemplate, ASTFTCPServerTemplate

    is_ipv6 = target.get("ip_version") == "v6"
    ip_gen = make_ipv6_ip_gen(target) if is_ipv6 else make_ip_gen(target)
    marker = payload_marker(args.payload_direction)

    prog_c = ASTFProgram()
    prog_s = ASTFProgram()

    if args.payload_direction == "server-to-client":
        prog_c.send(marker)
        prog_c.recv(int(bytes_per_session))
        prog_c.wait_for_peer_close()

        prog_s.recv(len(marker))
        add_tcp_send_bytes(prog_s, bytes_per_session, args.payload_chunk_bytes)

    elif args.payload_direction == "client-to-server":
        add_tcp_send_bytes(prog_c, bytes_per_session, args.payload_chunk_bytes)
        prog_c.recv(len(marker))
        prog_c.wait_for_peer_close()

        prog_s.recv(int(bytes_per_session))
        prog_s.send(marker)

    elif args.payload_direction == "bidirectional":
        add_tcp_send_bytes(prog_c, bytes_per_session, args.payload_chunk_bytes)
        prog_c.recv(int(bytes_per_session))
        prog_c.wait_for_peer_close()

        prog_s.recv(int(bytes_per_session))
        add_tcp_send_bytes(prog_s, bytes_per_session, args.payload_chunk_bytes)
    else:
        raise ValueError(f"unsupported payload direction: {args.payload_direction}")

    client_info = ASTFGlobalInfo()
    server_info = ASTFGlobalInfo()
    apply_tcp_info(client_info, args)
    apply_tcp_info(server_info, args)
    if is_ipv6:
        enable_ipv6_global_info(client_info, server_info, target)

    temp_c = ASTFTCPClientTemplate(
        program=prog_c,
        ip_gen=ip_gen,
        port=int(args.astf_tcp_port),
        cps=float(args.cycle_cps),
        limit=int(args.cycle_sessions),
    )
    temp_s = ASTFTCPServerTemplate(program=prog_s)
    template = ASTFTemplate(
        client_template=temp_c,
        server_template=temp_s,
        tg_name=TCP_TG_NAME,
    )
    return ASTFProfile(
        default_ip_gen=ip_gen,
        templates=template,
        default_c_glob_info=client_info,
        default_s_glob_info=server_info,
    )


def make_udp_nat_stability_profile(target, args, bytes_per_session):
    from trex.astf.api import ASTFAssociationRule, ASTFGlobalInfo, ASTFProfile, ASTFProgram, ASTFTemplate, ASTFTCPClientTemplate, ASTFTCPServerTemplate

    is_ipv6 = target.get("ip_version") == "v6"
    ip_gen = make_ipv6_ip_gen(target) if is_ipv6 else make_ip_gen(target)
    marker = payload_marker(args.payload_direction)
    msg_payload = int(args.udp_msg_payload_bytes)
    msg_count = udp_packet_count(bytes_per_session, msg_payload)

    prog_c = ASTFProgram(stream=False, udp_mtu=int(args.udp_mtu))
    prog_s = ASTFProgram(stream=False, udp_mtu=int(args.udp_mtu))

    if args.payload_direction == "server-to-client":
        prog_c.send_msg(marker)
        prog_c.recv_msg(msg_count)
        prog_c.close_msg()

        prog_s.recv_msg(1)
        add_udp_send_messages(prog_s, bytes_per_session, msg_payload)
        prog_s.close_msg()

    elif args.payload_direction == "client-to-server":
        add_udp_send_messages(prog_c, bytes_per_session, msg_payload)
        prog_c.recv_msg(1)
        prog_c.close_msg()

        prog_s.recv_msg(msg_count)
        prog_s.send_msg(marker)
        prog_s.close_msg()

    elif args.payload_direction == "bidirectional":
        add_udp_send_messages(prog_c, bytes_per_session, msg_payload)
        prog_c.recv_msg(msg_count)
        prog_c.close_msg()

        prog_s.recv_msg(msg_count)
        add_udp_send_messages(prog_s, bytes_per_session, msg_payload)
        prog_s.close_msg()
    else:
        raise ValueError(f"unsupported payload direction: {args.payload_direction}")

    temp_c = ASTFTCPClientTemplate(
        program=prog_c,
        ip_gen=ip_gen,
        port=int(args.astf_udp_port),
        cps=float(args.cycle_cps),
        limit=int(args.cycle_sessions),
    )
    temp_s = ASTFTCPServerTemplate(
        program=prog_s,
        assoc=ASTFAssociationRule(port=int(args.astf_udp_port)),
    )
    template = ASTFTemplate(
        client_template=temp_c,
        server_template=temp_s,
        tg_name=UDP_TG_NAME,
    )
    client_info = ASTFGlobalInfo()
    server_info = ASTFGlobalInfo()
    if is_ipv6:
        enable_ipv6_global_info(client_info, server_info, target)
    return ASTFProfile(
        default_ip_gen=ip_gen,
        templates=template,
        default_c_glob_info=client_info,
        default_s_glob_info=server_info,
    )


def make_nat_stability_profile(protocol, target, args, bytes_per_session):
    if protocol == "tcp":
        return make_tcp_nat_stability_profile(target, args, bytes_per_session)
    if protocol == "udp":
        return make_udp_nat_stability_profile(target, args, bytes_per_session)
    raise ValueError(f"unsupported protocol: {protocol}")


def refresh_router_neighbors(client, target):
    ports = [target["lan_port"], target["wan_port"]]
    if target.get("ip_version") == "v6":
        return resolve_ipv6_gateway_macs(client, target, set_l2=True)
    try_arp(client, ports)
    return {}


def tcp_payload_stats_for_direction(stats, direction, requested_sessions, bytes_per_session):
    client = stats.get("traffic", {}).get("client", {}) if isinstance(stats, dict) else {}
    server = stats.get("traffic", {}).get("server", {}) if isinstance(stats, dict) else {}

    expected_one_way = float(requested_sessions) * float(bytes_per_session)
    client_rx = first_number(client, ["tcps_rcvbyte", "tcps_rcvbytes", "rcvbyte"])
    server_rx = first_number(server, ["tcps_rcvbyte", "tcps_rcvbytes", "rcvbyte"])

    if direction == "server-to-client":
        expected = expected_one_way
        actual = client_rx
    elif direction == "client-to-server":
        expected = expected_one_way
        actual = server_rx
    elif direction == "bidirectional":
        expected = expected_one_way * 2.0
        actual = client_rx + server_rx
    else:
        expected = expected_one_way
        actual = 0.0

    completion_rate = clamp_rate(actual / expected) if expected > 0 else 0.0
    shortfall = max(0.0, expected - actual)
    return {
        "expected_payload_bytes": expected,
        "actual_payload_rx_bytes": actual,
        "payload_completion_rate": completion_rate,
        "payload_shortfall_bytes": shortfall,
    }


def udp_payload_stats_for_direction(stats, direction, requested_sessions, bytes_per_session, udp_msg_payload_bytes):
    client = stats.get("traffic", {}).get("client", {}) if isinstance(stats, dict) else {}
    server = stats.get("traffic", {}).get("server", {}) if isinstance(stats, dict) else {}

    client_tx_pkt = first_number(client, ["udps_sndpkt", "udp_sndpkt", "sndpkt"])
    server_rx_pkt = first_number(server, ["udps_rcvpkt", "udp_rcvpkt", "rcvpkt"])
    server_tx_pkt = first_number(server, ["udps_sndpkt", "udp_sndpkt", "sndpkt"])
    client_rx_pkt = first_number(client, ["udps_rcvpkt", "udp_rcvpkt", "rcvpkt"])
    client_tx_byte = first_number(client, ["udps_sndbyte", "udps_sndbytes", "udp_sndbyte", "sndbyte"])
    server_rx_byte = first_number(server, ["udps_rcvbyte", "udps_rcvbytes", "udp_rcvbyte", "rcvbyte"])
    server_tx_byte = first_number(server, ["udps_sndbyte", "udps_sndbytes", "udp_sndbyte", "sndbyte"])
    client_rx_byte = first_number(client, ["udps_rcvbyte", "udps_rcvbytes", "udp_rcvbyte", "rcvbyte"])

    msg_count = udp_packet_count(bytes_per_session, udp_msg_payload_bytes)
    expected_payload_one_way = float(requested_sessions) * float(bytes_per_session)
    expected_msg_one_way = float(requested_sessions) * float(msg_count)

    if direction == "server-to-client":
        expected_payload = expected_payload_one_way
        actual_payload = client_rx_byte
        expected_msgs = expected_msg_one_way
        actual_msgs = client_rx_pkt
    elif direction == "client-to-server":
        expected_payload = expected_payload_one_way
        actual_payload = server_rx_byte
        expected_msgs = expected_msg_one_way
        actual_msgs = server_rx_pkt
    elif direction == "bidirectional":
        expected_payload = expected_payload_one_way * 2.0
        actual_payload = client_rx_byte + server_rx_byte
        expected_msgs = expected_msg_one_way * 2.0
        actual_msgs = client_rx_pkt + server_rx_pkt
    else:
        expected_payload = expected_payload_one_way
        actual_payload = 0.0
        expected_msgs = expected_msg_one_way
        actual_msgs = 0.0

    tx_packets = client_tx_pkt + server_tx_pkt
    rx_packets = client_rx_pkt + server_rx_pkt
    packet_loss = max(0.0, tx_packets - rx_packets)
    packet_loss_rate = clamp_rate(packet_loss / tx_packets) if tx_packets > 0 else 0.0
    payload_completion_rate = clamp_rate(actual_payload / expected_payload) if expected_payload > 0 else 0.0
    msg_completion_rate = clamp_rate(actual_msgs / expected_msgs) if expected_msgs > 0 else 0.0

    return {
        "expected_payload_bytes": expected_payload,
        "actual_payload_rx_bytes": actual_payload,
        "payload_completion_rate": payload_completion_rate,
        "payload_shortfall_bytes": max(0.0, expected_payload - actual_payload),
        "udp_msg_payload_bytes": int(udp_msg_payload_bytes),
        "udp_expected_rx_msgs": expected_msgs,
        "udp_actual_rx_msgs": actual_msgs,
        "udp_message_completion_rate": msg_completion_rate,
        "client_udps_sndpkt": client_tx_pkt,
        "server_udps_rcvpkt": server_rx_pkt,
        "server_udps_sndpkt": server_tx_pkt,
        "client_udps_rcvpkt": client_rx_pkt,
        "client_udps_sndbyte": client_tx_byte,
        "server_udps_rcvbyte": server_rx_byte,
        "server_udps_sndbyte": server_tx_byte,
        "client_udps_rcvbyte": client_rx_byte,
        "udp_tx_packets": tx_packets,
        "udp_rx_packets": rx_packets,
        "udp_packet_loss_packets": packet_loss,
        "udp_packet_loss_rate": packet_loss_rate,
        "packet_loss_packets": packet_loss,
        "packet_loss_rate": packet_loss_rate,
        "throughput_loss_rate": clamp_rate(max(0.0, expected_payload - actual_payload) / expected_payload) if expected_payload > 0 else 0.0,
    }


def payload_stats_for_direction(stats, protocol, direction, requested_sessions, bytes_per_session, args):
    if protocol == "tcp":
        return tcp_payload_stats_for_direction(stats, direction, requested_sessions, bytes_per_session)
    return udp_payload_stats_for_direction(
        stats,
        direction=direction,
        requested_sessions=requested_sessions,
        bytes_per_session=bytes_per_session,
        udp_msg_payload_bytes=int(args.udp_msg_payload_bytes),
    )


def udp_session_metrics(payload_metrics, requested_sessions):
    msg_completion = float(payload_metrics.get("udp_message_completion_rate") or payload_metrics.get("payload_completion_rate") or 0.0)
    completed = clamp_rate(msg_completion) * float(requested_sessions)
    failed = max(0.0, float(requested_sessions) - completed)
    return {
        "client_connects": completed,
        "server_accepts": completed,
        "established_sessions": completed,
        "failed_or_unestablished_sessions": failed,
        "session_success_rate": clamp_rate(completed / float(requested_sessions)) if requested_sessions else 0.0,
        "session_failure_rate": clamp_rate(failed / float(requested_sessions)) if requested_sessions else 0.0,
        "client_closed_sessions": completed,
        "server_closed_sessions": completed,
        "closed_sessions": completed,
        "estimated_active_sessions": 0.0,
        "active_sessions_source": "udp_payload_completion_estimate",
    }


def calculate_cycle_hard_timeout(protocol, args, target, bytes_per_session):
    if float(args.cycle_hard_timeout_sec) > 0:
        return float(args.cycle_hard_timeout_sec)

    generation_sec = float(args.cycle_sessions) / float(args.cycle_cps)
    one_way_factor = 2.0 if args.payload_direction == "bidirectional" else 1.0
    expected_bits = float(args.cycle_sessions) * float(bytes_per_session) * 8.0 * one_way_factor
    line_rate_bps = max(1.0, float(target.get("line_rate_gbps", 1.0)) * 1_000_000_000.0)
    theoretical_transfer_sec = expected_bits / line_rate_bps
    protocol_floor = 60.0 if protocol == "tcp" else 30.0
    return max(protocol_floor, generation_sec + (theoretical_transfer_sec * 4.0) + 10.0)


def is_cycle_complete(protocol, metrics, payload_metrics, elapsed, generation_sec, args, no_payload_progress_sec):
    established = float(metrics.get("established_sessions") or 0.0)
    active = float(metrics.get("estimated_active_sessions") or 0.0)
    success_rate = float(metrics.get("session_success_rate") or 0.0)
    payload_completion = float(payload_metrics.get("payload_completion_rate") or 0.0)
    udp_msg_completion = float(payload_metrics.get("udp_message_completion_rate") or payload_completion)

    generation_window_done = elapsed >= generation_sec
    generation_grace_done = elapsed >= generation_sec + float(args.generation_grace_sec)
    connect_timeout_sec = float(getattr(args, "connect_timeout_sec", 0.0) or 0.0)
    connect_timeout_done = connect_timeout_sec > 0.0 and elapsed >= generation_sec + connect_timeout_sec
    enough_established = established >= float(args.cycle_sessions)
    active_drained = active <= float(args.active_flow_complete_threshold)
    quiet_enough = no_payload_progress_sec >= float(args.completion_quiet_sec)
    payload_complete_for_established = False
    if success_rate > 0.0:
        payload_complete_for_established = payload_completion >= success_rate * float(args.payload_completion_threshold)

    if protocol == "udp":
        if generation_window_done and udp_msg_completion >= float(args.payload_completion_threshold):
            return True, "all_udp_flows_completed"
        if generation_grace_done and quiet_enough and udp_msg_completion < float(args.payload_completion_threshold):
            return True, "ended_with_udp_payload_shortfall"
        return False, "running"

    if generation_window_done and enough_established and active_drained and payload_completion >= float(args.payload_completion_threshold):
        return True, "all_tcp_sessions_completed"

    if connect_timeout_done and not enough_established and (payload_complete_for_established or quiet_enough):
        return True, "ended_with_tcp_connect_timeout"

    if generation_grace_done and active_drained and quiet_enough and success_rate < float(args.stable_success_threshold):
        return True, "ended_with_session_shortfall"

    if generation_window_done and enough_established and active_drained and quiet_enough:
        return True, "ended_with_payload_shortfall"

    return False, "running"


def is_cycle_stable(row, args):
    if row.get("cycle_timeout") in (True, "true", "True"):
        return False
    if row.get("communication_outage_suspected") in (True, "true", "True", "あり"):
        return False
    return (
        float(row.get("session_success_rate") or 0.0) >= float(args.stable_success_threshold)
        and float(row.get("packet_loss_rate") or 0.0) <= float(args.stable_loss_threshold)
        and float(row.get("throughput_loss_rate") or 0.0) <= float(args.stable_loss_threshold)
        and float(row.get("payload_completion_rate") or 0.0) >= float(args.payload_completion_threshold)
    )


def calc_metrics_for_protocol(protocol, stats, requested_cps, duration_sec, expected_sessions, payload_metrics):
    if protocol == "tcp":
        return calc_astf_metrics(
            stats,
            requested_cps=float(requested_cps),
            duration_sec=max(duration_sec, 1e-9),
            expected_sessions=int(expected_sessions),
        )
    return udp_session_metrics(payload_metrics, int(expected_sessions))


def run_one_cycle(client, profile, raw_writer, args, target, protocol, cycle_index, protocol_start_monotonic, test_start_monotonic, bytes_per_session):
    generation_sec = float(args.cycle_sessions) / float(args.cycle_cps)
    hard_timeout = calculate_cycle_hard_timeout(protocol, args, target, bytes_per_session)
    server_port = int(args.astf_tcp_port) if protocol == "tcp" else int(args.astf_udp_port)
    test_name = f"{protocol}_cycle_{cycle_index:06d}_{STABILITY_SLUG}_{int(args.cycle_sessions)}flow_{args.cycle_cps:g}cps_{args.mb_per_session:g}MB"

    client.reset()
    refresh_router_neighbors(client, target)
    client.load_profile(profile)
    client.clear_stats()

    ts_start = ts_iso()
    cycle_start = time.time()
    last_payload_actual = None
    last_payload_progress_time = cycle_start
    final_stats = {}
    final_payload_metrics = {}
    completion_reason = "timeout"
    cycle_completed = False

    client.start(mult=1, duration=float(hard_timeout), nc=False, e_duration=float(args.connect_timeout_sec), t_duration=float(args.teardown_timeout_sec))
    try:
        while True:
            now = time.time()
            elapsed = now - cycle_start
            stats = client.get_stats(skip_zero=False)
            final_stats = stats
            write_raw_stats(raw_writer, f"{STABILITY_SLUG}_{protocol}", test_name, elapsed, stats, ts_local=ts_iso())

            payload_metrics = payload_stats_for_direction(
                stats,
                protocol=protocol,
                direction=args.payload_direction,
                requested_sessions=int(args.cycle_sessions),
                bytes_per_session=int(bytes_per_session),
                args=args,
            )
            final_payload_metrics = payload_metrics
            metrics = calc_metrics_for_protocol(
                protocol=protocol,
                stats=stats,
                requested_cps=float(args.cycle_cps),
                duration_sec=max(elapsed, 1e-9),
                expected_sessions=int(args.cycle_sessions),
                payload_metrics=payload_metrics,
            )

            payload_actual = float(payload_metrics.get("actual_payload_rx_bytes") or 0.0)
            if last_payload_actual is None or payload_actual > last_payload_actual:
                last_payload_actual = payload_actual
                last_payload_progress_time = now
            no_payload_progress_sec = now - last_payload_progress_time

            completed, reason = is_cycle_complete(
                protocol=protocol,
                metrics=metrics,
                payload_metrics=payload_metrics,
                elapsed=elapsed,
                generation_sec=generation_sec,
                args=args,
                no_payload_progress_sec=no_payload_progress_sec,
            )
            if completed:
                cycle_completed = True
                completion_reason = reason
                break

            if elapsed >= hard_timeout:
                completion_reason = "timeout"
                break

            time.sleep(float(args.sample_interval_sec))
    finally:
        try:
            client.stop()
        except Exception:
            pass

    final_elapsed = time.time() - cycle_start
    final_stats = client.get_stats(skip_zero=False)
    write_raw_stats(raw_writer, f"{STABILITY_SLUG}_{protocol}", test_name, final_elapsed, final_stats, ts_local=ts_iso())

    payload_metrics = payload_stats_for_direction(
        final_stats,
        protocol=protocol,
        direction=args.payload_direction,
        requested_sessions=int(args.cycle_sessions),
        bytes_per_session=int(bytes_per_session),
        args=args,
    )
    metrics = calc_metrics_for_protocol(
        protocol=protocol,
        stats=final_stats,
        requested_cps=float(args.cycle_cps),
        duration_sec=max(final_elapsed, 1e-9),
        expected_sessions=int(args.cycle_sessions),
        payload_metrics=payload_metrics,
    )

    row = {
        "ts_local_start": ts_start,
        "ts_local_end": ts_iso(),
        "cycle_index": cycle_index,
        "protocol": protocol,
        "test_name": test_name,
        "test_elapsed_total_sec": time.time() - test_start_monotonic,
        "protocol_elapsed_sec": time.time() - protocol_start_monotonic,
        "cycle_elapsed_sec": final_elapsed,
        "cycle_hard_timeout_sec": hard_timeout,
        "connect_timeout_sec": float(args.connect_timeout_sec),
        "completion_reason": completion_reason,
        "cycle_completed": cycle_completed,
        "cycle_timeout": completion_reason == "timeout",
        "requested_cps": float(args.cycle_cps),
        "requested_sessions": int(args.cycle_sessions),
        "session_generation_duration_sec": generation_sec,
        "inter_cycle_sleep_sec": float(args.inter_cycle_sleep_sec),
        "payload_direction": args.payload_direction,
        "mb_per_session": float(args.mb_per_session),
        "bytes_per_session": int(bytes_per_session),
        "stable_success_threshold": float(args.stable_success_threshold),
        "stable_loss_threshold": float(args.stable_loss_threshold),
        "payload_completion_threshold": float(args.payload_completion_threshold),
        "client_port_min_requested": int(args.client_port_min),
        "client_port_max_requested": int(args.client_port_max),
        "client_source_port_mode": "trex_astf_internal_random_not_overridden",
        "server_port": server_port,
    }
    row.update(payload_metrics)
    row.update(metrics)
    row["cycle_stable"] = is_cycle_stable(row, args)
    return row


def percentile(values, pct):
    vals = sorted(float(v) for v in values if v not in (None, ""))
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


def make_summary_row(cycle_rows, args, test_elapsed):
    cycles = list(cycle_rows)
    total_requested = sum(float(r.get("requested_sessions") or 0.0) for r in cycles)
    total_established = sum(float(r.get("established_sessions") or 0.0) for r in cycles)
    total_expected_payload = sum(float(r.get("expected_payload_bytes") or 0.0) for r in cycles)
    total_actual_payload = sum(float(r.get("actual_payload_rx_bytes") or 0.0) for r in cycles)
    cycle_durations = [float(r.get("cycle_elapsed_sec") or 0.0) for r in cycles]
    unstable = [r for r in cycles if not r.get("cycle_stable")]
    timeout = [r for r in cycles if r.get("cycle_timeout")]

    aggregate_session_success = clamp_rate(total_established / total_requested) if total_requested > 0 else 0.0
    aggregate_payload_completion = clamp_rate(total_actual_payload / total_expected_payload) if total_expected_payload > 0 else 0.0
    max_packet_loss = max([float(r.get("packet_loss_rate") or 0.0) for r in cycles], default=0.0)
    max_throughput_loss = max([float(r.get("throughput_loss_rate") or 0.0) for r in cycles], default=0.0)

    first_unstable = unstable[0] if unstable else {}
    protocols = sorted(set(str(r.get("protocol") or "") for r in cycles if r.get("protocol")))
    per_protocol = {}
    for proto in protocols:
        proto_cycles = [r for r in cycles if r.get("protocol") == proto]
        proto_unstable = [r for r in proto_cycles if not r.get("cycle_stable")]
        per_protocol[proto] = {
            "cycles_attempted": len(proto_cycles),
            "cycles_stable": len(proto_cycles) - len(proto_unstable),
            "cycles_unstable": len(proto_unstable),
            "cycles_timeout": len([r for r in proto_cycles if r.get("cycle_timeout")]),
        }

    return {
        "phase": f"{STABILITY_SLUG}_summary",
        "test_name": f"{STABILITY_SLUG}_summary",
        "protocols": ",".join(protocols),
        "soak_duration_sec_requested_per_protocol": float(args.soak_duration_sec),
        "soak_duration_sec_actual_total": test_elapsed,
        "cycles_attempted": len(cycles),
        "cycles_stable": len(cycles) - len(unstable),
        "cycles_unstable": len(unstable),
        "cycles_timeout": len(timeout),
        "per_protocol_json": json.dumps(per_protocol, ensure_ascii=False, sort_keys=True),
        "requested_cps": float(args.cycle_cps),
        "requested_sessions_per_cycle": int(args.cycle_sessions),
        "mb_per_session": float(args.mb_per_session),
        "bytes_per_session": int(getattr(args, "bytes_per_session", resolve_bytes_per_session(args))),
        "payload_direction": args.payload_direction,
        "client_port_min_requested": int(args.client_port_min),
        "client_port_max_requested": int(args.client_port_max),
        "client_source_port_mode": "trex_astf_internal_random_not_overridden",
        "total_sessions_requested": total_requested,
        "total_sessions_established_or_completed": total_established,
        "aggregate_session_success_rate": aggregate_session_success,
        "aggregate_payload_completion_rate": aggregate_payload_completion,
        "max_packet_loss_rate": max_packet_loss,
        "max_throughput_loss_rate": max_throughput_loss,
        "cycle_duration_min_sec": min(cycle_durations) if cycle_durations else "",
        "cycle_duration_p50_sec": percentile(cycle_durations, 50),
        "cycle_duration_p95_sec": percentile(cycle_durations, 95),
        "cycle_duration_p99_sec": percentile(cycle_durations, 99),
        "cycle_duration_max_sec": max(cycle_durations) if cycle_durations else "",
        "stable_success_threshold": float(args.stable_success_threshold),
        "stable_loss_threshold": float(args.stable_loss_threshold),
        "payload_completion_threshold": float(args.payload_completion_threshold),
        "result": "PASS" if not unstable else "FAIL",
        "first_unstable_protocol": first_unstable.get("protocol", ""),
        "first_unstable_cycle_index": first_unstable.get("cycle_index", ""),
        "first_unstable_reason": first_unstable.get("completion_reason", ""),
        "first_unstable_session_success_rate": first_unstable.get("session_success_rate", ""),
        "first_unstable_payload_completion_rate": first_unstable.get("payload_completion_rate", ""),
        "first_unstable_packet_loss_rate": first_unstable.get("packet_loss_rate", ""),
        "first_unstable_throughput_loss_rate": first_unstable.get("throughput_loss_rate", ""),
    }


def write_summary_csv(path, rows):
    keys = []
    for row in rows:
        for key in row.keys():
            if key not in keys:
                keys.append(key)
    with open(path, "w", newline="") as out_file:
        writer = csv.DictWriter(out_file, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def protocol_result_paths(out_dir, protocol):
    prefix = f"{STABILITY_SLUG}_{protocol}"
    return {
        "cycles": out_dir / f"{prefix}_cycles.csv",
        "raw": out_dir / f"{prefix}_raw.csv",
        "summary": out_dir / f"summary_{protocol}.csv",
    }


def protocol_elapsed_from_rows(rows):
    vals = []
    for row in rows:
        try:
            vals.append(float(row.get("protocol_elapsed_sec") or 0.0))
        except (TypeError, ValueError):
            pass
    return max(vals, default=0.0)


def write_raw_header(raw_writer):
    raw_writer.writerow(["ts_local", "elapsed_sec", "phase", "test_name", "metric", "value"])


def write_cycle_header(cycle_writer):
    cycle_writer.writeheader()


def write_metadata(out_dir, target_name, target, args, bytes_per_session):
    metadata = {
        "created_at": ts_iso(),
        "target_name": target_name,
        "target": target,
        "args": vars(args),
        "bytes_per_session": bytes_per_session,
        "mode": METADATA_MODE,
        "client_source_port_notice": (
            "TRex ASTF public Python profile API does not expose deterministic client source-port round-robin control; "
            "client source ports are allocated by TRex internally."
        ),
        "test_pattern": {
            "protocols": list(args.protocols),
            "cycle_cps": float(args.cycle_cps),
            "cycle_sessions": int(args.cycle_sessions),
            "mb_per_session_decimal": float(args.mb_per_session),
            "payload_direction": args.payload_direction,
            "inter_cycle_sleep_sec": float(args.inter_cycle_sleep_sec),
            "connect_timeout_sec": float(args.connect_timeout_sec),
            "soak_duration_sec_per_protocol": float(args.soak_duration_sec),
            "client_port_min_requested": int(args.client_port_min),
            "client_port_max_requested": int(args.client_port_max),
        },
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def print_cycle_line(protocol, cycle_index, row, args, elapsed_after_cycle, remaining_after_cycle):
    print(
        f"{STABILITY_LABEL} proto={protocol} cycle={cycle_index} "
        f"elapsed={seconds_text(elapsed_after_cycle)} remain={seconds_text(remaining_after_cycle)} "
        f"cps={float(args.cycle_cps):g} flows={int(args.cycle_sessions)} "
        f"payload={float(args.mb_per_session):g}MB timeout={float(args.connect_timeout_sec):g}s "
        f"stable={row.get('cycle_stable')} reason={row.get('completion_reason')} "
        f"cycle={seconds_text(row.get('cycle_elapsed_sec') or 0.0)} "
        f"sess={float(row.get('session_success_rate') or 0.0):.6f} "
        f"payload={float(row.get('payload_completion_rate') or 0.0):.6f} "
        f"loss={float(row.get('packet_loss_rate') or 0.0):.6f}/{float(row.get('throughput_loss_rate') or 0.0):.6f}",
        flush=True,
    )



def run_protocol_roundtrip_preflight(
    client, raw_writer, args, target, protocol, cycle_index, attempt
):
    """Run a small protocol-matched ASTF request/reply canary before each soak cycle."""
    pre_args = argparse.Namespace(**vars(args))
    pre_args.cycle_cps = min(5.0, max(1.0, float(args.cycle_cps)))
    pre_args.cycle_sessions = min(5, max(1, int(args.cycle_sessions)))
    pre_args.mb_per_session = min(0.001, max(0.000001, float(args.mb_per_session)))
    pre_args.connect_timeout_sec = max(1.0, min(3.0, float(args.connect_timeout_sec)))
    pre_args.completion_quiet_sec = min(0.5, float(args.completion_quiet_sec))
    pre_args.generation_grace_sec = min(2.0, float(args.generation_grace_sec))
    pre_args.sample_interval_sec = min(0.25, float(args.sample_interval_sec))
    pre_args.stable_success_threshold = 0.80
    pre_args.stable_loss_threshold = 0.20
    pre_args.payload_completion_threshold = 0.80
    pre_bytes = 1200 if protocol == "udp" else 4096
    profile = make_nat_stability_profile(protocol, target, pre_args, pre_bytes)
    row = run_one_cycle(
        client=client,
        profile=profile,
        raw_writer=raw_writer,
        args=pre_args,
        target=target,
        protocol=protocol,
        cycle_index=0,
        protocol_start_monotonic=time.time(),
        test_start_monotonic=time.time(),
        bytes_per_session=pre_bytes,
    )
    result = {
        "preflight_ok": int(
            float(row.get("session_success_rate") or 0.0) >= 0.80
            and float(row.get("payload_completion_rate") or 0.0) >= 0.80
        ),
        "preflight_session_success_rate": row.get("session_success_rate", 0.0),
        "preflight_payload_completion_rate": row.get("payload_completion_rate", 0.0),
        "preflight_packet_loss_rate": row.get("packet_loss_rate", 0.0),
        "preflight_throughput_loss_rate": row.get("throughput_loss_rate", 0.0),
    }
    raw_writer.writerow([
        ts_iso(), "", f"{STABILITY_SLUG}_{protocol}_preflight",
        f"{protocol}_cycle_{cycle_index:06d}_preflight_attempt{attempt}", "summary",
        json.dumps(result, ensure_ascii=False, default=str),
    ])
    if not result["preflight_ok"]:
        raise RuntimeError(
            f"ASTF {protocol} round-trip preflight failed before cycle {cycle_index}: {result}"
        )
    return result

def run_protocol_cycles(client, raw_writer, cycle_writer, args, target, out_dir, protocol, test_start, bytes_per_session):
    protocol_start = time.time()
    rows = []
    cycle_index = 1
    preflight_elapsed_total = 0.0
    preflight = None
    while True:
        preflight_timed_out = False
        preflight_failed = False
        elapsed_total = time.time() - protocol_start - preflight_elapsed_total
        if elapsed_total >= float(args.soak_duration_sec):
            break
        try:
            if preflight is None:
                preflight_started = time.time()
                try:
                    outcome = run_consecutive_preflight(
                        lambda attempt: run_protocol_roundtrip_preflight(
                            client=client,
                            raw_writer=raw_writer,
                            args=args,
                            target=target,
                            protocol=protocol,
                            cycle_index=cycle_index,
                            attempt=attempt,
                        ),
                        phase=f"{protocol}_protocol_start",
                        csv_path=out_dir / "astf_preflight_recovery.csv",
                        required_successes=args.preflight_success_consecutive,
                        interval_sec=args.preflight_probe_interval_sec,
                        timeout_sec=args.preflight_timeout_sec,
                    )
                    preflight = dict(outcome.value)
                    preflight.update({
                        "preflight_attempts": outcome.attempts,
                        "preflight_consecutive_successes": outcome.consecutive_successes,
                        "preflight_recovery_elapsed_sec": outcome.elapsed_sec,
                    })
                finally:
                    preflight_elapsed_total += time.time() - preflight_started
            profile = make_nat_stability_profile(
                protocol, target, args, bytes_per_session
            )
            row = run_one_cycle(
                client=client,
                profile=profile,
                raw_writer=raw_writer,
                args=args,
                target=target,
                protocol=protocol,
                cycle_index=cycle_index,
                protocol_start_monotonic=protocol_start + preflight_elapsed_total,
                test_start_monotonic=test_start,
                bytes_per_session=bytes_per_session,
            )
            row.update(preflight)
        except Exception as exc:
            preflight_timed_out = isinstance(
                exc, ConsecutivePreflightTimeoutError
            )
            preflight_failed = preflight is None
            row = {
                "ts_local_start": ts_iso(),
                "ts_local_end": ts_iso(),
                "cycle_index": cycle_index,
                "protocol": protocol,
                "test_name": f"{protocol}_cycle_{cycle_index:06d}_{STABILITY_SLUG}_error",
                "test_elapsed_total_sec": time.time() - test_start,
                "protocol_elapsed_sec": time.time() - protocol_start - preflight_elapsed_total,
                "completion_reason": "exception",
                "cycle_completed": False,
                "cycle_timeout": False,
                "cycle_stable": False,
                "preflight_ok": 0,
                "requested_cps": float(args.cycle_cps),
                "requested_sessions": int(args.cycle_sessions),
                "payload_direction": args.payload_direction,
                "mb_per_session": float(args.mb_per_session),
                "bytes_per_session": bytes_per_session,
                "connect_timeout_sec": float(args.connect_timeout_sec),
                "client_port_min_requested": int(args.client_port_min),
                "client_port_max_requested": int(args.client_port_max),
                "client_source_port_mode": "trex_astf_internal_random_not_overridden",
                "error": traceback.format_exc(),
            }
            print(row["error"])

        rows.append(row)
        cycle_writer.writerow({field: row.get(field, "") for field in CYCLE_CSV_FIELDS})
        raw_writer.writerow([ts_iso(), "", f"{STABILITY_SLUG}_{protocol}", row.get("test_name", ""), "cycle_summary", json.dumps(row, ensure_ascii=False, default=str)])

        elapsed_after_cycle = time.time() - protocol_start - preflight_elapsed_total
        remaining_after_cycle = max(0.0, float(args.soak_duration_sec) - elapsed_after_cycle)
        print_cycle_line(protocol, cycle_index, row, args, elapsed_after_cycle, remaining_after_cycle)

        if preflight_timed_out or preflight_failed:
            break

        cycle_index += 1
        if elapsed_after_cycle >= float(args.soak_duration_sec):
            break
        sleep_sec = min(float(args.inter_cycle_sleep_sec), max(0.0, float(args.soak_duration_sec) - elapsed_after_cycle))
        if sleep_sec > 0:
            time.sleep(sleep_sec)
    return rows


def run_nat_stability_test(target, out_dir, args):
    add_trex_api_path()
    from trex.astf.api import ASTFClient

    ports = [target["lan_port"], target["wan_port"]]

    bytes_per_session = resolve_bytes_per_session(args)
    test_start = None

    rows = []
    protocol_results = {}
    client = ASTFClient(server="127.0.0.1")
    client.connect()
    try:
        client.reset()
        refresh_router_neighbors(client, target)
        wait_before_test_start(args.test_start_delay_sec, f"{STABILITY_LABEL} benchmark")
        test_start = time.time()

        for protocol in args.protocols:
            paths = protocol_result_paths(out_dir, protocol)
            print(f"--- {STABILITY_LABEL} protocol={protocol} start ---", flush=True)
            with open(paths["raw"], "w", newline="") as raw_file, open(paths["cycles"], "w", newline="") as cycle_file:
                raw_writer = csv.writer(raw_file)
                write_raw_header(raw_writer)
                cycle_writer = csv.DictWriter(cycle_file, fieldnames=CYCLE_CSV_FIELDS, extrasaction="ignore")
                write_cycle_header(cycle_writer)

                protocol_rows = run_protocol_cycles(
                    client=client,
                    raw_writer=raw_writer,
                    cycle_writer=cycle_writer,
                    args=args,
                    target=target,
                    out_dir=out_dir,
                    protocol=protocol,
                    test_start=test_start,
                    bytes_per_session=bytes_per_session,
                )
                raw_file.flush()
                cycle_file.flush()

            rows.extend(protocol_rows)
            protocol_results[protocol] = {
                "rows": protocol_rows,
                "paths": paths,
                "summary_row": make_summary_row(protocol_rows, args, protocol_elapsed_from_rows(protocol_rows)),
            }
            print(f"--- {STABILITY_LABEL} protocol={protocol} end cycles={len(protocol_rows)} ---", flush=True)
    finally:
        try:
            client.stop()
        except Exception:
            pass
        try:
            client.reset()
        except Exception:
            pass
        client.disconnect()

    summary_row = make_summary_row(rows, args, time.time() - test_start if test_start is not None else 0.0)
    return rows, summary_row, protocol_results


def main():
    parser = argparse.ArgumentParser(description=BENCHMARK_DESCRIPTION)
    parser.add_argument("--ip-version", choices=("v4", "v6"), default="v4", help=argparse.SUPPRESS)
    parser.add_argument("--target", choices=TARGETS.keys(), default=_NAT_DEFAULTS["target"])
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--test-start-delay-sec", type=parse_non_negative_float, default=DEFAULT_TEST_START_DELAY_SEC, help="Seconds to wait after TRex ports report link-up and before testing. Default: 15.")
    parser.add_argument("--soak-duration-sec",
                        dest="soak_duration_sec", type=parse_positive_float,
                        default=DEFAULT_SOAK_DURATION_SEC,
                        help="Wall-clock soak duration in seconds for each protocol. Default: 3600 seconds.")
    parser.add_argument("--protocols", type=parse_protocols, default=parse_protocols("tcp,udp"),
                        help="Protocols to run in order. Default: tcp,udp. Examples: tcp / udp / tcp,udp")
    parser.add_argument("--cycle-cps", "--max-cps", dest="cycle_cps", type=parse_positive_float, default=DEFAULT_CYCLE_CPS,
                        help="New ASTF flows per second in each cycle. --max-cps is accepted for compatibility with the existing ASTF CPS option. Default: 50.")
    parser.add_argument("--cycle-sessions", type=parse_positive_int, default=DEFAULT_CYCLE_SESSIONS,
                        help="ASTF flows created in each cycle. Default: 200.")
    parser.add_argument("--mb-per-session", "--payload-mb-per-session", "--payload-size-mb", dest="mb_per_session", type=parse_positive_float, default=DEFAULT_MB_PER_SESSION,
                        help="Decimal MB transferred per flow. Default: 1 MB = 1,000,000 bytes.")
    parser.add_argument("--payload-bytes-per-session", "--bytes-per-session", dest="payload_bytes_per_session",
                        type=parse_positive_int, default=None,
                        help="Payload bytes transferred per flow. Overrides --mb-per-session when specified.")
    parser.add_argument("--payload-direction", choices=["server-to-client", "client-to-server", "bidirectional"],
                        default=DEFAULT_PAYLOAD_DIRECTION,
                        help="L7 payload direction. Default: server-to-client.")
    parser.add_argument("--inter-cycle-sleep-sec", type=parse_non_negative_float, default=DEFAULT_INTER_CYCLE_SLEEP_SEC,
                        help="Sleep after a cycle has finished. Default: 1 second.")
    add_consecutive_preflight_arguments(parser)
    parser.add_argument("--cycle-hard-timeout-sec", type=parse_non_negative_float, default=_NAT_DEFAULTS["cycle_hard_timeout_sec"],
                        help="Maximum seconds per cycle. 0 means auto based on target line rate.")
    parser.add_argument("--connect-timeout-sec", "--establish-timeout-sec", dest="connect_timeout_sec",
                        type=parse_non_negative_float, default=DEFAULT_CONNECT_TIMEOUT_SEC,
                        help="Maximum seconds to wait for one TCP flow to be established. Passed to ASTFClient.start(e_duration=...). Default: 1 second. 0 disables this TRex-side establish timeout.")
    parser.add_argument("--teardown-timeout-sec", type=parse_non_negative_float, default=_NAT_DEFAULTS["teardown_timeout_sec"])
    parser.add_argument("--generation-grace-sec", type=parse_non_negative_float, default=DEFAULT_GENERATION_GRACE_SEC)
    parser.add_argument("--completion-quiet-sec", type=parse_non_negative_float, default=DEFAULT_COMPLETION_QUIET_SEC,
                        help="Seconds with no payload progress before a drained-but-short cycle is considered complete.")
    parser.add_argument("--active-flow-complete-threshold", type=parse_non_negative_float, default=_NAT_DEFAULTS["active_flow_complete_threshold"])
    parser.add_argument("--sample-interval-sec", type=parse_positive_float, default=RAW_INTERVAL_SEC)
    parser.add_argument("--stable-success-threshold", type=float, default=DEFAULT_STABLE_SUCCESS_THRESHOLD)
    parser.add_argument("--stable-loss-threshold", type=float, default=DEFAULT_STABLE_LOSS_THRESHOLD)
    parser.add_argument("--payload-completion-threshold", type=float, default=DEFAULT_PAYLOAD_COMPLETION_THRESHOLD)
    parser.add_argument("--payload-chunk-bytes", type=parse_positive_int, default=DEFAULT_CHUNK_BYTES)
    parser.add_argument("--udp-msg-payload-bytes", type=parse_positive_int, default=DEFAULT_UDP_MSG_PAYLOAD_BYTES,
                        help="UDP L7 payload bytes per UDP message. Default: 1200.")
    parser.add_argument("--udp-mtu", type=parse_positive_int, default=DEFAULT_UDP_MTU,
                        help="ASTF UDP MTU hint. Default: 1400.")
    parser.add_argument("--tcp-mss", type=parse_positive_int, default=DEFAULT_TCP_MSS)
    parser.add_argument("--tcp-window-kb", type=parse_positive_int, default=DEFAULT_TCP_WINDOW_KB)
    parser.add_argument("--astf-tcp-port", type=int, default=ASTF_TCP_PORT)
    parser.add_argument("--astf-udp-port", type=int, default=ASTF_TCP_PORT)
    parser.add_argument("--client-port-min", type=int, default=DEFAULT_CLIENT_PORT_MIN,
                        help="Requested client source-port lower bound recorded in metadata. Public ASTF API does not force this value.")
    parser.add_argument("--client-port-max", type=int, default=DEFAULT_CLIENT_PORT_MAX,
                        help="Requested client source-port upper bound recorded in metadata. Public ASTF API does not force this value.")
    parser.add_argument("--no-install-cfg", action="store_true")
    parser.add_argument("--auto-dpdk-bind", dest="auto_dpdk_bind", action="store_true", default=_NAT_DEFAULTS["auto_dpdk_bind"], help="Before starting TRex, delete stale benchmark namespaces and bind profile PCI devices to their DPDK driver. Enabled by default.")
    parser.add_argument("--no-auto-dpdk-bind", dest="auto_dpdk_bind", action="store_false", help="Do not attempt DPDK driver binding before starting TRex. Stale benchmark namespaces are still removed.")
    parser.add_argument("--dpdk-driver", default="", help="Override the DPDK driver used for all profile PCI devices, e.g. vfio-pci or igb_uio.")
    parser.add_argument("--show-progress", dest="show_progress", action="store_true", help="Accepted for compatibility; stability does not print progress bars.")
    parser.add_argument("--no-progress", dest="show_progress", action="store_false")
    parser.set_defaults(show_progress=False)
    parser.add_argument("--progress-percent-step", type=parse_positive_float, default=_NAT_DEFAULTS["progress_percent_step"])

    parser.add_argument("--interfaces", type=parse_interfaces, default=None)
    parser.add_argument("--lan-ip", default="")
    parser.add_argument("--lan-gw", default="")
    parser.add_argument("--wan-ip", default="")
    parser.add_argument("--wan-gw", default="")
    parser.add_argument("--router-wan-ip", default="")
    parser.add_argument("--line-rate-gbps", type=float, default=None)
    parser.add_argument("--cores", type=int, default=None)

    args, unknown_args = parser.parse_known_args()
    if any(argument == "--repeat" or argument.startswith("--repeat=") for argument in unknown_args):
        raise SystemExit(f"ERROR: --repeat is not supported by the {STABILITY_LABEL} benchmark")
    if unknown_args:
        print(f"WARNING: ignored unsupported options for {STABILITY_LABEL} script: {' '.join(unknown_args)}")

    update_payload_size_args(args)

    require_root()

    if args.cycle_sessions <= 0 or args.cycle_sessions > 65535:
        raise SystemExit("ERROR: --cycle-sessions must be 1..65535")
    if args.cycle_cps <= 0:
        raise SystemExit("ERROR: --cycle-cps must be positive")
    if args.astf_tcp_port <= 0 or args.astf_tcp_port > 65535:
        raise SystemExit("ERROR: --astf-tcp-port must be 1..65535")
    if args.astf_udp_port <= 0 or args.astf_udp_port > 65535:
        raise SystemExit("ERROR: --astf-udp-port must be 1..65535")
    if args.client_port_min < 1 or args.client_port_min > 65535 or args.client_port_max < 1 or args.client_port_max > 65535:
        raise SystemExit("ERROR: --client-port-min/--client-port-max must be 1..65535")
    if args.client_port_min > args.client_port_max:
        raise SystemExit("ERROR: --client-port-min must be <= --client-port-max")
    if args.stable_success_threshold < 0 or args.stable_success_threshold > 1:
        raise SystemExit("ERROR: --stable-success-threshold must be 0..1")
    if args.stable_loss_threshold < 0 or args.stable_loss_threshold > 1:
        raise SystemExit("ERROR: --stable-loss-threshold must be 0..1")
    if args.payload_completion_threshold < 0 or args.payload_completion_threshold > 1:
        raise SystemExit("ERROR: --payload-completion-threshold must be 0..1")
    if args.progress_percent_step <= 0 or args.progress_percent_step > 100:
        raise SystemExit("ERROR: --progress-percent-step must be greater than 0 and less than or equal to 100")
    validate_consecutive_preflight_arguments(args)

    target = merge_target_with_overrides(
        target_for_ip_version(TARGETS[args.target], args.ip_version),
        args,
    )
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_RESULT_ROOT / f"{DEFAULT_OUTPUT_PREFIX}_{args.target}_{ts_compact()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    details_dir = prepare_details_dir(out_dir)
    bytes_per_session = resolve_bytes_per_session(args)
    tcp_cycle_hard_timeout = calculate_cycle_hard_timeout("tcp", args, target, bytes_per_session)
    udp_cycle_hard_timeout = calculate_cycle_hard_timeout("udp", args, target, bytes_per_session)

    print("============================================================")
    print(BENCHMARK_TITLE)
    print("============================================================")
    print(f"Target              : {args.target}")
    print(f"Description         : {target['description']}")
    print(f"Interfaces          : {target['interfaces']}")
    print(f"ASTF DP cores       : {resolve_astf_cores(target)}")
    print(f"LAN/client          : port {target['lan_port']} / {target['lan_ip']} gw {target['lan_gw']}")
    print(f"WAN/server          : port {target['wan_port']} / {target['wan_ip']} gw {target['wan_gw']}")
    print(f"Router WAN IP       : {target['router_wan_ip']}")
    print(f"Protocols           : {','.join(args.protocols)}")
    print(f"ASTF TCP port       : {args.astf_tcp_port}")
    print(f"ASTF UDP port       : {args.astf_udp_port}")
    print(f"Client port request : {args.client_port_min}-{args.client_port_max} (recorded; ASTF source ports are internal)")
    print(f"Soak duration       : {args.soak_duration_sec:g}s per protocol")
    print(f"Cycle flows         : {args.cycle_sessions}")
    print(f"Cycle CPS           : {args.cycle_cps:g}")
    print(f"Generation duration : {float(args.cycle_sessions) / float(args.cycle_cps):.3f}s")
    print(f"Connect timeout     : {args.connect_timeout_sec:g}s")
    print(f"Payload per flow    : {args.mb_per_session:g} MB ({bytes_per_session} bytes)")
    print(f"Payload direction   : {args.payload_direction}")
    print(f"TCP cycle timeout   : {tcp_cycle_hard_timeout:.3f}s")
    print(f"UDP cycle timeout   : {udp_cycle_hard_timeout:.3f}s")
    print(f"UDP msg payload     : {args.udp_msg_payload_bytes} bytes")
    print(f"Inter-cycle sleep   : {args.inter_cycle_sleep_sec:g}s")
    print(f"Output              : {out_dir}")
    print("============================================================")

    prepare_log = prepare_trex_interfaces_from_profile(
        target,
        details_dir,
        auto_dpdk_bind=args.auto_dpdk_bind,
        dpdk_driver_override=args.dpdk_driver,
    )
    print(f"TRex interface prep : {prepare_log}")

    install_trex_cfg(target, details_dir, args.no_install_cfg)
    write_metadata(details_dir, args.target, target, args, bytes_per_session)

    server = TrexAstfServer(cores=resolve_astf_cores(target), out_dir=details_dir)
    try:
        try:
            server.start()
            cycle_rows, summary_row, protocol_results = run_nat_stability_test(target=target, out_dir=details_dir, args=args)
        except BaseException:
            (details_dir / "astf_nat_stability_runner_error.log").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
            raise
    finally:
        server.stop()

    summary_csv = out_dir / "summary_all.csv"
    write_summary_csv(summary_csv, [summary_row])
    for protocol in args.protocols:
        result = protocol_results.get(protocol)
        if not result:
            continue
        write_summary_csv(result["paths"]["summary"], [result["summary_row"]])

    print("============================================================")
    print("DONE")
    print("============================================================")
    print(f"Result directory : {out_dir}")
    print(f"Result           : {summary_row.get('result')}")
    print(f"Protocols        : {summary_row.get('protocols')}")
    print(f"Cycles attempted : {summary_row.get('cycles_attempted')}")
    print(f"Cycles unstable  : {summary_row.get('cycles_unstable')}")
    print(f"Summary CSV      : {summary_csv}")
    print(f"Details          : {details_dir}")
    for protocol in args.protocols:
        paths = protocol_result_paths(details_dir, protocol)
        print(f"{protocol.upper()} summary CSV : {paths['summary']}")
        print(f"{protocol.upper()} cycle CSV   : {paths['cycles']}")
        print(f"{protocol.upper()} raw CSV     : {paths['raw']}")
    print(f"Metadata         : {details_dir / 'metadata.json'}")
    print(f"TRex server log  : {details_dir / 'trex_server_astf.log'}")


if __name__ == "__main__":
    main()
