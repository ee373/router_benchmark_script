#!/usr/bin/env python3
"""IPv4 router DNS stability benchmark.

The measurement engine is shared with run_router_bench_v4_dns_latency.py.
This entry point selects the delayed cache-table recheck and the fixed-QPS
sustained DNS load.
"""

try:
    from tests.run_router_bench_v4_dns_latency import main
except ImportError:
    from run_router_bench_v4_dns_latency import main


if __name__ == "__main__":
    main("stability")
