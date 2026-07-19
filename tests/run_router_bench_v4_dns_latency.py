#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPv4 router DNS forwarder/cache benchmark using a built-in dummy DNS server.

Topology:
  - LAN namespace: DNS client sends queries to the DUT LAN IP.
  - WAN namespace: dummy authoritative-like DNS server listens on the test host WAN IP.
  - The DUT/router must be configured to use the WAN-side test-host IP as its DNS server.

Measurements (execution order):
  1. cache_table_reuse_size/cache_table_size: preload N unique names at 100 QPS, then
     re-query the same N names and detect cache overflow from benchmark-name WAN re-queries.
  2. nocache_rate: unique A queries at E6 QPS steps from 10 to 10000.
  3. cache_rate: preload 100 names, then round-robin those 100 names at E6 QPS steps.
"""

import argparse
import csv
import datetime as dt
import ipaddress
import hashlib
import secrets
from collections import Counter, deque
import json
import math
import os
import random
import queue
import select
import threading
import shlex
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
DEFAULT_RESULT_ROOT = Path(os.environ.get("TREX_RESULT_ROOT", "/var/trex"))
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from tests.target_profiles import TARGETS  # type: ignore
except Exception:
    from target_profiles import TARGETS  # type: ignore

try:
    from tests.routerbench_test_start_delay import (  # type: ignore
        DEFAULT_TEST_START_DELAY_SEC,
        wait_before_test_start,
    )
except Exception:
    from routerbench_test_start_delay import (  # type: ignore
        DEFAULT_TEST_START_DELAY_SEC,
        wait_before_test_start,
    )

try:
    from tests.run_router_bench_v4_iperf3_latency import (  # type: ignore
        DEFAULT_INTERFACE_READY_TIMEOUT_SEC,
        DEFAULT_LAN_NS,
        DEFAULT_LAN_PREFIX,
        DEFAULT_POST_NAMESPACE_SETTLE_SEC,
        DEFAULT_WAN_NS,
        DEFAULT_WAN_PREFIX,
        cleanup_namespaces,
        delete_existing_namespaces,
        merge_target_with_overrides,
        ns_cmd,
        parse_non_negative_float,
        parse_positive_float,
        parse_positive_int,
        prepare_kernel_interfaces_from_profile,
        prepare_trex_interfaces_from_profile,
        require_command,
        require_profile_interfaces,
        require_root,
        resolve_profile_interfaces,
        setup_namespaces,
        ts_iso,
        wait_test_interfaces_ready,
        make_namespace_arp_refresher,
    )
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"ERROR: failed to import shared namespace helpers from L4 latency script: {exc}")

DEFAULT_DNS_PORT = 53
DEFAULT_DNS_DURATION_SEC = 10.0
DEFAULT_DNS_TIMEOUT_SEC = 1.0
DEFAULT_DNS_TTL_SEC = 300
DEFAULT_DNS_SERVER_START_DELAY_SEC = 1.0
DEFAULT_DOMAIN_SUFFIX = "routerbench.example.net"
DEFAULT_E6_STEPS = [10, 15, 22, 33, 47, 68, 100, 150, 220, 330, 470, 680, 1000, 1500, 2200, 3300, 4700, 6800, 10000]
DEFAULT_CACHE_E6_STEPS = [10, 15, 22, 33, 47, 68, 100, 150, 220, 330, 470, 680, 1000, 1500, 2200, 3300, 4700, 6800, 10000, 15000, 22000, 33000, 48000]
DEFAULT_CACHE_PRELOAD_COUNT = 100
DEFAULT_TABLE_QPS = 100
DEFAULT_STOP_FAILURE_RATE_PCT = 50.0
DEFAULT_STOP_RESPONSE_MS = 1000.0
DEFAULT_TABLE_REUSE_INTERVAL_SEC = 600.0
DEFAULT_TABLE_OVERFLOW_RATE_PCT = 50.0
DEFAULT_DNS_SERVER_LOG_BATCH_SIZE = 100000  # retained for CLI compatibility
DEFAULT_DNS_SERVER_LOG_FLUSH_SEC = 1.0      # stats sidecar update interval
DEFAULT_DNS_SOCKET_BUFFER_BYTES = 4 * 1024 * 1024
DEFAULT_DNS_SAMPLE_LIMIT = -1
DEFAULT_DNS_SERVER_QUERY_LOG_LIMIT = 1000   # maximum server-side failure rows; successful queries are never logged
DEFAULT_NOCACHE_LOAD_DURATION_SEC = 3600.0
DEFAULT_DNS_LOAD_PROGRESS_INTERVAL_SEC = 10.0
DEFAULT_DNS_LOAD_RTT_SAMPLE_LIMIT = 100000

_ARP_REFRESHER = None
_DNS_SERVER_QUERY_LOG = None
_DNS_RUN_LABEL = None
_DNS_PREFLIGHT_COUNTER = 0

SAMPLE_FIELDS = [
    "phase", "stage", "step_value", "sample_index", "qname", "qtype", "server_ip", "server_port",
    "unix_ts_start", "unix_ts_end", "rtt_ms", "rcode", "answers", "answer_ips", "expected_answer_ip",
    "answer_match", "result", "error",
]

SUMMARY_FIELDS = [
    "phase", "stage", "step_value", "test_name", "result", "target_name", "lan_ns", "wan_ns",
    "lan_iface", "wan_iface", "lan_ip", "lan_gw", "wan_ip", "router_wan_ip", "dns_client_server_ip",
    "dns_wan_server_ip", "dns_port", "dns_ttl_sec", "duration_sec", "target_qps", "actual_qps",
    "samples_requested", "samples_sent", "samples_received", "responses_received_total", "timeout_count",
    "error_count", "wrong_answer_count", "rcode_noerror_count", "rcode_formerr_count",
    "rcode_servfail_count", "rcode_nxdomain_count", "rcode_notimp_count", "rcode_refused_count",
    "rcode_other_count",
    "success_rate_pct", "failure_rate_pct", "fill_rate_pct", "rtt_min_ms", "rtt_avg_ms", "rtt_p50_ms",
    "rtt_p90_ms", "rtt_p95_ms", "rtt_p99_ms", "rtt_max_ms", "wan_queries_before", "wan_queries_after",
    "wan_queries_delta", "wan_queries_delta_raw", "preflight_wan_queries", "preflight_ok",
    "preflight_samples_requested", "preflight_samples_received", "preflight_wrong_answer_count",
    "preflight_expected_ip", "preflight_observed_ips", "preflight_wan_qname_seen",
    "cache_preload_count", "cache_table_size", "cache_overflow_detected",
    "cache_overflow_rate_pct", "stop_reason", "error",
]


def ts_compact():
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def mean(values):
    return sum(values) / len(values) if values else ""


def percentile(values, pct):
    if not values:
        return ""
    data = sorted(values)
    if len(data) == 1:
        return data[0]
    pos = (len(data) - 1) * (pct / 100.0)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return data[lo]
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def parse_e6_steps(text):
    if not text:
        return list(DEFAULT_E6_STEPS)
    out = []
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        value = int(part)
        if value <= 0:
            raise argparse.ArgumentTypeError("step values must be positive")
        out.append(value)
    if not out:
        raise argparse.ArgumentTypeError("at least one step value is required")
    return out


def encode_qname(name):
    parts = name.rstrip(".").split(".")
    out = bytearray()
    for part in parts:
        raw = part.encode("ascii")
        if len(raw) > 63:
            raise ValueError(f"DNS label too long: {part}")
        out.append(len(raw))
        out.extend(raw)
    out.append(0)
    return bytes(out)


def decode_qname(packet, offset):
    labels = []
    jumped = False
    original_offset = offset
    seen = set()
    while True:
        if offset >= len(packet):
            raise ValueError("qname exceeds packet length")
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise ValueError("truncated compression pointer")
            ptr = ((length & 0x3F) << 8) | packet[offset + 1]
            if ptr in seen:
                raise ValueError("compression pointer loop")
            seen.add(ptr)
            if not jumped:
                original_offset = offset + 2
            offset = ptr
            jumped = True
            continue
        offset += 1
        if length == 0:
            break
        if offset + length > len(packet):
            raise ValueError("truncated label")
        labels.append(packet[offset:offset + length].decode("ascii", errors="replace"))
        offset += length
    return ".".join(labels).lower(), (original_offset if jumped else offset)


def build_dns_query(qname, qid):
    return build_dns_query_from_question(encode_qname(qname) + struct.pack("!HH", 1, 1), qid)


def build_dns_query_from_question(question, qid):
    header = struct.pack("!HHHHHH", int(qid) & 0xFFFF, 0x0100, 1, 0, 0, 0)
    return header + question


def skip_dns_name(packet, offset):
    """Return the offset immediately after a DNS name without allocating label strings."""
    seen = set()
    while True:
        if offset >= len(packet):
            raise ValueError("DNS name exceeds packet length")
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise ValueError("truncated compression pointer")
            ptr = ((length & 0x3F) << 8) | packet[offset + 1]
            if ptr in seen:
                raise ValueError("compression pointer loop")
            return offset + 2
        if length & 0xC0:
            raise ValueError("invalid DNS label length")
        offset += 1
        if length == 0:
            return offset
        offset += length
        if offset > len(packet):
            raise ValueError("truncated DNS label")


def fast_parse_dns_response(packet, expected_qid=None):
    """Parse DNS header and A records while avoiding qname string decoding in the hot loop."""
    if len(packet) < 12:
        raise ValueError("short DNS response")
    qid, flags, qdcount, ancount, nscount, arcount = struct.unpack("!HHHHHH", packet[:12])
    if expected_qid is not None and qid != expected_qid:
        raise ValueError(f"unexpected DNS transaction ID: got={qid} expected={expected_qid}")
    if not (flags & 0x8000):
        raise ValueError("packet is not a DNS response")
    offset = 12
    for _ in range(qdcount):
        offset = skip_dns_name(packet, offset)
        if offset + 4 > len(packet):
            raise ValueError("truncated DNS question")
        offset += 4
    answer_ips = []
    total_rrs = int(ancount) + int(nscount) + int(arcount)
    for rr_index in range(total_rrs):
        offset = skip_dns_name(packet, offset)
        if offset + 10 > len(packet):
            raise ValueError("truncated RR header")
        rtype, rclass, _ttl, rdlen = struct.unpack("!HHIH", packet[offset:offset + 10])
        offset += 10
        if offset + rdlen > len(packet):
            raise ValueError("truncated RR data")
        if rr_index < ancount and rtype == 1 and rclass == 1 and rdlen == 4:
            answer_ips.append(str(ipaddress.IPv4Address(packet[offset:offset + 4])))
        offset += rdlen
    return {"rcode": flags & 0x000F, "answers": ancount, "answer_ips": answer_ips}


def parse_dns_response(packet, expected_qid=None):
    if len(packet) < 12:
        raise ValueError("short DNS response")
    qid, flags, qdcount, ancount, _nscount, _arcount = struct.unpack("!HHHHHH", packet[:12])
    if expected_qid is not None and qid != expected_qid:
        raise ValueError(f"unexpected DNS transaction ID: got={qid} expected={expected_qid}")
    rcode = flags & 0x000F
    offset = 12
    for _ in range(qdcount):
        _name, offset = decode_qname(packet, offset)
        offset += 4
    answer_ips = []
    for _ in range(ancount):
        _name, offset = decode_qname(packet, offset)
        if offset + 10 > len(packet):
            raise ValueError("truncated RR header")
        rtype, rclass, _ttl, rdlen = struct.unpack("!HHIH", packet[offset:offset + 10])
        offset += 10
        rdata = packet[offset:offset + rdlen]
        offset += rdlen
        if rtype == 1 and rclass == 1 and rdlen == 4:
            answer_ips.append(str(ipaddress.IPv4Address(rdata)))
    return {"rcode": rcode, "answers": ancount, "answer_ips": answer_ips}


PUBLIC_ANSWER_PREFIXES = (
    (8, 0, 0, 1),
    (9, 0, 0, 1),
    (13, 0, 0, 1),
    (18, 0, 0, 1),
    (20, 0, 0, 1),
    (23, 0, 0, 1),
    (34, 0, 0, 1),
    (35, 0, 0, 1),
    (40, 0, 0, 1),
    (44, 0, 0, 1),
    (52, 0, 0, 1),
    (54, 0, 0, 1),
    (93, 184, 0, 1),
)


def ip_for_index(base_ip, index):
    return str(ipaddress.IPv4Address((int(ipaddress.IPv4Address(base_ip)) + int(index)) & 0xFFFFFFFF))


def random_public_ip_for_qname(qname, seed):
    # Deterministic pseudo-random answer. The result intentionally avoids RFC1918,
    # link-local, loopback, multicast, documentation, and carrier-grade NAT ranges
    # because many router DNS forwarders filter those answers as DNS rebind risks.
    digest = hashlib.blake2s((str(seed) + "|" + qname.lower().rstrip(".")).encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    a, b, c0, d0 = PUBLIC_ANSWER_PREFIXES[value % len(PUBLIC_ANSWER_PREFIXES)]
    c = (c0 + ((value >> 8) & 0xFF)) & 0xFF
    d = (d0 + (value & 0xFE)) & 0xFF
    if d == 0 or d == 255:
        d = 1
    return f"{a}.{b}.{c}.{d}"


def answer_ip_for_qname(qname, suffix, base_ip, max_records, mode="random", seed=0):
    qname = qname.lower().rstrip(".")
    suffix = suffix.lower().rstrip(".")
    if not qname.endswith("." + suffix):
        return ""
    left = qname[:-(len(suffix) + 1)]
    # The first label contains the record index.  Additional labels identify the
    # benchmark run and preflight step, making every cache key unique across runs.
    record_label = left.split(".", 1)[0]
    if not record_label.startswith("r"):
        return ""
    digits = record_label[1:]
    if not digits.isdigit():
        return ""
    idx = int(digits)
    if idx < 0 or idx >= int(max_records):
        return ""
    if str(mode).lower() == "fixed":
        return ip_for_index(base_ip, idx)
    return random_public_ip_for_qname(qname, seed)


def dns_answer_matches(expected_ip, answer_ips):
    expected = str(expected_ip or "").strip()
    observed = [str(value).strip() for value in (answer_ips or []) if str(value).strip()]
    return bool(expected) and observed == [expected]


def rcode_summary_fields(rcode_counts):
    counts = Counter({int(key): int(value) for key, value in dict(rcode_counts or {}).items()})
    known = {0, 1, 2, 3, 4, 5}
    return {
        "rcode_counts": {str(key): counts[key] for key in sorted(counts)},
        "rcode_noerror_count": counts[0],
        "rcode_formerr_count": counts[1],
        "rcode_servfail_count": counts[2],
        "rcode_nxdomain_count": counts[3],
        "rcode_notimp_count": counts[4],
        "rcode_refused_count": counts[5],
        "rcode_other_count": sum(value for key, value in counts.items() if key not in known),
    }


def build_dns_response(query, suffix, base_ip, max_records, ttl, answer_mode="random", random_seed=0):
    if len(query) < 12:
        return b""
    qid, _flags, qdcount, _ancount, _nscount, _arcount = struct.unpack("!HHHHHH", query[:12])
    if qdcount < 1:
        return struct.pack("!HHHHHH", qid, 0x8181, 0, 0, 0, 0)
    try:
        qname, offset = decode_qname(query, 12)
        if offset + 4 > len(query):
            raise ValueError("truncated question")
        qtype, qclass = struct.unpack("!HH", query[offset:offset + 4])
        question = query[12:offset + 4]
    except Exception:
        return struct.pack("!HHHHHH", qid, 0x8181, 0, 0, 0, 0)
    answer_ip = answer_ip_for_qname(qname, suffix, base_ip, max_records, answer_mode, random_seed)
    if qtype == 1 and qclass == 1 and answer_ip:
        flags = 0x8180
        answer = b"\xC0\x0C" + struct.pack("!HHIH", 1, 1, int(ttl), 4) + socket.inet_aton(answer_ip)
        return struct.pack("!HHHHHH", qid, flags, 1, 1, 0, 0) + question + answer
    return struct.pack("!HHHHHH", qid, 0x8183, 1, 0, 0, 0) + question


def make_qnames(start, count, suffix, run_label=None):
    label = str(run_label or _DNS_RUN_LABEL or "").strip(".").lower()
    middle = f".{label}" if label else ""
    return [f"r{idx:09d}{middle}.{suffix}".lower().rstrip(".") for idx in range(int(start), int(start) + int(count))]


def make_preflight_qname(max_records, suffix):
    global _DNS_PREFLIGHT_COUNTER
    _DNS_PREFLIGHT_COUNTER += 1
    label = str(_DNS_RUN_LABEL or "run").strip(".").lower()
    return f"r{int(max_records) - 1:09d}.pf{_DNS_PREFLIGHT_COUNTER:06d}.{label}.{suffix}".lower().rstrip(".")




def run_internal_dns_server(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--bind-ip", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--domain-suffix", required=True)
    parser.add_argument("--answer-base-ip", required=True)
    parser.add_argument("--answer-mode", choices=["random", "fixed"], default="random")
    parser.add_argument("--answer-random-seed", type=int, default=0)
    parser.add_argument("--max-records", type=int, required=True)
    parser.add_argument("--ttl-sec", type=int, required=True)
    parser.add_argument("--query-log", required=True)
    parser.add_argument("--stats-file", default="")
    parser.add_argument("--query-log-limit", type=int, default=DEFAULT_DNS_SERVER_QUERY_LOG_LIMIT)
    parser.add_argument("--log-batch-size", type=int, default=DEFAULT_DNS_SERVER_LOG_BATCH_SIZE)
    parser.add_argument("--log-flush-sec", type=float, default=DEFAULT_DNS_SERVER_LOG_FLUSH_SEC)
    parser.add_argument("--socket-buffer-bytes", type=int, default=DEFAULT_DNS_SOCKET_BUFFER_BYTES)
    args = parser.parse_args(argv)

    stop_event = threading.Event()
    stats_file = Path(args.stats_file) if args.stats_file else Path(str(args.query_log) + ".stats.json")
    total_received_all = [0]
    total_received_benchmark = [0]
    total_server_failures = [0]
    failure_log_dropped = [0]
    recent_benchmark_qnames = deque(maxlen=2048)
    # Successful WAN queries are counted only in the sidecar JSON and are never written
    # to CSV.  Only server-side failures are retained, with a bounded row count and
    # minimal columns, so high-QPS tests do not consume large amounts of memory or disk.
    failure_rows = []

    def handle_stop_signal(_signum, _frame):
        stop_event.set()

    try:
        signal.signal(signal.SIGTERM, handle_stop_signal)
        signal.signal(signal.SIGINT, handle_stop_signal)
    except Exception:
        pass

    def write_stats():
        try:
            tmp = stats_file.with_suffix(stats_file.suffix + ".tmp")
            all_queries = int(total_received_all[0])
            benchmark_queries = int(total_received_benchmark[0])
            tmp.write_text(
                json.dumps({
                    # total_queries remains the benchmark-only counter for compatibility
                    # with existing readers.  all_queries exposes unrelated/background
                    # DNS traffic for diagnostics without contaminating table-size results.
                    "total_queries": benchmark_queries,
                    "benchmark_queries": benchmark_queries,
                    "all_queries": all_queries,
                    "ignored_queries": max(0, all_queries - benchmark_queries),
                    "answered_queries": max(0, all_queries - int(total_server_failures[0])),
                    "server_failure_count": int(total_server_failures[0]),
                    "failure_log_rows": len(failure_rows),
                    "failure_log_dropped": int(failure_log_dropped[0]),
                    "recent_benchmark_qnames": list(recent_benchmark_qnames),
                    "unix_ts": time.time(),
                }),
                encoding="utf-8",
            )
            tmp.replace(stats_file)
        except Exception:
            pass

    def stats_thread_main():
        while not stop_event.is_set():
            write_stats()
            time.sleep(max(0.02, float(args.log_flush_sec)))
        write_stats()

    def write_failure_log_at_end():
        if not failure_rows:
            return
        with open(args.query_log, "a", newline="", encoding="utf-8", buffering=1024 * 1024) as f:
            writer = csv.writer(f)
            writer.writerows(failure_rows)

    stats_thread = threading.Thread(target=stats_thread_main, name="dns-query-stats", daemon=True)
    stats_thread.start()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, int(args.socket_buffer_bytes))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, int(args.socket_buffer_bytes))
    except Exception:
        pass
    sock.bind((args.bind_ip, args.port))
    sock.settimeout(0.2)

    try:
        while not stop_event.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            received = time.time()
            qname = ""
            qtype = ""
            status = "error"
            try:
                qname, offset = decode_qname(data, 12)
                if offset + 4 <= len(data):
                    qtype, _qclass = struct.unpack("!HH", data[offset:offset + 4])
                response = build_dns_response(data, args.domain_suffix, args.answer_base_ip, args.max_records, args.ttl_sec, args.answer_mode, args.answer_random_seed)
                if response:
                    sock.sendto(response, addr)
                    status = "answered"
                else:
                    status = "error"
            except Exception as exc:
                status = f"error:{exc}"
            total_received_all[0] += 1
            # Only generated router-benchmark names participate in WAN miss and
            # cache-overflow calculations.  Routers may send their own NTP/vendor/
            # connectivity-check DNS traffic to the configured upstream server;
            # counting that traffic caused small table steps to terminate falsely.
            if answer_ip_for_qname(
                qname, args.domain_suffix, args.answer_base_ip, args.max_records,
                args.answer_mode, args.answer_random_seed,
            ):
                total_received_benchmark[0] += 1
                recent_benchmark_qnames.append(str(qname).lower().rstrip("."))
            if status != "answered":
                total_server_failures[0] += 1
                limit = int(args.query_log_limit)
                if limit < 0 or len(failure_rows) < limit:
                    failure_rows.append([
                        f"{received:.6f}",
                        str(qname or "")[:255],
                        str(status or "error")[:160],
                    ])
                else:
                    failure_log_dropped[0] += 1
    finally:
        stop_event.set()
        try:
            write_stats()
            write_failure_log_at_end()
            stats_thread.join(timeout=1.0)
        except Exception:
            pass


def run_dns_load_client_streaming(server_ip, port, qnames, target_qps, timeout_sec, duration_sec, total_count, phase, stage, step_value, samples_csv, expected_answer_ips=None, sample_limit=DEFAULT_DNS_SAMPLE_LIMIT, socket_buffer_bytes=DEFAULT_DNS_SOCKET_BUFFER_BYTES, progress_interval_sec=0.0):
    """Run a long DNS load without per-request arrays.

    This path is intended for nocache_load.  It keeps only the active QID map,
    aggregate counters, and a bounded RTT sample for percentile calculation.
    """
    server = (server_ip, int(port))
    target_qps = float(target_qps)
    timeout_sec = float(timeout_sec)
    progress_interval_sec = float(progress_interval_sec or 0.0)
    if total_count is None or int(total_count) <= 0:
        total = max(1, int(round(target_qps * float(duration_sec))))
    else:
        total = int(total_count)
    if not qnames:
        raise ValueError("qnames must not be empty")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, int(socket_buffer_bytes))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, int(socket_buffer_bytes))
    except Exception:
        pass

    questions = [encode_qname(qn) + struct.pack("!HH", 1, 1) for qn in qnames]
    qname_count = len(qnames)
    expected_answer_ips = list(expected_answer_ips or [])
    if len(expected_answer_ips) != qname_count or any(not str(ip or "").strip() for ip in expected_answer_ips):
        raise ValueError("expected_answer_ips must contain one non-empty A record for every qname")
    samples_path = Path(samples_csv) if samples_csv else None
    sample_limit = int(sample_limit)

    sample_rows = []
    rows_written = 0

    def keep_row(is_failure=False):
        nonlocal rows_written
        if not samples_path or sample_limit == 0:
            return False
        if sample_limit < 0:
            rows_written += 1
            return True
        if rows_written < sample_limit or is_failure:
            rows_written += 1
            return True
        return False

    sent = 0
    send_error_count = 0
    response_error_count = 0
    timeout_count = 0
    received_ok = 0
    received_total = 0
    wrong_answer_count = 0
    rcode_counts = Counter()
    observed_answer_ips = set()
    outstanding = {}
    next_qid = 1
    start_perf = time.perf_counter()
    start_unix = time.time()
    next_send = start_perf
    last_send = start_perf
    next_progress = start_perf + progress_interval_sec if progress_interval_sec > 0 else 0.0

    rtt_min = None
    rtt_max = None
    rtt_sum = 0.0
    rtt_count = 0
    rtt_samples = []
    rtt_sample_limit = DEFAULT_DNS_LOAD_RTT_SAMPLE_LIMIT

    def record_rtt(value_ms):
        nonlocal rtt_min, rtt_max, rtt_sum, rtt_count
        rtt_count += 1
        rtt_sum += value_ms
        rtt_min = value_ms if rtt_min is None else min(rtt_min, value_ms)
        rtt_max = value_ms if rtt_max is None else max(rtt_max, value_ms)
        if len(rtt_samples) < rtt_sample_limit:
            rtt_samples.append(value_ms)

    def progress_line(now, final=False):
        elapsed = max(0.000001, now - start_perf)
        send_pct = min(100.0, sent / total * 100.0) if total else 0.0
        success_pct = (received_ok / sent * 100.0) if sent else 0.0
        actual_qps = received_ok / elapsed if elapsed > 0 else 0.0
        status = "done" if final else "running"
        return (
            f"DNS nocache_load progress: status={status} elapsed={elapsed:.1f}s/{float(duration_sec):.1f}s "
            f"send={send_pct:.1f}% target_qps={target_qps:.0f} actual_qps={actual_qps:.1f} "
            f"sent={sent}/{total} received={received_ok} success={success_pct:.3f}% "
            f"timeouts={timeout_count} errors={send_error_count + response_error_count} inflight={len(outstanding)}"
        )

    try:
        while True:
            now = time.perf_counter()
            while sent < total and now >= next_send:
                idx = sent % qname_count
                qid = next_qid
                next_qid = (next_qid % 65535) + 1
                if qid in outstanding:
                    # QID wrap while the older request is still unanswered.  Count the older
                    # request as timed out and replace it so the map remains bounded.
                    timeout_count += 1
                    outstanding.pop(qid, None)
                packet = build_dns_query_from_question(questions[idx], qid)
                t0 = time.perf_counter()
                try:
                    sock.sendto(packet, server)
                    outstanding[qid] = (sent, t0, idx)
                    last_send = t0
                except Exception as exc:
                    send_error_count += 1
                    if keep_row(is_failure=True):
                        sample_rows.append({
                            "phase": phase, "stage": stage, "step_value": step_value,
                            "sample_index": sent, "qname": qnames[idx], "qtype": "A",
                            "server_ip": server_ip, "server_port": port,
                            "unix_ts_start": start_unix + (t0 - start_perf), "unix_ts_end": "",
                            "rtt_ms": "", "rcode": "", "answers": "", "answer_ips": "",
                            "expected_answer_ip": expected_answer_ips[idx], "answer_match": 0,
                            "result": "FAIL", "error": str(exc) or "send_error",
                        })
                sent += 1
                next_send = start_perf + sent / target_qps
                now = time.perf_counter()

            while True:
                try:
                    data, _addr = sock.recvfrom(4096)
                except BlockingIOError:
                    break
                t1 = time.perf_counter()
                if len(data) < 12:
                    continue
                try:
                    qid = struct.unpack("!H", data[:2])[0]
                    item = outstanding.pop(qid, None)
                    if item is None:
                        continue
                    sample_index, t0, qname_idx = item
                    parsed = fast_parse_dns_response(data, expected_qid=qid)
                    rcode = int(parsed.get("rcode", -1))
                    answers = int(parsed.get("answers", 0))
                    answer_ips = list(parsed.get("answer_ips") or [])
                    expected_ip = str(expected_answer_ips[qname_idx % qname_count])
                    received_total += 1
                    rcode_counts[rcode] += 1
                    observed_answer_ips.update(answer_ips)
                    rtt_ms = (t1 - t0) * 1000.0
                    answer_match = dns_answer_matches(expected_ip, answer_ips)
                    ok = rcode == 0 and answers > 0 and answer_match
                    if ok:
                        received_ok += 1
                        record_rtt(rtt_ms)
                    else:
                        response_error_count += 1
                        if rcode == 0 and answers > 0 and not answer_match:
                            wrong_answer_count += 1
                    if keep_row(is_failure=(not ok)):
                        error = ""
                        if not ok:
                            error = f"rcode={rcode} answers={answers} expected={expected_ip} observed={';'.join(answer_ips) or '-'}"
                        sample_rows.append({
                            "phase": phase, "stage": stage, "step_value": step_value,
                            "sample_index": sample_index, "qname": qnames[qname_idx % qname_count], "qtype": "A",
                            "server_ip": server_ip, "server_port": port,
                            "unix_ts_start": start_unix + (t0 - start_perf),
                            "unix_ts_end": start_unix + (t1 - start_perf),
                            "rtt_ms": rtt_ms, "rcode": rcode, "answers": answers,
                            "answer_ips": ";".join(answer_ips), "expected_answer_ip": expected_ip,
                            "answer_match": 1 if answer_match else 0,
                            "result": "PASS" if ok else "FAIL", "error": error,
                        })
                except Exception as exc:
                    response_error_count += 1
                    if keep_row(is_failure=True):
                        sample_rows.append({
                            "phase": phase, "stage": stage, "step_value": step_value,
                            "sample_index": "", "qname": "", "qtype": "A",
                            "server_ip": server_ip, "server_port": port,
                            "unix_ts_start": "", "unix_ts_end": start_unix + (t1 - start_perf),
                            "rtt_ms": "", "rcode": "", "answers": "", "answer_ips": "",
                            "expected_answer_ip": "", "answer_match": 0,
                            "result": "FAIL", "error": str(exc),
                        })

            now = time.perf_counter()
            if progress_interval_sec > 0 and now >= next_progress:
                print(progress_line(now), file=sys.stderr, flush=True)
                while next_progress <= now:
                    next_progress += progress_interval_sec

            if sent >= total:
                if not outstanding:
                    break
                if time.perf_counter() >= last_send + timeout_sec:
                    break

            sleep_until = next_send if sent < total else (last_send + timeout_sec)
            wait = max(0.0, min(0.001, sleep_until - time.perf_counter()))
            if wait > 0:
                r, _w, _x = select.select([sock], [], [], wait)
                if r:
                    continue
            else:
                time.sleep(0)
    finally:
        sock.close()

    timeout_count += len(outstanding)
    elapsed = time.perf_counter() - start_perf
    if progress_interval_sec > 0:
        print(progress_line(time.perf_counter(), final=True), file=sys.stderr, flush=True)

    if samples_path:
        with open(samples_path, "w", newline="", encoding="utf-8", buffering=1024 * 1024) as sample_file:
            writer = csv.DictWriter(sample_file, fieldnames=SAMPLE_FIELDS, extrasaction="ignore")
            writer.writeheader()
            if sample_rows:
                writer.writerows(sample_rows)

    requested = total
    failures = requested - received_ok
    success_rate = (received_ok / requested * 100.0) if requested else 0.0
    failure_rate = (failures / requested * 100.0) if requested else 0.0
    return {
        "phase": phase,
        "stage": stage,
        "step_value": step_value,
        "target_qps": target_qps,
        "actual_qps": received_ok / elapsed if elapsed > 0 else 0.0,
        "duration_sec": elapsed,
        "samples_requested": requested,
        "samples_sent": sent,
        "samples_received": received_ok,
        "responses_received_total": received_total,
        "timeout_count": timeout_count,
        "error_count": send_error_count + response_error_count,
        "wrong_answer_count": wrong_answer_count,
        "observed_answer_ips": sorted(observed_answer_ips),
        **rcode_summary_fields(rcode_counts),
        "success_rate_pct": success_rate,
        "failure_rate_pct": failure_rate,
        "fill_rate_pct": failure_rate,
        "rtt_min_ms": rtt_min if rtt_min is not None else "",
        "rtt_avg_ms": (rtt_sum / rtt_count) if rtt_count else "",
        "rtt_p50_ms": percentile(rtt_samples, 50),
        "rtt_p90_ms": percentile(rtt_samples, 90),
        "rtt_p95_ms": percentile(rtt_samples, 95),
        "rtt_p99_ms": percentile(rtt_samples, 99),
        "rtt_max_ms": rtt_max if rtt_max is not None else "",
        "sample_rows_written": rows_written,
        "post_aggregation": 0,
        "streaming_aggregation": 1,
        "rtt_percentile_sample_size": len(rtt_samples),
    }


def run_dns_load_client(server_ip, port, qnames, target_qps, timeout_sec, duration_sec, total_count, phase, stage, step_value, samples_csv, expected_answer_ips=None, sample_limit=DEFAULT_DNS_SAMPLE_LIMIT, socket_buffer_bytes=DEFAULT_DNS_SOCKET_BUFFER_BYTES, progress_interval_sec=0.0):
    """Run the LAN-side DNS load generator.

    High-QPS behavior is intentionally split into two phases:
      1. timed measurement: send/receive only, store raw timestamps and tiny status fields
      2. post aggregation: calculate rates, latency percentiles, failures, and write CSV

    The previous implementation expired timeouts inside the hot loop by scanning all
    outstanding queries.  At 6.8k/10k QPS that scan becomes expensive because the
    outstanding map grows to thousands of entries.  This version does not classify
    timeouts during the timed section.  It stops receiving after the last-send timeout
    window, then classifies any not-received queries as timeouts in the post phase.
    """
    if str(phase) == "nocache_load":
        return run_dns_load_client_streaming(
            server_ip, port, qnames, target_qps, timeout_sec, duration_sec, total_count,
            phase, stage, step_value, samples_csv, expected_answer_ips=expected_answer_ips,
            sample_limit=sample_limit, socket_buffer_bytes=socket_buffer_bytes,
            progress_interval_sec=progress_interval_sec,
        )

    server = (server_ip, int(port))
    target_qps = float(target_qps)
    timeout_sec = float(timeout_sec)
    if total_count is None or int(total_count) <= 0:
        total = max(1, int(round(target_qps * float(duration_sec))))
    else:
        total = int(total_count)
    if not qnames:
        raise ValueError("qnames must not be empty")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, int(socket_buffer_bytes))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, int(socket_buffer_bytes))
    except Exception:
        pass

    questions = [encode_qname(qn) + struct.pack("!HH", 1, 1) for qn in qnames]
    qname_count = len(qnames)
    expected_answer_ips = list(expected_answer_ips or [])
    if len(expected_answer_ips) != qname_count or any(not str(ip or "").strip() for ip in expected_answer_ips):
        raise ValueError("expected_answer_ips must contain one non-empty A record for every qname")

    samples_path = Path(samples_csv) if samples_csv else None
    sample_limit = int(sample_limit)

    # Raw per-query state.  These arrays are filled during measurement and interpreted
    # after the step ends.  Keeping aggregation out of the hot loop reduces CPU spikes.
    send_times = [0.0] * total
    recv_times = [0.0] * total
    qname_indices = [0] * total
    rcodes = [-1] * total
    answer_counts = [0] * total
    answer_ip_values = [""] * total
    error_messages = [""] * total

    sent = 0
    send_error_count = 0
    response_error_count = 0
    outstanding = {}
    next_qid = 1
    start_perf = time.perf_counter()
    start_unix = time.time()
    next_send = start_perf
    last_send = start_perf
    received_total = 0

    try:
        while True:
            now = time.perf_counter()

            # Send due packets.  This loop contains no statistics calculations and no CSV
            # row construction.  It only records the raw send timestamp and qname index.
            while sent < total and now >= next_send:
                idx = sent % qname_count
                qid = next_qid
                next_qid = (next_qid % 65535) + 1
                packet = build_dns_query_from_question(questions[idx], qid)
                t0 = time.perf_counter()
                send_times[sent] = t0
                qname_indices[sent] = idx
                try:
                    sock.sendto(packet, server)
                    outstanding[qid] = sent
                    last_send = t0
                except Exception as exc:
                    send_error_count += 1
                    rcodes[sent] = -2
                    error_messages[sent] = str(exc)
                sent += 1
                next_send = start_perf + sent / target_qps
                now = time.perf_counter()

            # Receive as many responses as are currently queued.  Response handling only
            # records tiny raw fields; percentile and success/failure handling happens later.
            while True:
                try:
                    data, _addr = sock.recvfrom(4096)
                except BlockingIOError:
                    break
                t1 = time.perf_counter()
                if len(data) < 12:
                    continue
                try:
                    qid = struct.unpack("!H", data[:2])[0]
                    sample_index = outstanding.pop(qid, None)
                    if sample_index is None:
                        continue
                    recv_times[sample_index] = t1
                    parsed = fast_parse_dns_response(data, expected_qid=qid)
                    rcodes[sample_index] = int(parsed.get("rcode", -1))
                    answer_counts[sample_index] = int(parsed.get("answers", 0))
                    answer_ip_values[sample_index] = ";".join(parsed.get("answer_ips") or [])
                    received_total += 1
                except Exception as exc:
                    response_error_count += 1
                    if 'sample_index' in locals() and sample_index is not None:
                        rcodes[sample_index] = -3
                        error_messages[sample_index] = str(exc)

            if sent >= total:
                if not outstanding:
                    break
                if time.perf_counter() >= last_send + timeout_sec:
                    break

            sleep_until = next_send if sent < total else (last_send + timeout_sec)
            wait = max(0.0, min(0.001, sleep_until - time.perf_counter()))
            if wait > 0:
                r, _w, _x = select.select([sock], [], [], wait)
                if r:
                    continue
            else:
                time.sleep(0)
    finally:
        sock.close()

    elapsed = time.perf_counter() - start_perf

    # Post aggregation.  Do all classification, latency statistics, and CSV row creation
    # after the timing-sensitive section has completed.
    rtts = []
    received_ok = 0
    timeout_count = 0
    error_count = send_error_count + response_error_count
    wrong_answer_count = 0
    rcode_counts = Counter()
    observed_answer_ips = set()

    rows_written = 0
    sample_rows = []

    def keep_row(is_failure=False):
        nonlocal rows_written
        if not samples_path or sample_limit == 0:
            return False
        if sample_limit < 0:
            rows_written += 1
            return True
        if rows_written < sample_limit or is_failure:
            rows_written += 1
            return True
        return False

    for i in range(total):
        qname = qnames[qname_indices[i] % qname_count]
        sent_ok = send_times[i] > 0.0 and rcodes[i] != -2
        got_response = recv_times[i] > 0.0
        answer_ips = [value for value in answer_ip_values[i].split(";") if value]
        expected_ip = str(expected_answer_ips[qname_indices[i] % qname_count])
        answer_match = dns_answer_matches(expected_ip, answer_ips)
        ok = got_response and rcodes[i] == 0 and answer_counts[i] > 0 and answer_match
        rtt_ms = (recv_times[i] - send_times[i]) * 1000.0 if got_response and send_times[i] > 0.0 else ""

        if got_response:
            rcode_counts[rcodes[i]] += 1
            observed_answer_ips.update(answer_ips)

        if ok:
            received_ok += 1
            rtts.append(float(rtt_ms))
            result = "PASS"
            err = ""
        elif rcodes[i] == -2:
            error_count += 1
            result = "FAIL"
            err = error_messages[i] or "send_error"
        elif got_response:
            error_count += 1
            if rcodes[i] == 0 and answer_counts[i] > 0 and not answer_match:
                wrong_answer_count += 1
            result = "FAIL"
            err = error_messages[i] or (
                f"rcode={rcodes[i]} answers={answer_counts[i]} expected={expected_ip} "
                f"observed={';'.join(answer_ips) or '-'}"
            )
        else:
            # No response was received before the post-send timeout window expired.
            if sent_ok:
                timeout_count += 1
            result = "FAIL"
            err = "timeout"

        if keep_row(is_failure=(result != "PASS")):
            sample_rows.append({
                "phase": phase,
                "stage": stage,
                "step_value": step_value,
                "sample_index": i,
                "qname": qname,
                "qtype": "A",
                "server_ip": server_ip,
                "server_port": port,
                "unix_ts_start": start_unix + (send_times[i] - start_perf) if send_times[i] > 0.0 else "",
                "unix_ts_end": start_unix + (recv_times[i] - start_perf) if got_response else "",
                "rtt_ms": rtt_ms,
                "rcode": rcodes[i] if got_response else "",
                "answers": answer_counts[i] if got_response else "",
                "answer_ips": ";".join(answer_ips),
                "expected_answer_ip": expected_ip,
                "answer_match": 1 if answer_match else 0,
                "result": result,
                "error": err,
            })

    if samples_path:
        with open(samples_path, "w", newline="", encoding="utf-8", buffering=1024 * 1024) as sample_file:
            writer = csv.DictWriter(sample_file, fieldnames=SAMPLE_FIELDS, extrasaction="ignore")
            writer.writeheader()
            if sample_rows:
                writer.writerows(sample_rows)

    requested = total
    failures = requested - received_ok
    success_rate = (received_ok / requested * 100.0) if requested else 0.0
    failure_rate = (failures / requested * 100.0) if requested else 0.0
    return {
        "phase": phase,
        "stage": stage,
        "step_value": step_value,
        "target_qps": target_qps,
        "actual_qps": received_ok / elapsed if elapsed > 0 else 0.0,
        "duration_sec": elapsed,
        "samples_requested": requested,
        "samples_sent": sent,
        "samples_received": received_ok,
        "responses_received_total": received_total,
        "timeout_count": timeout_count,
        "error_count": error_count,
        "wrong_answer_count": wrong_answer_count,
        "observed_answer_ips": sorted(observed_answer_ips),
        **rcode_summary_fields(rcode_counts),
        "success_rate_pct": success_rate,
        "failure_rate_pct": failure_rate,
        "fill_rate_pct": failure_rate,
        "rtt_min_ms": min(rtts) if rtts else "",
        "rtt_avg_ms": mean(rtts),
        "rtt_p50_ms": percentile(rtts, 50),
        "rtt_p90_ms": percentile(rtts, 90),
        "rtt_p95_ms": percentile(rtts, 95),
        "rtt_p99_ms": percentile(rtts, 99),
        "rtt_max_ms": max(rtts) if rtts else "",
        "sample_rows_written": rows_written,
        "post_aggregation": 1,
    }


def run_internal_dns_load_client(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--server-ip", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--qnames-json", required=True)
    parser.add_argument("--expected-answers-json", required=True)
    parser.add_argument("--target-qps", type=float, required=True)
    parser.add_argument("--timeout-sec", type=float, required=True)
    parser.add_argument("--duration-sec", type=float, default=0.0)
    parser.add_argument("--total-count", type=int, default=0)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--step-value", required=True)
    parser.add_argument("--samples-csv", required=True)
    parser.add_argument("--sample-limit", type=int, default=DEFAULT_DNS_SAMPLE_LIMIT)
    parser.add_argument("--socket-buffer-bytes", type=int, default=DEFAULT_DNS_SOCKET_BUFFER_BYTES)
    parser.add_argument("--progress-interval-sec", type=float, default=0.0)
    args = parser.parse_args(argv)
    qnames = json.loads(Path(args.qnames_json).read_text(encoding="utf-8"))
    expected_answer_ips = json.loads(Path(args.expected_answers_json).read_text(encoding="utf-8"))
    result = run_dns_load_client(
        args.server_ip, args.port, qnames, args.target_qps, args.timeout_sec,
        args.duration_sec, args.total_count, args.phase, args.stage, args.step_value, args.samples_csv,
        expected_answer_ips=expected_answer_ips, sample_limit=args.sample_limit,
        socket_buffer_bytes=args.socket_buffer_bytes,
        progress_interval_sec=args.progress_interval_sec,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("samples_received", 0) > 0 else 1


def read_server_stats(path):
    stats_path = Path(str(path) + ".stats.json")
    if not stats_path.exists():
        return {}
    try:
        return json.loads(stats_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def wait_for_server_qname(path, qname, start_count, timeout_sec=5.0):
    expected = str(qname).lower().rstrip(".")
    deadline = time.monotonic() + max(0.1, float(timeout_sec))
    last_stats = {}
    while time.monotonic() < deadline:
        last_stats = read_server_stats(path)
        current = int(last_stats.get("benchmark_queries", last_stats.get("total_queries", 0)) or 0)
        recent = {str(value).lower().rstrip(".") for value in (last_stats.get("recent_benchmark_qnames") or [])}
        if current > int(start_count) and expected in recent:
            return True, current, last_stats
        time.sleep(0.05)
    current = int(last_stats.get("benchmark_queries", last_stats.get("total_queries", 0)) or 0)
    return False, current, last_stats


def count_server_queries(path):
    # Successful WAN queries are not written to dns_server_queries.csv.  Query counts
    # therefore come exclusively from the sidecar stats file.
    time.sleep(max(0.02, DEFAULT_DNS_SERVER_LOG_FLUSH_SEC * 2.0))
    data = read_server_stats(path)
    if data:
        return int(data.get("benchmark_queries", data.get("total_queries", 0)) or 0)
    return 0


def read_server_qnames_since(path, start_row_count):
    qnames = []
    if not path.exists():
        return qnames
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            if idx >= start_row_count:
                qnames.append(str(row.get("qname") or "").lower().rstrip("."))
    return qnames


def wait_for_process_exit(proc, timeout_sec, name):
    try:
        return proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            return proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.communicate(timeout=5)


def wait_for_process_exit_relay_stderr(proc, timeout_sec, name, err_f):
    def relay():
        try:
            for line in proc.stderr or []:
                err_f.write(line)
                err_f.flush()
                print(line.rstrip("\n"), flush=True)
        except Exception as exc:
            err_f.write(f"progress relay error: {exc}\n")
            err_f.flush()

    relay_thread = threading.Thread(target=relay, name=f"{name}-stderr-relay", daemon=True)
    relay_thread.start()
    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    relay_thread.join(timeout=2.0)


def start_dns_server(args, target, out_dir, server_query_log, max_records):
    server_stdout = out_dir / "dns_server.stdout.log"
    server_stderr = out_dir / "dns_server.stderr.log"
    cmd = ns_cmd(args.wan_ns, [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_dns-server",
        "--bind-ip", target["wan_ip"],
        "--port", str(args.dns_port),
        "--domain-suffix", str(args.dns_domain_suffix),
        "--answer-base-ip", str(args.dns_answer_base_ip),
        "--answer-mode", str(args.dns_answer_mode),
        "--answer-random-seed", str(args.dns_answer_random_seed),
        "--max-records", str(max_records),
        "--ttl-sec", str(args.dns_ttl_sec),
        "--query-log", str(server_query_log),
        "--stats-file", str(Path(str(server_query_log) + ".stats.json")),
        "--query-log-limit", str(args.dns_server_query_log_limit),
        "--log-batch-size", str(args.dns_server_log_batch_size),
        "--log-flush-sec", str(args.dns_server_log_flush_sec),
        "--socket-buffer-bytes", str(args.dns_socket_buffer_bytes),
    ])
    (out_dir / "dns_server.cmd.txt").write_text(" ".join(shlex.quote(str(x)) for x in cmd) + "\n", encoding="utf-8")
    out_f = open(server_stdout, "w", encoding="utf-8")
    err_f = open(server_stderr, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=out_f, stderr=err_f, text=True)
    return proc, out_f, err_f


def make_phase_error_data(phase, stage, step_value, target_qps, duration_sec, total_count, error, *, preflight=None, preflight_expected_ip="", preflight_wan_qname_seen=0, preflight_wan_queries=0):
    preflight = preflight or {}
    requested = int(total_count or 0)
    if requested <= 0 and float(duration_sec or 0.0) > 0.0:
        requested = max(1, int(float(target_qps or 0.0) * float(duration_sec)))
    return {
        "phase": phase,
        "stage": stage,
        "step_value": step_value,
        "step_status": "ERROR",
        "duration_sec": float(duration_sec or 0.0),
        "target_qps": float(target_qps or 0.0),
        "actual_qps": 0.0,
        "samples_requested": requested,
        "samples_sent": 0,
        "samples_received": 0,
        "responses_received_total": int(preflight.get("responses_received_total") or 0),
        "timeout_count": int(preflight.get("timeout_count") or 0),
        "error_count": max(1, int(preflight.get("error_count") or 0)),
        "wrong_answer_count": int(preflight.get("wrong_answer_count") or 0),
        "observed_answer_ips": list(preflight.get("observed_answer_ips") or []),
        "rcode_counts": dict(preflight.get("rcode_counts") or {}),
        "rcode_noerror_count": int(preflight.get("rcode_noerror_count") or 0),
        "rcode_formerr_count": int(preflight.get("rcode_formerr_count") or 0),
        "rcode_servfail_count": int(preflight.get("rcode_servfail_count") or 0),
        "rcode_nxdomain_count": int(preflight.get("rcode_nxdomain_count") or 0),
        "rcode_notimp_count": int(preflight.get("rcode_notimp_count") or 0),
        "rcode_refused_count": int(preflight.get("rcode_refused_count") or 0),
        "rcode_other_count": int(preflight.get("rcode_other_count") or 0),
        "success_rate_pct": 0.0,
        "failure_rate_pct": 100.0,
        "fill_rate_pct": 100.0,
        "rtt_min_ms": "",
        "rtt_avg_ms": "",
        "rtt_p50_ms": "",
        "rtt_p90_ms": "",
        "rtt_p95_ms": "",
        "rtt_p99_ms": "",
        "rtt_max_ms": "",
        "client_returncode": int(preflight.get("client_returncode") or 1),
        "preflight_ok": 0,
        "preflight_samples_requested": int(preflight.get("samples_requested") or 0),
        "preflight_samples_received": int(preflight.get("samples_received") or 0),
        "preflight_wrong_answer_count": int(preflight.get("wrong_answer_count") or 0),
        "preflight_expected_ip": preflight_expected_ip,
        "preflight_observed_ips": ";".join(preflight.get("observed_answer_ips") or []),
        "preflight_wan_qname_seen": int(bool(preflight_wan_qname_seen)),
        "preflight_wan_queries": int(preflight_wan_queries or 0),
        "error": str(error),
    }


def make_phase_skipped_data(phase, stage, step_value, target_qps, duration_sec, total_count, reason):
    data = make_phase_error_data(
        phase, stage, step_value, target_qps, duration_sec, total_count, reason
    )
    data["step_status"] = "SKIPPED"
    data["error_count"] = 0
    data["failure_rate_pct"] = ""
    data["fill_rate_pct"] = ""
    return data


def phase_has_error(data):
    return str(data.get("step_status") or "").upper() in {"ERROR", "SKIPPED"} or bool(data.get("error"))


def run_client_load_phase_safe(*args, **kwargs):
    try:
        return run_client_load_phase(*args, **kwargs)
    except Exception as exc:
        phase = args[0] if len(args) > 0 else kwargs.get("phase", "unknown")
        stage = args[1] if len(args) > 1 else kwargs.get("stage", "unknown")
        step_value = args[2] if len(args) > 2 else kwargs.get("step_value", "")
        target_qps = args[7] if len(args) > 7 else kwargs.get("target_qps", 0)
        duration_sec = kwargs.get("duration_sec", 0.0)
        total_count = kwargs.get("total_count", 0)
        message = f"{type(exc).__name__}: {exc}"
        print(f"ERROR: DNS step {phase}/{stage}/{step_value} failed: {message}", flush=True)
        return make_phase_error_data(
            phase, stage, step_value, target_qps, duration_sec, total_count, message
        )


def run_client_load_phase(phase, stage, step_value, args, target, out_dir, qnames, target_qps, duration_sec=0.0, total_count=0, sample_limit_override=None, run_preflight=True):
    preflight_wan_queries = 0
    preflight = {}
    preflight_qname = ""
    preflight_expected_ip = ""
    preflight_wan_qname_seen = 0
    if run_preflight:
        preflight_qname = make_preflight_qname(args._dns_max_records, args.dns_domain_suffix)
        preflight_expected_ip = answer_ip_for_qname(
            preflight_qname, args.dns_domain_suffix, args.dns_answer_base_ip,
            args._dns_max_records, args.dns_answer_mode, args.dns_answer_random_seed,
        )
        before_pf = count_server_queries(_DNS_SERVER_QUERY_LOG) if _DNS_SERVER_QUERY_LOG is not None else 0
        preflight = run_client_load_phase(
            "preflight", f"{phase}_{stage}", step_value, args, target, out_dir,
            [preflight_qname], 3, total_count=3, sample_limit_override=3, run_preflight=False,
        )
        if _DNS_SERVER_QUERY_LOG is not None:
            preflight_wan_qname_seen, after_pf, _stats = wait_for_server_qname(
                _DNS_SERVER_QUERY_LOG, preflight_qname, before_pf,
                timeout_sec=max(3.0, float(args.dns_timeout_sec) + 2.0),
            )
        else:
            after_pf = before_pf
        preflight_wan_queries = max(0, int(after_pf) - int(before_pf))
        preflight_failure_rate = preflight.get("failure_rate_pct")
        if preflight_failure_rate in (None, ""):
            preflight_failure_rate = 100.0
        preflight_wrong = int(preflight.get("wrong_answer_count") or 0)
        if (
            int(preflight.get("samples_received") or 0) < 3
            or float(preflight_failure_rate) > 0.0
            or preflight_wrong > 0
        ):
            message = (
                f"DNS answer-validation preflight failed before {phase}/{stage}/{step_value}: "
                f"expected_ip={preflight_expected_ip} observed={preflight.get('observed_answer_ips')}"
            )
            print(f"ERROR: {message}; skipping this DNS item", flush=True)
            return make_phase_error_data(
                phase, stage, step_value, target_qps, duration_sec, total_count, message,
                preflight=preflight,
                preflight_expected_ip=preflight_expected_ip,
                preflight_wan_qname_seen=preflight_wan_qname_seen,
                preflight_wan_queries=preflight_wan_queries,
            )
        if not preflight_wan_qname_seen:
            message = (
                f"DNS WAN-path preflight failed before {phase}/{stage}/{step_value}: "
                f"qname={preflight_qname} did not reach the WAN test DNS server "
                f"at {target['wan_ip']}:{args.dns_port}"
            )
            print(f"ERROR: {message}; skipping this DNS item", flush=True)
            return make_phase_error_data(
                phase, stage, step_value, target_qps, duration_sec, total_count, message,
                preflight=preflight,
                preflight_expected_ip=preflight_expected_ip,
                preflight_wan_qname_seen=preflight_wan_qname_seen,
                preflight_wan_queries=preflight_wan_queries,
            )
    if _ARP_REFRESHER is not None:
        _ARP_REFRESHER.refresh_now(f"before_{phase}_{stage}_{step_value}", required=True)
    safe_step = str(step_value).replace(".", "_")
    qnames_path = out_dir / f"qnames_{phase}_{stage}_{safe_step}.json"
    expected_answers_path = out_dir / f"expected_answers_{phase}_{stage}_{safe_step}.json"
    samples_csv = out_dir / f"dns_{phase}_{stage}_{safe_step}_samples.csv"
    summary_json = out_dir / f"dns_{phase}_{stage}_{safe_step}.json"
    stderr_path = out_dir / f"dns_{phase}_{stage}_{safe_step}.stderr.log"
    qnames = list(qnames)
    expected_answer_ips = [
        answer_ip_for_qname(
            qname, args.dns_domain_suffix, args.dns_answer_base_ip,
            args._dns_max_records, args.dns_answer_mode, args.dns_answer_random_seed,
        )
        for qname in qnames
    ]
    if any(not value for value in expected_answer_ips):
        raise RuntimeError(f"failed to calculate expected DNS answer for {phase}/{stage}/{step_value}")
    qnames_path.write_text(json.dumps(qnames, ensure_ascii=False), encoding="utf-8")
    expected_answers_path.write_text(json.dumps(expected_answer_ips, ensure_ascii=False), encoding="utf-8")
    server_ip = args.dns_client_server_ip or target["lan_gw"]
    cmd = ns_cmd(args.lan_ns, [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_dns-load-client",
        "--server-ip", server_ip,
        "--port", str(args.dns_port),
        "--qnames-json", str(qnames_path),
        "--expected-answers-json", str(expected_answers_path),
        "--target-qps", str(target_qps),
        "--timeout-sec", str(args.dns_timeout_sec),
        "--duration-sec", str(duration_sec),
        "--total-count", str(total_count),
        "--phase", phase,
        "--stage", stage,
        "--step-value", str(step_value),
        "--samples-csv", str(samples_csv),
        "--sample-limit", str(args.dns_sample_limit if sample_limit_override is None else sample_limit_override),
        "--socket-buffer-bytes", str(args.dns_socket_buffer_bytes),
    ])
    relay_progress = (str(phase) == "nocache_load" and float(getattr(args, "dns_load_progress_interval_sec", 0.0) or 0.0) > 0.0)
    if relay_progress:
        cmd.extend(["--progress-interval-sec", str(args.dns_load_progress_interval_sec)])
    (out_dir / f"dns_{phase}_{stage}_{safe_step}.cmd.txt").write_text(" ".join(shlex.quote(str(x)) for x in cmd) + "\n", encoding="utf-8")
    expected_total = int(total_count) if total_count else max(1, int(float(target_qps) * float(duration_sec)))
    timeout = max(10.0, expected_total / max(1.0, float(target_qps)) + float(args.dns_timeout_sec) + 30.0)
    with open(summary_json, "w", encoding="utf-8") as out_f, open(stderr_path, "w", encoding="utf-8") as err_f:
        if relay_progress:
            proc = subprocess.Popen(cmd, stdout=out_f, stderr=subprocess.PIPE, text=True, bufsize=1)
            wait_for_process_exit_relay_stderr(proc, timeout, f"DNS load client {phase}/{stage}/{step_value}", err_f)
        else:
            proc = subprocess.Popen(cmd, stdout=out_f, stderr=err_f, text=True)
            wait_for_process_exit(proc, timeout, f"DNS load client {phase}/{stage}/{step_value}")
    try:
        data = json.loads(summary_json.read_text(encoding="utf-8"))
    except Exception as exc:
        data = {
            "phase": phase, "stage": stage, "step_value": step_value,
            "samples_requested": expected_total, "samples_sent": 0, "samples_received": 0,
            "responses_received_total": 0, "timeout_count": 0, "error_count": 1,
            "wrong_answer_count": 0, "success_rate_pct": 0.0, "failure_rate_pct": 100.0,
            "fill_rate_pct": 100.0, "error": f"failed to parse DNS client JSON: {exc}",
        }
    data["client_returncode"] = proc.returncode
    data["step_status"] = "OK" if not data.get("error") else "ERROR"
    data["samples_csv"] = str(samples_csv)
    data["expected_answers_json"] = str(expected_answers_path)
    data["preflight_ok"] = 1 if not run_preflight or (
        int(preflight.get("samples_received") or 0) >= 3
        and int(preflight.get("wrong_answer_count") or 0) == 0
        and preflight_wan_qname_seen
    ) else 0
    data["preflight_samples_requested"] = int(preflight.get("samples_requested") or 0) if run_preflight else 0
    data["preflight_samples_received"] = int(preflight.get("samples_received") or 0) if run_preflight else 0
    data["preflight_wrong_answer_count"] = int(preflight.get("wrong_answer_count") or 0) if run_preflight else 0
    data["preflight_expected_ip"] = preflight_expected_ip if run_preflight else ""
    data["preflight_observed_ips"] = ";".join(preflight.get("observed_answer_ips") or []) if run_preflight else ""
    data["preflight_wan_qname_seen"] = int(bool(preflight_wan_qname_seen)) if run_preflight else 0
    data["preflight_wan_queries"] = preflight_wan_queries
    return data

def decorate_summary(base, args, target, phase, stage, step_value, wan_before, wan_after, **extra):
    requested = int(base.get("samples_requested") or 0)
    received = int(base.get("samples_received") or 0)
    wan_delta_raw = max(0, int(wan_after) - int(wan_before))
    preflight_wan_queries = max(0, int(base.get("preflight_wan_queries") or 0))
    wan_delta = max(0, wan_delta_raw - preflight_wan_queries)
    row = {
        "phase": phase,
        "stage": stage,
        "step_value": step_value,
        "test_name": f"dns_{phase}_{stage}_{step_value}",
        "result": (
            str(base.get("step_status")).upper()
            if str(base.get("step_status") or "").upper() in {"ERROR", "SKIPPED"}
            else ("PASS" if received > 0 and int(base.get("wrong_answer_count") or 0) == 0 else "FAIL")
        ),
        "target_name": args.target,
        "lan_ns": args.lan_ns,
        "wan_ns": args.wan_ns,
        "lan_iface": args.lan_iface,
        "wan_iface": args.wan_iface,
        "lan_ip": target["lan_ip"],
        "lan_gw": target["lan_gw"],
        "wan_ip": target["wan_ip"],
        "router_wan_ip": target["router_wan_ip"],
        "dns_client_server_ip": args.dns_client_server_ip or target["lan_gw"],
        "dns_wan_server_ip": target["wan_ip"],
        "dns_port": args.dns_port,
        "dns_ttl_sec": args.dns_ttl_sec,
        "duration_sec": base.get("duration_sec", ""),
        "target_qps": base.get("target_qps", ""),
        "actual_qps": base.get("actual_qps", ""),
        "samples_requested": requested,
        "samples_sent": base.get("samples_sent", ""),
        "samples_received": received,
        "responses_received_total": base.get("responses_received_total", ""),
        "timeout_count": base.get("timeout_count", ""),
        "error_count": base.get("error_count", ""),
        "wrong_answer_count": base.get("wrong_answer_count", ""),
        "rcode_noerror_count": base.get("rcode_noerror_count", ""),
        "rcode_formerr_count": base.get("rcode_formerr_count", ""),
        "rcode_servfail_count": base.get("rcode_servfail_count", ""),
        "rcode_nxdomain_count": base.get("rcode_nxdomain_count", ""),
        "rcode_notimp_count": base.get("rcode_notimp_count", ""),
        "rcode_refused_count": base.get("rcode_refused_count", ""),
        "rcode_other_count": base.get("rcode_other_count", ""),
        "success_rate_pct": base.get("success_rate_pct", ""),
        "failure_rate_pct": base.get("failure_rate_pct", ""),
        "fill_rate_pct": base.get("fill_rate_pct", base.get("failure_rate_pct", "")),
        "rtt_min_ms": base.get("rtt_min_ms", ""),
        "rtt_avg_ms": base.get("rtt_avg_ms", ""),
        "rtt_p50_ms": base.get("rtt_p50_ms", ""),
        "rtt_p90_ms": base.get("rtt_p90_ms", ""),
        "rtt_p95_ms": base.get("rtt_p95_ms", ""),
        "rtt_p99_ms": base.get("rtt_p99_ms", ""),
        "rtt_max_ms": base.get("rtt_max_ms", ""),
        "wan_queries_before": wan_before,
        "wan_queries_after": wan_after,
        "wan_queries_delta": wan_delta,
        "wan_queries_delta_raw": wan_delta_raw,
        "preflight_wan_queries": preflight_wan_queries,
        "preflight_ok": base.get("preflight_ok", ""),
        "preflight_samples_requested": base.get("preflight_samples_requested", ""),
        "preflight_samples_received": base.get("preflight_samples_received", ""),
        "preflight_wrong_answer_count": base.get("preflight_wrong_answer_count", ""),
        "preflight_expected_ip": base.get("preflight_expected_ip", ""),
        "preflight_observed_ips": base.get("preflight_observed_ips", ""),
        "preflight_wan_qname_seen": base.get("preflight_wan_qname_seen", ""),
        "cache_preload_count": extra.get("cache_preload_count", ""),
        "cache_table_size": extra.get("cache_table_size", ""),
        "cache_overflow_detected": extra.get("cache_overflow_detected", ""),
        "cache_overflow_rate_pct": extra.get("cache_overflow_rate_pct", ""),
        "stop_reason": extra.get("stop_reason", ""),
        "error": base.get("error", ""),
    }
    return row


def should_stop(row, args, use_loss=True, use_latency=True):
    reasons = []
    if use_loss:
        try:
            if float(row.get("failure_rate_pct") or 0.0) >= float(args.dns_stop_failure_rate_pct):
                reasons.append(f"failure_rate>={args.dns_stop_failure_rate_pct}%")
        except Exception:
            pass
    if use_latency:
        try:
            max_ms = row.get("rtt_max_ms")
            if max_ms not in ("", None) and float(max_ms) >= float(args.dns_stop_response_ms):
                reasons.append(f"response>={args.dns_stop_response_ms}ms")
        except Exception:
            pass
    return ";".join(reasons)


def failure_limit_hit(row, args):
    try:
        return float(row.get("failure_rate_pct") or 0.0) >= float(args.dns_stop_failure_rate_pct)
    except Exception:
        return False


def should_stop_table(row, args):
    reasons = []
    base = should_stop(row, args, use_loss=True, use_latency=False)
    if base:
        reasons.extend(base.split(";"))
    try:
        ratio = float(row.get("cache_overflow_rate_pct") or 0.0)
        if ratio >= float(args.dns_table_overflow_rate_pct):
            reasons.append(f"wan_verify_rate>={args.dns_table_overflow_rate_pct}%")
    except Exception:
        pass
    return ";".join(r for r in reasons if r)


def dedupe_keep_order(items):
    seen = set()
    out = []
    for item in items:
        key = str(item).lower().rstrip(".")
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def extend_cached_qnames(cache_pool, desired_size, cursor, suffix):
    if len(cache_pool) >= int(desired_size):
        return [], cursor
    add_count = int(desired_size) - len(cache_pool)
    new_qnames = make_qnames(cursor, add_count, suffix)
    cursor += add_count
    cache_pool.extend(new_qnames)
    return new_qnames, cursor


def overflow_rate_pct(wan_delta, table_size):
    try:
        return (float(wan_delta) / max(1.0, float(table_size))) * 100.0
    except Exception:
        return ""


def write_summary_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_metadata(out_dir, args, target, steps, cache_steps, max_records):
    metadata = {
        "created_at": ts_iso(),
        "mode": "dns_rate_cache_table_benchmark",
        "target_name": args.target,
        "target": target,
        "args": vars(args),
        "e6_steps": steps,
        "cache_e6_steps": cache_steps,
        "max_records": max_records,
        "tools": {
            "dns_server": "built-in Python UDP DNS server on WAN namespace",
            "dns_client": "built-in non-blocking Python UDP DNS client on LAN namespace",
            "namespace_setup": "ip (iproute2)",
            "kernel_rebind": "shared L4 latency PCI bind/unbind helper",
        },
        "notes": [
            "Configure the DUT/router WAN DNS server to the test host WAN IP before running this benchmark.",
            "The client sends DNS A queries to the DUT LAN IP by default. Override with --dns-client-server-ip when needed.",
            "nocache_rate uses unique qnames for each request.",
            "cache_rate preloads 100 qnames, then round-robins those qnames at each target QPS.",
            "cache_rate records WAN re-query count divided by the preload qname count as cache_overflow_rate_pct. It does not stop on WAN re-query rate.",
            "Table-size tests run before nocache_rate and cache_rate so high-QPS traffic cannot contaminate cache-capacity measurements.",
            "cache_table_reuse_size grows an independent cumulative qname pool and verifies the whole pool at 100 QPS.",
            "cache_table_size is the optional delayed table pass using a second independent qname range. It runs only when --dns-run-delayed-table is set, normally injected by launcher --run-all or --run-dns-all.",
            "WAN counters used for cache decisions include only generated benchmark qnames and are stored in dns_server_queries.csv.stats.json. dns_server_queries.csv contains only bounded server-side failure rows.",
            "WAN re-query count divided by verified table size is recorded as cache_overflow_rate_pct. Table-size tests stop when it reaches --dns-table-overflow-rate-pct.",
            "Every LAN response is accepted only when RCODE=NOERROR and the A record exactly matches the deterministic expected IP for that qname.",
            "Every measurement step uses a unique preflight qname and aborts unless that exact qname reaches the WAN test DNS server.",
            "A per-run qname label prevents results from being satisfied by cache entries left by an earlier benchmark run.",
            "fill_rate_pct is reported as failure_rate_pct for compatibility with the requested wording.",
        ],
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def main():
    global _ARP_REFRESHER, _DNS_SERVER_QUERY_LOG, _DNS_RUN_LABEL, _DNS_PREFLIGHT_COUNTER
    parser = argparse.ArgumentParser(description="IPv4 router DNS rate/cache/table benchmark", allow_abbrev=False)
    parser.add_argument("--target", choices=TARGETS.keys(), default="i226_2p5g")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--lan-iface", default="")
    parser.add_argument("--wan-iface", default="")
    parser.add_argument("--lan-ns", default=DEFAULT_LAN_NS)
    parser.add_argument("--wan-ns", default=DEFAULT_WAN_NS)
    parser.add_argument("--setup-namespaces", dest="setup_namespaces", action="store_true", default=True)
    parser.add_argument("--no-setup-namespaces", dest="setup_namespaces", action="store_false")
    parser.add_argument("--replace-namespaces", dest="replace_namespaces", action="store_true", default=True)
    parser.add_argument("--no-replace-namespaces", dest="replace_namespaces", action="store_false")
    parser.add_argument("--keep-namespaces", action="store_true")
    parser.add_argument("--auto-kernel-bind", dest="auto_kernel_bind", action="store_true", default=True)
    parser.add_argument("--no-auto-kernel-bind", dest="auto_kernel_bind", action="store_false")
    parser.add_argument("--restore-dpdk-after", dest="restore_dpdk_after", action="store_true", default=True)
    parser.add_argument("--no-restore-dpdk-after", dest="restore_dpdk_after", action="store_false")
    parser.add_argument("--dpdk-driver", default="")
    parser.add_argument("--lan-prefix", type=parse_positive_int, default=DEFAULT_LAN_PREFIX)
    parser.add_argument("--wan-prefix", type=parse_positive_int, default=DEFAULT_WAN_PREFIX)
    parser.add_argument("--lan-ip", default="")
    parser.add_argument("--lan-gw", default="")
    parser.add_argument("--wan-ip", default="")
    parser.add_argument("--wan-gw", default="")
    parser.add_argument("--router-wan-ip", default="")
    parser.add_argument("--post-namespace-settle-sec", type=parse_non_negative_float, default=DEFAULT_POST_NAMESPACE_SETTLE_SEC)
    parser.add_argument("--test-start-delay-sec", type=parse_non_negative_float, default=DEFAULT_TEST_START_DELAY_SEC, help="Seconds to wait after namespace interfaces report link-up and before testing. Default: 15.")
    parser.add_argument("--wait-interfaces-ready", dest="wait_interfaces_ready", action="store_true", default=True)
    parser.add_argument("--no-wait-interfaces-ready", dest="wait_interfaces_ready", action="store_false")
    parser.add_argument("--interface-ready-timeout-sec", type=parse_positive_float, default=DEFAULT_INTERFACE_READY_TIMEOUT_SEC)

    parser.add_argument("--duration", type=parse_positive_float, default=DEFAULT_DNS_DURATION_SEC, help="Measurement duration in seconds for each QPS step.")
    parser.add_argument("--dns-port", type=parse_positive_int, default=DEFAULT_DNS_PORT)
    parser.add_argument("--dns-domain-suffix", default=DEFAULT_DOMAIN_SUFFIX)
    parser.add_argument("--dns-run-label", default="", help="Optional DNS label identifying this run. Default: random unique label.")
    parser.add_argument("--dns-answer-mode", choices=["random", "fixed"], default="random", help="A-record answer mode. Default: random deterministic public-looking addresses.")
    parser.add_argument("--dns-answer-base-ip", default="93.184.0.1", help="Base A-record answer address used when --dns-answer-mode fixed is selected.")
    parser.add_argument("--dns-answer-random-seed", type=int, default=20260621, help="Seed for deterministic random public-looking DNS answer addresses.")
    parser.add_argument("--dns-ttl-sec", type=parse_positive_int, default=DEFAULT_DNS_TTL_SEC)
    parser.add_argument("--dns-timeout-sec", type=parse_positive_float, default=DEFAULT_DNS_TIMEOUT_SEC)
    parser.add_argument("--dns-sample-limit", type=int, default=DEFAULT_DNS_SAMPLE_LIMIT, help="Per-step sample CSV rows. -1 logs all rows after each step, 0 disables per-query sample CSV output, positive values keep first N rows plus important failures.")
    parser.add_argument("--dns-server-query-log-limit", type=int, default=DEFAULT_DNS_SERVER_QUERY_LOG_LIMIT, help="Maximum server-side failure rows written to dns_server_queries.csv. Successful queries are never logged. Default: 1000; -1 keeps all failures.")
    parser.add_argument("--dns-socket-buffer-bytes", type=parse_positive_int, default=DEFAULT_DNS_SOCKET_BUFFER_BYTES, help="UDP socket send/receive buffer size for built-in DNS server/client.")
    parser.add_argument("--dns-server-log-batch-size", type=parse_positive_int, default=DEFAULT_DNS_SERVER_LOG_BATCH_SIZE, help="Deprecated compatibility option.")
    parser.add_argument("--dns-server-log-flush-sec", type=parse_positive_float, default=DEFAULT_DNS_SERVER_LOG_FLUSH_SEC, help="Stats sidecar update interval in seconds. Failure CSV rows are written when the DNS server exits.")
    parser.add_argument("--dns-server-start-delay-sec", type=parse_non_negative_float, default=DEFAULT_DNS_SERVER_START_DELAY_SEC)
    parser.add_argument("--dns-client-server-ip", default="", help="DNS server IP queried by LAN client. Default: target LAN gateway, normally the DUT LAN IP.")
    parser.add_argument("--dns-e6-steps", type=parse_e6_steps, default=list(DEFAULT_E6_STEPS), help="Comma-separated QPS/table E6 steps for nocache and table tests. Default: 10..10000.")
    parser.add_argument("--dns-cache-e6-steps", type=parse_e6_steps, default=list(DEFAULT_CACHE_E6_STEPS), help="Comma-separated QPS steps for cache_rate. Default: 10..48000.")
    parser.add_argument("--dns-record-count", type=int, default=0, help="Maximum dummy records. 0 means auto-size from duration and max QPS.")
    parser.add_argument("--dns-cache-preload-count", type=parse_positive_int, default=DEFAULT_CACHE_PRELOAD_COUNT)
    parser.add_argument("--dns-table-qps", type=parse_positive_int, default=DEFAULT_TABLE_QPS)
    parser.add_argument("--dns-stop-failure-rate-pct", type=parse_positive_float, default=DEFAULT_STOP_FAILURE_RATE_PCT)
    parser.add_argument("--dns-stop-response-ms", type=parse_positive_float, default=DEFAULT_STOP_RESPONSE_MS)
    parser.add_argument("--dns-table-overflow-rate-pct", type=parse_positive_float, default=DEFAULT_TABLE_OVERFLOW_RATE_PCT, help="Stop DNS table-size tests when WAN re-query count / verified table size reaches this percentage.")
    parser.add_argument("--dns-table-reuse-interval-sec", type=parse_non_negative_float, default=DEFAULT_TABLE_REUSE_INTERVAL_SEC, help="Wait time before the delayed second/table-size pass. Default: 600 seconds. This pass runs only with --dns-run-delayed-table.")
    parser.add_argument("--dns-run-delayed-table", action="store_true", help="Run the delayed cache_table_size pass after --dns-table-reuse-interval-sec. The launcher injects this for --run-all and --run-dns-all.")
    parser.add_argument("--dns-run-nocache-load", action="store_true", help="Run a sustained nocache DNS load after nocache_rate. The load QPS is the step before the failure-rate stop point, or the highest attempted QPS when the failure-rate threshold is not hit.")
    parser.add_argument("--dns-nocache-load-duration-sec", type=parse_positive_float, default=DEFAULT_NOCACHE_LOAD_DURATION_SEC, help="Sustained nocache DNS load duration in seconds. Default: 3600.")
    parser.add_argument("--dns-nocache-load-sample-limit", type=int, default=0, help="LAN-side sample CSV row limit for nocache_load. Default: 0 to avoid large files.")
    parser.add_argument("--dns-load-progress-interval-sec", type=parse_non_negative_float, default=DEFAULT_DNS_LOAD_PROGRESS_INTERVAL_SEC, help="Progress print interval for nocache_load in seconds. Default: 10. Use 0 to disable.")
    parser.add_argument("--dns-randomize", action="store_true", help="Shuffle generated unique names before each measurement.")
    parser.add_argument("--dns-skip-nocache", action="store_true")
    parser.add_argument("--dns-skip-cache", action="store_true")
    parser.add_argument("--dns-skip-table", action="store_true")
    parser.add_argument("--dns-skip-table-reuse", action="store_true", help="Skip the cumulative/reuse cache table-size test that runs before the delayed table test.")

    args, unknown_args = parser.parse_known_args()
    if unknown_args:
        print(f"WARNING: ignored unsupported options for DNS latency script: {' '.join(unknown_args)}")
    if args.dns_port > 65535:
        raise SystemExit("ERROR: --dns-port must be 1..65535")
    if args.lan_prefix > 32 or args.wan_prefix > 32:
        raise SystemExit("ERROR: --lan-prefix and --wan-prefix must be 1..32")
    if args.dns_record_count < 0:
        raise SystemExit("ERROR: --dns-record-count must be zero or positive")
    args.dns_domain_suffix = str(args.dns_domain_suffix).strip(".").lower()
    run_label = str(args.dns_run_label or f"run{dt.datetime.now().strftime('%Y%m%dt%H%M%S')}-{secrets.token_hex(4)}").strip(".").lower()
    if not run_label or len(run_label) > 63 or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in run_label) or run_label[0] == "-" or run_label[-1] == "-":
        raise SystemExit("ERROR: --dns-run-label must be a valid single DNS label (letters, digits, hyphen; max 63 characters)")
    _DNS_RUN_LABEL = run_label
    _DNS_PREFLIGHT_COUNTER = 0
    args.dns_run_label = run_label

    require_root()
    require_command("ip")
    require_command(sys.executable)

    steps = list(args.dns_e6_steps)
    cache_steps = list(args.dns_cache_e6_steps)
    max_step = max(steps)
    max_cache_step = max(cache_steps) if cache_steps else max_step
    # Allocate disjoint qname ranges for each DNS phase.  The previous
    # implementation sized the dummy table from only max_step * duration;
    # that was insufficient because nocache_rate consumes a fresh qname range
    # for every E6 QPS step.  It also allowed cache_table_size to reuse names
    # already touched by nocache_rate, which could contaminate cache capacity
    # checks.  These offsets keep the phases independent and make the automatic
    # dummy record count deterministic.
    nocache_record_need = 0 if args.dns_skip_nocache else sum(max(1, int(round(float(qps) * float(args.duration)))) for qps in steps)
    cache_record_need = 0 if args.dns_skip_cache else int(args.dns_cache_preload_count)
    # Table tests run before the rate tests and use independent cumulative qname pools.
    # One max_step-sized range is enough for each pass.  A second disjoint range keeps
    # the optional delayed pass independent from the immediate table pass.
    table_record_need = 0 if args.dns_skip_table else int(max_step)
    table_delayed_record_need = 0 if (args.dns_skip_table or not args.dns_run_delayed_table) else int(max_step)
    record_margin = max(20000, int(args.dns_cache_preload_count) + int(max_step))
    nocache_start = 0
    cache_start = nocache_start + nocache_record_need
    table_start = cache_start + cache_record_need
    table_delayed_start = table_start + table_record_need
    auto_records = max(10000, table_delayed_start + table_delayed_record_need + record_margin)
    max_records = int(args.dns_record_count) if int(args.dns_record_count) > 0 else auto_records
    required_records = table_delayed_start + table_delayed_record_need
    if max_records < 10000:
        raise SystemExit("ERROR: --dns-record-count must be at least 10000 when specified")
    if max_records < required_records:
        raise SystemExit(
            "ERROR: --dns-record-count is too small for selected DNS phases: "
            f"need at least {required_records}, got {max_records}. "
            "Use --dns-record-count 0 for auto-sizing or specify a larger value."
        )

    target = merge_target_with_overrides(TARGETS[args.target], args)
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_RESULT_ROOT / f"dns_rate_cache_table_{args.target}_{ts_compact()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    server_query_log = out_dir / "dns_server_queries.csv"
    _DNS_SERVER_QUERY_LOG = server_query_log
    args._dns_max_records = max_records
    with open(server_query_log, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(["unix_ts", "qname", "error"])

    rows = []
    ns_log_path = out_dir / "namespace_setup.csv"
    with open(ns_log_path, "w", newline="", encoding="utf-8") as ns_log_file:
        ns_log = csv.writer(ns_log_file)
        ns_log.writerow(["ts_local", "action", "detail"])
        if args.setup_namespaces and args.replace_namespaces:
            delete_existing_namespaces(args, ns_log)
        if args.setup_namespaces:
            prepare_kernel_interfaces_from_profile(args, target, ns_log)
        resolve_profile_interfaces(args, target, ns_log)
        if args.setup_namespaces:
            require_profile_interfaces(args, target)
        write_metadata(out_dir, args, target, steps, cache_steps, max_records)

        print("============================================================")
        print("IPv4 router DNS rate/cache/table benchmark")
        print("============================================================")
        print(f"Target          : {args.target}")
        print(f"Description     : {target['description']}")
        print(f"Topology        : {target.get('topology', 'router_nat')}")
        print("Tools           : built-in UDP DNS server/client, ip(iproute2 namespaces)")
        print(f"LAN namespace   : {args.lan_ns}")
        print(f"WAN namespace   : {args.wan_ns}")
        print(f"LAN endpoint    : {target['lan_ip']} gw {target['lan_gw']}")
        print(f"WAN DNS server  : {target['wan_ip']}:{args.dns_port}")
        print(f"LAN DNS target  : {args.dns_client_server_ip or target['lan_gw']}:{args.dns_port}")
        print(f"Duration        : {args.duration}s per QPS step")
        if args.dns_run_nocache_load:
            print(f"Nocache load    : enabled duration={args.dns_nocache_load_duration_sec}s progress_interval={args.dns_load_progress_interval_sec}s")
        print(f"E6 steps        : {','.join(str(x) for x in steps)}")
        print(f"Cache E6 steps  : {','.join(str(x) for x in cache_steps)}")
        print(f"Max records     : {max_records}")
        print(f"DNS run label   : {args.dns_run_label}")
        def fmt_range(start, count):
            return "disabled" if int(count) <= 0 else f"{start}..{start + int(count) - 1}"
        print(
            "Qname ranges    : "
            f"nocache={fmt_range(nocache_start, nocache_record_need)}, "
            f"cache={fmt_range(cache_start, cache_record_need)}, "
            f"table_reuse={fmt_range(table_start, table_record_need)}, "
            f"table_delayed={fmt_range(table_delayed_start, table_delayed_record_need)}"
        )
        print(f"Stop criteria   : failure_rate>={args.dns_stop_failure_rate_pct}% OR response>={args.dns_stop_response_ms}ms")
        print("Cache criteria  : failure/response only; WAN re-query rate is recorded, not a stop condition")
        print(f"Table criteria  : wan_verify/table_size>={args.dns_table_overflow_rate_pct}%")
        print(f"Output          : {out_dir}")
        print("============================================================")

        server_proc = server_stdout = server_stderr = None
        try:
            if args.setup_namespaces:
                setup_namespaces(args, target, ns_log)
            wait_test_interfaces_ready(args)
            if args.post_namespace_settle_sec > 0:
                time.sleep(args.post_namespace_settle_sec)
            wait_before_test_start(args.test_start_delay_sec, "DNS benchmark")
            _ARP_REFRESHER = make_namespace_arp_refresher(args, target, out_dir)
            _ARP_REFRESHER.refresh_now("startup", required=True)
            _ARP_REFRESHER.start()

            server_proc, server_stdout, server_stderr = start_dns_server(args, target, out_dir, server_query_log, max_records)
            time.sleep(args.dns_server_start_delay_sec)
            if server_proc.poll() is not None:
                raise RuntimeError(f"DNS server exited before measurement; exit_code={server_proc.returncode}")

            # Run cache-table capacity checks before nocache/cache rate tests.
            # High-QPS DNS phases can leave queued upstream traffic in the DUT and
            # can evict or expire entries before the table check begins.
            table_cached_qnames = []
            table_cursor = table_start
            delayed_table_cursor = table_delayed_start

            if not args.dns_skip_table and not args.dns_skip_table_reuse:
                print("--- DNS cache_table_reuse_size start ---", flush=True)
                for table_size in steps:
                    new_qnames, table_cursor = extend_cached_qnames(table_cached_qnames, table_size, table_cursor, args.dns_domain_suffix)
                    if new_qnames:
                        preload_before = count_server_queries(server_query_log)
                        preload = run_client_load_phase_safe("cache_table_reuse_size", "preload_added", table_size, args, target, out_dir, new_qnames, args.dns_table_qps, total_count=len(new_qnames))
                        preload_after = count_server_queries(server_query_log)
                        preload_row = decorate_summary(preload, args, target, "cache_table_reuse_size", "preload_added", table_size, preload_before, preload_after, cache_table_size=table_size)
                        rows.append(preload_row)
                        if phase_has_error(preload):
                            preload_row["stop_reason"] = "step_error"
                            print(
                                f"ERROR: cache_table_reuse_size preload failed at table={table_size}: "
                                f"{preload_row.get('error')}; moving to the next DNS item",
                                flush=True,
                            )
                            break
                    verify_qnames = list(table_cached_qnames[:table_size])
                    before = count_server_queries(server_query_log)
                    data = run_client_load_phase_safe("cache_table_reuse_size", "verify_reused", table_size, args, target, out_dir, verify_qnames, args.dns_table_qps, total_count=table_size)
                    time.sleep(0.2)
                    after = count_server_queries(server_query_log)
                    wan_delta = max(0, after - before)
                    overflow_pct = overflow_rate_pct(wan_delta, table_size)
                    overflow = 1 if float(overflow_pct or 0.0) >= float(args.dns_table_overflow_rate_pct) else 0
                    row = decorate_summary(data, args, target, "cache_table_reuse_size", "verify_reused", table_size, before, after, cache_table_size=table_size, cache_overflow_detected=overflow, cache_overflow_rate_pct=overflow_pct)
                    stop_reason = "step_error" if phase_has_error(data) else should_stop_table(row, args)
                    row["stop_reason"] = stop_reason
                    rows.append(row)
                    if phase_has_error(data):
                        print(
                            f"ERROR: cache_table_reuse_size verify failed at table={table_size}: "
                            f"{row.get('error')}; moving to the next DNS item",
                            flush=True,
                        )
                    else:
                        print(f"reuse_table={table_size}: success={row['success_rate_pct']:.3f}% failure={row['failure_rate_pct']:.3f}% wan_verify={row['wan_queries_delta']} wan_rate={row['cache_overflow_rate_pct']:.3f}% overflow={overflow} stop={stop_reason or '-'}", flush=True)
                    if stop_reason:
                        break

            if not args.dns_skip_table and args.dns_run_delayed_table:
                if float(args.dns_table_reuse_interval_sec) > 0:
                    print(f"--- DNS cache_table_size interval {args.dns_table_reuse_interval_sec}s start ---", flush=True)
                    time.sleep(float(args.dns_table_reuse_interval_sec))
                print("--- DNS cache_table_size start ---", flush=True)
                delayed_table_cached_qnames = []
                for table_size in steps:
                    new_qnames, delayed_table_cursor = extend_cached_qnames(delayed_table_cached_qnames, table_size, delayed_table_cursor, args.dns_domain_suffix)
                    verify_qnames = list(delayed_table_cached_qnames[:table_size])
                    # The delayed pass runs after a long idle interval.  Many DUTs expire
                    # their DNS cache during that interval, so every step must explicitly
                    # preload the exact qname set that will be verified.  Without this,
                    # the verify stage merely measures expired-cache misses and reports
                    # wan_verify ~= table_size even when the cache table itself is fine.
                    preload_before = count_server_queries(server_query_log)
                    preload = run_client_load_phase_safe("cache_table_size", "preload_full", table_size, args, target, out_dir, verify_qnames, args.dns_table_qps, total_count=table_size)
                    preload_after = count_server_queries(server_query_log)
                    preload_row = decorate_summary(preload, args, target, "cache_table_size", "preload_full", table_size, preload_before, preload_after, cache_table_size=table_size)
                    rows.append(preload_row)
                    if phase_has_error(preload):
                        preload_row["stop_reason"] = "step_error"
                        print(
                            f"ERROR: cache_table_size preload failed at table={table_size}: "
                            f"{preload_row.get('error')}; moving to the next DNS item",
                            flush=True,
                        )
                        break
                    before = count_server_queries(server_query_log)
                    data = run_client_load_phase_safe("cache_table_size", "verify_reused", table_size, args, target, out_dir, verify_qnames, args.dns_table_qps, total_count=table_size)
                    time.sleep(0.2)
                    after = count_server_queries(server_query_log)
                    wan_delta = max(0, after - before)
                    overflow_pct = overflow_rate_pct(wan_delta, table_size)
                    overflow = 1 if float(overflow_pct or 0.0) >= float(args.dns_table_overflow_rate_pct) else 0
                    row = decorate_summary(data, args, target, "cache_table_size", "verify_reused", table_size, before, after, cache_table_size=table_size, cache_overflow_detected=overflow, cache_overflow_rate_pct=overflow_pct)
                    stop_reason = "step_error" if phase_has_error(data) else should_stop_table(row, args)
                    row["stop_reason"] = stop_reason
                    rows.append(row)
                    if phase_has_error(data):
                        print(
                            f"ERROR: cache_table_size verify failed at table={table_size}: "
                            f"{row.get('error')}; moving to the next DNS item",
                            flush=True,
                        )
                    else:
                        print(f"table={table_size}: success={row['success_rate_pct']:.3f}% failure={row['failure_rate_pct']:.3f}% wan_verify={row['wan_queries_delta']} wan_rate={row['cache_overflow_rate_pct']:.3f}% overflow={overflow} stop={stop_reason or '-'}", flush=True)
                    if stop_reason:
                        break


            cursor = nocache_start
            nocache_rate_qnames = []
            nocache_last_attempted_qps = None
            nocache_prev_before_failure_qps = None
            nocache_load_qps = None
            nocache_rate_usable = False
            if not args.dns_skip_nocache:
                print("--- DNS nocache_rate start ---", flush=True)
                for qps in steps:
                    count = max(1, int(round(float(qps) * float(args.duration))))
                    qnames = make_qnames(cursor, count, args.dns_domain_suffix)
                    cursor += count
                    if args.dns_randomize:
                        random.shuffle(qnames)
                    nocache_rate_qnames.extend(qnames)
                    nocache_last_attempted_qps = qps
                    before = count_server_queries(server_query_log)
                    data = run_client_load_phase_safe("nocache_rate", "qps", qps, args, target, out_dir, qnames, qps, duration_sec=args.duration)
                    after = count_server_queries(server_query_log)
                    row = decorate_summary(data, args, target, "nocache_rate", "qps", qps, before, after)
                    stop_reason = "step_error" if phase_has_error(data) else should_stop(row, args, use_loss=True, use_latency=True)
                    row["stop_reason"] = stop_reason
                    rows.append(row)
                    if phase_has_error(data):
                        print(
                            f"ERROR: nocache_rate failed at qps={qps}: {row.get('error')}; "
                            "moving to the next DNS item",
                            flush=True,
                        )
                    else:
                        nocache_rate_usable = True
                        print(f"nocache qps={qps}: success={row['success_rate_pct']:.3f}% failure={row['failure_rate_pct']:.3f}% p95={row['rtt_p95_ms']}ms max={row['rtt_max_ms']}ms wan={row['wan_queries_delta']} stop={stop_reason or '-'}", flush=True)
                        if failure_limit_hit(row, args):
                            nocache_load_qps = nocache_prev_before_failure_qps if nocache_prev_before_failure_qps is not None else qps
                        nocache_prev_before_failure_qps = qps
                    if stop_reason:
                        break

            if args.dns_run_nocache_load:
                if nocache_load_qps is None:
                    nocache_load_qps = nocache_last_attempted_qps
                if not nocache_rate_usable or nocache_load_qps is None or not nocache_rate_qnames:
                    reason = "nocache_load skipped because nocache_rate did not produce a usable step"
                    skipped = make_phase_skipped_data(
                        "nocache_load", "hold", nocache_load_qps or "", nocache_load_qps or 0,
                        args.dns_nocache_load_duration_sec, 0, reason,
                    )
                    now_count = count_server_queries(server_query_log)
                    row = decorate_summary(skipped, args, target, "nocache_load", "hold", nocache_load_qps or "", now_count, now_count)
                    row["stop_reason"] = "dependency_error"
                    rows.append(row)
                    print(f"SKIPPED: {reason}; moving to the next DNS item", flush=True)
                else:
                    print(f"--- DNS nocache_load start qps={nocache_load_qps} duration={args.dns_nocache_load_duration_sec}s qnames={len(nocache_rate_qnames)} ---", flush=True)
                    before = count_server_queries(server_query_log)
                    data = run_client_load_phase_safe(
                        "nocache_load",
                        "hold",
                        nocache_load_qps,
                        args,
                        target,
                        out_dir,
                        nocache_rate_qnames,
                        nocache_load_qps,
                        duration_sec=args.dns_nocache_load_duration_sec,
                        sample_limit_override=args.dns_nocache_load_sample_limit,
                    )
                    after = count_server_queries(server_query_log)
                    row = decorate_summary(data, args, target, "nocache_load", "hold", nocache_load_qps, before, after)
                    row["stop_reason"] = "step_error" if phase_has_error(data) else ""
                    rows.append(row)
                    if phase_has_error(data):
                        print(f"ERROR: nocache_load failed: {row.get('error')}; moving to the next DNS item", flush=True)
                    else:
                        print(f"nocache_load qps={nocache_load_qps}: success={row['success_rate_pct']:.3f}% failure={row['failure_rate_pct']:.3f}% actual_qps={row['actual_qps']:.3f} sent={row['samples_sent']} received={row['samples_received']} timeout={row['timeout_count']} error={row['error_count']} wan={row['wan_queries_delta']}", flush=True)

            if not args.dns_skip_cache:
                print("--- DNS cache_rate start ---", flush=True)
                cache_qnames = make_qnames(cache_start, args.dns_cache_preload_count, args.dns_domain_suffix)
                preload_before = count_server_queries(server_query_log)
                preload = run_client_load_phase_safe("cache_rate", "preload", args.dns_cache_preload_count, args, target, out_dir, cache_qnames, min(1000, max(1, args.dns_cache_preload_count)), total_count=args.dns_cache_preload_count)
                preload_after = count_server_queries(server_query_log)
                preload_row = decorate_summary(preload, args, target, "cache_rate", "preload", args.dns_cache_preload_count, preload_before, preload_after, cache_preload_count=args.dns_cache_preload_count)
                rows.append(preload_row)
                if phase_has_error(preload):
                    preload_row["stop_reason"] = "step_error"
                    print(
                        f"ERROR: cache_rate preload failed: {preload_row.get('error')}; "
                        "cache_rate QPS steps are skipped",
                        flush=True,
                    )
                else:
                    print(f"cache preload: success={preload_row['success_rate_pct']:.3f}% wan={preload_row['wan_queries_delta']}", flush=True)
                    for qps in cache_steps:
                        before = count_server_queries(server_query_log)
                        data = run_client_load_phase_safe("cache_rate", "qps", qps, args, target, out_dir, cache_qnames, qps, duration_sec=args.duration)
                        after = count_server_queries(server_query_log)
                        wan_miss = max(0, after - before)
                        cache_overflow_pct = overflow_rate_pct(wan_miss, args.dns_cache_preload_count)
                        cache_overflow = 1 if float(cache_overflow_pct or 0.0) >= float(args.dns_table_overflow_rate_pct) else 0
                        row = decorate_summary(
                            data,
                            args,
                            target,
                            "cache_rate",
                            "qps",
                            qps,
                            before,
                            after,
                            cache_preload_count=args.dns_cache_preload_count,
                            cache_table_size=args.dns_cache_preload_count,
                            cache_overflow_detected=cache_overflow,
                            cache_overflow_rate_pct=cache_overflow_pct,
                        )
                        # cache_rate should stop only on client-side failure/latency criteria.
                        # WAN re-query rate is still recorded as cache_overflow_rate_pct,
                        # but it must not terminate cache_rate.
                        stop_reason = "step_error" if phase_has_error(data) else should_stop(row, args, use_loss=True, use_latency=True)
                        row["stop_reason"] = stop_reason
                        rows.append(row)
                        if phase_has_error(data):
                            print(f"ERROR: cache_rate failed at qps={qps}: {row.get('error')}; moving to completion", flush=True)
                        else:
                            print(f"cache qps={qps}: success={row['success_rate_pct']:.3f}% failure={row['failure_rate_pct']:.3f}% p95={row['rtt_p95_ms']}ms max={row['rtt_max_ms']}ms wan={row['wan_queries_delta']} wan_rate={row['cache_overflow_rate_pct']:.3f}% overflow={cache_overflow} stop={stop_reason or '-'}", flush=True)
                        if stop_reason:
                            break

        finally:
            if _ARP_REFRESHER is not None:
                _ARP_REFRESHER.stop()
                _ARP_REFRESHER = None
            if server_proc is not None and server_proc.poll() is None:
                server_proc.terminate()
                wait_for_process_exit(server_proc, 60.0, "DNS server")
            for handle in (server_stdout, server_stderr):
                try:
                    if handle:
                        handle.close()
                except Exception:
                    pass
            cleanup_namespaces(args, ns_log)
            if getattr(args, "restore_dpdk_after", True):
                try:
                    prep_log = prepare_trex_interfaces_from_profile(target, out_dir, auto_dpdk_bind=True, dpdk_driver_override=getattr(args, "dpdk_driver", ""))
                    ns_log.writerow([ts_iso(), "restore_dpdk_after_done", str(prep_log)])
                except Exception as exc:
                    ns_log.writerow([ts_iso(), "restore_dpdk_after_warning", str(exc)])
                    print(f"WARNING: failed to restore DPDK driver state after DNS benchmark: {exc}", flush=True)

    summary_csv = out_dir / "summary_all.csv"
    write_summary_csv(summary_csv, rows)
    error_rows = [row for row in rows if str(row.get("result") or "").upper() in {"ERROR", "SKIPPED"}]
    print("============================================================")
    print("DONE WITH RECORDED ERRORS" if error_rows else "DONE")
    print("============================================================")
    if error_rows:
        print(f"Recorded errors    : {len(error_rows)} (see summary_all.csv result/error columns)")
    print(f"Result directory  : {out_dir}")
    print(f"Summary CSV       : {summary_csv}")
    print(f"WAN DNS query log : {server_query_log}")
    print(f"Metadata          : {out_dir / 'metadata.json'}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--_dns-server":
        run_internal_dns_server(sys.argv[2:])
    elif len(sys.argv) >= 2 and sys.argv[1] == "--_dns-load-client":
        raise SystemExit(run_internal_dns_load_client(sys.argv[2:]))
    else:
        main()
