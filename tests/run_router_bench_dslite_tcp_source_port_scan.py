#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DS-Lite TCP source-port reachability scan using only Python sockets."""

import argparse
import csv
import datetime as dt
import json
import selectors
import signal
import socket
import struct
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from tests.lib.common import (
        DEFAULT_RESULT_ROOT,
        parse_non_negative_float,
        parse_positive_float,
        parse_positive_int,
        ts_compact,
        ts_iso,
    )
    from tests.lib.config import DSLITE_TARGETS, load_defaults
    from tests.lib.dslite import build_trex_target, validate_dslite_profile
    from tests.lib.network import (
        DEFAULT_INTERFACE_READY_TIMEOUT_SEC,
        DEFAULT_LAN_NS,
        DEFAULT_LAN_PREFIX,
        DEFAULT_POST_NAMESPACE_SETTLE_SEC,
        DEFAULT_WAN_NS,
        DEFAULT_WAN_PREFIX,
        cleanup_namespaces,
        delete_existing_namespaces,
        make_namespace_arp_refresher,
        ns_cmd,
        prepare_kernel_interfaces_from_profile,
        prepare_trex_interfaces_from_profile,
        require_command,
        require_profile_interfaces,
        require_root,
        resolve_profile_interfaces,
        setup_namespaces,
        verify_namespaces,
        wait_test_interfaces_ready,
    )
    from tests.lib.output import prepare_details_dir
    from tests.lib.repeat import DEFAULT_REPEAT, argparse_repeat
    from tests.lib.timing import DEFAULT_TEST_START_DELAY_SEC, wait_before_test_start
except Exception as exc:  # pragma: no cover - direct execution compatibility
    try:
        from lib.common import (
            DEFAULT_RESULT_ROOT,
            parse_non_negative_float,
            parse_positive_float,
            parse_positive_int,
            ts_compact,
            ts_iso,
        )
        from lib.config import DSLITE_TARGETS, load_defaults
        from lib.dslite import build_trex_target, validate_dslite_profile
        from lib.network import (
            DEFAULT_INTERFACE_READY_TIMEOUT_SEC,
            DEFAULT_LAN_NS,
            DEFAULT_LAN_PREFIX,
            DEFAULT_POST_NAMESPACE_SETTLE_SEC,
            DEFAULT_WAN_NS,
            DEFAULT_WAN_PREFIX,
            cleanup_namespaces,
            delete_existing_namespaces,
            make_namespace_arp_refresher,
            ns_cmd,
            prepare_kernel_interfaces_from_profile,
            prepare_trex_interfaces_from_profile,
            require_command,
            require_profile_interfaces,
            require_root,
            resolve_profile_interfaces,
            setup_namespaces,
            verify_namespaces,
            wait_test_interfaces_ready,
        )
        from lib.output import prepare_details_dir
        from lib.repeat import DEFAULT_REPEAT, argparse_repeat
        from lib.timing import DEFAULT_TEST_START_DELAY_SEC, wait_before_test_start
    except Exception:
        raise SystemExit(f"ERROR: failed to import shared benchmark helpers: {exc}")


_DEFAULTS = load_defaults("dslite_source_port_scan")
_NETWORK_DEFAULTS = load_defaults("network")

REQUEST = struct.Struct("!4sHI")
RESPONSE = struct.Struct("!4sHI")
REQUEST_MAGIC = b"RBSP"
RESPONSE_MAGIC = b"RBOK"

RESULT_FIELDS = [
    "ts_local",
    "repeat",
    "scan_index",
    "source_port",
    "local_ip",
    "local_port",
    "server_ip",
    "server_port",
    "result",
    "success",
    "bind_ms",
    "connect_ms",
    "round_trip_ms",
    "total_ms",
    "errno",
    "error",
]

RANGE_FIELDS = [
    "low_port",
    "high_port",
    "classification",
    "detail",
    "tested_port_count",
    "attempt_count",
    "success_count",
    "failure_count",
]

RECOVERY_FIELDS = [
    "ts_local",
    "repeat",
    "batch",
    "probe",
    "baseline_source_port",
    "result",
    "success",
    "probe_total_ms",
    "recovery_elapsed_sec",
]


def parse_port(value):
    try:
        port = int(str(value).replace("_", ""))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def build_scan_ports(start_port, end_port, step=1):
    """Return the requested descending source-port sequence."""
    start_port = int(start_port)
    end_port = int(end_port)
    step = int(step)
    if not 1 <= end_port <= start_port <= 65535:
        raise ValueError("scan_start_port must be greater than or equal to scan_end_port")
    if step <= 0:
        raise ValueError("scan step must be positive")
    return list(range(start_port, end_port - 1, -step))


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = int(round((len(ordered) - 1) * float(fraction)))
    return ordered[max(0, min(index, len(ordered) - 1))]


def classify_port_results(rows):
    """Classify each tested port and compress adjacent equal classifications."""
    by_port = defaultdict(list)
    for row in rows:
        by_port[int(row["source_port"])].append(row)

    per_port = []
    for port in sorted(by_port):
        port_rows = by_port[port]
        success_count = sum(int(row.get("success", 0)) for row in port_rows)
        failure_count = len(port_rows) - success_count
        if success_count == len(port_rows):
            classification = "usable"
            detail = "success"
        elif success_count == 0:
            failures = sorted({str(row.get("result", "unknown")) for row in port_rows})
            classification = "unusable"
            detail = failures[0] if len(failures) == 1 else "mixed_failure"
        else:
            classification = "intermittent"
            outcomes = sorted({str(row.get("result", "unknown")) for row in port_rows})
            detail = "+".join(outcomes)
        per_port.append(
            {
                "port": port,
                "classification": classification,
                "detail": detail,
                "attempt_count": len(port_rows),
                "success_count": success_count,
                "failure_count": failure_count,
            }
        )

    ranges = []
    for item in per_port:
        if (
            ranges
            and item["port"] == ranges[-1]["high_port"] + 1
            and item["classification"] == ranges[-1]["classification"]
            and item["detail"] == ranges[-1]["detail"]
        ):
            current = ranges[-1]
            current["high_port"] = item["port"]
            current["tested_port_count"] += 1
            current["attempt_count"] += item["attempt_count"]
            current["success_count"] += item["success_count"]
            current["failure_count"] += item["failure_count"]
        else:
            ranges.append(
                {
                    "low_port": item["port"],
                    "high_port": item["port"],
                    "classification": item["classification"],
                    "detail": item["detail"],
                    "tested_port_count": 1,
                    "attempt_count": item["attempt_count"],
                    "success_count": item["success_count"],
                    "failure_count": item["failure_count"],
                }
            )

    boundaries = []
    for previous, current in zip(per_port, per_port[1:]):
        if (
            current["port"] == previous["port"] + 1
            and current["classification"] != previous["classification"]
        ):
            boundaries.append(
                {
                    "lower_port": previous["port"],
                    "lower_classification": previous["classification"],
                    "upper_port": current["port"],
                    "upper_classification": current["classification"],
                }
            )
    return per_port, ranges, boundaries


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def result_row(
    *,
    repeat,
    scan_index,
    source_port,
    client_ip,
    server_ip,
    server_port,
    result,
    success,
    started,
    bound_at=None,
    connected_at=None,
    finished_at=None,
    local_port=0,
    exc=None,
):
    finished_at = finished_at if finished_at is not None else time.monotonic()
    error_number = getattr(exc, "errno", "") if exc is not None else ""
    error_text = str(exc) if exc is not None else ""
    return {
        "ts_local": dt.datetime.now().isoformat(timespec="milliseconds"),
        "repeat": int(repeat),
        "scan_index": int(scan_index),
        "source_port": int(source_port),
        "local_ip": str(client_ip),
        "local_port": int(local_port or 0),
        "server_ip": str(server_ip),
        "server_port": int(server_port),
        "result": str(result),
        "success": int(bool(success)),
        "bind_ms": round(((bound_at or finished_at) - started) * 1000.0, 3),
        "connect_ms": (
            round((connected_at - (bound_at or started)) * 1000.0, 3)
            if connected_at is not None
            else ""
        ),
        "round_trip_ms": (
            round((finished_at - connected_at) * 1000.0, 3)
            if connected_at is not None
            else ""
        ),
        "total_ms": round((finished_at - started) * 1000.0, 3),
        "errno": error_number if error_number is not None else "",
        "error": error_text,
    }


def recv_exact(sock, size):
    chunks = bytearray()
    while len(chunks) < size:
        data = sock.recv(size - len(chunks))
        if not data:
            raise ConnectionError("connection closed before the complete response")
        chunks.extend(data)
    return bytes(chunks)


def tcp_probe(
    *,
    client_ip,
    source_port,
    server_ip,
    server_port,
    connect_timeout_sec,
    io_timeout_sec,
    repeat,
    scan_index,
):
    """Bind one explicit source port and verify one TCP request/response."""
    started = time.monotonic()
    bound_at = None
    connected_at = None
    local_port = 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # An abortive close prevents a completed probe from occupying TIME_WAIT
        # and from retaining a DS-Lite/NAT mapping throughout a long scan.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        try:
            sock.bind((str(client_ip), int(source_port)))
            local_port = int(sock.getsockname()[1])
            bound_at = time.monotonic()
        except OSError as exc:
            return result_row(
                repeat=repeat,
                scan_index=scan_index,
                source_port=source_port,
                client_ip=client_ip,
                server_ip=server_ip,
                server_port=server_port,
                result="bind_error",
                success=False,
                started=started,
                local_port=local_port,
                exc=exc,
            )

        sock.settimeout(float(connect_timeout_sec))
        try:
            sock.connect((str(server_ip), int(server_port)))
            connected_at = time.monotonic()
        except socket.timeout as exc:
            return result_row(
                repeat=repeat,
                scan_index=scan_index,
                source_port=source_port,
                client_ip=client_ip,
                server_ip=server_ip,
                server_port=server_port,
                result="connect_timeout",
                success=False,
                started=started,
                bound_at=bound_at,
                local_port=local_port,
                exc=exc,
            )
        except ConnectionRefusedError as exc:
            return result_row(
                repeat=repeat,
                scan_index=scan_index,
                source_port=source_port,
                client_ip=client_ip,
                server_ip=server_ip,
                server_port=server_port,
                result="connect_refused",
                success=False,
                started=started,
                bound_at=bound_at,
                local_port=local_port,
                exc=exc,
            )
        except OSError as exc:
            return result_row(
                repeat=repeat,
                scan_index=scan_index,
                source_port=source_port,
                client_ip=client_ip,
                server_ip=server_ip,
                server_port=server_port,
                result="connect_error",
                success=False,
                started=started,
                bound_at=bound_at,
                local_port=local_port,
                exc=exc,
            )

        sock.settimeout(float(io_timeout_sec))
        request = REQUEST.pack(REQUEST_MAGIC, local_port, int(scan_index) & 0xFFFFFFFF)
        try:
            sock.sendall(request)
        except socket.timeout as exc:
            return result_row(
                repeat=repeat,
                scan_index=scan_index,
                source_port=source_port,
                client_ip=client_ip,
                server_ip=server_ip,
                server_port=server_port,
                result="send_timeout",
                success=False,
                started=started,
                bound_at=bound_at,
                connected_at=connected_at,
                local_port=local_port,
                exc=exc,
            )
        except OSError as exc:
            return result_row(
                repeat=repeat,
                scan_index=scan_index,
                source_port=source_port,
                client_ip=client_ip,
                server_ip=server_ip,
                server_port=server_port,
                result="send_error",
                success=False,
                started=started,
                bound_at=bound_at,
                connected_at=connected_at,
                local_port=local_port,
                exc=exc,
            )

        try:
            response = recv_exact(sock, RESPONSE.size)
        except socket.timeout as exc:
            return result_row(
                repeat=repeat,
                scan_index=scan_index,
                source_port=source_port,
                client_ip=client_ip,
                server_ip=server_ip,
                server_port=server_port,
                result="receive_timeout",
                success=False,
                started=started,
                bound_at=bound_at,
                connected_at=connected_at,
                local_port=local_port,
                exc=exc,
            )
        except ConnectionError as exc:
            return result_row(
                repeat=repeat,
                scan_index=scan_index,
                source_port=source_port,
                client_ip=client_ip,
                server_ip=server_ip,
                server_port=server_port,
                result="connection_closed",
                success=False,
                started=started,
                bound_at=bound_at,
                connected_at=connected_at,
                local_port=local_port,
                exc=exc,
            )
        except OSError as exc:
            return result_row(
                repeat=repeat,
                scan_index=scan_index,
                source_port=source_port,
                client_ip=client_ip,
                server_ip=server_ip,
                server_port=server_port,
                result="receive_error",
                success=False,
                started=started,
                bound_at=bound_at,
                connected_at=connected_at,
                local_port=local_port,
                exc=exc,
            )

        expected = RESPONSE.pack(RESPONSE_MAGIC, local_port, int(scan_index) & 0xFFFFFFFF)
        finished_at = time.monotonic()
        if response != expected:
            return result_row(
                repeat=repeat,
                scan_index=scan_index,
                source_port=source_port,
                client_ip=client_ip,
                server_ip=server_ip,
                server_port=server_port,
                result="response_mismatch",
                success=False,
                started=started,
                bound_at=bound_at,
                connected_at=connected_at,
                finished_at=finished_at,
                local_port=local_port,
                exc=ValueError(f"expected={expected.hex()} received={response.hex()}"),
            )
        return result_row(
            repeat=repeat,
            scan_index=scan_index,
            source_port=source_port,
            client_ip=client_ip,
            server_ip=server_ip,
            server_port=server_port,
            result="success",
            success=True,
            started=started,
            bound_at=bound_at,
            connected_at=connected_at,
            finished_at=finished_at,
            local_port=local_port,
        )
    except Exception as exc:  # pragma: no cover - defensive result capture
        return result_row(
            repeat=repeat,
            scan_index=scan_index,
            source_port=source_port,
            client_ip=client_ip,
            server_ip=server_ip,
            server_port=server_port,
            result="unexpected_error",
            success=False,
            started=started,
            bound_at=bound_at,
            connected_at=connected_at,
            local_port=local_port,
            exc=exc,
        )
    finally:
        sock.close()


@dataclass
class ServerConnection:
    sock: socket.socket
    received: bytearray = field(default_factory=bytearray)
    pending: bytearray = field(default_factory=bytearray)


def run_server(args):
    ready_path = Path(args.ready_file)
    summary_path = Path(args.server_summary_file)
    ready_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    stopping = False
    counters = Counter()
    started = time.monotonic()

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    selector = selectors.DefaultSelector()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.setblocking(False)
    connections = {}
    try:
        listener.bind((str(args.bind_ip), int(args.server_port)))
        listener.listen(int(args.listen_backlog))
        selector.register(listener, selectors.EVENT_READ, data=None)
        write_json(
            ready_path,
            {
                "ready": 1,
                "ts_local": ts_iso(),
                "bind_ip": args.bind_ip,
                "server_port": args.server_port,
            },
        )
        while not stopping:
            for key, mask in selector.select(timeout=0.1):
                if key.data is None:
                    while True:
                        try:
                            conn, _address = listener.accept()
                        except BlockingIOError:
                            break
                        conn.setblocking(False)
                        state = ServerConnection(conn)
                        connections[conn.fileno()] = state
                        selector.register(conn, selectors.EVENT_READ, data=state)
                        counters["accept_total"] += 1
                    continue

                state = key.data
                conn = state.sock
                if mask & selectors.EVENT_READ:
                    try:
                        data = conn.recv(REQUEST.size - len(state.received))
                    except (BlockingIOError, InterruptedError):
                        data = None
                    except OSError:
                        counters["recv_error_total"] += 1
                        data = b""
                    if data:
                        state.received.extend(data)
                        if len(state.received) == REQUEST.size:
                            magic, source_port, scan_index = REQUEST.unpack(bytes(state.received))
                            if magic != REQUEST_MAGIC:
                                counters["parse_error_total"] += 1
                                state.pending = bytearray()
                            else:
                                state.pending = bytearray(
                                    RESPONSE.pack(RESPONSE_MAGIC, source_port, scan_index)
                                )
                                counters["request_total"] += 1
                                selector.modify(conn, selectors.EVENT_WRITE, data=state)
                    elif data == b"":
                        counters["early_close_total"] += 1
                        try:
                            selector.unregister(conn)
                        except Exception:
                            pass
                        connections.pop(conn.fileno(), None)
                        conn.close()

                if mask & selectors.EVENT_WRITE and state.pending:
                    try:
                        sent = conn.send(state.pending)
                    except (BlockingIOError, InterruptedError):
                        sent = 0
                    except OSError:
                        counters["send_error_total"] += 1
                        sent = -1
                    if sent > 0:
                        del state.pending[:sent]
                    if sent < 0 or not state.pending:
                        if sent >= 0:
                            counters["response_total"] += 1
                        try:
                            selector.unregister(conn)
                        except Exception:
                            pass
                        connections.pop(conn.fileno(), None)
                        conn.close()
        return 0
    finally:
        for state in list(connections.values()):
            try:
                selector.unregister(state.sock)
            except Exception:
                pass
            state.sock.close()
        try:
            selector.unregister(listener)
        except Exception:
            pass
        listener.close()
        selector.close()
        write_json(
            summary_path,
            {
                "result": "stopped",
                "ts_local": ts_iso(),
                "elapsed_sec": time.monotonic() - started,
                "bind_ip": args.bind_ip,
                "server_port": args.server_port,
                **dict(counters),
            },
        )


def run_preflight(args, details_dir):
    rows = []
    consecutive = 0
    preflight_path = details_dir / "source_port_scan_preflight.csv"
    with preflight_path.open("w", newline="", encoding="utf-8", buffering=1) as output:
        writer = csv.DictWriter(output, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        for attempt in range(1, int(args.preflight_attempts) + 1):
            row = tcp_probe(
                client_ip=args.client_ip,
                source_port=int(args.preflight_source_port),
                server_ip=args.server_ip,
                server_port=args.server_port,
                connect_timeout_sec=args.connect_timeout_sec,
                io_timeout_sec=args.io_timeout_sec,
                repeat=0,
                scan_index=0xF0000000 + attempt,
            )
            rows.append(row)
            writer.writerow(row)
            if int(row["success"]):
                consecutive += 1
            else:
                consecutive = 0
            if consecutive >= int(args.preflight_success_consecutive):
                break
            if args.preflight_interval_sec > 0:
                time.sleep(args.preflight_interval_sec)
    summary = {
        "preflight_ok": int(consecutive >= int(args.preflight_success_consecutive)),
        "source_port_mode": "automatic" if int(args.preflight_source_port) == 0 else "fixed",
        "requested_source_port": int(args.preflight_source_port),
        "attempts": len(rows),
        "required_consecutive_successes": int(args.preflight_success_consecutive),
        "final_consecutive_successes": consecutive,
        "selected_local_ports": [int(row["local_port"]) for row in rows],
        "results": [str(row["result"]) for row in rows],
        "baseline_source_port": next(
            (int(row["local_port"]) for row in reversed(rows) if int(row["success"])),
            0,
        ),
    }
    write_json(details_dir / "source_port_scan_preflight.json", summary)
    return bool(summary["preflight_ok"]), summary


def wait_for_batch_recovery(
    args,
    *,
    repeat_number,
    batch_number,
    baseline_source_port,
    writer,
):
    """Wait until the known-good source port works after one scan batch."""
    started = time.monotonic()
    consecutive = 0
    probe_number = 0
    while time.monotonic() - started <= float(args.recovery_timeout_sec):
        probe_number += 1
        row = tcp_probe(
            client_ip=args.client_ip,
            source_port=int(baseline_source_port),
            server_ip=args.server_ip,
            server_port=args.server_port,
            connect_timeout_sec=args.connect_timeout_sec,
            io_timeout_sec=args.io_timeout_sec,
            repeat=0,
            scan_index=0xE0000000 + probe_number,
        )
        if int(row["success"]):
            consecutive += 1
        else:
            consecutive = 0
        writer.writerow(
            {
                "ts_local": row["ts_local"],
                "repeat": repeat_number,
                "batch": batch_number,
                "probe": probe_number,
                "baseline_source_port": baseline_source_port,
                "result": row["result"],
                "success": row["success"],
                "probe_total_ms": row["total_ms"],
                "recovery_elapsed_sec": round(time.monotonic() - started, 3),
            }
        )
        if consecutive >= int(args.recovery_success_consecutive):
            return True, {
                "ok": 1,
                "repeat": repeat_number,
                "batch": batch_number,
                "probe_count": probe_number,
                "elapsed_sec": time.monotonic() - started,
                "baseline_source_port": baseline_source_port,
            }
        if args.recovery_probe_interval_sec > 0:
            time.sleep(args.recovery_probe_interval_sec)
    return False, {
        "ok": 0,
        "repeat": repeat_number,
        "batch": batch_number,
        "probe_count": probe_number,
        "elapsed_sec": time.monotonic() - started,
        "baseline_source_port": baseline_source_port,
    }


def execute_scan_pass(
    args,
    repeat_number,
    ports,
    writer,
    all_rows,
    progress_state,
    *,
    scan_index_base,
    total_planned,
):
    started = time.monotonic()
    interval = 1.0 / float(args.scan_cps)
    next_submit = started
    pending = {}
    next_index = 0
    completed = 0
    success_count = 0
    failure_count = 0
    next_progress = float(progress_state.setdefault("next_progress", started))

    with ThreadPoolExecutor(max_workers=int(args.concurrency)) as executor:
        while next_index < len(ports) or pending:
            now = time.monotonic()
            while (
                next_index < len(ports)
                and len(pending) < int(args.concurrency)
                and now >= next_submit
            ):
                source_port = ports[next_index]
                global_index = int(scan_index_base) + next_index + 1
                future = executor.submit(
                    tcp_probe,
                    client_ip=args.client_ip,
                    source_port=source_port,
                    server_ip=args.server_ip,
                    server_port=args.server_port,
                    connect_timeout_sec=args.connect_timeout_sec,
                    io_timeout_sec=args.io_timeout_sec,
                    repeat=repeat_number,
                    scan_index=global_index,
                )
                pending[future] = source_port
                next_index += 1
                next_submit = max(next_submit + interval, now)
                now = time.monotonic()

            if not pending:
                time.sleep(min(0.05, max(0.0, next_submit - time.monotonic())))
                continue

            timeout = 0.05
            if next_index < len(ports) and len(pending) < int(args.concurrency):
                timeout = min(timeout, max(0.0, next_submit - time.monotonic()))
            done, _not_done = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
            for future in done:
                source_port = pending.pop(future)
                try:
                    row = future.result()
                except Exception as exc:  # pragma: no cover - executor defense
                    row = result_row(
                        repeat=repeat_number,
                        scan_index=int(scan_index_base) + completed + 1,
                        source_port=source_port,
                        client_ip=args.client_ip,
                        server_ip=args.server_ip,
                        server_port=args.server_port,
                        result="unexpected_error",
                        success=False,
                        started=time.monotonic(),
                        exc=exc,
                    )
                writer.writerow(row)
                all_rows.append(row)
                completed += 1
                if int(row["success"]):
                    success_count += 1
                else:
                    failure_count += 1

            now = time.monotonic()
            if now >= next_progress or completed == len(ports):
                total_completed = progress_state["completed"] + completed
                total_success = progress_state["success"] + success_count
                total_failure = progress_state["failure"] + failure_count
                print(
                    "TCP source-port scan progress: "
                    f"repeat={repeat_number}/{args.repeat} "
                    f"completed={total_completed}/{total_planned} "
                    f"success={total_success} failure={total_failure} "
                    f"inflight={len(pending)}",
                    flush=True,
                )
                next_progress = now + float(args.progress_interval_sec)
                progress_state["next_progress"] = next_progress

    progress_state["completed"] += completed
    progress_state["success"] += success_count
    progress_state["failure"] += failure_count


def run_client(args):
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    details_dir = prepare_details_dir(out_dir)
    ports = build_scan_ports(args.scan_start_port, args.scan_end_port, args.scan_step)

    preflight_ok, preflight_summary = run_preflight(args, details_dir)
    if not preflight_ok and args.require_preflight:
        write_json(
            out_dir / "source_port_scan_summary.json",
            {
                "result": "preflight_failed",
                "preflight": preflight_summary,
                "scan_started": 0,
            },
        )
        print(
            "ERROR: automatic-source-port TCP preflight failed; "
            f"see {details_dir / 'source_port_scan_preflight.json'}",
            file=sys.stderr,
        )
        return 2

    results_path = out_dir / "source_port_scan_results.csv"
    rows = []
    progress_state = {"completed": 0, "success": 0, "failure": 0}
    started = time.monotonic()
    recovery_rows = []
    scan_abort = None
    recovery_path = details_dir / "source_port_scan_recovery.csv"
    with results_path.open("w", newline="", encoding="utf-8", buffering=1) as output, recovery_path.open(
        "w", newline="", encoding="utf-8", buffering=1
    ) as recovery_output:
        writer = csv.DictWriter(output, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        recovery_writer = csv.DictWriter(recovery_output, fieldnames=RECOVERY_FIELDS)
        recovery_writer.writeheader()
        for repeat_number in range(1, int(args.repeat) + 1):
            for batch_offset in range(0, len(ports), int(args.scan_batch_size)):
                batch_number = batch_offset // int(args.scan_batch_size) + 1
                batch_ports = ports[batch_offset:batch_offset + int(args.scan_batch_size)]
                execute_scan_pass(
                    args,
                    repeat_number,
                    batch_ports,
                    writer,
                    rows,
                    progress_state,
                    scan_index_base=(repeat_number - 1) * len(ports) + batch_offset,
                    total_planned=len(ports) * int(args.repeat),
                )
                is_last_batch = (
                    repeat_number == int(args.repeat)
                    and batch_offset + len(batch_ports) >= len(ports)
                )
                if is_last_batch:
                    continue
                recovery_ok, recovery = wait_for_batch_recovery(
                    args,
                    repeat_number=repeat_number,
                    batch_number=batch_number,
                    baseline_source_port=int(preflight_summary["baseline_source_port"]),
                    writer=recovery_writer,
                )
                recovery_rows.append(recovery)
                if not recovery_ok:
                    scan_abort = {
                        "reason": "batch_recovery_timeout",
                        **recovery,
                    }
                    break
            if scan_abort is not None:
                break

    per_port, ranges, boundaries = classify_port_results(rows)
    ranges_path = out_dir / "source_port_scan_ranges.csv"
    with ranges_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=RANGE_FIELDS)
        writer.writeheader()
        writer.writerows(ranges)

    status_counts = Counter(str(row["result"]) for row in rows)
    classification_counts = Counter(str(item["classification"]) for item in per_port)
    success_rtt = [float(row["round_trip_ms"]) for row in rows if int(row["success"])]
    summary = {
        "result": "done" if scan_abort is None else "incomplete",
        "scan_complete": int(scan_abort is None),
        "scan_abort": scan_abort,
        "elapsed_sec": time.monotonic() - started,
        "client_ip": args.client_ip,
        "server_ip": args.server_ip,
        "server_port": args.server_port,
        "scan_start_port": args.scan_start_port,
        "scan_end_port": args.scan_end_port,
        "scan_step": args.scan_step,
        "repeat": args.repeat,
        "planned_port_count": len(ports),
        "tested_port_count": len({int(row["source_port"]) for row in rows}),
        "planned_attempt_count": len(ports) * int(args.repeat),
        "attempt_count": len(rows),
        "success_count": sum(int(row["success"]) for row in rows),
        "failure_count": sum(1 - int(row["success"]) for row in rows),
        "classification_counts": dict(classification_counts),
        "result_counts": dict(status_counts),
        "successful_round_trip_ms": {
            "min": min(success_rtt) if success_rtt else None,
            "p50": percentile(success_rtt, 0.50),
            "p95": percentile(success_rtt, 0.95),
            "max": max(success_rtt) if success_rtt else None,
        },
        "preflight": preflight_summary,
        "batch_recovery": {
            "batch_size": int(args.scan_batch_size),
            "probe_interval_sec": float(args.recovery_probe_interval_sec),
            "required_consecutive_successes": int(args.recovery_success_consecutive),
            "timeout_sec": float(args.recovery_timeout_sec),
            "checks": recovery_rows,
            "csv": str(recovery_path),
        },
        "ranges": ranges,
        "boundaries": boundaries,
        "files": {
            "per_attempt_csv": str(results_path),
            "ranges_csv": str(ranges_path),
        },
    }
    write_json(out_dir / "source_port_scan_summary.json", summary)
    return 0 if scan_abort is None else 3


def wait_for_ready_file(path, proc, timeout_sec):
    deadline = time.monotonic() + float(timeout_sec)
    path = Path(path)
    while time.monotonic() < deadline:
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if int(value.get("ready", 0)) == 1:
                    return value
            except (OSError, ValueError, TypeError):
                pass
        if proc.poll() is not None:
            raise RuntimeError(f"TCP scan server exited before readiness: exit_code={proc.returncode}")
        time.sleep(0.05)
    raise RuntimeError(f"TCP scan server readiness timeout after {timeout_sec:g}s")


def terminate_process(proc, timeout_sec=5.0):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=float(timeout_sec))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=float(timeout_sec))


def resolve_target(args):
    target = build_trex_target(validate_dslite_profile(args.target, DSLITE_TARGETS[args.target]))
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
    return target


def build_default_out_dir(target):
    return DEFAULT_RESULT_ROOT / f"trex_dslite_source_port_scan_{target}_{ts_compact()}"


def run_orchestrator(args):
    target = resolve_target(args)
    out_dir = Path(args.out_dir) if args.out_dir else build_default_out_dir(args.target)
    args.out_dir = str(out_dir)
    args.client_ip = args.client_ip or target["lan_ip"]
    args.server_ip = args.server_ip or target["wan_ip"]
    ports = build_scan_ports(args.scan_start_port, args.scan_end_port, args.scan_step)

    print("============================================================")
    print("DS-Lite TCP source-port scan")
    print("============================================================")
    print(f"Target          : {args.target}")
    print(f"Description     : {target.get('description', '')}")
    print("Tool            : built-in Python TCP server/client, ip(iproute2 namespaces)")
    print(f"LAN endpoint    : {args.client_ip} gw {target.get('lan_gw')}")
    print(f"WAN server      : {args.server_ip}:{args.server_port}")
    print(f"Source ports    : {args.scan_start_port} down to {args.scan_end_port}, step {args.scan_step}")
    print(f"Tested ports    : {len(ports)} x repeat {args.repeat}")
    print(f"Scan rate       : {args.scan_cps:g} CPS, concurrency {args.concurrency}")
    print(f"Batch recovery  : every {args.scan_batch_size} ports, baseline port check")
    print(f"Timeouts        : connect {args.connect_timeout_sec:g}s, I/O {args.io_timeout_sec:g}s")
    print(f"Output dir      : {out_dir}")
    print("============================================================")
    if args.dry_run:
        return 0

    require_root()
    require_command("ip")
    out_dir.mkdir(parents=True, exist_ok=True)
    details_dir = prepare_details_dir(out_dir)
    ready_path = details_dir / "source_port_scan_server_ready.json"
    server_summary_path = details_dir / "source_port_scan_server_summary.json"
    server_stdout_path = details_dir / "source_port_scan_server.stdout.log"
    server_stderr_path = details_dir / "source_port_scan_server.stderr.log"
    client_stderr_path = details_dir / "source_port_scan_client.stderr.log"
    namespace_log_path = details_dir / "namespace_setup.csv"
    ready_path.unlink(missing_ok=True)

    metadata = {
        "created_at": ts_iso(),
        "target": args.target,
        "description": target.get("description", ""),
        "tool": "python_socket_tcp_source_port_scan",
        "lan_namespace": args.lan_ns,
        "wan_namespace": args.wan_ns,
        "client_ip": args.client_ip,
        "server_ip": args.server_ip,
        "server_port": args.server_port,
        "scan_start_port": args.scan_start_port,
        "scan_end_port": args.scan_end_port,
        "scan_step": args.scan_step,
        "tested_port_count": len(ports),
        "repeat": args.repeat,
        "scan_cps": args.scan_cps,
        "concurrency": args.concurrency,
        "scan_batch_size": args.scan_batch_size,
        "connect_timeout_sec": args.connect_timeout_sec,
        "io_timeout_sec": args.io_timeout_sec,
        "preflight_source_port": args.preflight_source_port,
        "preflight_attempts": args.preflight_attempts,
        "preflight_success_consecutive": args.preflight_success_consecutive,
        "require_preflight": args.require_preflight,
        "recovery_probe_interval_sec": args.recovery_probe_interval_sec,
        "recovery_success_consecutive": args.recovery_success_consecutive,
        "recovery_timeout_sec": args.recovery_timeout_sec,
        "notes": [
            "Each client socket binds the source port under test before connect().",
            "A port is successful only after a TCP connection and a binary request/response exchange complete.",
            "Successful probes use an abortive close to release TCP and translation state during a long scan.",
            "The test is diagnostic; unreachable ports do not make the process fail.",
        ],
    }
    write_json(details_dir / "source_port_scan_metadata.json", metadata)

    arp_refresher = None
    server_proc = None
    server_stdout = None
    server_stderr = None
    with namespace_log_path.open("w", newline="", encoding="utf-8") as namespace_output:
        namespace_log = csv.writer(namespace_output)
        namespace_log.writerow(["ts_local", "action", "detail"])
        try:
            if args.setup_namespaces and args.replace_namespaces:
                delete_existing_namespaces(args, namespace_log)
            if args.setup_namespaces:
                prepare_kernel_interfaces_from_profile(args, target, namespace_log)
            resolve_profile_interfaces(args, target, namespace_log)
            if args.setup_namespaces:
                require_profile_interfaces(args, target)
                setup_namespaces(args, target, namespace_log)
            verify_namespaces(args)
            if args.wait_interfaces_ready:
                wait_test_interfaces_ready(args)
            if args.post_namespace_settle_sec > 0:
                time.sleep(args.post_namespace_settle_sec)
            wait_before_test_start(args.test_start_delay_sec, "DS-Lite TCP source-port scan")

            arp_refresher = make_namespace_arp_refresher(args, target, details_dir)
            arp_refresher.refresh_now("startup", required=True)
            arp_refresher.start()

            server_cmd = ns_cmd(
                args.wan_ns,
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--internal-role",
                    "server",
                    "--bind-ip",
                    str(args.server_ip),
                    "--server-port",
                    str(args.server_port),
                    "--listen-backlog",
                    str(args.listen_backlog),
                    "--ready-file",
                    str(ready_path),
                    "--server-summary-file",
                    str(server_summary_path),
                ],
            )
            client_cmd = ns_cmd(
                args.lan_ns,
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--internal-role",
                    "client",
                    "--out-dir",
                    str(out_dir),
                    "--client-ip",
                    str(args.client_ip),
                    "--server-ip",
                    str(args.server_ip),
                    "--server-port",
                    str(args.server_port),
                    "--scan-start-port",
                    str(args.scan_start_port),
                    "--scan-end-port",
                    str(args.scan_end_port),
                    "--scan-step",
                    str(args.scan_step),
                    "--repeat",
                    str(args.repeat),
                    "--scan-cps",
                    str(args.scan_cps),
                    "--concurrency",
                    str(args.concurrency),
                    "--scan-batch-size",
                    str(args.scan_batch_size),
                    "--connect-timeout-sec",
                    str(args.connect_timeout_sec),
                    "--io-timeout-sec",
                    str(args.io_timeout_sec),
                    "--progress-interval-sec",
                    str(args.progress_interval_sec),
                    "--preflight-source-port",
                    str(args.preflight_source_port),
                    "--preflight-attempts",
                    str(args.preflight_attempts),
                    "--preflight-success-consecutive",
                    str(args.preflight_success_consecutive),
                    "--preflight-interval-sec",
                    str(args.preflight_interval_sec),
                    "--recovery-probe-interval-sec",
                    str(args.recovery_probe_interval_sec),
                    "--recovery-success-consecutive",
                    str(args.recovery_success_consecutive),
                    "--recovery-timeout-sec",
                    str(args.recovery_timeout_sec),
                    "--require-preflight" if args.require_preflight else "--no-require-preflight",
                ],
            )

            print("[RUN] WAN Python TCP server")
            print(" ".join(server_cmd))
            server_stdout = server_stdout_path.open("w", encoding="utf-8")
            server_stderr = server_stderr_path.open("w", encoding="utf-8")
            server_proc = subprocess.Popen(
                server_cmd,
                stdout=server_stdout,
                stderr=server_stderr,
                text=True,
            )
            wait_for_ready_file(ready_path, server_proc, args.server_ready_timeout_sec)
            arp_refresher.refresh_now("before_source_port_scan", required=True)

            print("[RUN] LAN Python TCP source-port scanner")
            print(" ".join(client_cmd))
            with client_stderr_path.open("w", encoding="utf-8") as client_stderr:
                client_proc = subprocess.run(client_cmd, stderr=client_stderr, text=True, check=False)
            if client_proc.returncode != 0:
                raise RuntimeError(
                    f"source-port scanner failed: exit_code={client_proc.returncode}; "
                    f"see {client_stderr_path}"
                )
        finally:
            terminate_process(server_proc)
            if server_stdout is not None:
                server_stdout.close()
            if server_stderr is not None:
                server_stderr.close()
            if arp_refresher is not None:
                arp_refresher.stop()
            if args.restore_dpdk_after:
                try:
                    prep_log = prepare_trex_interfaces_from_profile(
                        target,
                        details_dir,
                        auto_dpdk_bind=True,
                        dpdk_driver_override=args.dpdk_driver,
                    )
                    namespace_log.writerow([ts_iso(), "restore_dpdk_after_done", str(prep_log)])
                except Exception as exc:
                    namespace_log.writerow([ts_iso(), "restore_dpdk_after_warning", str(exc)])
            cleanup_namespaces(args, namespace_log)

    summary_path = out_dir / "source_port_scan_summary.json"
    if summary_path.exists() and server_summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["server"] = json.loads(server_summary_path.read_text(encoding="utf-8"))
        write_json(summary_path, summary)

    print("============================================================")
    print("DONE")
    print(f"Output dir : {out_dir}")
    print(f"Results CSV: {out_dir / 'source_port_scan_results.csv'}")
    print(f"Ranges CSV : {out_dir / 'source_port_scan_ranges.csv'}")
    print(f"Summary    : {summary_path}")
    print("============================================================")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Scan DS-Lite TCP source-port reachability using built-in Python sockets.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--internal-role",
        choices=("orchestrator", "server", "client"),
        default="orchestrator",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--target", choices=DSLITE_TARGETS.keys(), default=_DEFAULTS["target"])
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repeat", type=argparse_repeat, default=DEFAULT_REPEAT)
    parser.add_argument(
        "--scan-start-port",
        "--source-start-port",
        dest="scan_start_port",
        type=parse_port,
        default=int(_DEFAULTS["scan_start_port"]),
    )
    parser.add_argument(
        "--scan-end-port",
        "--source-end-port",
        dest="scan_end_port",
        type=parse_port,
        default=int(_DEFAULTS["scan_end_port"]),
    )
    parser.add_argument("--scan-step", type=parse_positive_int, default=int(_DEFAULTS["scan_step"]))
    parser.add_argument("--server-port", type=parse_port, default=int(_DEFAULTS["server_port"]))
    parser.add_argument("--scan-cps", type=parse_positive_float, default=float(_DEFAULTS["scan_cps"]))
    parser.add_argument("--concurrency", type=parse_positive_int, default=int(_DEFAULTS["concurrency"]))
    parser.add_argument(
        "--scan-batch-size",
        type=parse_positive_int,
        default=int(_DEFAULTS["scan_batch_size"]),
        help="Ports per scan batch before validating the known-good baseline port.",
    )
    parser.add_argument(
        "--connect-timeout-sec",
        type=parse_positive_float,
        default=float(_DEFAULTS["connect_timeout_sec"]),
    )
    parser.add_argument(
        "--io-timeout-sec",
        type=parse_positive_float,
        default=float(_DEFAULTS["io_timeout_sec"]),
    )
    parser.add_argument(
        "--progress-interval-sec",
        type=parse_positive_float,
        default=float(_DEFAULTS["progress_interval_sec"]),
    )
    parser.add_argument(
        "--preflight-source-port",
        type=int,
        default=int(_DEFAULTS["preflight_source_port"]),
        help="Preflight source port. Zero requests an automatic ephemeral port.",
    )
    parser.add_argument(
        "--preflight-attempts",
        type=parse_positive_int,
        default=int(_DEFAULTS["preflight_attempts"]),
    )
    parser.add_argument(
        "--preflight-success-consecutive",
        type=parse_positive_int,
        default=int(_DEFAULTS["preflight_success_consecutive"]),
    )
    parser.add_argument(
        "--preflight-interval-sec",
        type=parse_non_negative_float,
        default=float(_DEFAULTS["preflight_interval_sec"]),
    )
    parser.add_argument(
        "--require-preflight",
        dest="require_preflight",
        action="store_true",
        default=bool(_DEFAULTS["require_preflight"]),
    )
    parser.add_argument("--no-require-preflight", dest="require_preflight", action="store_false")
    parser.add_argument(
        "--recovery-probe-interval-sec",
        type=parse_non_negative_float,
        default=float(_DEFAULTS["recovery_probe_interval_sec"]),
    )
    parser.add_argument(
        "--recovery-success-consecutive",
        type=parse_positive_int,
        default=int(_DEFAULTS["recovery_success_consecutive"]),
    )
    parser.add_argument(
        "--recovery-timeout-sec",
        type=parse_positive_float,
        default=float(_DEFAULTS["recovery_timeout_sec"]),
    )

    parser.add_argument("--lan-ns", default=DEFAULT_LAN_NS)
    parser.add_argument("--wan-ns", default=DEFAULT_WAN_NS)
    parser.add_argument(
        "--setup-namespaces",
        dest="setup_namespaces",
        action="store_true",
        default=bool(_NETWORK_DEFAULTS["setup_namespaces"]),
    )
    parser.add_argument("--no-setup-namespaces", dest="setup_namespaces", action="store_false")
    parser.add_argument(
        "--replace-namespaces",
        dest="replace_namespaces",
        action="store_true",
        default=bool(_NETWORK_DEFAULTS["replace_namespaces"]),
    )
    parser.add_argument("--no-replace-namespaces", dest="replace_namespaces", action="store_false")
    parser.add_argument("--keep-namespaces", action="store_true")
    parser.add_argument(
        "--auto-kernel-bind",
        dest="auto_kernel_bind",
        action="store_true",
        default=bool(_NETWORK_DEFAULTS["auto_kernel_bind"]),
    )
    parser.add_argument("--no-auto-kernel-bind", dest="auto_kernel_bind", action="store_false")
    parser.add_argument(
        "--restore-dpdk-after",
        dest="restore_dpdk_after",
        action="store_true",
        default=bool(_NETWORK_DEFAULTS["restore_dpdk_after"]),
    )
    parser.add_argument("--no-restore-dpdk-after", dest="restore_dpdk_after", action="store_false")
    parser.add_argument("--dpdk-driver", default="")
    parser.add_argument(
        "--post-namespace-settle-sec",
        type=parse_non_negative_float,
        default=DEFAULT_POST_NAMESPACE_SETTLE_SEC,
    )
    parser.add_argument(
        "--test-start-delay-sec",
        type=parse_non_negative_float,
        default=DEFAULT_TEST_START_DELAY_SEC,
    )
    parser.add_argument(
        "--wait-interfaces-ready",
        dest="wait_interfaces_ready",
        action="store_true",
        default=bool(_NETWORK_DEFAULTS["wait_interfaces_ready"]),
    )
    parser.add_argument("--no-wait-interfaces-ready", dest="wait_interfaces_ready", action="store_false")
    parser.add_argument(
        "--interface-ready-timeout-sec",
        type=parse_positive_float,
        default=DEFAULT_INTERFACE_READY_TIMEOUT_SEC,
    )
    parser.add_argument("--lan-iface", default="")
    parser.add_argument("--wan-iface", default="")
    parser.add_argument("--lan-prefix", type=parse_positive_int, default=DEFAULT_LAN_PREFIX)
    parser.add_argument("--wan-prefix", type=parse_positive_int, default=DEFAULT_WAN_PREFIX)
    parser.add_argument("--lan-ip", default="")
    parser.add_argument("--lan-gw", default="")
    parser.add_argument("--wan-ip", "--wan-link-ip", dest="wan_ip", default="")
    parser.add_argument("--wan-gw", "--wan-arp-target", dest="wan_gw", default="")
    parser.add_argument("--router-wan-ip", "--wan-gateway-ip", dest="router_wan_ip", default="")
    parser.add_argument("--client-ip", default="")
    parser.add_argument("--server-ip", default="")

    parser.add_argument("--listen-backlog", type=parse_positive_int, default=int(_DEFAULTS["listen_backlog"]), help=argparse.SUPPRESS)
    parser.add_argument("--server-ready-timeout-sec", type=parse_positive_float, default=float(_DEFAULTS["server_ready_timeout_sec"]), help=argparse.SUPPRESS)
    parser.add_argument("--bind-ip", default="", help=argparse.SUPPRESS)
    parser.add_argument("--ready-file", default="", help=argparse.SUPPRESS)
    parser.add_argument("--server-summary-file", default="", help=argparse.SUPPRESS)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.scan_start_port < args.scan_end_port:
        raise SystemExit("ERROR: --scan-start-port must be greater than or equal to --scan-end-port")
    if not 0 <= int(args.preflight_source_port) <= 65535:
        raise SystemExit("ERROR: --preflight-source-port must be between 0 and 65535")
    if args.preflight_success_consecutive > args.preflight_attempts:
        raise SystemExit(
            "ERROR: --preflight-success-consecutive cannot exceed --preflight-attempts"
        )
    # Shared namespace setup accepts this attribute when present. The scan binds
    # every source port explicitly and must not change ip_local_port_range.
    args.lan_source_port_range = None
    if args.internal_role == "server":
        if not args.bind_ip or not args.ready_file or not args.server_summary_file:
            raise SystemExit("ERROR: internal server arguments are incomplete")
        return run_server(args)
    if args.internal_role == "client":
        if not args.out_dir or not args.client_ip or not args.server_ip:
            raise SystemExit("ERROR: internal client arguments are incomplete")
        return run_client(args)
    return run_orchestrator(args)


if __name__ == "__main__":
    raise SystemExit(main())
