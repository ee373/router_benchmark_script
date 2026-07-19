#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Common post-link stabilization delay for router benchmark invocations."""

import os
import time

def _default_wait_seconds():
    value = os.environ.get("ROUTERBENCH_TEST_START_WAIT_SEC", "15")
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        print(
            f"WARNING: invalid ROUTERBENCH_TEST_START_WAIT_SEC={value!r}; using 15 seconds",
            flush=True,
        )
        return 15.0


DEFAULT_TEST_START_WAIT_SEC = _default_wait_seconds()


def wait_before_test(test_name, seconds=None):
    """Wait once after interfaces report link-up and before benchmark traffic begins."""
    delay = DEFAULT_TEST_START_WAIT_SEC if seconds is None else float(seconds)
    if delay <= 0:
        return 0.0
    label = str(test_name or "unnamed_test")
    print(
        f"--- Test start stabilization wait: test={label} duration={delay:g}s ---",
        flush=True,
    )
    time.sleep(delay)
    return delay
