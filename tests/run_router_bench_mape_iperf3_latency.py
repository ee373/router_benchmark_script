#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MAP-E TCP/UDP request-response latency benchmark with iperf3 load."""

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from tests.lib.config import load_defaults  # type: ignore
    from tests.lib.mape import (  # type: ignore
        build_trex_target,
        generate_mape_ports,
        validate_mape_profile,
    )
    from tests.lib.config import MAPE_TARGETS  # type: ignore
    from tests import run_router_bench_v4_iperf3_latency as latency  # type: ignore
except Exception:
    from lib.config import load_defaults  # type: ignore
    from lib.mape import (  # type: ignore
        build_trex_target,
        generate_mape_ports,
        validate_mape_profile,
    )
    from lib.config import MAPE_TARGETS  # type: ignore
    import run_router_bench_v4_iperf3_latency as latency  # type: ignore


_MAPE_LATENCY_DEFAULTS = load_defaults("mape_l4_latency")


def build_latency_targets():
    """Build namespace targets with separate link and MAP-E server addresses."""
    targets = {}
    for name, profile in MAPE_TARGETS.items():
        validate_mape_profile(name, profile)
        target = build_trex_target(profile)
        target["wan_ip"] = profile["server_ipv4"]
        target["wan_iface_ip"] = profile["wan_link_ip"]
        targets[name] = target
    return targets


def main():
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument(
        "--target",
        choices=MAPE_TARGETS,
        default=_MAPE_LATENCY_DEFAULTS["target"],
    )
    parser.add_argument("--out-dir", default="")
    args, _ = parser.parse_known_args()

    targets = build_latency_targets()
    target = targets[args.target]
    profile = MAPE_TARGETS[args.target]
    if not args.out_dir:
        out_dir = (
            latency.DEFAULT_RESULT_ROOT
            / (
                f"mape_iperf3_l4rr_latency_{args.target}_"
                f"{latency.ts_compact()}"
            )
        )
        sys.argv.extend(["--out-dir", str(out_dir)])

    settings = {
        "TARGETS": targets,
        "DEFAULT_TCP_MSS": int(target["tcp_mss"]),
        "SOURCE_PORTS": tuple(
            generate_mape_ports(
                profile["psid"],
                profile["psid_offset"],
                profile["psid_length"],
            )
        ),
        "BENCHMARK_DESCRIPTION": (
            "Run MAP-E latency benchmark using iperf3 plus TCP/UDP "
            "request-response, without TRex."
        ),
        "BENCHMARK_TITLE": (
            "MAP-E latency benchmark using iperf3 + TCP/UDP request-response"
        ),
        "DEFAULT_OUTPUT_PREFIX": "mape_iperf3_l4rr_latency",
        "METADATA_MODE": "mape_iperf3_l4rr_latency",
        "METADATA_NOTES_EXTRA": [
            "The LAN client and WAN IPv4 server use separate Linux network namespaces and communicate through the MAP-E path.",
            "The WAN interface uses the profile wan_link_ip; server_ipv4 is assigned as an additional /32 address and used by the latency and iperf3 servers.",
            f"The selected MAP-E profile sets TCP MSS to {target['tcp_mss']} bytes for TCP request-response and TCP iperf3 load.",
            "LAN client source ports are explicitly selected from the MAP-E profile PSID port set.",
        ],
    }
    previous = {key: getattr(latency, key) for key in settings}
    previous_target = latency._L4_DEFAULTS["target"]
    previous_source_port_index = latency._SOURCE_PORT_INDEX
    try:
        for key, value in settings.items():
            setattr(latency, key, value)
        latency._SOURCE_PORT_INDEX = 0
        latency._L4_DEFAULTS["target"] = _MAPE_LATENCY_DEFAULTS["target"]
        latency.main()
    finally:
        for key, value in previous.items():
            setattr(latency, key, value)
        latency._SOURCE_PORT_INDEX = previous_source_port_index
        latency._L4_DEFAULTS["target"] = previous_target


if __name__ == "__main__":
    main()
