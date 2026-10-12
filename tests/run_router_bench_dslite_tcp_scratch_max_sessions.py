#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DS-Lite TCP maximum concurrent session benchmark without TRex."""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from tests.lib.config import load_defaults
    from tests.lib.dslite import build_trex_target, validate_dslite_profile
    from tests.run_router_bench_v4_tcp_scratch_max_sessions import main as scratch_main
    from tests.lib.config import DSLITE_TARGETS
except Exception:
    from lib.config import load_defaults
    from lib.dslite import build_trex_target, validate_dslite_profile
    from run_router_bench_v4_tcp_scratch_max_sessions import main as scratch_main
    from lib.config import DSLITE_TARGETS


_DEFAULTS = load_defaults("dslite_tcp_scratch")
_TARGETS = {
    name: build_trex_target(validate_dslite_profile(name, profile))
    for name, profile in DSLITE_TARGETS.items()
}


def main():
    return scratch_main(
        targets=_TARGETS,
        default_target=_DEFAULTS["target"],
        parser_defaults=_DEFAULTS,
        benchmark_mode="dslite",
        benchmark_title="DS-Lite TCP scratch maximum session benchmark",
        test_name="dslite_tcp_scratch_max_sessions",
        out_prefix="trex_dslite_tcp_scratch_max_sessions",
        description=(
            "Run the DS-Lite TCP maximum concurrent session benchmark. "
            "Multiple WAN server IPv4 addresses let one internal source port "
            "identify simultaneous connections to different destinations while "
            "VPP AFTR reuses one endpoint-independent mapping."
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
