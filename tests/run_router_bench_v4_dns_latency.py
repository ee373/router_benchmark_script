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
DEFAULT_DNS_SERVER_LOG_BATCH_SIZE = 100000  # retained for CLI compatibility; WAN logs are written at server shutdown
DEFAULT_DNS_SERVER_LOG_FLUSH_SEC = 1.0      # stats sidecar update interval; query CSV is written at server shutdown
DEFAULT_DNS_SOCKET_BUFFER_BYTES = 4 * 1024 * 1024
DEFAULT_DNS_SAMPLE_LIMIT = -1
DEFAULT_DNS_SERVER_QUERY_LOG_LIMIT = -1
DEFAULT_NOCACHE_LOAD_DURATION_SEC = 3600.0
DEFAULT_DNS_LOAD_PROGRESS_INTERVAL_SEC = 10.0
DEFAULT_DNS_LOAD_RTT_SAMPLE_LIMIT = 100000

SAMPLE_FIELDS = [
    "phase", "stage", "step_value", "sample_index", "qname", "qtype", "server_ip", "server_port",
    "unix_ts_start", "unix_ts_end", "rtt_ms", "rcode", "answers", "answer_ips", "result", "error",
]

SUMMARY_FIELDS = [
    "phase", "stage", "step_value", "test_name", "result", "target_name", "lan_ns", "wan_ns",
    "lan_iface", "wan_iface", "lan_ip", "lan_gw", "wan_ip", "router_wan_ip", "dns_client_server_ip",
    "dns_wan_server_ip", "dns_port", "dns_ttl_sec", "duration_sec", "target_qps", "actual_qps",
    "samples_requested", "samples_sent", "samples_received", "timeout_count", "error_count",
    "success_rate_pct", "failure_rate_pct", "fill_rate_pct", "rtt_min_ms", "rtt_avg_ms", "rtt_p50_ms",
    "rtt_p90_ms", "rtt_p95_ms", "rtt_p99_ms", "rtt_max_ms", "wan_queries_before", "wan_queries_after",
    "wan_queries_delta", "cache_preload_count", "cache_table_size", "cache_overflow_detected",
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


def fast_parse_dns_response(packet, expected_qid=None):
    if len(packet) < 12:
        raise ValueError("short DNS response")
    qid, flags, _qdcount, ancount, _nscount, _arcount = struct.unpack("!HHHHHH", packet[:12])
    if expected_qid is not None and qid != expected_qid:
        raise ValueError(f"unexpected DNS transaction ID: got={qid} expected={expected_qid}")
    return {"rcode": flags & 0x000F, "answers": ancount, "answer_ips": []}


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
    if not left.startswith("r"):
        return ""
    digits = left[1:]
    if not digits.isdigit():
        return ""
    idx = int(digits)
    if idx < 0 or idx >= int(max_records):
        return ""
    if str(mode).lower() == "fixed":
        return ip_for_index(base_ip, idx)
    return random_public_ip_for_qname(qname, seed)


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


def make_qnames(start, count, suffix):
    return [f"r{idx:09d}.{suffix}".lower().rstrip(".") for idx in range(int(start), int(start) + int(count))]




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
    # Keep WAN-side query logs in memory during measurement.  Disk writes are intentionally
    # delayed until the DNS server process is terminated at the end of the DNS benchmark.
    # This keeps server-side CSV I/O out of the timed load phases while preserving full logs.
    query_rows = []

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

    def write_query_log_at_end():
        if not query_rows:
            return
        with open(args.query_log, "a", newline="", encoding="utf-8", buffering=4 * 1024 * 1024) as f:
            writer = csv.writer(f)
            writer.writerows(query_rows)

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
            if int(args.query_log_limit) < 0 or total_received_all[0] <= int(args.query_log_limit) or status != "answered":
                query_rows.append([received, addr[0], addr[1], qname, qtype, status])
    finally:
        stop_event.set()
        try:
            write_stats()
            write_query_log_at_end()
            stats_thread.join(timeout=1.0)
        except Exception:
            pass


def run_dns_load_client_streaming(server_ip, port, qnames, target_qps, timeout_sec, duration_sec, total_count, phase, stage, step_value, samples_csv, sample_limit=DEFAULT_DNS_SAMPLE_LIMIT, socket_buffer_bytes=DEFAULT_DNS_SOCKET_BUFFER_BYTES, progress_interval_sec=0.0):
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
                    received_total += 1
                    rtt_ms = (t1 - t0) * 1000.0
                    ok = rcode == 0 and answers > 0
                    if ok:
                        received_ok += 1
                        record_rtt(rtt_ms)
                    else:
                        response_error_count += 1
                    if keep_row(is_failure=(not ok)):
                        sample_rows.append({
                            "phase": phase, "stage": stage, "step_value": step_value,
                            "sample_index": sample_index, "qname": qnames[qname_idx % qname_count], "qtype": "A",
                            "server_ip": server_ip, "server_port": port,
                            "unix_ts_start": start_unix + (t0 - start_perf),
                            "unix_ts_end": start_unix + (t1 - start_perf),
                            "rtt_ms": rtt_ms, "rcode": rcode, "answers": answers, "answer_ips": "",
                            "result": "PASS" if ok else "FAIL",
                            "error": "" if ok else f"rcode={rcode} answers={answers}",
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
        "timeout_count": timeout_count,
        "error_count": send_error_count + response_error_count,
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


def run_dns_load_client(server_ip, port, qnames, target_qps, timeout_sec, duration_sec, total_count, phase, stage, step_value, samples_csv, sample_limit=DEFAULT_DNS_SAMPLE_LIMIT, socket_buffer_bytes=DEFAULT_DNS_SOCKET_BUFFER_BYTES, progress_interval_sec=0.0):
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
            phase, stage, step_value, samples_csv, sample_limit=sample_limit,
            socket_buffer_bytes=socket_buffer_bytes, progress_interval_sec=progress_interval_sec,
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

    samples_path = Path(samples_csv) if samples_csv else None
    sample_limit = int(sample_limit)

    # Raw per-query state.  These arrays are filled during measurement and interpreted
    # after the step ends.  Keeping aggregation out of the hot loop reduces CPU spikes.
    send_times = [0.0] * total
    recv_times = [0.0] * total
    qname_indices = [0] * total
    rcodes = [-1] * total
    answer_counts = [0] * total
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
        ok = got_response and rcodes[i] == 0 and answer_counts[i] > 0
        rtt_ms = (recv_times[i] - send_times[i]) * 1000.0 if got_response and send_times[i] > 0.0 else ""

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
            result = "FAIL"
            err = error_messages[i] or f"rcode={rcodes[i]} answers={answer_counts[i]}"
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
                "answer_ips": "",
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
        "timeout_count": timeout_count,
        "error_count": error_count,
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
    result = run_dns_load_client(
        args.server_ip, args.port, qnames, args.target_qps, args.timeout_sec,
        args.duration_sec, args.total_count, args.phase, args.stage, args.step_value, args.samples_csv,
        sample_limit=args.sample_limit, socket_buffer_bytes=args.socket_buffer_bytes,
        progress_interval_sec=args.progress_interval_sec,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("samples_received", 0) > 0 else 1


def count_server_queries(path):
    # The WAN DNS server writes logs asynchronously and publishes a sidecar counter.
    # Reading the sidecar avoids repeatedly scanning multi-million-line CSV files.
    time.sleep(max(0.02, DEFAULT_DNS_SERVER_LOG_FLUSH_SEC * 2.0))
    stats_path = Path(str(path) + ".stats.json")
    if stats_path.exists():
        try:
            data = json.loads(stats_path.read_text(encoding="utf-8"))
            return int(data.get("benchmark_queries", data.get("total_queries", 0)) or 0)
        except Exception:
            pass
    if not path.exists():
        return 0
    with open(path, newline="", encoding="utf-8") as f:
        return max(0, sum(1 for _ in f) - 1)


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


def run_client_load_phase(phase, stage, step_value, args, target, out_dir, qnames, target_qps, duration_sec=0.0, total_count=0, sample_limit_override=None):
    qnames_path = out_dir / f"qnames_{phase}_{stage}_{step_value}.json"
    safe_step = str(step_value).replace(".", "_")
    samples_csv = out_dir / f"dns_{phase}_{stage}_{safe_step}_samples.csv"
    summary_json = out_dir / f"dns_{phase}_{stage}_{safe_step}.json"
    stderr_path = out_dir / f"dns_{phase}_{stage}_{safe_step}.stderr.log"
    qnames_path.write_text(json.dumps(qnames, ensure_ascii=False), encoding="utf-8")
    server_ip = args.dns_client_server_ip or target["lan_gw"]
    cmd = ns_cmd(args.lan_ns, [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_dns-load-client",
        "--server-ip", server_ip,
        "--port", str(args.dns_port),
        "--qnames-json", str(qnames_path),
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
        data = {"phase": phase, "stage": stage, "step_value": step_value, "samples_requested": expected_total, "samples_sent": 0, "samples_received": 0, "timeout_count": 0, "error_count": 1, "success_rate_pct": 0.0, "failure_rate_pct": 100.0, "fill_rate_pct": 100.0, "error": f"failed to parse DNS client JSON: {exc}"}
    data["client_returncode"] = proc.returncode
    data["samples_csv"] = str(samples_csv)
    return data


def decorate_summary(base, args, target, phase, stage, step_value, wan_before, wan_after, **extra):
    requested = int(base.get("samples_requested") or 0)
    received = int(base.get("samples_received") or 0)
    wan_delta = max(0, int(wan_after) - int(wan_before))
    row = {
        "phase": phase,
        "stage": stage,
        "step_value": step_value,
        "test_name": f"dns_{phase}_{stage}_{step_value}",
        "result": "PASS" if received > 0 else "FAIL",
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
        "timeout_count": base.get("timeout_count", ""),
        "error_count": base.get("error_count", ""),
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
            "WAN counters used for cache decisions include only generated benchmark qnames; unrelated router background DNS traffic remains in dns_server_queries.csv but is excluded from wan_queries_delta.",
            "WAN re-query count divided by verified table size is recorded as cache_overflow_rate_pct. Table-size tests stop when it reaches --dns-table-overflow-rate-pct.",
            "fill_rate_pct is reported as failure_rate_pct for compatibility with the requested wording.",
        ],
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def main():
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
    parser.add_argument("--wait-interfaces-ready", dest="wait_interfaces_ready", action="store_true", default=True)
    parser.add_argument("--no-wait-interfaces-ready", dest="wait_interfaces_ready", action="store_false")
    parser.add_argument("--interface-ready-timeout-sec", type=parse_positive_float, default=DEFAULT_INTERFACE_READY_TIMEOUT_SEC)

    parser.add_argument("--duration", type=parse_positive_float, default=DEFAULT_DNS_DURATION_SEC, help="Measurement duration in seconds for each QPS step.")
    parser.add_argument("--dns-port", type=parse_positive_int, default=DEFAULT_DNS_PORT)
    parser.add_argument("--dns-domain-suffix", default=DEFAULT_DOMAIN_SUFFIX)
    parser.add_argument("--dns-answer-mode", choices=["random", "fixed"], default="random", help="A-record answer mode. Default: random deterministic public-looking addresses.")
    parser.add_argument("--dns-answer-base-ip", default="93.184.0.1", help="Base A-record answer address used when --dns-answer-mode fixed is selected.")
    parser.add_argument("--dns-answer-random-seed", type=int, default=20260621, help="Seed for deterministic random public-looking DNS answer addresses.")
    parser.add_argument("--dns-ttl-sec", type=parse_positive_int, default=DEFAULT_DNS_TTL_SEC)
    parser.add_argument("--dns-timeout-sec", type=parse_positive_float, default=DEFAULT_DNS_TIMEOUT_SEC)
    parser.add_argument("--dns-sample-limit", type=int, default=DEFAULT_DNS_SAMPLE_LIMIT, help="Per-step sample CSV rows. -1 logs all rows after each step, 0 disables per-query sample CSV output, positive values keep first N rows plus important failures.")
    parser.add_argument("--dns-server-query-log-limit", type=int, default=DEFAULT_DNS_SERVER_QUERY_LOG_LIMIT, help="Maximum WAN DNS query rows written to dns_server_queries.csv. Default -1 logs all rows. Sidecar counters still count all queries.")
    parser.add_argument("--dns-socket-buffer-bytes", type=parse_positive_int, default=DEFAULT_DNS_SOCKET_BUFFER_BYTES, help="UDP socket send/receive buffer size for built-in DNS server/client.")
    parser.add_argument("--dns-server-log-batch-size", type=parse_positive_int, default=DEFAULT_DNS_SERVER_LOG_BATCH_SIZE, help="Deprecated compatibility option. WAN query logs are kept in memory and written when the DNS server exits.")
    parser.add_argument("--dns-server-log-flush-sec", type=parse_positive_float, default=DEFAULT_DNS_SERVER_LOG_FLUSH_SEC, help="Stats sidecar update interval in seconds. Query CSV rows are written when the DNS server exits.")
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
    with open(server_query_log, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(["unix_ts", "client_ip", "client_port", "qname", "qtype", "result"])

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
                        preload = run_client_load_phase("cache_table_reuse_size", "preload_added", table_size, args, target, out_dir, new_qnames, args.dns_table_qps, total_count=len(new_qnames))
                        preload_after = count_server_queries(server_query_log)
                        preload_row = decorate_summary(preload, args, target, "cache_table_reuse_size", "preload_added", table_size, preload_before, preload_after, cache_table_size=table_size)
                        rows.append(preload_row)
                    verify_qnames = list(table_cached_qnames[:table_size])
                    before = count_server_queries(server_query_log)
                    data = run_client_load_phase("cache_table_reuse_size", "verify_reused", table_size, args, target, out_dir, verify_qnames, args.dns_table_qps, total_count=table_size)
                    time.sleep(0.2)
                    after = count_server_queries(server_query_log)
                    wan_delta = max(0, after - before)
                    overflow_pct = overflow_rate_pct(wan_delta, table_size)
                    overflow = 1 if float(overflow_pct or 0.0) >= float(args.dns_table_overflow_rate_pct) else 0
                    row = decorate_summary(data, args, target, "cache_table_reuse_size", "verify_reused", table_size, before, after, cache_table_size=table_size, cache_overflow_detected=overflow, cache_overflow_rate_pct=overflow_pct)
                    stop_reason = should_stop_table(row, args)
                    row["stop_reason"] = stop_reason
                    rows.append(row)
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
                    preload = run_client_load_phase("cache_table_size", "preload_full", table_size, args, target, out_dir, verify_qnames, args.dns_table_qps, total_count=table_size)
                    preload_after = count_server_queries(server_query_log)
                    preload_row = decorate_summary(preload, args, target, "cache_table_size", "preload_full", table_size, preload_before, preload_after, cache_table_size=table_size)
                    rows.append(preload_row)
                    before = count_server_queries(server_query_log)
                    data = run_client_load_phase("cache_table_size", "verify_reused", table_size, args, target, out_dir, verify_qnames, args.dns_table_qps, total_count=table_size)
                    time.sleep(0.2)
                    after = count_server_queries(server_query_log)
                    wan_delta = max(0, after - before)
                    overflow_pct = overflow_rate_pct(wan_delta, table_size)
                    overflow = 1 if float(overflow_pct or 0.0) >= float(args.dns_table_overflow_rate_pct) else 0
                    row = decorate_summary(data, args, target, "cache_table_size", "verify_reused", table_size, before, after, cache_table_size=table_size, cache_overflow_detected=overflow, cache_overflow_rate_pct=overflow_pct)
                    stop_reason = should_stop_table(row, args)
                    row["stop_reason"] = stop_reason
                    rows.append(row)
                    print(f"table={table_size}: success={row['success_rate_pct']:.3f}% failure={row['failure_rate_pct']:.3f}% wan_verify={row['wan_queries_delta']} wan_rate={row['cache_overflow_rate_pct']:.3f}% overflow={overflow} stop={stop_reason or '-'}", flush=True)
                    if stop_reason:
                        break


            cursor = nocache_start
            nocache_rate_qnames = []
            nocache_last_attempted_qps = None
            nocache_prev_before_failure_qps = None
            nocache_load_qps = None
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
                    data = run_client_load_phase("nocache_rate", "qps", qps, args, target, out_dir, qnames, qps, duration_sec=args.duration)
                    after = count_server_queries(server_query_log)
                    row = decorate_summary(data, args, target, "nocache_rate", "qps", qps, before, after)
                    stop_reason = should_stop(row, args, use_loss=True, use_latency=True)
                    row["stop_reason"] = stop_reason
                    rows.append(row)
                    print(f"nocache qps={qps}: success={row['success_rate_pct']:.3f}% failure={row['failure_rate_pct']:.3f}% p95={row['rtt_p95_ms']}ms max={row['rtt_max_ms']}ms wan={row['wan_queries_delta']} stop={stop_reason or '-'}", flush=True)
                    if failure_limit_hit(row, args):
                        nocache_load_qps = nocache_prev_before_failure_qps if nocache_prev_before_failure_qps is not None else qps
                    if stop_reason:
                        break
                    nocache_prev_before_failure_qps = qps

            if args.dns_run_nocache_load:
                if nocache_load_qps is None:
                    nocache_load_qps = nocache_last_attempted_qps
                if nocache_load_qps is None or not nocache_rate_qnames:
                    raise RuntimeError("nocache_load has no qnames or QPS. nocache_rate must complete at least one step.")
                print(f"--- DNS nocache_load start qps={nocache_load_qps} duration={args.dns_nocache_load_duration_sec}s qnames={len(nocache_rate_qnames)} ---", flush=True)
                before = count_server_queries(server_query_log)
                data = run_client_load_phase(
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
                row["stop_reason"] = ""
                rows.append(row)
                print(f"nocache_load qps={nocache_load_qps}: success={row['success_rate_pct']:.3f}% failure={row['failure_rate_pct']:.3f}% actual_qps={row['actual_qps']:.3f} sent={row['samples_sent']} received={row['samples_received']} timeout={row['timeout_count']} error={row['error_count']} wan={row['wan_queries_delta']}", flush=True)

            if not args.dns_skip_cache:
                print("--- DNS cache_rate start ---", flush=True)
                cache_qnames = make_qnames(cache_start, args.dns_cache_preload_count, args.dns_domain_suffix)
                preload_before = count_server_queries(server_query_log)
                preload = run_client_load_phase("cache_rate", "preload", args.dns_cache_preload_count, args, target, out_dir, cache_qnames, min(1000, max(1, args.dns_cache_preload_count)), total_count=args.dns_cache_preload_count)
                preload_after = count_server_queries(server_query_log)
                preload_row = decorate_summary(preload, args, target, "cache_rate", "preload", args.dns_cache_preload_count, preload_before, preload_after, cache_preload_count=args.dns_cache_preload_count)
                rows.append(preload_row)
                print(f"cache preload: success={preload_row['success_rate_pct']:.3f}% wan={preload_row['wan_queries_delta']}", flush=True)
                for qps in cache_steps:
                    before = count_server_queries(server_query_log)
                    data = run_client_load_phase("cache_rate", "qps", qps, args, target, out_dir, cache_qnames, qps, duration_sec=args.duration)
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
                    stop_reason = should_stop(row, args, use_loss=True, use_latency=True)
                    row["stop_reason"] = stop_reason
                    rows.append(row)
                    print(f"cache qps={qps}: success={row['success_rate_pct']:.3f}% failure={row['failure_rate_pct']:.3f}% p95={row['rtt_p95_ms']}ms max={row['rtt_max_ms']}ms wan={row['wan_queries_delta']} wan_rate={row['cache_overflow_rate_pct']:.3f}% overflow={cache_overflow} stop={stop_reason or '-'}", flush=True)
                    if stop_reason:
                        break

        finally:
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
    print("============================================================")
    print("DONE")
    print("============================================================")
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
