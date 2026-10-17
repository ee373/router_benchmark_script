"""Shared link-up to test-start waiting helpers."""

import os
import time

from .config import load_defaults as _load_defaults


_COMMON = _load_defaults("common")
DEFAULT_TEST_START_DELAY_SEC = float(_COMMON["test_start_delay_sec"])


def _default_wait_seconds():
    raw = os.environ.get(_COMMON["test_start_wait_env"], str(_COMMON["test_start_wait_sec"]))
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        fallback = float(_COMMON["test_start_wait_sec"])
        print(
            f"WARNING: invalid {_COMMON['test_start_wait_env']}={raw!r}; using {fallback:g} seconds",
            flush=True,
        )
        return fallback


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
