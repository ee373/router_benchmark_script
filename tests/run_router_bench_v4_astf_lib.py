#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for TRex IPv4 NAT ASTF benchmark scripts."""

import argparse
import csv
import datetime as dt
import json
import math
import os
import signal
import socket
import subprocess
import sys
import time
import traceback
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
    from tests.run_router_bench_v4_stl_throughput_pps import (  # type: ignore
        TREX_DIR,
        TREX_CFG_PATH,
        add_trex_api_path,
        flatten,
        install_trex_cfg,
        json_cell,
        kill_existing_trex,
        merge_target_with_overrides,
        parse_interfaces,
        prepare_trex_interfaces_from_profile,
        require_root,
        ts_compact,
        ts_iso,
        tail_text,
        wait_rpc,
    )
except Exception:
    from run_router_bench_v4_stl_throughput_pps import (  # type: ignore
        TREX_DIR,
        TREX_CFG_PATH,
        add_trex_api_path,
        flatten,
        install_trex_cfg,
        json_cell,
        kill_existing_trex,
        merge_target_with_overrides,
        parse_interfaces,
        prepare_trex_interfaces_from_profile,
        require_root,
        ts_compact,
        ts_iso,
        tail_text,
        wait_rpc,
    )

RAW_INTERVAL_SEC = 1.0
PROGRESS_BAR_WIDTH = 28
ASTF_TCP_PORT = 80
DEFAULT_MAX_SESSIONS = 65535
DEFAULT_SESSION_RAMP_CPS = 100.0
DEFAULT_SESSION_HOLD_SEC = 30.0
DEFAULT_CPS_DURATION_SEC = 20.0
DEFAULT_LOSS_STOP_THRESHOLD = 0.50
DEFAULT_SESSION_SUCCESS_STOP_THRESHOLD = 0.50
DEFAULT_CPS_STEPS = (
    10, 15, 22, 33, 47, 68,
    100, 150, 220, 330, 470, 680,
    1000, 1500, 2200, 3300, 4700, 6800,
    10000, 15000, 22000, 33000, 47000, 65535,
)
DEFAULT_STABLE_SUCCESS_THRESHOLD = 0.99
DEFAULT_STABLE_LOSS_THRESHOLD = 0.001
DEFAULT_REPRESENTATIVE_CPS = None



def progress_bar_from_ratio(ratio, width=PROGRESS_BAR_WIDTH):
    ratio = max(0.0, min(1.0, float(ratio)))
    filled = int(round(ratio * int(width)))
    filled = max(0, min(int(width), filled))
    return "#" * filled + "-" * (int(width) - filled), ratio


def progress_bar_text(elapsed, duration, width=PROGRESS_BAR_WIDTH):
    if duration <= 0:
        ratio = 1.0
    else:
        ratio = max(0.0, min(1.0, float(elapsed) / float(duration)))
    return progress_bar_from_ratio(ratio, width=width)


def progress_phase(elapsed, ramp_sec, duration):
    if ramp_sec is None:
        return "running"
    if elapsed < float(ramp_sec):
        return "ramp"
    if elapsed < float(duration):
        return "hold"
    return "done"


def print_progress(label, elapsed, duration, ramp_sec=None, session_ramp_cps=None,
                   max_sessions=None, enabled=True, percent_override=None):
    """Print progress as ordinary log lines.

    Do not use carriage-return based in-place updates here.  TRex / Scapy can also
    write to the terminal, and long progress lines can wrap on narrow consoles.
    Plain newline logs are more verbose, but they do not corrupt nearby output.
    """
    if not enabled:
        return
    elapsed = max(0.0, float(elapsed))
    duration = max(0.0, float(duration))
    remaining = max(0.0, duration - elapsed)
    if percent_override is None:
        bar, ratio = progress_bar_text(elapsed, duration, width=18)
        percent = ratio * 100.0
    else:
        percent = max(0.0, min(100.0, float(percent_override)))
        bar, ratio = progress_bar_from_ratio(percent / 100.0, width=18)
    phase = progress_phase(elapsed, ramp_sec, duration)
    parts = [
        f"[progress] {label}",
        f"[{bar}]",
        f"{percent:6.2f}%",
        f"elapsed={elapsed:.0f}s",
        f"remain={remaining:.0f}s",
        f"phase={phase}",
    ]
    if session_ramp_cps is not None and max_sessions is not None:
        if ramp_sec is None or elapsed < float(ramp_sec):
            estimated_sessions = min(float(max_sessions), elapsed * float(session_ramp_cps))
        else:
            estimated_sessions = float(max_sessions)
        parts.append(f"sessions~={estimated_sessions:,.0f}/{int(max_sessions):,}")
    print(" ".join(parts), flush=True)


def write_raw_stats(writer, phase, test_name, elapsed, stats, ts_local=None):
    now = ts_local or ts_iso()
    for metric, value in flatten(stats):
        writer.writerow([now, f"{elapsed:.3f}", phase, test_name, metric, json_cell(value)])


class TrexAstfServer:
    def __init__(self, cores, out_dir):
        self.cores = cores
        self.out_dir = out_dir
        self.proc = None
        self.log_path = out_dir / "trex_server_astf.log"
        self.log_file = None

    def start(self):
        kill_existing_trex()
        cmd = ["./t-rex-64", "-i", "--astf", "-c", str(self.cores)]
        self.log_file = open(self.log_path, "w")
        print("=== Starting TRex ASTF mode ===")
        print(" ".join(cmd))
        print(f"server log: {self.log_path}")
        self.proc = subprocess.Popen(
            cmd,
            cwd=TREX_DIR,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
        )
        try:
            wait_rpc(timeout=60)
        except Exception as exc:
            exit_code = self.proc.poll() if self.proc is not None else None
            if self.log_file:
                self.log_file.flush()
            try:
                log_tail = tail_text(self.log_path)
            except NameError:
                log_tail = ""
            detail = f"{exc}\nTRex process exit_code={exit_code}\nTRex server log: {self.log_path}"
            if log_tail:
                detail += "\n--- last TRex server log lines ---\n" + log_tail
            raise RuntimeError(detail) from exc

    def stop(self):
        print("=== Stopping TRex ASTF mode ===")
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                self.proc.wait(timeout=8)
            except Exception:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except Exception:
                    pass
        kill_existing_trex()
        if self.log_file:
            self.log_file.close()
            self.log_file = None


def make_default_cps_steps(max_cps=DEFAULT_MAX_SESSIONS):
    steps = [int(x) for x in DEFAULT_CPS_STEPS if int(x) <= int(max_cps)]
    if int(max_cps) not in steps:
        steps.append(int(max_cps))
    return sorted(set(steps))


def bool_text(value):
    return "あり" if bool(value) else "なし"


def clamp_rate(value):
    try:
        value = float(value)
    except Exception:
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def is_row_stable(row, stable_success_threshold, stable_loss_threshold):
    if row.get("communication_outage_suspected") in (True, "true", "True", "あり"):
        return False
    session_success = float(row.get("session_success_rate") or 0.0)
    packet_loss = float(row.get("packet_loss_rate") or 0.0)
    throughput_loss = float(row.get("throughput_loss_rate") or 0.0)
    return (
        session_success >= float(stable_success_threshold)
        and packet_loss <= float(stable_loss_threshold)
        and throughput_loss <= float(stable_loss_threshold)
    )


def parse_cps_steps(value):
    try:
        steps = [int(x.strip().replace("_", "")) for x in value.split(",") if x.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--cps-steps must be comma-separated integers") from exc
    if not steps:
        raise argparse.ArgumentTypeError("--cps-steps must not be empty")
    for step in steps:
        if step <= 0:
            raise argparse.ArgumentTypeError("--cps-steps values must be positive")
    return steps


def safe_get(obj, path, default=0):
    cur = obj
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
    return cur


def first_number(mapping, names, default=0.0):
    if not isinstance(mapping, dict):
        return default
    for name in names:
        if name not in mapping:
            continue
        value = mapping.get(name)
        try:
            return float(value)
        except Exception:
            continue
    return default


def first_number_or_blank(mapping, names):
    """Return the first numeric counter value, or an empty string when absent."""
    if not isinstance(mapping, dict):
        return ""
    for name in names:
        if name not in mapping:
            continue
        value = mapping.get(name)
        try:
            return float(value)
        except Exception:
            continue
    return ""


def number_available(value):
    return value not in ("", None)


def float_or_zero(value):
    try:
        if value in ("", None):
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def min_available(*values):
    nums = [float(v) for v in values if number_available(v)]
    return min(nums) if nums else ""


def calc_trex_flow_counters(stats):
    """Return TRex-provided flow counters and selected TCP drop counters.

    m_active_flows and m_est_flows are generated by TRex itself.  The script
    keeps the derived open-session estimates for comparison, but max-session
    time-series charts should primarily use paired_trex_m_est_flows when it is
    present.
    """
    client = traffic_side(stats, "client")
    server = traffic_side(stats, "server")
    global_stats = safe_get(stats, ["global"], {})

    client_trex_m_active_flows = first_number_or_blank(client, ["m_active_flows"])
    client_trex_m_est_flows = first_number_or_blank(client, ["m_est_flows"])
    server_trex_m_active_flows = first_number_or_blank(server, ["m_active_flows"])
    server_trex_m_est_flows = first_number_or_blank(server, ["m_est_flows"])

    paired_trex_m_active_flows = min_available(client_trex_m_active_flows, server_trex_m_active_flows)
    paired_trex_m_est_flows = min_available(client_trex_m_est_flows, server_trex_m_est_flows)

    return {
        "global_trex_active_flows": first_number_or_blank(global_stats, ["active_flows"]),
        "global_trex_open_flows": first_number_or_blank(global_stats, ["open_flows"]),
        "global_trex_active_sockets": first_number_or_blank(global_stats, ["active_sockets"]),
        "global_trex_socket_util": first_number_or_blank(global_stats, ["socket_util"]),
        "global_trex_rx_drop_bps": first_number_or_blank(global_stats, ["rx_drop_bps"]),
        "client_trex_m_active_flows": client_trex_m_active_flows,
        "client_trex_m_est_flows": client_trex_m_est_flows,
        "server_trex_m_active_flows": server_trex_m_active_flows,
        "server_trex_m_est_flows": server_trex_m_est_flows,
        "paired_trex_m_active_flows": paired_trex_m_active_flows,
        "paired_trex_m_est_flows": paired_trex_m_est_flows,
        "client_tcps_drops": first_number_or_blank(client, ["tcps_drops"]),
        "server_tcps_drops": first_number_or_blank(server, ["tcps_drops"]),
        "client_tcps_conndrops": first_number_or_blank(client, ["tcps_conndrops"]),
        "server_tcps_conndrops": first_number_or_blank(server, ["tcps_conndrops"]),
        "client_tcps_timeoutdrop": first_number_or_blank(client, ["tcps_timeoutdrop"]),
        "server_tcps_timeoutdrop": first_number_or_blank(server, ["tcps_timeoutdrop"]),
        "client_tcps_keepdrops": first_number_or_blank(client, ["tcps_keepdrops"]),
        "server_tcps_keepdrops": first_number_or_blank(server, ["tcps_keepdrops"]),
        "client_tcps_testdrops": first_number_or_blank(client, ["tcps_testdrops"]),
        "server_tcps_testdrops": first_number_or_blank(server, ["tcps_testdrops"]),
        "client_tcps_rexmttimeo": first_number_or_blank(client, ["tcps_rexmttimeo"]),
        "server_tcps_rexmttimeo": first_number_or_blank(server, ["tcps_rexmttimeo"]),
    }


def select_active_session_counter(open_estimates, trex_flow_counters):
    """Choose the active-session counter used by estimated_active_sessions.

    Prefer TRex's own active established flow counter.  Keep the script-derived
    paired_open_sessions_est only as a fallback for older TRex versions or
    unexpected stat payloads.
    """
    preferred = trex_flow_counters.get("paired_trex_m_est_flows", "")
    if number_available(preferred):
        return float(preferred), "paired_trex_m_est_flows"

    fallback = open_estimates.get("paired_open_sessions_est", "")
    if number_available(fallback):
        return float(fallback), "paired_open_sessions_est_fallback"

    global_active = trex_flow_counters.get("global_trex_active_flows", "")
    if number_available(global_active):
        return float(global_active), "global_trex_active_flows_fallback"

    return 0.0, "none"


def traffic_side(stats, side):
    return safe_get(stats, ["traffic", side], {})



def calc_open_session_estimates(client_connects, server_accepts, client_closed, server_closed, established_sessions):
    """Return open-session estimates without mixing counters from different sides.

    legacy_estimated_active_sessions keeps the previous formula for comparison:
      max(0, min(connects, accepts) - max(client_closed, server_closed))

    paired_open_sessions_est is the preferred time-series value:
      min(max(0, client_connects - client_closed), max(0, server_accepts - server_closed))
    when both sides have lifecycle counters.
    """
    client_connects = float(client_connects or 0.0)
    server_accepts = float(server_accepts or 0.0)
    client_closed = float(client_closed or 0.0)
    server_closed = float(server_closed or 0.0)
    established_sessions = float(established_sessions or 0.0)

    closed_sessions = max(client_closed, server_closed)
    legacy_estimated_active_sessions = max(0.0, established_sessions - closed_sessions)

    client_open_sessions_est = max(0.0, client_connects - client_closed)
    server_open_sessions_est = max(0.0, server_accepts - server_closed)

    has_client_lifecycle = (client_connects > 0.0 or client_closed > 0.0)
    has_server_lifecycle = (server_accepts > 0.0 or server_closed > 0.0)
    if has_client_lifecycle and has_server_lifecycle:
        paired_open_sessions_est = min(client_open_sessions_est, server_open_sessions_est)
    elif has_client_lifecycle:
        paired_open_sessions_est = client_open_sessions_est
    elif has_server_lifecycle:
        paired_open_sessions_est = server_open_sessions_est
    else:
        paired_open_sessions_est = 0.0

    return {
        "closed_sessions": closed_sessions,
        "legacy_estimated_active_sessions": legacy_estimated_active_sessions,
        "client_open_sessions_est": client_open_sessions_est,
        "server_open_sessions_est": server_open_sessions_est,
        "paired_open_sessions_est": paired_open_sessions_est,
        # Backward-compatible alias.  New charts should prefer paired_open_sessions_est.
        "estimated_active_sessions": paired_open_sessions_est,
    }

def calc_astf_metrics(stats, requested_cps, duration_sec, expected_sessions=None):
    client = traffic_side(stats, "client")
    server = traffic_side(stats, "server")

    c_tx_bytes = first_number(client, ["tcps_sndbyte", "tcps_sndbytes", "sndbyte"])
    s_rx_bytes = first_number(server, ["tcps_rcvbyte", "tcps_rcvbytes", "rcvbyte"])
    s_tx_bytes = first_number(server, ["tcps_sndbyte", "tcps_sndbytes", "sndbyte"])
    c_rx_bytes = first_number(client, ["tcps_rcvbyte", "tcps_rcvbytes", "rcvbyte"])

    c_tx_pkts = first_number(client, ["tcps_sndtotal", "tcps_sndpack", "sndtotal", "sndpkt"])
    s_rx_pkts = first_number(server, ["tcps_rcvtotal", "tcps_rcvpack", "rcvtotal", "rcvpkt"])
    s_tx_pkts = first_number(server, ["tcps_sndtotal", "tcps_sndpack", "sndtotal", "sndpkt"])
    c_rx_pkts = first_number(client, ["tcps_rcvtotal", "tcps_rcvpack", "rcvtotal", "rcvpkt"])

    client_connects = first_number(client, ["tcps_connects", "tcps_connattempt", "connects", "connattempt"])
    server_accepts = first_number(server, ["tcps_accepts", "tcps_connects", "accepts", "connects"])
    if client_connects and server_accepts:
        established_sessions = min(client_connects, server_accepts)
    else:
        established_sessions = max(client_connects, server_accepts)

    client_closed = first_number(client, ["tcps_closed", "closed"])
    server_closed = first_number(server, ["tcps_closed", "closed"])
    open_estimates = calc_open_session_estimates(
        client_connects=client_connects,
        server_accepts=server_accepts,
        client_closed=client_closed,
        server_closed=server_closed,
        established_sessions=established_sessions,
    )
    trex_flow_counters = calc_trex_flow_counters(stats)
    selected_active_sessions, active_sessions_source = select_active_session_counter(
        open_estimates, trex_flow_counters
    )
    closed_sessions = open_estimates["closed_sessions"]
    legacy_estimated_active_sessions = open_estimates["legacy_estimated_active_sessions"]
    client_open_sessions_est = open_estimates["client_open_sessions_est"]
    server_open_sessions_est = open_estimates["server_open_sessions_est"]
    paired_open_sessions_est = open_estimates["paired_open_sessions_est"]
    estimated_active_sessions = selected_active_sessions

    tx_pkts = c_tx_pkts + s_tx_pkts
    rx_pkts = s_rx_pkts + c_rx_pkts
    packet_loss_pkts = max(0.0, tx_pkts - rx_pkts)
    packet_loss_rate = (packet_loss_pkts / tx_pkts) if tx_pkts else 0.0

    tx_l7_bytes = c_tx_bytes + s_tx_bytes
    rx_l7_bytes = s_rx_bytes + c_rx_bytes
    l7_loss_bytes = max(0.0, tx_l7_bytes - rx_l7_bytes)
    throughput_loss_rate = (l7_loss_bytes / tx_l7_bytes) if tx_l7_bytes else 0.0

    achieved_cps = established_sessions / duration_sec if duration_sec > 0 else 0.0
    expected = float(expected_sessions) if expected_sessions is not None else requested_cps * duration_sec
    session_success_rate = (established_sessions / expected) if expected > 0 else 0.0

    session_success_rate = clamp_rate(session_success_rate)
    session_failure_rate = clamp_rate(1.0 - session_success_rate)
    failed_or_unestablished_sessions = max(0.0, expected - established_sessions)

    estimated_mid_test_disconnects = max(0.0, established_sessions - estimated_active_sessions)
    session_keep_success_rate = (estimated_active_sessions / established_sessions) if established_sessions > 0 else 0.0
    session_keep_success_rate = clamp_rate(session_keep_success_rate)
    session_keep_failure_rate = clamp_rate(1.0 - session_keep_success_rate)

    communication_outage_suspected = False
    outage_reasons = []
    if expected >= 1.0 and established_sessions <= 0.0:
        communication_outage_suspected = True
        outage_reasons.append("no_established_sessions")
    if tx_pkts > 0.0 and rx_pkts <= 0.0:
        communication_outage_suspected = True
        outage_reasons.append("no_received_tcp_packets")
    if expected >= max(10.0, requested_cps) and session_success_rate <= 0.01:
        communication_outage_suspected = True
        outage_reasons.append("session_success_rate_le_1pct")

    return {
        "requested_cps": requested_cps,
        "duration_sec": duration_sec,
        "expected_sessions": expected,
        "client_connects": client_connects,
        "server_accepts": server_accepts,
        "established_sessions": established_sessions,
        "failed_or_unestablished_sessions": failed_or_unestablished_sessions,
        "legacy_estimated_active_sessions": legacy_estimated_active_sessions,
        "client_open_sessions_est": client_open_sessions_est,
        "server_open_sessions_est": server_open_sessions_est,
        "paired_open_sessions_est": paired_open_sessions_est,
        "estimated_active_sessions": estimated_active_sessions,
        "selected_active_sessions": selected_active_sessions,
        "active_sessions_source": active_sessions_source,
        **trex_flow_counters,
        "achieved_cps": achieved_cps,
        "session_success_rate": session_success_rate,
        "session_failure_rate": session_failure_rate,
        "client_closed_sessions": client_closed,
        "server_closed_sessions": server_closed,
        "client_tcps_closed": client_closed,
        "server_tcps_closed": server_closed,
        "closed_sessions": closed_sessions,
        "estimated_mid_test_disconnects": estimated_mid_test_disconnects,
        "session_keep_success_rate": session_keep_success_rate,
        "session_keep_failure_rate": session_keep_failure_rate,
        "communication_outage_suspected": communication_outage_suspected,
        "communication_outage": bool_text(communication_outage_suspected),
        "communication_outage_reason": ";".join(outage_reasons),
        "client_tx_l7_bytes": c_tx_bytes,
        "server_rx_l7_bytes": s_rx_bytes,
        "server_tx_l7_bytes": s_tx_bytes,
        "client_rx_l7_bytes": c_rx_bytes,
        "tx_l7_bytes": tx_l7_bytes,
        "rx_l7_bytes": rx_l7_bytes,
        "l7_loss_bytes": l7_loss_bytes,
        "throughput_loss_rate": throughput_loss_rate,
        "client_tx_tcp_packets": c_tx_pkts,
        "server_rx_tcp_packets": s_rx_pkts,
        "server_tx_tcp_packets": s_tx_pkts,
        "client_rx_tcp_packets": c_rx_pkts,
        "tx_tcp_packets": tx_pkts,
        "rx_tcp_packets": rx_pkts,
        "packet_loss_packets": packet_loss_pkts,
        "packet_loss_rate": packet_loss_rate,
    }


def calc_session_lifecycle_stats(stats):
    """Return connection lifecycle counters from one ASTF stats snapshot."""
    client = traffic_side(stats, "client")
    server = traffic_side(stats, "server")

    client_connects = first_number(client, ["tcps_connects", "tcps_connattempt", "connects", "connattempt"])
    server_accepts = first_number(server, ["tcps_accepts", "tcps_connects", "accepts", "connects"])
    if client_connects and server_accepts:
        established_sessions = min(client_connects, server_accepts)
    else:
        established_sessions = max(client_connects, server_accepts)

    client_closed = first_number(client, ["tcps_closed", "closed"])
    server_closed = first_number(server, ["tcps_closed", "closed"])
    open_estimates = calc_open_session_estimates(
        client_connects=client_connects,
        server_accepts=server_accepts,
        client_closed=client_closed,
        server_closed=server_closed,
        established_sessions=established_sessions,
    )
    trex_flow_counters = calc_trex_flow_counters(stats)
    selected_active_sessions, active_sessions_source = select_active_session_counter(
        open_estimates, trex_flow_counters
    )

    return {
        "client_connects": client_connects,
        "server_accepts": server_accepts,
        "established_sessions": established_sessions,
        "client_closed_sessions": client_closed,
        "server_closed_sessions": server_closed,
        "client_tcps_closed": client_closed,
        "server_tcps_closed": server_closed,
        "closed_sessions": open_estimates["closed_sessions"],
        "legacy_estimated_active_sessions": open_estimates["legacy_estimated_active_sessions"],
        "client_open_sessions_est": open_estimates["client_open_sessions_est"],
        "server_open_sessions_est": open_estimates["server_open_sessions_est"],
        "paired_open_sessions_est": open_estimates["paired_open_sessions_est"],
        "estimated_active_sessions": selected_active_sessions,
        "selected_active_sessions": selected_active_sessions,
        "active_sessions_source": active_sessions_source,
        **trex_flow_counters,
    }


def calc_hold_metrics_from_samples(samples, ramp_sec, duration_sec):
    """Calculate max-session hold metrics from raw per-second ASTF samples.

    The final ASTF stats snapshot can include client-side close counters from
    traffic shutdown.  For maximum-session hold quality, use raw time-series
    snapshots collected before shutdown, then compare the active-session estimate
    around ramp completion with the active-session estimate near hold completion.
    """
    live_samples = []
    for sample in samples or []:
        try:
            elapsed = float(sample.get("elapsed_sec"))
        except Exception:
            continue
        if elapsed < 0:
            continue
        if elapsed > float(duration_sec):
            continue
        live_samples.append({
            "elapsed_sec": elapsed,
            "stats": sample.get("stats") or {},
            "lifecycle": calc_session_lifecycle_stats(sample.get("stats") or {}),
        })

    if not live_samples:
        return {
            "max_session_summary_source": "final_stats_fallback",
            "hold_start_elapsed_sec": "",
            "hold_end_elapsed_sec": "",
            "hold_start_paired_open_sessions_est": "",
            "hold_end_paired_open_sessions_est": "",
            "hold_min_paired_open_sessions_est": "",
            "hold_start_established_sessions": "",
            "hold_end_established_sessions": "",
            "hold_start_closed_sessions": "",
            "hold_end_closed_sessions": "",
            "hold_mid_test_disconnects": "",
            "hold_session_success_rate": "",
            "hold_session_failure_rate": "",
        }

    ramp_sec = float(ramp_sec)
    duration_sec = float(duration_sec)
    hold_start_sample = min(live_samples, key=lambda sample: abs(sample["elapsed_sec"] - ramp_sec))
    hold_end_sample = max(live_samples, key=lambda sample: sample["elapsed_sec"])
    hold_window = [sample for sample in live_samples if sample["elapsed_sec"] >= hold_start_sample["elapsed_sec"]]
    if not hold_window:
        hold_window = [hold_start_sample, hold_end_sample]
    hold_min_sample = min(
        hold_window,
        key=lambda sample: float(sample["lifecycle"].get("paired_open_sessions_est") or 0.0),
    )

    hold_start_active = float(hold_start_sample["lifecycle"].get("paired_open_sessions_est") or 0.0)
    hold_end_active = float(hold_end_sample["lifecycle"].get("paired_open_sessions_est") or 0.0)
    hold_min_active = float(hold_min_sample["lifecycle"].get("paired_open_sessions_est") or 0.0)
    hold_mid_test_disconnects = max(0.0, hold_start_active - hold_end_active)
    hold_success_rate = (hold_end_active / hold_start_active) if hold_start_active > 0 else 0.0
    hold_success_rate = clamp_rate(hold_success_rate)
    hold_failure_rate = clamp_rate(1.0 - hold_success_rate)

    return {
        "max_session_summary_source": "raw_timeseries_hold_window",
        "hold_start_elapsed_sec": hold_start_sample["elapsed_sec"],
        "hold_end_elapsed_sec": hold_end_sample["elapsed_sec"],
        "hold_start_paired_open_sessions_est": hold_start_active,
        "hold_end_paired_open_sessions_est": hold_end_active,
        "hold_min_paired_open_sessions_est": hold_min_active,
        "hold_start_established_sessions": hold_start_sample["lifecycle"].get("established_sessions", 0.0),
        "hold_end_established_sessions": hold_end_sample["lifecycle"].get("established_sessions", 0.0),
        "hold_start_closed_sessions": hold_start_sample["lifecycle"].get("closed_sessions", 0.0),
        "hold_end_closed_sessions": hold_end_sample["lifecycle"].get("closed_sessions", 0.0),
        "hold_mid_test_disconnects": hold_mid_test_disconnects,
        "hold_session_success_rate": hold_success_rate,
        "hold_session_failure_rate": hold_failure_rate,
    }


def make_ip_gen(target):
    from trex.astf.api import ASTFIPGen, ASTFIPGenDist, ASTFIPGenGlobal

    ip_gen_c = ASTFIPGenDist(ip_range=[target["lan_ip"], target["lan_ip"]], distribution="seq")
    ip_gen_s = ASTFIPGenDist(ip_range=[target["wan_ip"], target["wan_ip"]], distribution="seq")
    return ASTFIPGen(
        glob=ASTFIPGenGlobal(ip_offset="0.0.0.0"),
        dist_client=ip_gen_c,
        dist_server=ip_gen_s,
    )


def make_long_session_profile(target, cps, max_sessions, keepalive_seconds, tcp_port):
    from trex.astf.api import ASTFGlobalInfo, ASTFProfile, ASTFProgram, ASTFTemplate, ASTFTCPClientTemplate, ASTFTCPServerTemplate

    loops = max(1, int(math.ceil(keepalive_seconds)))
    ip_gen = make_ip_gen(target)

    prog_c = ASTFProgram()
    prog_c.set_var("loops", loops)
    prog_c.set_label("keepalive:")
    prog_c.send(b"C")
    prog_c.recv(1)
    prog_c.delay(500000)
    prog_c.delay(500000)
    prog_c.jmp_nz("loops", "keepalive:")

    prog_s = ASTFProgram()
    prog_s.set_var("loops", loops)
    prog_s.set_label("keepalive:")
    prog_s.recv(1)
    prog_s.send(b"S")
    prog_s.delay(500000)
    prog_s.delay(500000)
    prog_s.jmp_nz("loops", "keepalive:")
    prog_s.wait_for_peer_close()

    info = ASTFGlobalInfo()
    info.tcp.no_delay = 1

    temp_c = ASTFTCPClientTemplate(
        program=prog_c,
        ip_gen=ip_gen,
        port=tcp_port,
        cps=float(cps),
        limit=int(max_sessions),
    )
    temp_s = ASTFTCPServerTemplate(program=prog_s)
    template = ASTFTemplate(client_template=temp_c, server_template=temp_s, tg_name="max_sessions")
    return ASTFProfile(default_ip_gen=ip_gen, templates=template, default_c_glob_info=info, default_s_glob_info=info)


def make_short_session_profile(target, cps, tcp_port):
    from trex.astf.api import ASTFGlobalInfo, ASTFProfile, ASTFProgram, ASTFTemplate, ASTFTCPClientTemplate, ASTFTCPServerTemplate

    ip_gen = make_ip_gen(target)

    prog_c = ASTFProgram()
    prog_c.send(b"C")
    prog_c.recv(1)
    prog_c.reset()

    prog_s = ASTFProgram()
    prog_s.recv(1)
    prog_s.send(b"S")

    info = ASTFGlobalInfo()
    info.tcp.no_delay = 1

    temp_c = ASTFTCPClientTemplate(program=prog_c, ip_gen=ip_gen, port=tcp_port, cps=float(cps))
    temp_s = ASTFTCPServerTemplate(program=prog_s)
    template = ASTFTemplate(client_template=temp_c, server_template=temp_s, tg_name="short_cps")
    return ASTFProfile(default_ip_gen=ip_gen, templates=template, default_c_glob_info=info, default_s_glob_info=info)


def try_arp(client, ports):
    try:
        client.set_service_mode(ports=ports, enabled=True)
        client.arp(ports=ports)
    except Exception:
        print("WARNING: automatic ARP failed. Check link, IP/gateway, and router ARP behavior.")
        print(traceback.format_exc())
    finally:
        try:
            client.set_service_mode(ports=ports, enabled=False)
        except Exception:
            pass


def run_astf_profile(client, profile, phase, test_name, duration, raw_writer,
                     wait_timeout_extra=30, nc=False, t_duration=0, summary_when="final",
                     progress_label=None, progress_enabled=False, progress_interval=1.0,
                     progress_percent_step=5.0,
                     progress_ramp_sec=None, progress_session_ramp_cps=None,
                     progress_max_sessions=None, return_samples=False):
    client.reset()
    client.load_profile(profile)
    client.clear_stats()
    start_time = time.time()
    live_stats = None
    raw_samples = []
    progress_percent_step = max(0.1, min(100.0, float(progress_percent_step)))
    next_progress_percent = progress_percent_step
    last_progress_percent = None
    client.start(mult=1, duration=float(duration), nc=nc, t_duration=float(t_duration))
    if progress_enabled and progress_label:
        print_progress(
            progress_label,
            0.0,
            duration,
            ramp_sec=progress_ramp_sec,
            session_ramp_cps=progress_session_ramp_cps,
            max_sessions=progress_max_sessions,
            enabled=True,
            percent_override=0.0,
        )
        last_progress_percent = 0.0
    while True:
        elapsed = time.time() - start_time
        stats = client.get_stats(skip_zero=False)
        sample_ts = ts_iso()
        write_raw_stats(raw_writer, phase, test_name, elapsed, stats, ts_local=sample_ts)
        if return_samples:
            raw_samples.append({"ts_local": sample_ts, "elapsed_sec": elapsed, "stats": stats})
        if progress_enabled and progress_label:
            if duration <= 0:
                current_percent = 100.0
            else:
                current_percent = min(100.0, max(0.0, (elapsed / float(duration)) * 100.0))
            while next_progress_percent <= 100.0 and current_percent + 1e-9 >= next_progress_percent:
                print_progress(
                    progress_label,
                    elapsed,
                    duration,
                    ramp_sec=progress_ramp_sec,
                    session_ramp_cps=progress_session_ramp_cps,
                    max_sessions=progress_max_sessions,
                    enabled=True,
                    percent_override=next_progress_percent,
                )
                last_progress_percent = next_progress_percent
                next_progress_percent += progress_percent_step
        if elapsed < duration:
            live_stats = stats
        if elapsed >= duration:
            break
        time.sleep(RAW_INTERVAL_SEC)
    if progress_enabled and progress_label and (last_progress_percent is None or last_progress_percent < 100.0):
        print_progress(
            progress_label,
            duration,
            duration,
            ramp_sec=progress_ramp_sec,
            session_ramp_cps=progress_session_ramp_cps,
            max_sessions=progress_max_sessions,
            enabled=True,
            percent_override=100.0,
        )
    client.wait_on_traffic(timeout=float(duration) + float(wait_timeout_extra))
    final_stats = client.get_stats(skip_zero=False)
    final_elapsed = time.time() - start_time
    final_ts = ts_iso()
    write_raw_stats(raw_writer, phase, test_name, final_elapsed, final_stats, ts_local=final_ts)
    try:
        client.stop()
    except Exception:
        pass
    if summary_when == "before_stop" and live_stats is not None:
        result_stats = live_stats
    else:
        result_stats = final_stats
    if return_samples:
        return result_stats, raw_samples
    return result_stats

