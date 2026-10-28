#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MAP-E wrapper for the shared TCP/UDP NAT stability soak runner."""

import argparse
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from tests.lib.config import MAPE_TARGETS, load_defaults
from tests.lib.mape import build_trex_target, validate_mape_profile
from tests import run_router_bench_v4_astf_nat_stability as stability

_MAPE_STABILITY_DEFAULTS = load_defaults("mape_nat_stability")


def build_stability_targets():
    targets = {}
    for name, profile in MAPE_TARGETS.items():
        validate_mape_profile(name, profile)
        target = build_trex_target(profile)
        target["astf_server_ipv4"] = profile["server_ipv4"]
        targets[name] = target
    return targets


def main():
    # Read only the target here so the selected profile supplies the MSS default.
    # The shared parser retains ownership of all measurement arguments.
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--target", choices=MAPE_TARGETS,
                        default=_MAPE_STABILITY_DEFAULTS["target"])
    args, _ = parser.parse_known_args()
    targets = build_stability_targets()
    settings = {
        "TARGETS": targets,
        "DEFAULT_TCP_MSS": int(targets[args.target]["tcp_mss"]),
        "BENCHMARK_DESCRIPTION": "Run TRex ASTF MAP-E TCP/UDP NAT stability soak test.",
        "BENCHMARK_TITLE": "TRex ASTF MAP-E NAT stability benchmark",
        "DEFAULT_OUTPUT_PREFIX": "trex_mape_astf_nat_stability",
        "METADATA_MODE": "mape_astf_nat_stability",
    }
    previous = {key: getattr(stability, key) for key in settings}
    previous_target = stability._NAT_DEFAULTS["target"]
    try:
        for key, value in settings.items():
            setattr(stability, key, value)
        stability._NAT_DEFAULTS["target"] = _MAPE_STABILITY_DEFAULTS["target"]
        stability.main()
    finally:
        for key, value in previous.items():
            setattr(stability, key, value)
        stability._NAT_DEFAULTS["target"] = previous_target


if __name__ == "__main__":
    main()
