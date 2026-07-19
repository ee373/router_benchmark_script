#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Common startup-settle delay for all router benchmark test programs."""

import time

DEFAULT_TEST_START_DELAY_SEC = 15.0


def wait_before_test_start(delay_sec=DEFAULT_TEST_START_DELAY_SEC, label="benchmark"):
    """Wait after interfaces report link-up and before benchmark traffic starts."""
    delay = float(delay_sec)
    if delay < 0:
        raise ValueError("test start delay must be >= 0")
    if delay == 0:
        print(f"Test start delay  : disabled ({label})", flush=True)
        return 0.0
    print(
        f"Test start delay  : waiting {delay:g}s after link-up before {label}",
        flush=True,
    )
    started = time.monotonic()
    time.sleep(delay)
    elapsed = time.monotonic() - started
    print(f"Test start delay  : completed ({elapsed:.3f}s)", flush=True)
    return elapsed
