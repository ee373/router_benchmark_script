#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Native IPv6 routing stability entry point for the shared ASTF soak test."""

import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from tests import run_router_bench_v4_astf_nat_stability as benchmark


if __name__ == "__main__":
    benchmark.BENCHMARK_DESCRIPTION = (
        "Run TRex ASTF native IPv6 routing stability soak test."
    )
    benchmark.BENCHMARK_TITLE = (
        "TRex ASTF native IPv6 routing stability benchmark"
    )
    benchmark.DEFAULT_OUTPUT_PREFIX = "trex_astf_ipv6_routing_stability"
    benchmark.METADATA_MODE = "astf_ipv6_routing_stability"
    benchmark.STABILITY_SLUG = "astf_ipv6_routing_stability"
    benchmark.STABILITY_LABEL = "ASTF native IPv6 routing stability"
    benchmark.DEFAULT_TCP_MSS = 1440
    sys.argv.extend(["--ip-version", "v6"])
    benchmark.main()
