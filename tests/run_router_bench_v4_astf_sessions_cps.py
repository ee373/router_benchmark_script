#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TRex IPv4 NAT ASTF short-session CPS benchmark."""

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
        DEFAULT_CPS_DURATION_SEC,
        DEFAULT_LOSS_STOP_THRESHOLD,
        DEFAULT_MAX_SESSIONS,
        DEFAULT_REPRESENTATIVE_CPS,
        DEFAULT_SESSION_SUCCESS_STOP_THRESHOLD,
        DEFAULT_STABLE_LOSS_THRESHOLD,
        DEFAULT_STABLE_SUCCESS_THRESHOLD,
        TREX_CFG_PATH,
        TREX_DIR,
        RAW_INTERVAL_SEC,
        TrexAstfServer,
        add_trex_api_path,
        bool_text,
        calc_astf_metrics,
        clamp_rate,
        first_number,
        install_trex_cfg,
        is_row_stable,
        json_cell,
        make_default_cps_steps,
        make_ip_gen,
        make_short_session_profile,
        merge_target_with_overrides,
        parse_cps_steps,
        parse_interfaces,
        prepare_trex_interfaces_from_profile,
        require_root,
        run_astf_profile,
        ts_compact,
        ts_iso,
        try_arp,
        write_raw_stats,
    )
except Exception:
    from run_router_bench_v4_astf_lib import (  # type: ignore
        ASTF_TCP_PORT,
        DEFAULT_CPS_DURATION_SEC,
        DEFAULT_LOSS_STOP_THRESHOLD,
        DEFAULT_MAX_SESSIONS,
        DEFAULT_REPRESENTATIVE_CPS,
        DEFAULT_SESSION_SUCCESS_STOP_THRESHOLD,
        DEFAULT_STABLE_LOSS_THRESHOLD,
        DEFAULT_STABLE_SUCCESS_THRESHOLD,
        TREX_CFG_PATH,
        TREX_DIR,
        RAW_INTERVAL_SEC,
        TrexAstfServer,
        add_trex_api_path,
        bool_text,
        calc_astf_metrics,
        clamp_rate,
        first_number,
        install_trex_cfg,
        is_row_stable,
        json_cell,
        make_default_cps_steps,
        make_ip_gen,
        make_short_session_profile,
        merge_target_with_overrides,
        parse_cps_steps,
        parse_interfaces,
        prepare_trex_interfaces_from_profile,
        require_root,
        run_astf_profile,
        ts_compact,
        ts_iso,
        try_arp,
        write_raw_stats,
    )


def run_cps_search(client, target, raw_writer, args, repeat_index):
    rows = []
    for cps in args.cps_steps:
        if cps > args.max_cps:
            continue
        test_name = f"r{repeat_index}_astf_short_cps_{cps}"
        print("============================================================")
        print("ASTF short-session CPS test")
        print("============================================================")
        print(f"Test name          : {test_name}")
        print(f"Requested CPS      : {cps}")
        print(f"Duration           : {args.duration:.3f}s")
        print(f"Loss stop threshold: {args.loss_stop_threshold:.3f}")
        print(f"Session success min: {args.session_success_stop_threshold:.3f}")
        print("============================================================")
        profile = make_short_session_profile(target=target, cps=cps, tcp_port=args.astf_tcp_port)
        stats = run_astf_profile(
            client=client,
            profile=profile,
            phase="astf_short_cps",
            test_name=test_name,
            duration=args.duration,
            raw_writer=raw_writer,
            wait_timeout_extra=args.cps_teardown_timeout + 30,
            nc=False,
            t_duration=args.cps_teardown_timeout,
        )
        row = {
            "phase": "astf_short_cps",
            "repeat": repeat_index,
            "test_name": test_name,
            "short_session_close": "client_rst_after_one_byte_each_direction",
        }
        row.update(calc_astf_metrics(stats, cps, args.duration))
        row["short_connection_cps"] = cps
        row["cps_success_rate"] = row.get("session_success_rate", 0.0)
        row["cps_failure_rate"] = row.get("session_failure_rate", 0.0)
        row["is_representative_cps"] = (
            args.representative_cps is not None
            and int(cps) == int(args.representative_cps)
        )
        row["representative_condition"] = (
            f"{int(args.representative_cps)}cps"
            if args.representative_cps is not None
            else "auto_limit_cps_minus_3_steps"
        )
        row["is_stable_cps"] = is_row_stable(row, args.stable_success_threshold, args.stable_loss_threshold)
        row["cps_stability_rule"] = (
            f"session_success_rate>={args.stable_success_threshold:g} "
            f"and packet_loss_rate<={args.stable_loss_threshold:g} "
            f"and throughput_loss_rate<={args.stable_loss_threshold:g}"
        )
        row["existing_flow_p99_latency_ms_during_cps"] = "not_measured"
        row["existing_flow_loss_rate_during_cps"] = "not_measured"

        packet_loss = float(row.get("packet_loss_rate") or 0.0)
        throughput_loss = float(row.get("throughput_loss_rate") or 0.0)
        session_success = float(row.get("session_success_rate") or 0.0)

        row["stop_condition"] = ""
        should_stop = False
        if packet_loss > args.loss_stop_threshold or throughput_loss > args.loss_stop_threshold:
            row["stop_condition"] = "loss_threshold_exceeded"
            should_stop = True
            print(
                f"Stopping CPS search at {cps} CPS: "
                f"packet_loss_rate={packet_loss:.6f}, "
                f"throughput_loss_rate={throughput_loss:.6f}, "
                f"session_success_rate={session_success:.6f}"
            )
        elif session_success <= args.session_success_stop_threshold:
            row["stop_condition"] = "session_success_threshold_not_met"
            should_stop = True
            print(
                f"Stopping CPS search at {cps} CPS: "
                f"session_success_rate={session_success:.6f} <= "
                f"{args.session_success_stop_threshold:.6f}"
            )
        elif cps >= args.max_cps:
            row["stop_condition"] = "max_cps_reached"
            should_stop = True

        rows.append(row)
        raw_writer.writerow([ts_iso(), "", "astf_short_cps", test_name, "summary", json.dumps(row, ensure_ascii=False, default=str)])

        if should_stop:
            break
        time.sleep(args.inter_test_sleep)
    return rows



def make_session_generation_overview(repeat_rows, args, repeat_index):
    cps_rows = [
        row for row in repeat_rows
        if row.get("phase") == "astf_short_cps" and "error" not in row and row.get("requested_cps") is not None
    ]
    max_rows = [
        row for row in repeat_rows
        if row.get("phase") == "astf_max_sessions" and "error" not in row
    ]

    cps_rows_sorted = sorted(cps_rows, key=lambda row: float(row.get("requested_cps") or 0.0))
    stable_rows = [
        row for row in cps_rows_sorted
        if is_row_stable(row, args.stable_success_threshold, args.stable_loss_threshold)
    ]
    unstable_rows = [
        row for row in cps_rows_sorted
        if not is_row_stable(row, args.stable_success_threshold, args.stable_loss_threshold)
    ]

    max_stable_row = max(stable_rows, key=lambda row: float(row.get("requested_cps") or 0.0), default=None)
    first_unstable_row = None
    seen_stable = False
    for row in cps_rows_sorted:
        if is_row_stable(row, args.stable_success_threshold, args.stable_loss_threshold):
            seen_stable = True
        elif seen_stable:
            first_unstable_row = row
            break
    if first_unstable_row is None:
        first_unstable_row = min(unstable_rows, key=lambda row: float(row.get("requested_cps") or 0.0), default=None)

    representative_row = None
    representative_condition = ""
    if args.representative_cps is not None:
        for row in cps_rows_sorted:
            if int(float(row.get("requested_cps") or -1)) == int(args.representative_cps):
                representative_row = row
                representative_condition = f"{int(args.representative_cps)}cps"
                break
        if representative_row is None:
            representative_row = max_stable_row or (cps_rows_sorted[-1] if cps_rows_sorted else None)
            representative_condition = f"{int(args.representative_cps)}cps_missing_fallback"
    elif first_unstable_row is not None and cps_rows_sorted:
        limit_cps = int(float(first_unstable_row.get("requested_cps") or 0))
        limit_index = None
        for index, row in enumerate(cps_rows_sorted):
            if int(float(row.get("requested_cps") or -1)) == limit_cps:
                limit_index = index
                break
        if limit_index is not None:
            representative_index = max(0, limit_index - 3)
            representative_row = cps_rows_sorted[representative_index]
            representative_condition = f"limit_cps_minus_3_steps_from_{limit_cps}cps"
    if representative_row is None:
        representative_row = max_stable_row or (cps_rows_sorted[-1] if cps_rows_sorted else None)
        representative_condition = "max_stable_cps_no_limit_reached" if max_stable_row is not None else "last_measured_cps_no_limit_reached"

    representative_cps_value = (representative_row or {}).get("requested_cps", "")

    max_session_row = max(max_rows, key=lambda row: float(row.get("max_sessions_maintained") or row.get("estimated_active_sessions") or 0.0), default=None)

    communication_outage_suspected = any(
        row.get("communication_outage_suspected") in (True, "true", "True", "あり")
        for row in repeat_rows
    )

    overview = {
        "phase": "astf_session_generation_summary",
        "repeat": repeat_index,
        "test_name": f"r{repeat_index}_astf_session_generation_summary",
        "summary_scope": "one_repeat",
        "short_connection_performance_cps": (max_stable_row or {}).get("requested_cps", ""),
        "maximum_stable_cps": (max_stable_row or {}).get("requested_cps", ""),
        "maximum_stable_achieved_cps": (max_stable_row or {}).get("achieved_cps", ""),
        "limit_cps": (first_unstable_row or {}).get("requested_cps", ""),
        "limit_cps_success_rate": (first_unstable_row or {}).get("session_success_rate", ""),
        "limit_cps_failure_rate": (first_unstable_row or {}).get("session_failure_rate", ""),
        "limit_cps_packet_loss_rate": (first_unstable_row or {}).get("packet_loss_rate", ""),
        "limit_cps_throughput_loss_rate": (first_unstable_row or {}).get("throughput_loss_rate", ""),
        "stable_success_threshold": args.stable_success_threshold,
        "stable_loss_threshold": args.stable_loss_threshold,
        "representative_cps": representative_cps_value,
        "representative_condition": representative_condition,
        "cps_success_rate": (representative_row or {}).get("session_success_rate", ""),
        "cps_failure_rate": (representative_row or {}).get("session_failure_rate", ""),
        "max_sessions_requested": (max_session_row or {}).get("target_max_sessions", args.max_sessions),
        "max_sessions_maintained": (max_session_row or {}).get("max_sessions_maintained", ""),
        "mid_test_disconnects": (max_session_row or {}).get("mid_test_disconnects", ""),
        "session_hold_success_rate": (max_session_row or {}).get("session_hold_success_rate", ""),
        "session_hold_failure_rate": (max_session_row or {}).get("session_hold_failure_rate", ""),
        "max_session_summary_source": (max_session_row or {}).get("max_session_summary_source", ""),
        "max_session_result_mode": (max_session_row or {}).get("max_session_result_mode", ""),
        "max_session_timeseries_csv": (max_session_row or {}).get("max_session_timeseries_csv", ""),
        "max_session_timeseries_rows": (max_session_row or {}).get("max_session_timeseries_rows", ""),
        "hold_start_elapsed_sec": (max_session_row or {}).get("hold_start_elapsed_sec", ""),
        "hold_end_elapsed_sec": (max_session_row or {}).get("hold_end_elapsed_sec", ""),
        "hold_start_paired_open_sessions_est": (max_session_row or {}).get("hold_start_paired_open_sessions_est", ""),
        "hold_end_paired_open_sessions_est": (max_session_row or {}).get("hold_end_paired_open_sessions_est", ""),
        "hold_min_paired_open_sessions_est": (max_session_row or {}).get("hold_min_paired_open_sessions_est", ""),
        "hold_start_established_sessions": (max_session_row or {}).get("hold_start_established_sessions", ""),
        "hold_end_established_sessions": (max_session_row or {}).get("hold_end_established_sessions", ""),
        "hold_start_closed_sessions": (max_session_row or {}).get("hold_start_closed_sessions", ""),
        "hold_end_closed_sessions": (max_session_row or {}).get("hold_end_closed_sessions", ""),
        "existing_flow_p99_latency_ms_during_cps": "not_measured",
        "existing_flow_loss_rate_during_cps": "not_measured",
        "communication_outage_suspected": communication_outage_suspected,
        "communication_outage": bool_text(communication_outage_suspected),
        "communication_outage_reason": ";".join(
            sorted(set(
                str(row.get("communication_outage_reason") or "")
                for row in repeat_rows
                if row.get("communication_outage_reason")
            ))
        ),
        "notes": (
            "existing_flow_p99_latency_ms_during_cps and existing_flow_loss_rate_during_cps "
            "need a latency-capable concurrent probe; ASTF regular counters do not provide this value."
        ),
    }
    return overview



def run_astf_cps_tests(target, out_dir, args):
    add_trex_api_path()
    from trex.astf.api import ASTFClient

    ports = [target["lan_port"], target["wan_port"]]
    raw_csv = out_dir / "astf_raw.csv"
    summaries = []

    client = ASTFClient(server="127.0.0.1")
    client.connect()
    try:
        client.reset()
        try_arp(client, ports)
        with open(raw_csv, "w", newline="") as raw_file:
            raw_writer = csv.writer(raw_file)
            raw_writer.writerow(["ts_local", "elapsed_sec", "phase", "test_name", "metric", "value"])

            for r in range(1, args.repeat + 1):
                repeat_rows = []
                try:
                    cps_rows = run_cps_search(client, target, raw_writer, args, r)
                    summaries.extend(cps_rows)
                    repeat_rows.extend(cps_rows)
                except Exception:
                    error_row = {
                        "phase": "astf_short_cps",
                        "repeat": r,
                        "test_name": f"r{r}_astf_short_cps_error",
                        "error": traceback.format_exc(),
                    }
                    print(error_row["error"])
                    summaries.append(error_row)
                    repeat_rows.append(error_row)
                raw_file.flush()

                overview_row = make_session_generation_overview(repeat_rows, args, r)
                summaries.append(overview_row)
                raw_writer.writerow([
                    ts_iso(),
                    "",
                    "astf_session_generation_summary",
                    overview_row["test_name"],
                    "summary",
                    json.dumps(overview_row, ensure_ascii=False, default=str),
                ])
                raw_file.flush()
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
        "mode": "astf_sessions_cps",
        "tests": ["astf_short_cps"],
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Run TRex ASTF IPv4 NAT short-session CPS test.")
    parser.add_argument("--target", choices=TARGETS.keys(), default="i226_1g")
    parser.add_argument("--out-dir", default="", help="Result directory. Default: /var/trex/trex_astf_sessions_cps_<target>_<timestamp>/.")
    parser.add_argument("--duration", type=float, default=DEFAULT_CPS_DURATION_SEC, help="Duration per short-session CPS step in seconds.")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--no-install-cfg", action="store_true")
    parser.add_argument("--auto-dpdk-bind", dest="auto_dpdk_bind", action="store_true", default=True, help="Before starting TRex, delete stale benchmark namespaces and bind profile PCI devices to their DPDK driver. Enabled by default.")
    parser.add_argument("--no-auto-dpdk-bind", dest="auto_dpdk_bind", action="store_false", help="Do not attempt DPDK driver binding before starting TRex. Stale benchmark namespaces are still removed.")
    parser.add_argument("--dpdk-driver", default="", help="Override the DPDK driver used for all profile PCI devices, e.g. vfio-pci or igb_uio.")

    parser.add_argument("--max-sessions", type=int, default=DEFAULT_MAX_SESSIONS, help=argparse.SUPPRESS)
    parser.add_argument("--max-cps", type=int, default=DEFAULT_MAX_SESSIONS)
    parser.add_argument("--cps-steps", type=parse_cps_steps, default=make_default_cps_steps(DEFAULT_MAX_SESSIONS))
    parser.add_argument("--loss-stop-threshold", type=float, default=DEFAULT_LOSS_STOP_THRESHOLD)
    parser.add_argument("--session-success-stop-threshold", type=float, default=DEFAULT_SESSION_SUCCESS_STOP_THRESHOLD, help="Stop short-session CPS search when session_success_rate is less than or equal to this value.")
    parser.add_argument("--stable-success-threshold", type=float, default=DEFAULT_STABLE_SUCCESS_THRESHOLD, help="Minimum session_success_rate required to count a CPS step as stable.")
    parser.add_argument("--stable-loss-threshold", type=float, default=DEFAULT_STABLE_LOSS_THRESHOLD, help="Maximum packet/L7 throughput loss rate allowed to count a CPS step as stable.")
    parser.add_argument("--representative-cps", type=int, default=DEFAULT_REPRESENTATIVE_CPS, help=("CPS value used for representative success/failure rate reporting. Default: auto-select the third measured CPS step before the limit CPS."))
    parser.add_argument("--cps-teardown-timeout", type=float, default=5.0)
    parser.add_argument("--astf-tcp-port", type=int, default=ASTF_TCP_PORT)
    parser.add_argument("--inter-test-sleep", type=float, default=5.0)

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
        print(f"WARNING: ignored unsupported options for ASTF CPS script: {' '.join(unknown_args)}")

    require_root()

    if args.duration <= 0:
        raise SystemExit("ERROR: --duration must be positive")
    if args.repeat <= 0:
        raise SystemExit("ERROR: --repeat must be positive")
    if args.max_cps <= 0 or args.max_cps > 65535:
        raise SystemExit("ERROR: --max-cps must be 1..65535")
    if args.loss_stop_threshold < 0 or args.loss_stop_threshold > 1:
        raise SystemExit("ERROR: --loss-stop-threshold must be 0..1")
    if args.session_success_stop_threshold < 0 or args.session_success_stop_threshold > 1:
        raise SystemExit("ERROR: --session-success-stop-threshold must be 0..1")
    if args.stable_success_threshold < 0 or args.stable_success_threshold > 1:
        raise SystemExit("ERROR: --stable-success-threshold must be 0..1")
    if args.stable_loss_threshold < 0 or args.stable_loss_threshold > 1:
        raise SystemExit("ERROR: --stable-loss-threshold must be 0..1")
    if args.representative_cps is not None and (args.representative_cps <= 0 or args.representative_cps > 65535):
        raise SystemExit("ERROR: --representative-cps must be 1..65535")
    if args.astf_tcp_port <= 0 or args.astf_tcp_port > 65535:
        raise SystemExit("ERROR: --astf-tcp-port must be 1..65535")

    args.cps_steps = [x for x in args.cps_steps if x <= args.max_cps]
    if args.max_cps not in args.cps_steps:
        args.cps_steps.append(args.max_cps)
    args.cps_steps = sorted(set(args.cps_steps))

    target = merge_target_with_overrides(TARGETS[args.target], args)
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_RESULT_ROOT / f"trex_astf_sessions_cps_{args.target}_{ts_compact()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("============================================================")
    print("TRex ASTF IPv4 NAT short-session CPS benchmark")
    print("============================================================")
    print(f"Target              : {args.target}")
    print(f"Description         : {target['description']}")
    print(f"Interfaces          : {target['interfaces']}")
    print(f"LAN/client          : port {target['lan_port']} / {target['lan_ip']} gw {target['lan_gw']}")
    print(f"WAN/server          : port {target['wan_port']} / {target['wan_ip']} gw {target['wan_gw']}")
    print(f"Router WAN IP       : {target['router_wan_ip']}")
    print(f"ASTF TCP port       : {args.astf_tcp_port}")
    print(f"CPS steps           : {args.cps_steps}")
    print(f"Stable success min  : {args.stable_success_threshold:g}")
    print(f"Stable loss max     : {args.stable_loss_threshold:g}")
    print(f"Representative CPS  : {args.representative_cps if args.representative_cps is not None else 'auto(limit CPS - 3 measured steps)'}")
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
    server = TrexAstfServer(cores=int(target["trex_cores"]), out_dir=out_dir)
    try:
        server.start()
        summaries.extend(run_astf_cps_tests(target=target, out_dir=out_dir, args=args))
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
    print(f"Metadata         : {out_dir / 'metadata.json'}")
    print(f"TRex server log  : {out_dir / 'trex_server_astf.log'}")


if __name__ == "__main__":
    main()
