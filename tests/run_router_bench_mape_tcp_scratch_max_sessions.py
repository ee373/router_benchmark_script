#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MAP-E TCP maximum concurrent session benchmark without TRex."""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from tests.lib.config import MAPE_TARGETS, load_defaults
from tests.lib.mape import build_trex_target, generate_mape_ports, validate_mape_profile
from tests.run_router_bench_v4_tcp_scratch_max_sessions import main as scratch_main


_DEFAULTS = load_defaults("mape_tcp_scratch")
_TARGETS = {
    name: build_trex_target(validate_mape_profile(name, profile))
    for name, profile in MAPE_TARGETS.items()
}


def source_ports_for_target(target_name):
    profile = MAPE_TARGETS[target_name]
    return generate_mape_ports(
        profile["psid"],
        profile["psid_offset"],
        profile["psid_length"],
    )


def main():
    return scratch_main(
        targets=_TARGETS,
        default_target=_DEFAULTS["target"],
        parser_defaults=_DEFAULTS,
        benchmark_mode="mape",
        benchmark_title="MAP-E TCP scratch maximum session benchmark",
        test_name="mape_tcp_scratch_max_sessions",
        out_prefix=_DEFAULTS["out_prefix"],
        description=(
            "Run the MAP-E TCP maximum concurrent session benchmark using "
            "the target profile's PSID source-port set and server IPv4 network."
        ),
        source_ports=source_ports_for_target,
    )


if __name__ == "__main__":
    raise SystemExit(main())
