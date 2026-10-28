#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TRex ASTF MAP-E short-session TCP CPS benchmark."""

import argparse
import ipaddress
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
    from tests.lib.config import MAPE_TARGETS, load_defaults  # type: ignore
    from tests.lib.output import prepare_details_dir  # type: ignore
    from tests.lib.preflight import (  # type: ignore
        add_consecutive_preflight_arguments,
        validate_consecutive_preflight_arguments,
    )
    from tests.lib.timing import DEFAULT_TEST_START_DELAY_SEC  # type: ignore
    from tests.lib.astf import (  # type: ignore
        ASTF_TCP_PORT,
        DEFAULT_CPS_DURATION_SEC,
        DEFAULT_LOSS_STOP_THRESHOLD,
        DEFAULT_MAX_SESSIONS,
        DEFAULT_ASTF_PREFLIGHT_CPS,
        DEFAULT_ASTF_PREFLIGHT_DURATION_SEC,
        DEFAULT_ASTF_PREFLIGHT_MIN_SUCCESS_RATE,
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
    from tests.lib.mape import build_trex_target, validate_mape_profile  # type: ignore
    from tests.run_router_bench_v4_astf_sessions_cps import run_astf_cps_tests  # type: ignore
except Exception:
    from lib.config import MAPE_TARGETS, load_defaults  # type: ignore
    from lib.output import prepare_details_dir  # type: ignore
    from lib.preflight import (  # type: ignore
        add_consecutive_preflight_arguments,
        validate_consecutive_preflight_arguments,
    )
    from lib.timing import DEFAULT_TEST_START_DELAY_SEC  # type: ignore
    from lib.astf import (  # type: ignore
        ASTF_TCP_PORT,
        DEFAULT_CPS_DURATION_SEC,
        DEFAULT_LOSS_STOP_THRESHOLD,
        DEFAULT_MAX_SESSIONS,
        DEFAULT_ASTF_PREFLIGHT_CPS,
        DEFAULT_ASTF_PREFLIGHT_DURATION_SEC,
        DEFAULT_ASTF_PREFLIGHT_MIN_SUCCESS_RATE,
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
    from lib.mape import build_trex_target, validate_mape_profile  # type: ignore
    from run_router_bench_v4_astf_sessions_cps import run_astf_cps_tests  # type: ignore


_ASTF_DEFAULTS = load_defaults("astf")
_MAPE_ASTF_DEFAULTS = load_defaults("mape_astf")


def build_server_ip_candidates(target, server_ip_count):
    network = ipaddress.ip_network(target["server_network"], strict=True)
    if network.version != 4:
        raise ValueError("MAP-E server_network must be IPv4")

    server_ip = ipaddress.ip_address(target["server_ipv4"])
    if server_ip.version != 4 or server_ip not in network:
        raise ValueError(
            f"MAP-E server_ipv4 {server_ip} must be inside server_network {network}"
        )

    reserved_ip = network.network_address + 1
    candidates = [
        ipaddress.IPv4Address(value)
        for value in range(
            int(network.network_address) + 1,
            int(network.broadcast_address),
        )
        if ipaddress.IPv4Address(value) != reserved_ip
    ]
    if server_ip not in candidates:
        raise ValueError(
            f"MAP-E server_ipv4 {server_ip} is reserved or is not a usable host"
        )

    count = int(server_ip_count)
    if count <= 0:
        raise ValueError("server_ip_count must be positive")
    if count > len(candidates):
        raise ValueError(
            f"server_ip_count {count} exceeds usable server address count "
            f"{len(candidates)} in {network}"
        )

    server_index = candidates.index(server_ip)
    start_index = min(server_index, len(candidates) - count)
    selected = candidates[start_index:start_index + count]
    if any(
        int(right) != int(left) + 1
        for left, right in zip(selected, selected[1:])
    ):
        raise ValueError(
            "Selected MAP-E server candidates must form one contiguous IPv4 range"
        )
    return [str(address) for address in selected]


def merge_mape_target_with_overrides(base_target, args):
    target = dict(base_target)
    if args.interfaces:
        target["interfaces"] = args.interfaces
    for argument_name, profile_key in (
        ("lan_ip", "lan_ip"),
        ("lan_gw", "lan_gw"),
        ("wan_link_ip", "wan_link_ip"),
        ("wan_gateway_ip", "wan_gateway_ip"),
        ("wan_arp_target", "wan_arp_target"),
        ("server_ipv4", "server_ipv4"),
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


def write_metadata(out_dir, target_name, target, trex_target, args, server_ips):
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
        "mode": "mape_astf_sessions_cps",
        "tests": ["astf_short_cps"],
        "transport": "tcp",
        "mape": {
            "shared_ipv4": target["shared_ipv4"],
            "psid": target["psid"],
            "psid_offset": target["psid_offset"],
            "psid_length": target["psid_length"],
            "server_ipv4": target["server_ipv4"],
            "server_ip_range": [server_ips[0], server_ips[-1]],
            "server_ip_count": len(server_ips),
            "server_ip_distribution": "seq",
        },
        "preflight": {
            "tcp_roundtrip": True,
            "cps": DEFAULT_ASTF_PREFLIGHT_CPS,
            "duration_sec": DEFAULT_ASTF_PREFLIGHT_DURATION_SEC,
            "minimum_success_rate": DEFAULT_ASTF_PREFLIGHT_MIN_SUCCESS_RATE,
            "consecutive_successes": args.preflight_success_consecutive,
            "probe_interval_sec": args.preflight_probe_interval_sec,
            "timeout_sec": args.preflight_timeout_sec,
            "client_to_server_payload": True,
            "server_to_client_payload": True,
            "translated_port_capture": False,
        },
    }
    (out_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(
        description="Run TRex ASTF MAP-E short-session TCP CPS test."
    )
    parser.add_argument(
        "--target",
        choices=MAPE_TARGETS.keys(),
        default=_MAPE_ASTF_DEFAULTS["target"],
    )
    parser.add_argument(
        "--out-dir",
        default="",
        help=(
            "Result directory. Default: "
            "/var/trex/trex_mape_astf_sessions_cps_<target>_<timestamp>/."
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
        help="Seconds to wait after TRex ports report link-up and before testing.",
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
    parser.add_argument("--dpdk-driver", default="")
    parser.add_argument("--max-sessions", type=int, default=DEFAULT_MAX_SESSIONS, help=argparse.SUPPRESS)
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
    )
    parser.add_argument(
        "--stable-success-threshold",
        type=float,
        default=DEFAULT_STABLE_SUCCESS_THRESHOLD,
    )
    parser.add_argument(
        "--stable-loss-threshold",
        type=float,
        default=DEFAULT_STABLE_LOSS_THRESHOLD,
    )
    parser.add_argument("--representative-cps", type=int, default=DEFAULT_REPRESENTATIVE_CPS)
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
    parser.add_argument("--server-ipv4", default="")
    parser.add_argument(
        "--server-ip-count",
        type=int,
        default=int(_MAPE_ASTF_DEFAULTS["server_ip_count"]),
        help="Number of sequential MAP-E server IPv4 addresses to use.",
    )
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
    if not 0 <= args.loss_stop_threshold <= 1:
        raise SystemExit("ERROR: --loss-stop-threshold must be 0..1")
    if not 0 <= args.session_success_stop_threshold <= 1:
        raise SystemExit("ERROR: --session-success-stop-threshold must be 0..1")
    if not 0 <= args.stable_success_threshold <= 1:
        raise SystemExit("ERROR: --stable-success-threshold must be 0..1")
    if not 0 <= args.stable_loss_threshold <= 1:
        raise SystemExit("ERROR: --stable-loss-threshold must be 0..1")
    if args.representative_cps is not None and not 1 <= args.representative_cps <= 65535:
        raise SystemExit("ERROR: --representative-cps must be 1..65535")
    if not 1 <= args.astf_tcp_port <= 65535:
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

    target = merge_mape_target_with_overrides(MAPE_TARGETS[args.target], args)
    validate_mape_profile(args.target, target)
    try:
        server_ips = build_server_ip_candidates(target, args.server_ip_count)
    except ValueError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    server_ip_range = (server_ips[0], server_ips[-1])
    trex_target = build_trex_target(target)

    out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else DEFAULT_RESULT_ROOT
        / f"{_MAPE_ASTF_DEFAULTS['out_prefix']}_{args.target}_{ts_compact()}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    details_dir = prepare_details_dir(out_dir)

    print("============================================================")
    print("TRex ASTF MAP-E short-session TCP CPS benchmark")
    print("============================================================")
    print(f"Target              : {args.target}")
    print(f"Description         : {target['description']}")
    print(f"Interfaces          : {target['interfaces']}")
    print(f"ASTF DP cores       : {resolve_astf_cores(trex_target)}")
    print(f"LAN/client          : port {target['lan_port']} / {target['lan_ip']} gw {target['lan_gw']}")
    print(f"WAN/link            : port {target['wan_port']} / {trex_target['wan_ip']} gw {trex_target['wan_gw']}")
    print(f"MAP-E shared IPv4   : {target['shared_ipv4']}")
    print(f"MAP-E PSID          : {target['psid']} (offset={target['psid_offset']}, length={target['psid_length']})")
    print(f"ASTF server IP count: {len(server_ips)}")
    print(
        f"ASTF servers        : {server_ip_range[0]}-{server_ip_range[1]} "
        f"({len(server_ips)} IPv4 addresses), TCP port {args.astf_tcp_port}"
    )
    print(f"CPS steps           : {args.cps_steps}")
    print(f"Stable success min  : {args.stable_success_threshold:g}")
    print(f"Stable loss max     : {args.stable_loss_threshold:g}")
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
    write_metadata(details_dir, args.target, target, trex_target, args, server_ips)

    summaries = []
    server = TrexAstfServer(cores=resolve_astf_cores(trex_target), out_dir=details_dir)
    try:
        server.start()
        summaries.extend(
            run_astf_cps_tests(
                target=trex_target,
                out_dir=details_dir,
                args=args,
                astf_server_ip_range=server_ip_range,
            )
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
