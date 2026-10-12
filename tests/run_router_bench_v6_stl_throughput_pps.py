#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Native IPv6 entry point for the shared TRex STL throughput/PPS benchmark."""

import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from tests import run_router_bench_v4_stl_throughput_pps as benchmark


if __name__ == "__main__":
    sys.argv.extend(["--ip-version", "v6"])
    benchmark.main()
