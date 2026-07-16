#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TRex IPv4 NAT ASTF maximum concurrent session benchmark."""

import argparse
import csv
import json
import os
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
    from tests.run_router_bench_v4_astf_lib import (  # type: ignore
        ASTF_TCP_PORT,
        DEFAULT_MAX_SESSIONS,
        DEFAULT_SESSION_HOLD_SEC,
        DEFAULT_SESSION_RAMP_CPS,
        DEFAULT_STABLE_LOSS_THRESHOLD,
        DEFAULT_STABLE_SUCCESS_THRESHOLD,
        TREX_CFG_PATH,
        TREX_DIR,
        TrexAstfServer,
        add_trex_api_path,
        calc_astf_metrics,
        calc_session_lifecycle_stats,
        calc_hold_metrics_from_samples,
        float_or_zero,
        install_trex_cfg,
        merge_target_with_overrides,
        number_available,
        parse_interfaces,
        prepare_trex_interfaces_from_profile,
        require_root,
        resolve_astf_cores,
        run_astf_profile,
        make_long_session_profile,
        ts_compact,
        ts_iso,
        try_arp,
    )
except Exception:
    from run_router_bench_v4_astf_lib import (  # type: ignore
        ASTF_TCP_PORT,
        DEFAULT_MAX_SESSIONS,
        DEFAULT_SESSION_HOLD_SEC,
        DEFAULT_SESSION_RAMP_CPS,
        DEFAULT_STABLE_LOSS_THRESHOLD,
        DEFAULT_STABLE_SUCCESS_THRESHOLD,
        TREX_CFG_PATH,
        TREX_DIR,
        TrexAstfServer,
        add_trex_api_path,
        calc_astf_metrics,
        calc_session_lifecycle_stats,
        calc_hold_metrics_from_samples,
        float_or_zero,
        install_trex_cfg,
        merge_target_with_overrides,
        number_available,
        parse_interfaces,
        prepare_trex_interfaces_from_profile,
        require_root,
        resolve_astf_cores,
        run_astf_profile,
        make_long_session_profile,
        ts_compact,
        ts_iso,
        try_arp,
    )


MAX_SESSION_TIMESERIES_FIELDS = [
    # Time and target
    "ts_local",
    "repeat",
    "test_name",
    "sample_index",
    "elapsed_sec",
    "test_phase",
    "target_max_sessions",
    "target_sessions_at_elapsed",

    # Session establishment and creation failure
    "client_connects",
    "server_accepts",
    "established_sessions",
    "established_sessions_per_sec",
    "failed_or_unestablished_sessions_at_elapsed",
    "failed_or_unestablished_rate_at_elapsed",
    "session_creation_success_rate_at_elapsed",

    # Active/open flow counters. active_session_flows uses TRex m_est_flows when available.
    "active_session_flows",
    "active_sessions_source",
    "paired_trex_m_est_flows",
    "client_trex_m_est_flows",
    "server_trex_m_est_flows",
    "paired_trex_m_active_flows",
    "estimated_active_sessions",
    "active_drop_from_previous",
    "active_drop_from_peak",

    # Closed flow counters. tcps_closed is TRex's closed counter and includes drops.
    "closed_sessions",
    "client_tcps_closed",
    "server_tcps_closed",
    "closed_sessions_per_sec",
    "client_tcps_closed_per_sec",
    "server_tcps_closed_per_sec",

    # Other important symptoms
    "tcp_drops_total",
    "tcp_connection_drops_total",
    "tcp_timeout_drops_total",
    "tcp_retransmit_timeouts_total",
    "global_trex_rx_drop_bps",
    "packet_loss_rate",
    "throughput_loss_rate",
    "global_trex_socket_util",
]


def _delta_rate(current, previous, field, delta_elapsed):
    if previous is None or delta_elapsed <= 0:
        return ""
    return (float(current.get(field) or 0.0) - float(previous.get(field) or 0.0)) / float(delta_elapsed)


def write_max_session_timeseries_csv(writer, samples, repeat_index, test_name, args, ramp_sec, duration_sec):
    """Write one row per max-session sample to astf_max_sessions_timeseries.csv.

    This CSV is intentionally not reduced into a single maintained-session value.
    It exposes the counters needed to see where tcps_closed, active sessions, and
    connection establishment change during ramp and hold.
    """
    previous = None
    peak_paired_open = 0.0
    peak_selected_active = 0.0
    rows_written = 0
    for sample_index, sample in enumerate(samples or [], start=1):
        try:
            elapsed = float(sample.get("elapsed_sec"))
        except Exception:
            continue
        stats = sample.get("stats") or {}
        lifecycle = calc_session_lifecycle_stats(stats)
        metrics = calc_astf_metrics(
            stats,
            requested_cps=float(args.session_ramp_cps),
            duration_sec=max(elapsed, 0.001),
            expected_sessions=min(float(args.max_sessions), max(0.0, elapsed) * float(args.session_ramp_cps)),
        )

        if elapsed < float(ramp_sec):
            phase_name = "ramp"
            target_sessions_at_elapsed = min(float(args.max_sessions), max(0.0, elapsed) * float(args.session_ramp_cps))
        elif elapsed < float(duration_sec):
            phase_name = "hold"
            target_sessions_at_elapsed = float(args.max_sessions)
        else:
            phase_name = "done"
            target_sessions_at_elapsed = float(args.max_sessions)

        established = float(lifecycle.get("established_sessions") or 0.0)
        legacy_active = float(lifecycle.get("legacy_estimated_active_sessions") or 0.0)
        client_open = float(lifecycle.get("client_open_sessions_est") or 0.0)
        server_open = float(lifecycle.get("server_open_sessions_est") or 0.0)
        paired_open = float(lifecycle.get("paired_open_sessions_est") or 0.0)
        active = float(lifecycle.get("selected_active_sessions") or lifecycle.get("estimated_active_sessions") or 0.0)
        peak_paired_open = max(peak_paired_open, paired_open)
        peak_selected_active = max(peak_selected_active, active)
        failed_at_elapsed = max(0.0, target_sessions_at_elapsed - established)
        if target_sessions_at_elapsed > 0.0:
            failed_rate_at_elapsed = failed_at_elapsed / target_sessions_at_elapsed
            creation_success_rate_at_elapsed = established / target_sessions_at_elapsed
        else:
            failed_rate_at_elapsed = 0.0
            creation_success_rate_at_elapsed = 1.0

        tcp_drops_total = float_or_zero(lifecycle.get("client_tcps_drops", "")) + float_or_zero(lifecycle.get("server_tcps_drops", ""))
        tcp_connection_drops_total = float_or_zero(lifecycle.get("client_tcps_conndrops", "")) + float_or_zero(lifecycle.get("server_tcps_conndrops", ""))
        tcp_timeout_drops_total = float_or_zero(lifecycle.get("client_tcps_timeoutdrop", "")) + float_or_zero(lifecycle.get("server_tcps_timeoutdrop", ""))
        tcp_retransmit_timeouts_total = float_or_zero(lifecycle.get("client_tcps_rexmttimeo", "")) + float_or_zero(lifecycle.get("server_tcps_rexmttimeo", ""))

        if previous is None:
            delta_elapsed = ""
            active_drop_from_previous = ""
            paired_open_drop_from_previous = ""
        else:
            delta_elapsed_value = max(0.0, elapsed - float(previous.get("elapsed_sec") or 0.0))
            delta_elapsed = delta_elapsed_value
            active_drop_from_previous = max(0.0, float(previous.get("estimated_active_sessions") or 0.0) - active)
            paired_open_drop_from_previous = max(0.0, float(previous.get("paired_open_sessions_est") or 0.0) - paired_open)

        row = {
            "ts_local": sample.get("ts_local") or "",
            "repeat": repeat_index,
            "test_name": test_name,
            "sample_index": sample_index,
            "elapsed_sec": elapsed,
            "test_phase": phase_name,
            "target_max_sessions": args.max_sessions,
            "target_sessions_at_elapsed": target_sessions_at_elapsed,
            "session_ramp_cps": args.session_ramp_cps,
            "session_hold_sec": args.session_hold_sec,
            "ramp_sec": ramp_sec,
            "duration_sec": duration_sec,
            "client_connects": lifecycle.get("client_connects", 0.0),
            "server_accepts": lifecycle.get("server_accepts", 0.0),
            "established_sessions": established,
            "failed_or_unestablished_sessions_at_elapsed": failed_at_elapsed,
            "failed_or_unestablished_rate_at_elapsed": failed_rate_at_elapsed,
            "session_creation_success_rate_at_elapsed": creation_success_rate_at_elapsed,
            "active_session_flows": active,
            "client_closed_sessions": lifecycle.get("client_closed_sessions", 0.0),
            "server_closed_sessions": lifecycle.get("server_closed_sessions", 0.0),
            "client_tcps_closed": lifecycle.get("client_tcps_closed", lifecycle.get("client_closed_sessions", 0.0)),
            "server_tcps_closed": lifecycle.get("server_tcps_closed", lifecycle.get("server_closed_sessions", 0.0)),
            "closed_sessions": lifecycle.get("closed_sessions", 0.0),
            "client_tcps_drops": lifecycle.get("client_tcps_drops", ""),
            "server_tcps_drops": lifecycle.get("server_tcps_drops", ""),
            "client_tcps_conndrops": lifecycle.get("client_tcps_conndrops", ""),
            "server_tcps_conndrops": lifecycle.get("server_tcps_conndrops", ""),
            "client_tcps_timeoutdrop": lifecycle.get("client_tcps_timeoutdrop", ""),
            "server_tcps_timeoutdrop": lifecycle.get("server_tcps_timeoutdrop", ""),
            "client_tcps_keepdrops": lifecycle.get("client_tcps_keepdrops", ""),
            "server_tcps_keepdrops": lifecycle.get("server_tcps_keepdrops", ""),
            "client_tcps_testdrops": lifecycle.get("client_tcps_testdrops", ""),
            "server_tcps_testdrops": lifecycle.get("server_tcps_testdrops", ""),
            "client_tcps_rexmttimeo": lifecycle.get("client_tcps_rexmttimeo", ""),
            "server_tcps_rexmttimeo": lifecycle.get("server_tcps_rexmttimeo", ""),
            "tcp_drops_total": tcp_drops_total,
            "tcp_connection_drops_total": tcp_connection_drops_total,
            "tcp_timeout_drops_total": tcp_timeout_drops_total,
            "tcp_retransmit_timeouts_total": tcp_retransmit_timeouts_total,
            "global_trex_active_flows": lifecycle.get("global_trex_active_flows", ""),
            "global_trex_open_flows": lifecycle.get("global_trex_open_flows", ""),
            "global_trex_active_sockets": lifecycle.get("global_trex_active_sockets", ""),
            "global_trex_socket_util": lifecycle.get("global_trex_socket_util", ""),
            "global_trex_rx_drop_bps": lifecycle.get("global_trex_rx_drop_bps", ""),
            "client_trex_m_active_flows": lifecycle.get("client_trex_m_active_flows", ""),
            "client_trex_m_est_flows": lifecycle.get("client_trex_m_est_flows", ""),
            "server_trex_m_active_flows": lifecycle.get("server_trex_m_active_flows", ""),
            "server_trex_m_est_flows": lifecycle.get("server_trex_m_est_flows", ""),
            "paired_trex_m_active_flows": lifecycle.get("paired_trex_m_active_flows", ""),
            "paired_trex_m_est_flows": lifecycle.get("paired_trex_m_est_flows", ""),
            "legacy_estimated_active_sessions": legacy_active,
            "client_open_sessions_est": client_open,
            "server_open_sessions_est": server_open,
            "paired_open_sessions_est": paired_open,
            "selected_active_sessions": active,
            "active_sessions_source": lifecycle.get("active_sessions_source", ""),
            "estimated_active_sessions": active,
            "peak_paired_open_sessions_est_so_far": peak_paired_open,
            "peak_selected_active_sessions_so_far": peak_selected_active,
            "peak_estimated_active_sessions_so_far": peak_selected_active,
            "paired_open_drop_from_previous": paired_open_drop_from_previous,
            "active_drop_from_previous": active_drop_from_previous,
            "paired_open_drop_from_peak": max(0.0, peak_paired_open - paired_open),
            "active_drop_from_peak": max(0.0, peak_selected_active - active),
            "delta_elapsed_sec": delta_elapsed,
            "client_tx_tcp_packets": metrics.get("client_tx_tcp_packets", 0.0),
            "server_rx_tcp_packets": metrics.get("server_rx_tcp_packets", 0.0),
            "server_tx_tcp_packets": metrics.get("server_tx_tcp_packets", 0.0),
            "client_rx_tcp_packets": metrics.get("client_rx_tcp_packets", 0.0),
            "tx_tcp_packets": metrics.get("tx_tcp_packets", 0.0),
            "rx_tcp_packets": metrics.get("rx_tcp_packets", 0.0),
            "packet_loss_packets": metrics.get("packet_loss_packets", 0.0),
            "packet_loss_rate": metrics.get("packet_loss_rate", 0.0),
            "client_tx_l7_bytes": metrics.get("client_tx_l7_bytes", 0.0),
            "server_rx_l7_bytes": metrics.get("server_rx_l7_bytes", 0.0),
            "server_tx_l7_bytes": metrics.get("server_tx_l7_bytes", 0.0),
            "client_rx_l7_bytes": metrics.get("client_rx_l7_bytes", 0.0),
            "tx_l7_bytes": metrics.get("tx_l7_bytes", 0.0),
            "rx_l7_bytes": metrics.get("rx_l7_bytes", 0.0),
            "l7_loss_bytes": metrics.get("l7_loss_bytes", 0.0),
            "throughput_loss_rate": metrics.get("throughput_loss_rate", 0.0),
        }

        for field in [
            "client_connects",
            "server_accepts",
            "established_sessions",
            "client_closed_sessions",
            "server_closed_sessions",
            "client_tcps_closed",
            "server_tcps_closed",
            "closed_sessions",
            "client_tcps_drops",
            "server_tcps_drops",
            "client_tcps_conndrops",
            "server_tcps_conndrops",
            "client_tcps_timeoutdrop",
            "server_tcps_timeoutdrop",
            "client_tcps_keepdrops",
            "server_tcps_keepdrops",
            "client_tcps_testdrops",
            "server_tcps_testdrops",
            "client_tcps_rexmttimeo",
            "server_tcps_rexmttimeo",
            "global_trex_active_flows",
            "global_trex_open_flows",
            "global_trex_active_sockets",
            "global_trex_socket_util",
            "global_trex_rx_drop_bps",
            "client_trex_m_active_flows",
            "client_trex_m_est_flows",
            "server_trex_m_active_flows",
            "server_trex_m_est_flows",
            "paired_trex_m_active_flows",
            "paired_trex_m_est_flows",
            "legacy_estimated_active_sessions",
            "client_open_sessions_est",
            "server_open_sessions_est",
            "paired_open_sessions_est",
            "selected_active_sessions",
            "estimated_active_sessions",
        ]:
            if previous is None:
                row[f"delta_{field}"] = ""
                row[f"{field}_per_sec"] = ""
            else:
                prev_value = float(previous.get(field) or 0.0)
                cur_value = float(row.get(field) or 0.0)
                delta_value = cur_value - prev_value
                row[f"delta_{field}"] = delta_value
                if isinstance(delta_elapsed, (int, float)) and delta_elapsed > 0:
                    row[f"{field}_per_sec"] = delta_value / delta_elapsed
                else:
                    row[f"{field}_per_sec"] = ""

        # Keep the historical column names concise for these high-value rates.
        row["client_connects_per_sec"] = row.pop("client_connects_per_sec", row.get("client_connects_per_sec", ""))
        row["server_accepts_per_sec"] = row.pop("server_accepts_per_sec", row.get("server_accepts_per_sec", ""))
        row["established_sessions_per_sec"] = row.pop("established_sessions_per_sec", row.get("established_sessions_per_sec", ""))
        row["client_closed_sessions_per_sec"] = row.pop("client_closed_sessions_per_sec", row.get("client_closed_sessions_per_sec", ""))
        row["server_closed_sessions_per_sec"] = row.pop("server_closed_sessions_per_sec", row.get("server_closed_sessions_per_sec", ""))
        row["closed_sessions_per_sec"] = row.pop("closed_sessions_per_sec", row.get("closed_sessions_per_sec", ""))
        row["legacy_estimated_active_sessions_delta_per_sec"] = row.pop(
            "legacy_estimated_active_sessions_per_sec",
            row.get("legacy_estimated_active_sessions_per_sec", ""),
        )
        row["client_open_sessions_est_delta_per_sec"] = row.pop(
            "client_open_sessions_est_per_sec",
            row.get("client_open_sessions_est_per_sec", ""),
        )
        row["server_open_sessions_est_delta_per_sec"] = row.pop(
            "server_open_sessions_est_per_sec",
            row.get("server_open_sessions_est_per_sec", ""),
        )
        row["paired_open_sessions_est_delta_per_sec"] = row.pop(
            "paired_open_sessions_est_per_sec",
            row.get("paired_open_sessions_est_per_sec", ""),
        )
        row["estimated_active_sessions_delta_per_sec"] = row.pop(
            "estimated_active_sessions_per_sec",
            row.get("estimated_active_sessions_per_sec", ""),
        )

        writer.writerow({field: row.get(field, "") for field in MAX_SESSION_TIMESERIES_FIELDS})
        previous = row
        rows_written += 1
    return rows_written



def run_max_session_test(client, target, raw_writer, max_session_writer, args, repeat_index):
    ramp_sec = float(args.max_sessions) / float(args.session_ramp_cps)
    duration = ramp_sec + float(args.session_hold_sec)
    keepalive_seconds = duration + 10.0
    test_name = f"r{repeat_index}_astf_max_sessions_{args.max_sessions}_at_{args.session_ramp_cps:g}cps"

    print("============================================================")
    print("ASTF maximum-session test")
    print("============================================================")
    print(f"Test name          : {test_name}")
    print(f"New sessions       : {args.session_ramp_cps:g} CPS")
    print(f"Maximum sessions   : {args.max_sessions}")
    print(f"Ramp duration      : {ramp_sec:.3f}s")
    print(f"Hold duration      : {args.session_hold_sec:.3f}s")
    print(f"Total duration     : {duration:.3f}s")
    print("============================================================")

    profile = make_long_session_profile(
        target=target,
        cps=args.session_ramp_cps,
        max_sessions=args.max_sessions,
        keepalive_seconds=keepalive_seconds,
        tcp_port=args.astf_tcp_port,
    )
    stats, raw_samples = run_astf_profile(
        client=client,
        profile=profile,
        phase="astf_max_sessions",
        test_name=test_name,
        duration=duration,
        raw_writer=raw_writer,
        wait_timeout_extra=60,
        nc=True,
        t_duration=0,
        summary_when="before_stop",
        progress_label="ASTF max sessions",
        progress_enabled=args.show_progress,
        progress_interval=args.progress_interval,
        progress_percent_step=args.progress_percent_step,
        progress_ramp_sec=ramp_sec,
        progress_session_ramp_cps=args.session_ramp_cps,
        progress_max_sessions=args.max_sessions,
        return_samples=True,
    )
    timeseries_rows = write_max_session_timeseries_csv(
        writer=max_session_writer,
        samples=raw_samples,
        repeat_index=repeat_index,
        test_name=test_name,
        args=args,
        ramp_sec=ramp_sec,
        duration_sec=duration,
    )
    outage_metrics = calc_astf_metrics(stats, args.session_ramp_cps, duration, expected_sessions=args.max_sessions)
    row = {
        "phase": "astf_max_sessions",
        "repeat": repeat_index,
        "test_name": test_name,
        "target_max_sessions": args.max_sessions,
        "session_ramp_cps": args.session_ramp_cps,
        "session_hold_sec": args.session_hold_sec,
        "keepalive_payload_bytes_each_direction": 1,
        "keepalive_interval_sec": 1,
        "max_sessions_requested": args.max_sessions,
        "max_session_summary_source": "not_summarized_see_astf_max_sessions_timeseries_csv",
        "max_session_result_mode": "per_second_timeseries_csv",
        "max_session_timeseries_csv": "astf_max_sessions_timeseries.csv",
        "max_session_timeseries_rows": timeseries_rows,
        "max_sessions_maintained": "",
        "mid_test_disconnects": "",
        "session_hold_success_rate": "",
        "session_hold_failure_rate": "",
        "session_hold_metric_applicable": False,
        "communication_outage_suspected": outage_metrics.get("communication_outage_suspected", False),
        "communication_outage": outage_metrics.get("communication_outage", "なし"),
        "communication_outage_reason": outage_metrics.get("communication_outage_reason", ""),
        "notes": (
            "ASTF maximum-session counters are not reduced to a maintained-session summary. "
            "Inspect astf_max_sessions_timeseries.csv for TRex-provided m_active_flows/m_est_flows, "
            "global active/open flow counters, TCP drop counters, the legacy script-derived estimates, "
            "and per-second deltas."
        ),
    }
    return row



def run_astf_max_session_tests(target, out_dir, args):
    add_trex_api_path()
    from trex.astf.api import ASTFClient

    ports = [target["lan_port"], target["wan_port"]]
    raw_csv = out_dir / "astf_raw.csv"
    max_session_timeseries_csv = out_dir / "astf_max_sessions_timeseries.csv"
    summaries = []

    client = ASTFClient(server="127.0.0.1")
    client.connect()
    try:
        client.reset()
        try_arp(client, ports)
        with open(raw_csv, "w", newline="") as raw_file, open(max_session_timeseries_csv, "w", newline="") as max_session_file:
            raw_writer = csv.writer(raw_file)
            raw_writer.writerow(["ts_local", "elapsed_sec", "phase", "test_name", "metric", "value"])
            max_session_writer = csv.DictWriter(max_session_file, fieldnames=MAX_SESSION_TIMESERIES_FIELDS, extrasaction="ignore")
            max_session_writer.writeheader()

            for r in range(1, args.repeat + 1):
                try:
                    row = run_max_session_test(client, target, raw_writer, max_session_writer, args, r)
                    summaries.append(row)
                except Exception:
                    error_row = {
                        "phase": "astf_max_sessions",
                        "repeat": r,
                        "test_name": f"r{r}_astf_max_sessions_error",
                        "error": traceback.format_exc(),
                    }
                    print(error_row["error"])
                    summaries.append(error_row)
                raw_file.flush()
                max_session_file.flush()
                if r < args.repeat:
                    time.sleep(args.inter_test_sleep)
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
    return summaries


def write_summary_csv(path, summaries):
    keys = []
    for row in summaries:
        for key in row.keys():
            if key not in keys:
                keys.append(key)
    with open(path, "w", newline="") as out_file:
        writer = csv.DictWriter(out_file, fieldnames=keys)
        writer.writeheader()
        for row in summaries:
            writer.writerow(row)


def write_metadata(out_dir, target_name, target, args):
    metadata = {
        "created_at": ts_iso(),
        "target_name": target_name,
        "target": target,
        "args": vars(args),
        "trex_dir": TREX_DIR,
        "trex_cfg_path": TREX_CFG_PATH,
        "mode": "astf_max_sessions",
        "tests": ["astf_max_sessions"],
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Run TRex ASTF IPv4 NAT maximum-session test.")
    parser.add_argument("--target", choices=TARGETS.keys(), default="i226_1g")
    parser.add_argument("--out-dir", default="", help="Result directory. Default: /var/trex/trex_astf_max_sessions_<target>_<timestamp>/.")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--no-install-cfg", action="store_true")
    parser.add_argument("--auto-dpdk-bind", dest="auto_dpdk_bind", action="store_true", default=True, help="Before starting TRex, delete stale benchmark namespaces and bind profile PCI devices to their DPDK driver. Enabled by default.")
    parser.add_argument("--no-auto-dpdk-bind", dest="auto_dpdk_bind", action="store_false", help="Do not attempt DPDK driver binding before starting TRex. Stale benchmark namespaces are still removed.")
    parser.add_argument("--dpdk-driver", default="", help="Override the DPDK driver used for all profile PCI devices, e.g. vfio-pci or igb_uio.")

    parser.add_argument("--max-sessions", type=int, default=DEFAULT_MAX_SESSIONS)
    parser.add_argument("--session-ramp-cps", type=float, default=DEFAULT_SESSION_RAMP_CPS)
    parser.add_argument("--session-hold-sec", type=float, default=DEFAULT_SESSION_HOLD_SEC)
    parser.add_argument("--stable-success-threshold", type=float, default=DEFAULT_STABLE_SUCCESS_THRESHOLD)
    parser.add_argument("--stable-loss-threshold", type=float, default=DEFAULT_STABLE_LOSS_THRESHOLD)
    parser.add_argument("--astf-tcp-port", type=int, default=ASTF_TCP_PORT)
    parser.add_argument("--inter-test-sleep", type=float, default=5.0)
    parser.add_argument("--show-progress", dest="show_progress", action="store_true", help="Show progress for long ASTF maximum-session tests. Default: enabled.")
    parser.add_argument("--no-progress", dest="show_progress", action="store_false", help="Disable ASTF maximum-session progress.")
    parser.set_defaults(show_progress=True)
    parser.add_argument("--progress-interval", type=float, default=10.0, help="Compatibility option retained for older command lines. Progress is printed by percent step.")
    parser.add_argument("--progress-percent-step", type=float, default=5.0, help="Progress display step in percent for the maximum-session test. Default: 5 percent.")

    parser.add_argument("--interfaces", type=parse_interfaces, default=None)
    parser.add_argument("--lan-ip", default="")
    parser.add_argument("--lan-gw", default="")
    parser.add_argument("--wan-ip", default="")
    parser.add_argument("--wan-gw", default="")
    parser.add_argument("--router-wan-ip", default="")
    parser.add_argument("--line-rate-gbps", type=float, default=None)
    parser.add_argument("--cores", type=int, default=None)

    args, unknown_args = parser.parse_known_args()
    if unknown_args:
        print(f"WARNING: ignored unsupported options for ASTF max-session script: {' '.join(unknown_args)}")

    require_root()

    if args.repeat <= 0:
        raise SystemExit("ERROR: --repeat must be positive")
    if args.max_sessions <= 0 or args.max_sessions > 65535:
        raise SystemExit("ERROR: --max-sessions must be 1..65535")
    if args.session_ramp_cps <= 0:
        raise SystemExit("ERROR: --session-ramp-cps must be positive")
    if args.session_hold_sec < 0:
        raise SystemExit("ERROR: --session-hold-sec must be >= 0")
    if args.stable_success_threshold < 0 or args.stable_success_threshold > 1:
        raise SystemExit("ERROR: --stable-success-threshold must be 0..1")
    if args.stable_loss_threshold < 0 or args.stable_loss_threshold > 1:
        raise SystemExit("ERROR: --stable-loss-threshold must be 0..1")
    if args.progress_interval <= 0:
        raise SystemExit("ERROR: --progress-interval must be positive")
    if args.progress_percent_step <= 0 or args.progress_percent_step > 100:
        raise SystemExit("ERROR: --progress-percent-step must be greater than 0 and less than or equal to 100")
    if args.astf_tcp_port <= 0 or args.astf_tcp_port > 65535:
        raise SystemExit("ERROR: --astf-tcp-port must be 1..65535")

    target = merge_target_with_overrides(TARGETS[args.target], args)
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_RESULT_ROOT / f"trex_astf_max_sessions_{args.target}_{ts_compact()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("============================================================")
    print("TRex ASTF IPv4 NAT maximum-session benchmark")
    print("============================================================")
    print(f"Target              : {args.target}")
    print(f"Description         : {target['description']}")
    print(f"Interfaces          : {target['interfaces']}")
    print(f"ASTF DP cores       : {resolve_astf_cores(target)}")
    print(f"LAN/client          : port {target['lan_port']} / {target['lan_ip']} gw {target['lan_gw']}")
    print(f"WAN/server          : port {target['wan_port']} / {target['wan_ip']} gw {target['wan_gw']}")
    print(f"Router WAN IP       : {target['router_wan_ip']}")
    print(f"ASTF TCP port       : {args.astf_tcp_port}")
    print(f"Maximum sessions    : {args.max_sessions}")
    print(f"Session ramp CPS    : {args.session_ramp_cps:g}")
    print(f"Session hold sec    : {args.session_hold_sec:g}")
    print(f"Progress display    : {args.show_progress}")
    print(f"Progress percent step: {args.progress_percent_step:g}%")
    print(f"Output              : {out_dir}")
    print("============================================================")

    prepare_log = prepare_trex_interfaces_from_profile(
        target,
        out_dir,
        auto_dpdk_bind=args.auto_dpdk_bind,
        dpdk_driver_override=args.dpdk_driver,
    )
    print(f"TRex interface prep : {prepare_log}")

    install_trex_cfg(target, out_dir, args.no_install_cfg)
    write_metadata(out_dir, args.target, target, args)

    summaries = []
    server = TrexAstfServer(cores=resolve_astf_cores(target), out_dir=out_dir)
    try:
        server.start()
        summaries.extend(run_astf_max_session_tests(target=target, out_dir=out_dir, args=args))
    finally:
        server.stop()

    summary_csv = out_dir / "summary_all.csv"
    write_summary_csv(summary_csv, summaries)

    print("============================================================")
    print("DONE")
    print("============================================================")
    print(f"Result directory : {out_dir}")
    print(f"Summary CSV      : {summary_csv}")
    print(f"Raw CSV          : {out_dir / 'astf_raw.csv'}")
    print(f"Max session CSV  : {out_dir / 'astf_max_sessions_timeseries.csv'}")
    print(f"Metadata         : {out_dir / 'metadata.json'}")
    print(f"TRex server log  : {out_dir / 'trex_server_astf.log'}")


if __name__ == "__main__":
    main()
