#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Basic speed test launcher.
#
# Execution policy:
#   --run-all              : run the standard full set, including native IPv6
#                            routing stability, but excluding IPv4 NAT stability
#                            and DNS stability.
#   no --run-* option      : run the default set, including TCP scratch max-session test
#                            and ASTF short-session CPS, but excluding NAT stability
#                            and DNS stability.
#   one or more --run-*    : run only the specified tests, in TEST_PLAN order.
#   legacy TRex ASTF max-session test runs only through its dedicated selector.
#
# Example:
#   sudo python3 ./test_basic_speed.py --target i226_2p5g --duration 20 --repeat 1
#
# All options are validated before any child benchmark starts. Child-specific
# options are accepted only when at least one selected child script supports them.

import argparse
import datetime as dt
import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
TESTS_DIR = SCRIPT_DIR / "tests"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from tests.lib.config import TARGETS  # type: ignore
from tests.lib.common import DEFAULT_RESULT_ROOT, ts_compact  # type: ignore
from tests.lib.config import load_defaults as _load_defaults  # type: ignore
from tests.lib.repeat import DEFAULT_REPEAT, argparse_repeat  # type: ignore
from tests.lib.output import prepare_details_dir  # type: ignore
from tests.lib.source_ports import (  # type: ignore
    add_lan_source_port_range_argument,
    format_lan_source_port_range,
)


_LAUNCHER_DEFAULTS = _load_defaults("launcher")
_RECOVERY_DEFAULTS = _load_defaults("recovery")
RECOVERY_SCRIPT = TESTS_DIR / "run_router_bench_recovery_check.py"
TEST_PLAN = []
for configured_test in _LAUNCHER_DEFAULTS["test_plan"]:
    test = dict(configured_test)
    test["script"] = TESTS_DIR / test["script"]
    TEST_PLAN.append(test)

RUN_SELECTOR_OPTIONS = {
    "--run-all",
    "--run-stl-throughput",
    "--run-tcp-scratch-max-sessions",
    "--run-astf-max-sessions",
    "--run-astf-sessions-cps",
    "--run-astf-natstability",
    "--run-ipv6-stability",
    "--run-l4-latency",
    "--run-dns-latency",
    "--run-dns-stability",
    "--run-dns-all",
    "--run-dns-load",
    "--run-dhcp-scratch",
    "--run-dhcp-emu",
}

LAUNCHER_OPTIONS = {
    "--help",
    "--target",
    "--out-dir",
    "--dry-run",
    "--repeat",
    "--ip-version",
    "--test-start-delay-sec",
    "--soak-duration-sec",
    "--inter-test-probe-interval-sec",
    "--inter-test-success-consecutive",
    "--lan-source-port-range",
    *RUN_SELECTOR_OPTIONS,
}

_CHILD_LONG_OPTIONS_CACHE = {}

DEPRECATED_LAUNCHER_OPTIONS = {
    "--no-stl",
    "--no-astf",
    "--run-astf-sessions",
    "--run-astf-nat-stability",
    "--run-astf-nat",
    "--run-iperf3-latency",
}


def build_default_out_dir(target):
    return DEFAULT_RESULT_ROOT / f"trex_basic_speed_{target}_{ts_compact()}"


def ensure_dir(path):
    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise SystemExit(
            f"ERROR: cannot create result directory: {path}\n"
            "Run with sudo, create the directory with writable permissions, "
            "or pass --out-dir to a writable path."
        ) from exc


def write_launcher_metadata(parent_out_dir, target, ip_version, enabled_tests, passthrough_args, dry_run, selection_mode, test_start_delay_sec, repeat, inter_test_probe_interval_sec, inter_test_success_consecutive, lan_source_port_range):
    metadata = {
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "target": target,
        "ip_version": ip_version,
        "dry_run": bool(dry_run),
        "selection_mode": selection_mode,
        "repeat": int(repeat),
        "test_start_delay_sec": float(test_start_delay_sec),
        "failure_policy": {
            "continue_after_test_failure": True,
            "continue_after_recovery_failure": True,
            "final_exit_code_nonzero_on_any_failure": True,
        },
        "inter_test_recovery": {
            "probe_interval_sec": float(inter_test_probe_interval_sec),
            "required_consecutive_successes": int(inter_test_success_consecutive),
            "output": "details/inter_test_recovery.csv",
        },
        "lan_source_port_range": format_lan_source_port_range(lan_source_port_range),
        "passthrough_args": list(passthrough_args),
        "tests": [
            {
                "key": item["key"],
                "name": item["name"],
                "script": str(item["script"].relative_to(SCRIPT_DIR)),
                "out_dir": str((parent_out_dir / item["out_dir_name"]).resolve()),
            }
            for item in enabled_tests
        ],
    }
    details_dir = prepare_details_dir(parent_out_dir)
    (details_dir / "launcher_metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def child_long_options(script):
    """Return long options accepted by a child script, based on its --help output."""
    script_key = str(script)
    if script_key in _CHILD_LONG_OPTIONS_CACHE:
        return set(_CHILD_LONG_OPTIONS_CACHE[script_key])
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=str(SCRIPT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except Exception:
        opts = set()
    else:
        opts = set(re.findall(r"(?<![A-Za-z0-9_-])--[A-Za-z0-9][A-Za-z0-9_-]*", proc.stdout or ""))
    _CHILD_LONG_OPTIONS_CACHE[script_key] = set(opts)
    return opts


def filter_passthrough_args(script, passthrough_args):
    """Forward only options understood by the child script.

    This prevents ASTF-only options from failing the STL script, and STL-only
    options from failing the ASTF script when multiple tests are executed.
    Positional values immediately following a removed long option are removed as
    well.
    """
    accepted = child_long_options(script)
    if not accepted:
        return list(passthrough_args)
    filtered = []
    skipped = []
    i = 0
    while i < len(passthrough_args):
        arg = passthrough_args[i]
        if arg.startswith("--"):
            opt = arg.split("=", 1)[0]
            if opt in accepted:
                filtered.append(arg)
            else:
                skipped.append(arg)
                if "=" not in arg and i + 1 < len(passthrough_args) and not passthrough_args[i + 1].startswith("-"):
                    skipped.append(passthrough_args[i + 1])
                    i += 1
        else:
            filtered.append(arg)
        i += 1
    if skipped:
        print(f"[INFO] {script.name}: filtered passthrough args: {' '.join(skipped)}")
    return filtered


def select_tests(args):
    selected_families = {"v4", "v6"} if args.ip_version == "dual" else {args.ip_version}

    def family_enabled(item):
        return bool(selected_families.intersection(item.get("families", ("v4", "v6"))))

    if args.run_all:
        return [
            item for item in TEST_PLAN
            if family_enabled(item) and item.get("run_all", True)
        ], "run_all"

    explicit_keys = {
        item["key"]
        for item in TEST_PLAN
        if bool(getattr(args, item["run_arg"], False))
    }
    if getattr(args, "run_dns_all", False):
        explicit_keys.update({"dns_latency", "dns_stability"})
    if getattr(args, "run_dns_load", False):
        explicit_keys.add("dns_stability")
    if getattr(args, "run_dhcp_scratch", False) or getattr(args, "run_dhcp_emu", False):
        explicit_keys.add("dhcp_scratch")
    if explicit_keys:
        return [
            item for item in TEST_PLAN
            if family_enabled(item) and item["key"] in explicit_keys
        ], "explicit"

    return [
        item for item in TEST_PLAN
        if family_enabled(item) and item.get("default", False)
    ], "default"


def enforce_target_supported_tests(target_name, enabled_tests, selection_mode):
    profile = TARGETS.get(target_name, {})
    supported = profile.get("supported_tests")
    if not supported:
        return
    supported_set = set(supported)
    if "astf_sessions" in supported_set:
        supported_set.update({"tcp_scratch_max_sessions", "astf_max_sessions", "astf_sessions_cps"})
    if "max_sessions" in supported_set:
        supported_set.add("tcp_scratch_max_sessions")
    if "l4_latency" in supported_set:
        supported_set.add("ipv6_l4_latency")
    unsupported = [item for item in enabled_tests if item["key"] not in supported_set]
    if unsupported:
        unsupported_names = ", ".join(item["key"] for item in unsupported)
        supported_opts = []
        for item in TEST_PLAN:
            if item["key"] in supported_set:
                supported_opts.append("--" + item["run_arg"].replace("_", "-"))
        raise SystemExit(
            f"ERROR: target {target_name} supports only {', '.join(sorted(supported_set))}; "
            f"requested/selected unsupported test(s): {unsupported_names}. "
            f"Use {' / '.join(supported_opts) if supported_opts else 'a supported target profile'}."
        )


def _launcher_selector_help():
    return (
        "Use --run-all, no --run-* option, or one or more of "
        "--run-stl-throughput / --run-tcp-scratch-max-sessions / --run-astf-max-sessions / --run-astf-sessions-cps / --run-astf-natstability / --run-ipv6-stability / --run-l4-latency / --run-dns-latency / --run-dns-stability / --run-dns-all / --run-dns-load / --run-dhcp-scratch."
    )


def reject_invalid_launcher_options(passthrough_args):
    deprecated_seen = []
    unknown_run_seen = []

    for arg in passthrough_args:
        if not arg.startswith("--"):
            continue
        opt = arg.split("=", 1)[0]
        if opt in DEPRECATED_LAUNCHER_OPTIONS:
            deprecated_seen.append(opt)
        elif opt.startswith("--run-"):
            unknown_run_seen.append(opt)

    if deprecated_seen:
        unique = ", ".join(sorted(set(deprecated_seen)))
        raise SystemExit(
            "ERROR: deprecated launcher option(s) are no longer accepted: " + unique + "\n"
            + _launcher_selector_help()
        )

    if unknown_run_seen:
        lines = [
            "ERROR: unknown launcher --run-* option(s): " + ", ".join(sorted(set(unknown_run_seen))),
            _launcher_selector_help(),
        ]
        suggestions = []
        for opt in sorted(set(unknown_run_seen)):
            match = difflib.get_close_matches(opt, sorted(RUN_SELECTOR_OPTIONS), n=1, cutoff=0.55)
            if match:
                suggestions.append(f"  {opt} -> {match[0]}")
        if suggestions:
            lines.append("Did you mean:")
            lines.extend(suggestions)
        raise SystemExit("\n".join(lines))


def selected_child_long_options(enabled_tests):
    opts = set()
    for item in enabled_tests:
        script = item.get("script")
        if script:
            opts.update(child_long_options(script))
    # The launcher itself injects these child arguments, so user-supplied copies
    # are accepted by argparse only for the launcher-level --target/--out-dir.
    return opts


def _all_known_long_options():
    opts = set(LAUNCHER_OPTIONS)
    for item in TEST_PLAN:
        script = item.get("script")
        if script:
            opts.update(child_long_options(script))
    opts.update(DEPRECATED_LAUNCHER_OPTIONS)
    return opts


def validate_passthrough_args(passthrough_args, enabled_tests):
    """Reject misspelled or irrelevant passthrough options before a test starts.

    parse_known_args() is intentionally used because many benchmark-specific
    options belong to child scripts.  This function makes that forwarding strict:
    every unknown-to-launcher long option must be accepted by at least one
    selected child script.  Otherwise, the launcher exits before any measurement.
    """
    accepted = selected_child_long_options(enabled_tests)
    known_for_suggestions = _all_known_long_options()
    unknown_options = []
    not_selected_options = []
    stray_values = []
    i = 0
    while i < len(passthrough_args):
        arg = passthrough_args[i]
        if arg.startswith("--"):
            opt = arg.split("=", 1)[0]
            if opt in DEPRECATED_LAUNCHER_OPTIONS or opt.startswith("--run-"):
                # Keep the more specific legacy/selector diagnostics.
                pass
            elif opt not in accepted:
                if opt in known_for_suggestions:
                    not_selected_options.append(opt)
                else:
                    unknown_options.append(opt)
            if "=" not in arg and i + 1 < len(passthrough_args) and not passthrough_args[i + 1].startswith("-"):
                i += 1
        elif arg.startswith("-"):
            unknown_options.append(arg)
        else:
            stray_values.append(arg)
        i += 1

    if not unknown_options and not not_selected_options and not stray_values:
        return

    lines = []
    if unknown_options:
        lines.append("ERROR: unknown option(s): " + ", ".join(sorted(set(unknown_options))))
    if not_selected_options:
        lines.append(
            "ERROR: option(s) not valid for the selected test set: "
            + ", ".join(sorted(set(not_selected_options)))
        )
    if stray_values:
        lines.append("ERROR: stray argument(s) not attached to a valid option: " + ", ".join(stray_values))
    lines.append("No benchmark was started.")
    lines.append(
        "Use only launcher options and options accepted by the selected test(s). "
        "For example, ASTF-only options are rejected when only --run-l4-latency is selected."
    )
    suggestions = []
    for opt in sorted(set(unknown_options)):
        match = difflib.get_close_matches(opt, sorted(known_for_suggestions), n=1, cutoff=0.55)
        if match:
            suggestions.append(f"  {opt} -> {match[0]}")
    if suggestions:
        lines.append("Did you mean:")
        lines.extend(suggestions)
    raise SystemExit("\n".join(lines))


def validate_launcher_option_combination(args, enabled_tests):
    individual_selected = []
    for item in TEST_PLAN:
        selector = "--" + item["run_arg"].replace("_", "-")
        if bool(getattr(args, item["run_arg"], False)) and selector not in individual_selected:
            individual_selected.append(selector)
    if getattr(args, "run_dns_all", False):
        individual_selected.append("--run-dns-all")
    if getattr(args, "run_dns_load", False):
        individual_selected.append("--run-dns-load")
    if getattr(args, "run_dhcp_scratch", False):
        individual_selected.append("--run-dhcp-scratch")
    if args.run_all and individual_selected:
        raise SystemExit(
            "ERROR: --run-all cannot be combined with individual test selectors: "
            + ", ".join(individual_selected)
            + "\nNo benchmark was started."
        )

    enabled_keys = {item["key"] for item in enabled_tests}
    if args.soak_duration_sec is not None and not enabled_keys.intersection({"astf_nat", "ipv6_stability"}):
        raise SystemExit(
            "ERROR: --soak-duration-sec is valid only when a long-duration stability test "
            "is selected.\nNo benchmark was started."
        )


def main():
    parser = argparse.ArgumentParser(
        description="Run the router benchmark test plan.",
        add_help=True,
        allow_abbrev=False,
    )

    parser.add_argument("--target", choices=TARGETS.keys(), default=_LAUNCHER_DEFAULTS["target"],
                        help="Target profile passed to child tests.")
    parser.add_argument("--ip-version", choices=("v4", "v6", "dual"), default="dual",
                        help="Address family to measure. Default: dual (IPv4 and native IPv6).")
    parser.add_argument("--out-dir", default="",
                        help="Parent result directory. Default: /var/trex/trex_basic_speed_<target>_<timestamp>/. A child directory is created for each test.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print commands without executing them.")
    parser.add_argument("--repeat", type=argparse_repeat, default=DEFAULT_REPEAT,
                        help="Repeat each supported benchmark. ASTF NAT stability and DNS stability do not use this option. Default: 1.")
    parser.add_argument("--test-start-delay-sec", type=float, default=_load_defaults("common")["test_start_delay_sec"],
                        help="Seconds each child test waits after link-up before sending benchmark traffic. Default: 15.")
    parser.add_argument("--inter-test-probe-interval-sec", type=float, default=_RECOVERY_DEFAULTS["probe_interval_sec"],
                        help="Seconds between TCP recovery probes before the next selected test. Default: 2.")
    parser.add_argument("--inter-test-success-consecutive", type=int, default=_RECOVERY_DEFAULTS["success_consecutive"],
                        help="Required consecutive successful TCP probes before the next selected test. Default: 3.")
    add_lan_source_port_range_argument(parser)

    selection = parser.add_argument_group("test selection")
    selection.add_argument("--run-all", action="store_true",
                           help="Run the full set. Native IPv6 routing stability is included; IPv4 NAT stability and DNS stability remain excluded.")
    selection.add_argument("--run-stl-throughput", action="store_true",
                           help="Run only/add STL throughput/PPS for the selected address family.")
    selection.add_argument("--run-tcp-scratch-max-sessions", action="store_true",
                           help="Run only/add IPv4 TCP scratch max-session test when any individual --run-* option is used.")
    selection.add_argument("--run-astf-max-sessions", action="store_true",
                           help="Run only/add legacy TRex ASTF max-session test. This is not included in the default set or --run-all.")
    selection.add_argument("--run-astf-sessions-cps", action="store_true",
                           help="Run only/add IPv4 ASTF short-session CPS test. This is included in the default set and --run-all.")
    selection.add_argument("--run-astf-natstability", action="store_true",
                           help="Run only/add IPv4 ASTF NAT stability soak when any individual --run-* option is used.")
    selection.add_argument("--run-ipv6-stability", action="store_true",
                           help="Run only/add native IPv6 routing stability soak.")
    selection.add_argument("--run-l4-latency", action="store_true",
                           help="Run only/add TCP/UDP request-response latency for the selected address family.")
    selection.add_argument("--run-dns-latency", action="store_true",
                           help="Run only/add IPv4 DNS cache-table size, cached response, and uncached response benchmark.")
    selection.add_argument("--run-dns-stability", action="store_true",
                           help="Run only/add IPv4 DNS stability benchmark at a fixed --dns-stability-qps. This is excluded from the default set and --run-all.")
    selection.add_argument("--run-dns-all", action="store_true",
                           help="Run both IPv4 DNS response and DNS stability scripts. This selects only DNS unless combined with other individual selectors.")
    selection.add_argument("--run-dns-load", action="store_true",
                           help="Compatibility alias for --run-dns-stability.")
    selection.add_argument("--run-dhcp-scratch", action="store_true",
                           help="Run only/add IPv4 DHCPv4 basic latency using the scratch raw-socket DHCP client.")
    selection.add_argument("--run-dhcp-emu", action="store_true", help=argparse.SUPPRESS)

    parser.add_argument("--soak-duration-sec", type=float, default=None,
                        help="Long-duration stability test duration in seconds. Default: child script default, 3600 seconds.")

    args, passthrough_args = parser.parse_known_args()
    reject_invalid_launcher_options(passthrough_args)

    if args.test_start_delay_sec < 0:
        raise SystemExit("ERROR: --test-start-delay-sec must be >= 0")
    if args.inter_test_probe_interval_sec < 0:
        raise SystemExit("ERROR: --inter-test-probe-interval-sec must be >= 0")
    if args.inter_test_success_consecutive <= 0:
        raise SystemExit("ERROR: --inter-test-success-consecutive must be positive")

    enabled_tests, selection_mode = select_tests(args)
    if selection_mode == "explicit" and not enabled_tests:
        raise SystemExit(
            f"ERROR: the selected test(s) do not support --ip-version {args.ip_version}.\n"
            "No benchmark was started."
        )
    validate_launcher_option_combination(args, enabled_tests)
    validate_passthrough_args(passthrough_args, enabled_tests)
    enforce_target_supported_tests(args.target, enabled_tests, selection_mode)

    parent_out_dir = Path(args.out_dir) if args.out_dir else build_default_out_dir(args.target)
    if not args.dry_run:
        ensure_dir(parent_out_dir)

    print("============================================================")
    print("Router benchmark launcher")
    print("============================================================")
    print(f"Target         : {args.target}")
    print(f"IP version     : {args.ip_version}")
    print(f"Selection mode : {selection_mode}")
    print(f"Parent out dir : {parent_out_dir}")
    print(f"Start delay    : {args.test_start_delay_sec:g}s after link-up per test")
    print(f"Repeat         : {args.repeat}")
    print(f"LAN source port: {format_lan_source_port_range(args.lan_source_port_range) or '(test default)'}")
    print(f"Passthrough    : {' '.join(passthrough_args) if passthrough_args else '(none)'}")

    if not args.dry_run:
        write_launcher_metadata(
            parent_out_dir=parent_out_dir,
            target=args.target,
            ip_version=args.ip_version,
            enabled_tests=enabled_tests,
            passthrough_args=passthrough_args,
            dry_run=args.dry_run,
            selection_mode=selection_mode,
            test_start_delay_sec=args.test_start_delay_sec,
            repeat=args.repeat,
            inter_test_probe_interval_sec=args.inter_test_probe_interval_sec,
            inter_test_success_consecutive=args.inter_test_success_consecutive,
            lan_source_port_range=args.lan_source_port_range,
        )

    if enabled_tests:
        print("Output layout  :")
        for item in enabled_tests:
            print(f"  - {item['name']}: {parent_out_dir / item['out_dir_name']}")
    else:
        print("Output layout  : no tests enabled")
    if args.dry_run:
        print("Launcher meta  : not written in dry-run mode")
    else:
        print(f"Launcher meta  : {parent_out_dir / 'details' / 'launcher_metadata.json'}")
    print("============================================================")

    failed = []
    previous_item = None

    for item in enabled_tests:
        script = item["script"]
        name = item["name"]
        child_out_dir = parent_out_dir / item["out_dir_name"]

        if not script.exists():
            print(f"[SKIP] {name}: script not found: {script}")
            failed.append((name, "script not found"))
            continue

        if previous_item is not None:
            families = item.get("families", ["v4"])
            recovery_ip_version = "v6" if families == ["v6"] else "v4"
            recovery_cmd = [
                sys.executable,
                str(RECOVERY_SCRIPT),
                "--mode", "basic",
                "--target", args.target,
                "--ip-version", recovery_ip_version,
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
                        f"exit code {recovery_rc}",
                    ))
                    print(
                        f"[WARNING] Inter-test recovery failed with exit code {recovery_rc}; "
                        f"continuing with {item['name']}.",
                        flush=True,
                    )

        child_passthrough_args = filter_passthrough_args(script, passthrough_args)
        if item.get("key") in {"astf_nat", "ipv6_stability"} and args.soak_duration_sec is not None:
            if args.soak_duration_sec <= 0:
                raise SystemExit("ERROR: --soak-duration-sec must be positive")
            child_passthrough_args.extend(["--soak-duration-sec", str(args.soak_duration_sec)])
        cmd = [
            sys.executable,
            str(script),
            "--target", args.target,
            "--out-dir", str(child_out_dir),
            "--test-start-delay-sec", str(args.test_start_delay_sec),
        ]
        if "--repeat" in child_long_options(script):
            cmd.extend(["--repeat", str(args.repeat)])
        if (
            args.lan_source_port_range is not None
            and "--lan-source-port-range" in child_long_options(script)
        ):
            cmd.extend([
                "--lan-source-port-range",
                format_lan_source_port_range(args.lan_source_port_range),
            ])
        cmd.extend(child_passthrough_args)

        print("------------------------------------------------------------")
        print(f"[RUN] {name}")
        print(" ".join(cmd))
        print("------------------------------------------------------------")

        if args.dry_run:
            previous_item = item
            continue

        rc = subprocess.call(cmd, cwd=str(SCRIPT_DIR))
        if rc != 0:
            failed.append((name, f"exit code {rc}"))
            print(
                f"[WARNING] {name} failed with exit code {rc}; "
                "continuing with the remaining selected tests.",
                flush=True,
            )
        previous_item = item

    print("============================================================")
    if failed:
        print("DONE WITH FAILURES")
        for name, reason in failed:
            print(f"- {name}: {reason}")
        print("All selected tests were attempted.")
        print(f"Parent out dir: {parent_out_dir}")
        print("============================================================")
        raise SystemExit(1)

    print("DONE")
    print(f"Parent out dir: {parent_out_dir}")
    print("============================================================")


if __name__ == "__main__":
    main()
