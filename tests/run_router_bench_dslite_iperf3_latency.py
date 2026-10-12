#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DS-Lite TCP/UDP request-response latency benchmark with iperf3 load."""

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
    from tests.lib.dslite import build_trex_target, validate_dslite_profile  # type: ignore
    from tests.lib.config import DSLITE_TARGETS  # type: ignore
    from tests import run_router_bench_v4_iperf3_latency as latency  # type: ignore
except Exception:
    from lib.config import load_defaults  # type: ignore
    from lib.dslite import build_trex_target, validate_dslite_profile  # type: ignore
    from lib.config import DSLITE_TARGETS  # type: ignore
    import run_router_bench_v4_iperf3_latency as latency  # type: ignore


_DSLITE_LATENCY_DEFAULTS = load_defaults("dslite_l4_latency")


def extract_dslite_overrides(argv):
    """Consume DS-Lite profile overrides before invoking the shared runner."""
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--wan-link-ip", default="")
    parser.add_argument("--wan-gateway-ip", default="")
    parser.add_argument("--wan-arp-target", default="")
    parser.add_argument("--server-ip", default="")
    parser.add_argument("--nat-pool-cidr", default="")
    return parser.parse_known_args(argv)


def build_latency_targets(overrides):
    """Validate DS-Lite profiles and add aliases required by the shared runner."""
    if (
        overrides.wan_link_ip
        and overrides.server_ip
        and overrides.wan_link_ip != overrides.server_ip
    ):
        raise SystemExit(
            "ERROR: --wan-link-ip and --server-ip must match for the "
            "namespace-based DS-Lite latency test"
        )

    targets = {}
    for name, base_profile in DSLITE_TARGETS.items():
        profile = dict(base_profile)
        endpoint_ip = overrides.server_ip or overrides.wan_link_ip
        if endpoint_ip:
            profile["wan_link_ip"] = endpoint_ip
            profile["server_ip"] = endpoint_ip
        for argument_name, profile_key in (
            ("wan_gateway_ip", "wan_gateway_ip"),
            ("wan_arp_target", "wan_arp_target"),
            ("nat_pool_cidr", "nat_pool_cidr"),
        ):
            value = getattr(overrides, argument_name)
            if value:
                profile[profile_key] = value
        validate_dslite_profile(name, profile)
        target = build_trex_target(profile)
        target["wan_ip"] = profile["server_ip"]
        targets[name] = target
    return targets


def main():
    overrides, remaining_args = extract_dslite_overrides(sys.argv[1:])

    latency.TARGETS = build_latency_targets(overrides)
    latency._L4_DEFAULTS["target"] = _DSLITE_LATENCY_DEFAULTS["target"]
    latency.BENCHMARK_DESCRIPTION = (
        "Run DS-Lite latency benchmark using iperf3 plus TCP/UDP "
        "request-response, without TRex."
    )
    latency.BENCHMARK_TITLE = (
        "DS-Lite latency benchmark using iperf3 + TCP/UDP request-response"
    )
    latency.DEFAULT_OUTPUT_PREFIX = "dslite_iperf3_l4rr_latency"
    latency.METADATA_MODE = "dslite_iperf3_l4rr_latency"
    latency.METADATA_NOTES_EXTRA = [
        "The LAN client and WAN IPv4 server use separate Linux network namespaces and communicate through the DS-Lite B4 and AFTR path.",
        "WAN addressing and AFTR gateway information are supplied by the selected DS-Lite target profile.",
    ]

    sys.argv = [sys.argv[0], *remaining_args]
    if "-h" in remaining_args or "--help" in remaining_args:
        try:
            latency.main()
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise
        print(
            "\nDS-Lite profile overrides:\n"
            "  --wan-link-ip WAN_LINK_IP\n"
            "  --wan-gateway-ip WAN_GATEWAY_IP\n"
            "  --wan-arp-target WAN_ARP_TARGET\n"
            "  --server-ip SERVER_IP\n"
            "  --nat-pool-cidr NAT_POOL_CIDR"
        )
        return
    latency.main()


if __name__ == "__main__":
    main()
