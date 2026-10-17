#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DS-Lite benchmark launcher."""

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
TESTS_DIR = SCRIPT_DIR / "tests"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from tests.lib.common import DEFAULT_RESULT_ROOT, ts_compact  # type: ignore
from tests.lib.config import load_defaults  # type: ignore
from tests.lib.repeat import DEFAULT_REPEAT, argparse_repeat  # type: ignore
from tests.lib.launcher import (  # type: ignore
    build_default_output_dir,
    filter_child_passthrough_args,
    launcher_test_entries,
    parse_child_long_options,
    validate_child_passthrough_args,
    write_launcher_metadata_file,
)
from tests.lib.config import DSLITE_TARGETS  # type: ignore
from tests.lib.source_ports import (  # type: ignore
    add_lan_source_port_range_argument,
    format_lan_source_port_range,
)


_LAUNCHER_DEFAULTS = load_defaults("dslite_launcher")
_RECOVERY_DEFAULTS = load_defaults("recovery")
RECOVERY_SCRIPT = TESTS_DIR / "run_router_bench_recovery_check.py"
TEST_PLAN = []
for configured_test in _LAUNCHER_DEFAULTS["test_plan"]:
    test = dict(configured_test)
    test["script"] = TESTS_DIR / test["script"]
    TEST_PLAN.append(test)


def build_default_out_dir(target):
    return build_default_output_dir(
        DEFAULT_RESULT_ROOT,
        "trex_dslite_speed",
        target,
        ts_compact(),
    )


def child_long_options(script):
    proc = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=str(SCRIPT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return parse_child_long_options(proc.stdout)


def validate_passthrough_args(passthrough_args, enabled_tests):
    accepted = set()
    for item in enabled_tests:
        accepted.update(child_long_options(item["script"]))
    validate_child_passthrough_args(passthrough_args, accepted)


def filter_passthrough_args(script, passthrough_args):
    accepted = child_long_options(script)
    return filter_child_passthrough_args(passthrough_args, accepted)


def write_launcher_metadata(out_dir, args, enabled_tests, passthrough_args, selection_mode):
    metadata = {
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "benchmark_mode": "dslite",
        "target": args.target,
        "selection_mode": selection_mode,
        "repeat": int(args.repeat),
        "test_start_delay_sec": float(args.test_start_delay_sec),
        "failure_policy": {
            "continue_after_test_failure": True,
            "continue_after_recovery_failure": True,
            "final_exit_code_nonzero_on_any_failure": True,
        },
        "inter_test_recovery": {
            "probe_interval_sec": float(args.inter_test_probe_interval_sec),
            "required_consecutive_successes": int(args.inter_test_success_consecutive),
            "output": "details/inter_test_recovery.csv",
        },
        "stl_recovery_probe": bool(args.stl_recovery_probe),
        "lan_source_port_range": format_lan_source_port_range(args.lan_source_port_range),
        "passthrough_args": list(passthrough_args),
        "tests": launcher_test_entries(out_dir, enabled_tests, SCRIPT_DIR),
    }
    write_launcher_metadata_file(out_dir, metadata)


def main():
    parser = argparse.ArgumentParser(
        description="Run the DS-Lite router benchmark test plan.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--target",
        choices=DSLITE_TARGETS.keys(),
        default=_LAUNCHER_DEFAULTS["target"],
        help="DS-Lite target profile passed to child tests.",
    )
    parser.add_argument(
        "--out-dir",
        default="",
        help="Parent result directory. Default: /var/trex/trex_dslite_speed_<target>_<timestamp>/.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them.")
    parser.add_argument(
        "--repeat",
        type=argparse_repeat,
        default=DEFAULT_REPEAT,
        help="Repeat each supported benchmark. DS-Lite ASTF NAT stability does not use this option. Default: 1.",
    )
    parser.add_argument(
        "--test-start-delay-sec",
        type=float,
        default=load_defaults("common")["test_start_delay_sec"],
        help="Seconds to wait after TRex link-up. Default: 15.",
    )
    parser.add_argument(
        "--inter-test-probe-interval-sec",
        type=float,
        default=_RECOVERY_DEFAULTS["probe_interval_sec"],
        help="Seconds between TCP recovery probes before the next selected test. Default: 2.",
    )
    parser.add_argument(
        "--inter-test-success-consecutive",
        type=int,
        default=_RECOVERY_DEFAULTS["success_consecutive"],
        help="Required consecutive successful TCP probes before the next selected test. Default: 3.",
    )
    parser.add_argument(
        "--stl-recovery-probe",
        action="store_true",
        help="Enable consecutive recovery probes before each STL measurement. Disabled by default; one preflight still runs.",
    )
    add_lan_source_port_range_argument(parser)
    selection = parser.add_argument_group("test selection")
    selection.add_argument(
        "--run-all",
        action="store_true",
        help="Run every implemented DS-Lite benchmark.",
    )
    selection.add_argument(
        "--run-stl-throughput",
        action="store_true",
        help="Run DS-Lite IPv4 throughput and small-packet PPS.",
    )
    selection.add_argument(
        "--run-astf-sessions-cps",
        action="store_true",
        help="Run DS-Lite ASTF short-session TCP CPS.",
    )
    selection.add_argument(
        "--run-tcp-scratch-max-sessions",
        action="store_true",
        help="Run DS-Lite TCP scratch maximum concurrent sessions.",
    )
    selection.add_argument(
        "--run-astf-natstability",
        action="store_true",
        help="Run DS-Lite ASTF TCP/UDP NAT stability soak.",
    )
    selection.add_argument(
        "--run-l4-latency",
        action="store_true",
        help="Run DS-Lite TCP/UDP request-response latency under iperf3 load.",
    )
    selection.add_argument(
        "--run-dslite-source-port-scan",
        action="store_true",
        help=(
            "Run the special DS-Lite TCP source-port scan. "
            "This test is excluded from default selection and --run-all."
        ),
    )
    args, passthrough_args = parser.parse_known_args()

    individual_selected = any(
        bool(getattr(args, item["run_arg"], False))
        for item in TEST_PLAN
    )
    if args.run_all and individual_selected:
        raise SystemExit(
            "ERROR: --run-all cannot be combined with an individual --run-* selector.\n"
            "No benchmark was started."
        )
    if args.test_start_delay_sec < 0:
        raise SystemExit("ERROR: --test-start-delay-sec must be >= 0")
    if args.inter_test_probe_interval_sec < 0:
        raise SystemExit("ERROR: --inter-test-probe-interval-sec must be >= 0")
    if args.inter_test_success_consecutive <= 0:
        raise SystemExit("ERROR: --inter-test-success-consecutive must be positive")

    if args.run_all:
        selection_mode = "run_all"
        enabled_tests = [
            item for item in TEST_PLAN
            if bool(item.get("run_all", True))
        ]
    elif individual_selected:
        selection_mode = "explicit"
        enabled_tests = [
            item
            for item in TEST_PLAN
            if bool(getattr(args, item["run_arg"], False))
        ]
    else:
        selection_mode = "default"
        enabled_tests = [
            item
            for item in TEST_PLAN
            if bool(item.get("default", False))
        ]
    validate_passthrough_args(passthrough_args, enabled_tests)

    parent_out_dir = Path(args.out_dir) if args.out_dir else build_default_out_dir(args.target)
    if not args.dry_run:
        parent_out_dir.mkdir(parents=True, exist_ok=True)
        write_launcher_metadata(parent_out_dir, args, enabled_tests, passthrough_args, selection_mode)

    print("============================================================")
    print("DS-Lite router benchmark launcher")
    print("============================================================")
    print(f"Target         : {args.target}")
    print(f"Selection mode : {selection_mode}")
    print(f"Parent out dir : {parent_out_dir}")
    print(f"Start delay    : {args.test_start_delay_sec:g}s after link-up")
    print(f"Repeat         : {args.repeat}")
    print(f"LAN source port: {format_lan_source_port_range(args.lan_source_port_range) or '(test default)'}")
    print("============================================================")

    failed = []
    previous_item = None
    for item in enabled_tests:
        child_out_dir = parent_out_dir / item["out_dir_name"]
        if previous_item is not None:
            recovery_cmd = [
                sys.executable,
                str(RECOVERY_SCRIPT),
                "--mode", "dslite",
                "--target", args.target,
                "--ip-version", "v4",
                "--out-dir", str(parent_out_dir / "details"),
                "--previous-test", previous_item["name"],
                "--next-test", item["name"],
                "--probe-interval-sec", str(args.inter_test_probe_interval_sec),
                "--success-consecutive", str(args.inter_test_success_consecutive),
            ]
            if args.lan_source_port_range is not None:
                recovery_cmd.extend([
                    "--lan-source-port-range",
                    format_lan_source_port_range(args.lan_source_port_range),
                ])
            print("------------------------------------------------------------")
            print(f"[RECOVERY] {previous_item['name']} -> {item['name']}")
            print(" ".join(recovery_cmd))
            print("------------------------------------------------------------")
            if not args.dry_run:
                recovery_rc = subprocess.call(recovery_cmd, cwd=str(SCRIPT_DIR))
                if recovery_rc != 0:
                    failed.append((
                        f"inter-test recovery: {previous_item['name']} -> {item['name']}",
                        recovery_rc,
                    ))
                    print(
                        f"[WARNING] Inter-test recovery failed with exit code {recovery_rc}; "
                        f"continuing with {item['name']}.",
                        flush=True,
                    )
        cmd = [
            sys.executable,
            str(item["script"]),
            "--target",
            args.target,
            "--out-dir",
            str(child_out_dir),
            "--test-start-delay-sec",
            str(args.test_start_delay_sec),
        ]
        if "--repeat" in child_long_options(item["script"]):
            cmd.extend(["--repeat", str(args.repeat)])
        if (
            args.lan_source_port_range is not None
            and "--lan-source-port-range" in child_long_options(item["script"])
        ):
            cmd.extend([
                "--lan-source-port-range",
                format_lan_source_port_range(args.lan_source_port_range),
            ])
        if (
            args.stl_recovery_probe
            and "--stl-recovery-probe" in child_long_options(item["script"])
        ):
            cmd.append("--stl-recovery-probe")
        cmd.extend(filter_passthrough_args(item["script"], passthrough_args))
        print("------------------------------------------------------------")
        print(f"[RUN] {item['name']}")
        print(" ".join(cmd))
        print("------------------------------------------------------------")
        if args.dry_run:
            previous_item = item
            continue
        return_code = subprocess.call(cmd, cwd=str(SCRIPT_DIR))
        if return_code != 0:
            failed.append((item["name"], return_code))
            print(
                f"[WARNING] {item['name']} failed with exit code {return_code}; "
                "continuing with the remaining selected tests.",
                flush=True,
            )
        previous_item = item

    print("============================================================")
    if failed:
        print("DONE WITH FAILURES")
        for name, return_code in failed:
            print(f"- {name}: exit code {return_code}")
        print("All selected tests were attempted.")
        print(f"Parent out dir: {parent_out_dir}")
        print("============================================================")
        raise SystemExit(1)
    print("DONE")
    print(f"Parent out dir: {parent_out_dir}")
    print("============================================================")


if __name__ == "__main__":
    main()
