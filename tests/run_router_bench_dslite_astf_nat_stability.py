#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TRex ASTF DS-Lite TCP/UDP NAT stability soak benchmark."""

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
    from tests import run_router_bench_v4_astf_nat_stability as stability  # type: ignore
except Exception:
    from lib.config import load_defaults  # type: ignore
    from lib.dslite import build_trex_target, validate_dslite_profile  # type: ignore
    from lib.config import DSLITE_TARGETS  # type: ignore
    import run_router_bench_v4_astf_nat_stability as stability  # type: ignore


_DSLITE_STABILITY_DEFAULTS = load_defaults("dslite_nat_stability")


def extract_dslite_overrides(argv):
    """Consume DS-Lite profile overrides before invoking the shared runner."""
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--wan-link-ip", default="")
    parser.add_argument("--wan-gateway-ip", default="")
    parser.add_argument("--wan-arp-target", default="")
    parser.add_argument("--server-ip", default="")
    parser.add_argument("--nat-pool-cidr", default="")
    return parser.parse_known_args(argv)


def build_stability_targets(overrides):
    """Validate DS-Lite profiles and add aliases required by shared ASTF code."""
    targets = {}
    for name, base_profile in DSLITE_TARGETS.items():
        profile = dict(base_profile)
        for argument_name, profile_key in (
            ("wan_link_ip", "wan_link_ip"),
            ("wan_gateway_ip", "wan_gateway_ip"),
            ("wan_arp_target", "wan_arp_target"),
            ("server_ip", "server_ip"),
            ("nat_pool_cidr", "nat_pool_cidr"),
        ):
            value = getattr(overrides, argument_name)
            if value:
                profile[profile_key] = value
        validate_dslite_profile(name, profile)
        targets[name] = build_trex_target(profile)
    return targets


def main():
    overrides, remaining_args = extract_dslite_overrides(sys.argv[1:])

    stability.TARGETS = build_stability_targets(overrides)
    stability._NAT_DEFAULTS["target"] = _DSLITE_STABILITY_DEFAULTS["target"]
    stability.DEFAULT_TCP_MSS = int(_DSLITE_STABILITY_DEFAULTS["tcp_mss"])
    stability.BENCHMARK_DESCRIPTION = (
        "Run TRex ASTF DS-Lite TCP/UDP NAT stability soak test."
    )
    stability.BENCHMARK_TITLE = "TRex ASTF DS-Lite NAT stability benchmark"
    stability.DEFAULT_OUTPUT_PREFIX = "trex_dslite_astf_nat_stability"
    stability.METADATA_MODE = "dslite_astf_nat_stability"

    sys.argv = [sys.argv[0], *remaining_args]
    if "-h" in remaining_args or "--help" in remaining_args:
        try:
            stability.main()
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
    stability.main()


if __name__ == "__main__":
    main()
