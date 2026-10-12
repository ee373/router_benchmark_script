#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TRex ASTF DS-Lite short-session TCP CPS benchmark."""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from tests.lib.config import DSLITE_TARGETS  # type: ignore
    from tests.lib.output import prepare_details_dir  # type: ignore
    from tests.lib.preflight import (  # type: ignore
        add_consecutive_preflight_arguments,
        validate_consecutive_preflight_arguments,
    )
except Exception:
    from lib.config import DSLITE_TARGETS  # type: ignore
    from lib.output import prepare_details_dir  # type: ignore
    from lib.preflight import (  # type: ignore
        add_consecutive_preflight_arguments,
        validate_consecutive_preflight_arguments,
    )

try:
    from tests.lib.timing import (  # type: ignore
        DEFAULT_TEST_START_DELAY_SEC,
    )
except Exception:
    from lib.timing import DEFAULT_TEST_START_DELAY_SEC  # type: ignore

try:
    from tests.lib.astf import (  # type: ignore
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
        TrexAstfServer,
        install_trex_cfg,
        make_default_cps_steps,
        parse_cps_steps,
        parse_interfaces,
        prepare_trex_interfaces_from_profile,
        require_root,
        resolve_astf_cores,
        ts_compact,
        ts_iso,
    )
    from tests.lib.common import DEFAULT_RESULT_ROOT, write_summary_csv  # type: ignore
    from tests.lib.config import load_defaults as _load_defaults  # type: ignore
    from tests.lib.dslite import build_trex_target, validate_dslite_profile  # type: ignore
    from tests.run_router_bench_v4_astf_sessions_cps import run_astf_cps_tests  # type: ignore
except Exception:
    from lib.astf import (  # type: ignore
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
        TrexAstfServer,
        install_trex_cfg,
        make_default_cps_steps,
        parse_cps_steps,
        parse_interfaces,
        prepare_trex_interfaces_from_profile,
        require_root,
        resolve_astf_cores,
        ts_compact,
        ts_iso,
    )
    from lib.common import DEFAULT_RESULT_ROOT, write_summary_csv  # type: ignore
    from lib.config import load_defaults as _load_defaults  # type: ignore
    from lib.dslite import build_trex_target, validate_dslite_profile  # type: ignore
    from run_router_bench_v4_astf_sessions_cps import run_astf_cps_tests  # type: ignore


_ASTF_DEFAULTS = _load_defaults("astf")
_DSLITE_ASTF_DEFAULTS = _load_defaults("dslite_astf")


def merge_dslite_target_with_overrides(base_target, args):
    target = dict(base_target)
    if args.interfaces:
        target["interfaces"] = args.interfaces
    for argument_name, profile_key in (
        ("lan_ip", "lan_ip"),
        ("lan_gw", "lan_gw"),
        ("wan_link_ip", "wan_link_ip"),
        ("wan_gateway_ip", "wan_gateway_ip"),
        ("wan_arp_target", "wan_arp_target"),
        ("server_ip", "server_ip"),
        ("nat_pool_cidr", "nat_pool_cidr"),
    ):
        value = getattr(args, argument_name)
        if value:
            target[profile_key] = value
    if args.line_rate_gbps is not None:
        target["line_rate_gbps"] = args.line_rate_gbps
    if args.cores is not None:
        target["trex_cores"] = args.cores
        target["astf_cores"] = args.cores
    return target


def write_metadata(out_dir, target_name, target, trex_target, args):
    metadata = {
        "created_at": ts_iso(),
        "target_name": target_name,
        "target": target,
        "trex_target": {
            "lan_ip": trex_target["lan_ip"],
            "lan_gw": trex_target["lan_gw"],
            "wan_ip": trex_target["wan_ip"],
            "wan_gw": trex_target["wan_gw"],
            "router_wan_ip": trex_target["router_wan_ip"],
        },
        "args": vars(args),
        "trex_dir": TREX_DIR,
        "trex_cfg_path": TREX_CFG_PATH,
        "mode": "dslite_astf_sessions_cps",
        "tests": ["astf_short_cps"],
        "transport": "tcp",
        "aftr_nat_pool": target["nat_pool_cidr"],
        "b4_session_limit": target["b4_session_limit"],
        "preflight": {
            "tcp_roundtrip": True,
            "client_to_server_payload": True,
            "server_to_client_payload": True,
        },
    }
    (out_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(
        description="Run TRex ASTF DS-Lite short-session TCP CPS test."
    )
    parser.add_argument(
        "--target",
        choices=DSLITE_TARGETS.keys(),
        default=_DSLITE_ASTF_DEFAULTS["target"],
    )
    parser.add_argument(
        "--out-dir",
        default="",
        help=(
            "Result directory. Default: "
            "/var/trex/trex_dslite_astf_sessions_cps_<target>_<timestamp>/."
        ),
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_CPS_DURATION_SEC,
        help="Duration per short-session CPS step in seconds.",
    )
    parser.add_argument("--repeat", type=int, default=_ASTF_DEFAULTS["repeat"])
    parser.add_argument(
        "--test-start-delay-sec",
        type=float,
        default=DEFAULT_TEST_START_DELAY_SEC,
        help="Seconds to wait after TRex ports report link-up and before testing. Default: 15.",
    )
    parser.add_argument("--no-install-cfg", action="store_true")
    parser.add_argument(
        "--auto-dpdk-bind",
        dest="auto_dpdk_bind",
        action="store_true",
        default=_ASTF_DEFAULTS["auto_dpdk_bind"],
        help="Bind profile PCI devices to their DPDK driver before starting TRex.",
    )
    parser.add_argument(
        "--no-auto-dpdk-bind",
        dest="auto_dpdk_bind",
        action="store_false",
        help="Do not bind profile PCI devices to a DPDK driver.",
    )
    parser.add_argument(
        "--dpdk-driver",
        default="",
        help="Override the DPDK driver used for all profile PCI devices.",
    )

    parser.add_argument(
        "--max-sessions",
        type=int,
        default=DEFAULT_MAX_SESSIONS,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--max-cps", type=int, default=DEFAULT_MAX_SESSIONS)
    parser.add_argument(
        "--cps-steps",
        type=parse_cps_steps,
        default=make_default_cps_steps(DEFAULT_MAX_SESSIONS),
    )
    parser.add_argument("--loss-stop-threshold", type=float, default=DEFAULT_LOSS_STOP_THRESHOLD)
    parser.add_argument(
        "--session-success-stop-threshold",
        type=float,
        default=DEFAULT_SESSION_SUCCESS_STOP_THRESHOLD,
        help="Stop when session_success_rate is less than or equal to this value.",
    )
    parser.add_argument(
        "--stable-success-threshold",
        type=float,
        default=DEFAULT_STABLE_SUCCESS_THRESHOLD,
        help="Minimum session_success_rate required for a stable CPS step.",
    )
    parser.add_argument(
        "--stable-loss-threshold",
        type=float,
        default=DEFAULT_STABLE_LOSS_THRESHOLD,
        help="Maximum packet/L7 loss rate allowed for a stable CPS step.",
    )
    parser.add_argument(
        "--representative-cps",
        type=int,
        default=DEFAULT_REPRESENTATIVE_CPS,
        help="CPS value used for representative success/failure reporting.",
    )
    parser.add_argument(
        "--cps-teardown-timeout",
        type=float,
        default=_ASTF_DEFAULTS["cps_teardown_timeout_sec"],
    )
    parser.add_argument("--astf-tcp-port", type=int, default=ASTF_TCP_PORT)
    parser.add_argument(
        "--inter-test-sleep",
        type=float,
        default=_ASTF_DEFAULTS["inter_test_sleep_sec"],
    )
    add_consecutive_preflight_arguments(parser)

    parser.add_argument("--interfaces", type=parse_interfaces, default=None)
    parser.add_argument("--lan-ip", default="")
    parser.add_argument("--lan-gw", default="")
    parser.add_argument("--wan-link-ip", default="")
    parser.add_argument("--wan-gateway-ip", default="")
    parser.add_argument("--wan-arp-target", default="")
    parser.add_argument("--server-ip", default="")
    parser.add_argument("--nat-pool-cidr", default="")
    parser.add_argument("--line-rate-gbps", type=float, default=None)
    parser.add_argument("--cores", type=int, default=None)

    args = parser.parse_args()
    require_root()

    if args.duration <= 0:
        raise SystemExit("ERROR: --duration must be positive")
    if args.repeat <= 0:
        raise SystemExit("ERROR: --repeat must be positive")
    if args.test_start_delay_sec < 0:
        raise SystemExit("ERROR: --test-start-delay-sec must be >= 0")
    if args.max_cps <= 0 or args.max_cps > 65535:
        raise SystemExit("ERROR: --max-cps must be 1..65535")
    if args.loss_stop_threshold < 0 or args.loss_stop_threshold > 1:
        raise SystemExit("ERROR: --loss-stop-threshold must be 0..1")
    if (
        args.session_success_stop_threshold < 0
        or args.session_success_stop_threshold > 1
    ):
        raise SystemExit("ERROR: --session-success-stop-threshold must be 0..1")
    if args.stable_success_threshold < 0 or args.stable_success_threshold > 1:
        raise SystemExit("ERROR: --stable-success-threshold must be 0..1")
    if args.stable_loss_threshold < 0 or args.stable_loss_threshold > 1:
        raise SystemExit("ERROR: --stable-loss-threshold must be 0..1")
    if args.representative_cps is not None and not (
        1 <= args.representative_cps <= 65535
    ):
        raise SystemExit("ERROR: --representative-cps must be 1..65535")
    if args.astf_tcp_port <= 0 or args.astf_tcp_port > 65535:
        raise SystemExit("ERROR: --astf-tcp-port must be 1..65535")
    if args.cps_teardown_timeout < 0:
        raise SystemExit("ERROR: --cps-teardown-timeout must be >= 0")
    if args.inter_test_sleep < 0:
        raise SystemExit("ERROR: --inter-test-sleep must be >= 0")
    validate_consecutive_preflight_arguments(args)

    args.cps_steps = [value for value in args.cps_steps if value <= args.max_cps]
    if args.max_cps not in args.cps_steps:
        args.cps_steps.append(args.max_cps)
    args.cps_steps = sorted(set(args.cps_steps))

    target = merge_dslite_target_with_overrides(
        DSLITE_TARGETS[args.target],
        args,
    )
    validate_dslite_profile(args.target, target)
    trex_target = build_trex_target(target)

    out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else DEFAULT_RESULT_ROOT
        / f"trex_dslite_astf_sessions_cps_{args.target}_{ts_compact()}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    details_dir = prepare_details_dir(out_dir)

    print("============================================================")
    print("TRex ASTF DS-Lite short-session TCP CPS benchmark")
    print("============================================================")
    print(f"Target              : {args.target}")
    print(f"Description         : {target['description']}")
    print(f"Interfaces          : {target['interfaces']}")
    print(f"ASTF DP cores       : {resolve_astf_cores(trex_target)}")
    print(
        f"LAN/client          : port {target['lan_port']} / "
        f"{target['lan_ip']} gw {target['lan_gw']}"
    )
    print(
        f"WAN/server          : port {target['wan_port']} / "
        f"{trex_target['wan_ip']} gw {trex_target['wan_gw']}"
    )
    print(f"AFTR NAT pool       : {target['nat_pool_cidr']}")
    print(f"B4 session limit    : {target['b4_session_limit']}")
    print(f"ASTF TCP port       : {args.astf_tcp_port}")
    print(f"CPS steps           : {args.cps_steps}")
    print(f"Stable success min  : {args.stable_success_threshold:g}")
    print(f"Stable loss max     : {args.stable_loss_threshold:g}")
    print(
        "Representative CPS  : "
        + (
            str(args.representative_cps)
            if args.representative_cps is not None
            else "auto(limit CPS - 3 measured steps)"
        )
    )
    print(f"Output              : {out_dir}")
    print("============================================================")

    prepare_log = prepare_trex_interfaces_from_profile(
        trex_target,
        details_dir,
        auto_dpdk_bind=args.auto_dpdk_bind,
        dpdk_driver_override=args.dpdk_driver,
    )
    print(f"TRex interface prep : {prepare_log}")

    install_trex_cfg(trex_target, details_dir, args.no_install_cfg)
    write_metadata(details_dir, args.target, target, trex_target, args)

    summaries = []
    server = TrexAstfServer(cores=resolve_astf_cores(trex_target), out_dir=details_dir)
    try:
        server.start()
        summaries.extend(
            run_astf_cps_tests(target=trex_target, out_dir=details_dir, args=args)
        )
    finally:
        server.stop()

    summary_csv = out_dir / "summary_all.csv"
    write_summary_csv(summary_csv, summaries)

    print("============================================================")
    print("DONE")
    print("============================================================")
    print(f"Result directory : {out_dir}")
    print(f"Summary CSV      : {summary_csv}")
    print(f"Details          : {details_dir}")
    print(f"Raw CSV          : {details_dir / 'astf_raw.csv'}")
    print(f"Metadata         : {details_dir / 'metadata.json'}")
    print(f"TRex server log  : {details_dir / 'trex_server_astf.log'}")


if __name__ == "__main__":
    main()
