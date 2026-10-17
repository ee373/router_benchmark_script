"""Shared control for test-specific consecutive preflight checks."""

from collections.abc import Callable
from dataclasses import dataclass
import csv
import datetime as dt
from pathlib import Path
import time
from typing import TypeVar

try:
    from tests.lib.config import load_defaults  # type: ignore
except Exception:
    from lib.config import load_defaults  # type: ignore


_RECOVERY_DEFAULTS = load_defaults("recovery")
_PreflightValue = TypeVar("_PreflightValue")
DEFAULT_PREFLIGHT_SUCCESS_CONSECUTIVE = int(
    _RECOVERY_DEFAULTS["success_consecutive"]
)
DEFAULT_PREFLIGHT_PROBE_INTERVAL_SEC = float(
    _RECOVERY_DEFAULTS["probe_interval_sec"]
)
DEFAULT_PREFLIGHT_TIMEOUT_SEC = float(
    _RECOVERY_DEFAULTS.get("preflight_timeout_sec", 300.0)
)

PREFLIGHT_RECOVERY_FIELDS = [
    "ts_local",
    "phase",
    "attempt",
    "status",
    "consecutive_successes",
    "required_consecutive_successes",
    "elapsed_sec",
    "recovery_complete",
    "detail",
]


class ConsecutivePreflightTimeoutError(RuntimeError):
    """Raised when a test-specific preflight cannot become stable in time."""


@dataclass(frozen=True)
class ConsecutivePreflightResult:
    value: object
    attempts: int
    consecutive_successes: int
    elapsed_sec: float


def run_preflight_with_optional_recovery_probe(
    run_preflight: Callable[[], _PreflightValue],
    wait_for_recovery: Callable[[], _PreflightValue],
    recovery_probe_enabled: bool,
) -> _PreflightValue:
    if recovery_probe_enabled:
        return wait_for_recovery()
    return run_preflight()


def _append_recovery_row(csv_path, row):
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8", buffering=1) as output:
        writer = csv.DictWriter(
            output,
            fieldnames=PREFLIGHT_RECOVERY_FIELDS,
            extrasaction="ignore",
        )
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def run_consecutive_preflight(
    probe,
    *,
    phase,
    csv_path,
    required_successes=DEFAULT_PREFLIGHT_SUCCESS_CONSECUTIVE,
    interval_sec=DEFAULT_PREFLIGHT_PROBE_INTERVAL_SEC,
    timeout_sec=DEFAULT_PREFLIGHT_TIMEOUT_SEC,
):
    """Run a test-specific probe until it succeeds consecutively.

    ``probe`` receives the one-based attempt number and must raise an exception
    when the attempt fails.  A failed attempt resets the consecutive-success
    counter to zero.  Every attempt is appended to ``csv_path``.
    """

    required_successes = int(required_successes)
    interval_sec = float(interval_sec)
    timeout_sec = float(timeout_sec)
    if required_successes <= 0:
        raise ValueError("required_successes must be positive")
    if interval_sec < 0:
        raise ValueError("interval_sec must be >= 0")
    if timeout_sec < 0:
        raise ValueError("timeout_sec must be >= 0")

    started = time.monotonic()
    attempt = 0
    consecutive = 0
    last_error = ""
    while True:
        if attempt > 0 and interval_sec > 0:
            time.sleep(interval_sec)
        attempt += 1
        value = None
        detail = ""
        try:
            value = probe(attempt)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            consecutive = 0
            status = "error"
            detail = f"{type(exc).__name__}: {exc}"
            last_error = detail
        else:
            consecutive += 1
            status = "ok"

        elapsed = time.monotonic() - started
        complete = consecutive >= required_successes
        _append_recovery_row(
            csv_path,
            {
                "ts_local": dt.datetime.now().isoformat(timespec="microseconds"),
                "phase": phase,
                "attempt": attempt,
                "status": status,
                "consecutive_successes": consecutive,
                "required_consecutive_successes": required_successes,
                "elapsed_sec": f"{elapsed:.6f}",
                "recovery_complete": int(complete),
                "detail": detail,
            },
        )
        print(
            f"--- Preflight recovery {phase}: attempt={attempt} "
            f"status={status} consecutive={consecutive}/{required_successes} "
            f"elapsed={elapsed:.3f}s ---",
            flush=True,
        )
        if complete:
            return ConsecutivePreflightResult(
                value=value,
                attempts=attempt,
                consecutive_successes=consecutive,
                elapsed_sec=elapsed,
            )
        if timeout_sec > 0 and elapsed >= timeout_sec:
            raise ConsecutivePreflightTimeoutError(
                f"preflight did not reach {required_successes} consecutive "
                f"successes within {timeout_sec:g}s for {phase}; "
                f"attempts={attempt}, last_error={last_error}"
            )


def add_consecutive_preflight_arguments(parser):
    parser.add_argument(
        "--preflight-success-consecutive",
        type=int,
        default=DEFAULT_PREFLIGHT_SUCCESS_CONSECUTIVE,
        help="Required consecutive successful test-specific preflights. Default: 3.",
    )
    parser.add_argument(
        "--preflight-probe-interval-sec",
        type=float,
        default=DEFAULT_PREFLIGHT_PROBE_INTERVAL_SEC,
        help="Seconds between test-specific preflight attempts. Default: 2.",
    )
    parser.add_argument(
        "--preflight-timeout-sec",
        type=float,
        default=DEFAULT_PREFLIGHT_TIMEOUT_SEC,
        help="Maximum recovery wait. Zero disables the timeout. Default: 300.",
    )


def validate_consecutive_preflight_arguments(args):
    if int(args.preflight_success_consecutive) <= 0:
        raise SystemExit("ERROR: --preflight-success-consecutive must be positive")
    if float(args.preflight_probe_interval_sec) < 0:
        raise SystemExit("ERROR: --preflight-probe-interval-sec must be >= 0")
    if float(args.preflight_timeout_sec) < 0:
        raise SystemExit("ERROR: --preflight-timeout-sec must be >= 0")
