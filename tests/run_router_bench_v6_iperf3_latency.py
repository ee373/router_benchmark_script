#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Native IPv6 entry point for the shared L4 latency benchmark."""

import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from tests import run_router_bench_v4_iperf3_latency as benchmark


if __name__ == "__main__":
    benchmark.BENCHMARK_DESCRIPTION = (
        "Run native IPv6 routing latency benchmark using iperf3 plus "
        "TCP/UDP request-response."
    )
    benchmark.BENCHMARK_TITLE = (
        "Native IPv6 routing latency benchmark using iperf3 + "
        "TCP/UDP request-response"
    )
    benchmark.DEFAULT_OUTPUT_PREFIX = "ipv6_iperf3_l4rr_latency"
    benchmark.METADATA_MODE = "native_ipv6_iperf3_l4rr_latency"
    sys.argv.extend(["--ip-version", "v6"])
    benchmark.main()
