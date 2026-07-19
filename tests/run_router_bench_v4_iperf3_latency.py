#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPv4 NAT latency benchmark using iperf3 plus TCP/UDP request-response, without TRex.

Purpose:
  - Measure idle application-layer RTT using TCP or UDP request-response.
  - Measure application-layer RTT at 0%, 10%, 20%, 30%, 40%, 50%, 60%, 70%, 80%, 90%, 95%, and 99% of target line rate.
  - The 0% phase is a no-load baseline; the remaining phases use iperf3 load through the DUT.

Recommended single-host topology:
  - One Linux host has two physical NICs connected to the router/DUT.
  - The LAN NIC is moved into a LAN network namespace.
  - The WAN NIC is moved into a WAN network namespace.
  - The latency client and iperf3 client execute in the LAN namespace.
  - The latency echo server and iperf3 server execute in the WAN namespace.
  - By default, the script creates and removes these namespaces for each run.
  - When TRex/DPDK has detached the NICs from Linux, the script can rebind
    profile PCI devices back to their kernel drivers before namespace setup.

Why namespaces are used:
  When both LAN and WAN addresses are configured in the same Linux network
  namespace, Linux may deliver packets locally and bypass the router/DUT.
  Separate namespaces force packets to leave the LAN NIC, traverse the DUT,
  and return through the WAN NIC.

Tools used:
  - iperf3: TCP/UDP load generation
  - built-in Python socket request-response helper: TCP/UDP RTT samples
  - ip from iproute2: network namespace setup, enabled by default
"""

import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
DEFAULT_RESULT_ROOT = Path(os.environ.get("TREX_RESULT_ROOT", "/var/trex"))
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from tests.target_profiles import TARGETS  # type: ignore
except Exception:
    from target_profiles import TARGETS  # type: ignore

try:
    from tests.routerbench_test_start_delay import (  # type: ignore
        DEFAULT_TEST_START_DELAY_SEC,
        wait_before_test_start,
    )
except Exception:
    from routerbench_test_start_delay import (  # type: ignore
        DEFAULT_TEST_START_DELAY_SEC,
        wait_before_test_start,
    )

try:
    from tests.routerbench_arp_refresh import NamespaceArpRefresher, DEFAULT_ARP_REFRESH_INTERVAL_SEC  # type: ignore
except Exception:
    from routerbench_arp_refresh import NamespaceArpRefresher, DEFAULT_ARP_REFRESH_INTERVAL_SEC  # type: ignore


try:
    from tests.run_router_bench_v4_stl_throughput_pps import prepare_trex_interfaces_from_profile  # type: ignore
except Exception:
    try:
        from run_router_bench_v4_stl_throughput_pps import prepare_trex_interfaces_from_profile  # type: ignore
    except Exception:
        prepare_trex_interfaces_from_profile = None  # type: ignore


DEFAULT_LATENCY_DURATION_SEC = 30.0
DEFAULT_LATENCY_INTERVAL_SEC = 0.2
DEFAULT_LATENCY_PAYLOAD_SIZE_BYTES = 64
DEFAULT_LATENCY_TIMEOUT_SEC = 1.0
DEFAULT_LATENCY_CONNECT_TIMEOUT_SEC = 2.0
DEFAULT_LOAD_WARMUP_SEC = 3.0
DEFAULT_LOAD_COOLDOWN_SEC = 1.0
DEFAULT_POST_NAMESPACE_SETTLE_SEC = 3.0
DEFAULT_INTERFACE_READY_TIMEOUT_SEC = 10.0
DEFAULT_L4RR_PREFLIGHT_TIMEOUT_SEC = 12.0
DEFAULT_L4RR_PREFLIGHT_DURATION_SEC = 0.6
DEFAULT_L4RR_PREFLIGHT_INTERVAL_SEC = 0.1
DEFAULT_LOAD_PERCENTAGES = (0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 95.0, 99.0)
DEFAULT_IPERF3_PORT = 5201
DEFAULT_LATENCY_PORT = 5501
DEFAULT_LAN_NS = "rb_lan"
DEFAULT_WAN_NS = "rb_wan"
DEFAULT_LAN_PREFIX = 24
DEFAULT_WAN_PREFIX = 24
L4RR_HEADER = struct.Struct("!Qd")

SAMPLE_CSV_FIELDS = [
    "phase",
    "sample_index",
    "seq",
    "latency_protocol",
    "unix_ts_start",
    "unix_ts_end",
    "rtt_ms",
    "payload_bytes",
    "reply_bytes",
    "result",
    "error",
]

SUMMARY_FIELDS = [
    "phase",
    "test_name",
    "result",
    "tool_latency",
    "tool_load",
    "target_name",
    "lan_ns",
    "wan_ns",
    "lan_iface",
    "wan_iface",
    "lan_ip",
    "lan_gw",
    "wan_ip",
    "wan_gw",
    "router_wan_ip",
    "latency_protocol",
    "latency_port",
    "latency_interval_sec",
    "latency_payload_size_bytes",
    "latency_timeout_sec",
    "load_protocol",
    "load_direction",
    "load_rate",
    "load_percent",
    "computed_load_bps",
    "iperf3_parallel",
    "duration_sec",
    "samples_requested",
    "samples_received",
    "timeout_count",
    "error_count",
    "request_loss_pct",
    "rtt_min_ms",
    "rtt_avg_ms",
    "rtt_p50_ms",
    "rtt_p90_ms",
    "rtt_p95_ms",
    "rtt_p99_ms",
    "rtt_max_ms",
    "rtt_mdev_ms",
    "iperf3_bits_per_second",
    "iperf3_retransmits",
    "iperf3_jitter_ms",
    "iperf3_lost_packets",
    "iperf3_packets",
    "iperf3_lost_percent",
    "error",
]


class CommandError(RuntimeError):
    pass


def ts_compact():
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def ts_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


def parse_positive_float(value):
    try:
        f = float(str(value).replace("_", ""))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a number") from exc
    if f <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return f


def parse_non_negative_float(value):
    try:
        f = float(str(value).replace("_", ""))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a number") from exc
    if f < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return f


def parse_positive_int(value):
    try:
        i = int(str(value).replace("_", ""))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if i <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return i


def parse_load_rate(value):
    text = str(value).strip()
    if not text:
        raise argparse.ArgumentTypeError("load rate must not be empty")
    return text


def parse_load_percentages(value):
    text = str(value).strip()
    if not text:
        raise argparse.ArgumentTypeError("load percentages must not be empty")
    percentages = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            pct = float(item.replace("_", ""))
        except ValueError as exc:
            raise argparse.ArgumentTypeError("load percentages must be comma-separated numbers") from exc
        if pct < 0 or pct >= 100:
            raise argparse.ArgumentTypeError("load percentages must be greater than or equal to 0 and less than 100")
        percentages.append(pct)
    if not percentages:
        raise argparse.ArgumentTypeError("load percentages must contain at least one value")
    return percentages


def percent_label(percent):
    pct = float(percent)
    if pct.is_integer():
        return str(int(pct))
    return (f"{pct:g}").replace(".", "_")


def phase_for_load_percent(percent):
    pct = float(percent)
    if math.isclose(pct, 0.0, abs_tol=1e-12):
        return "idle"
    return f"load_{percent_label(percent)}pct"


def split_load_percentages(percentages):
    idle_requested = False
    loaded = []
    seen = set()
    for percent in percentages:
        pct = float(percent)
        key = percent_label(pct)
        if key in seen:
            continue
        seen.add(key)
        if math.isclose(pct, 0.0, abs_tol=1e-12):
            idle_requested = True
        else:
            loaded.append(pct)
    return idle_requested, loaded


def compute_load_bps(target, percent):
    line_rate_gbps = float(target.get("line_rate_gbps") or 0.0)
    if line_rate_gbps <= 0:
        raise SystemExit("ERROR: target profile must define positive line_rate_gbps for percentage-based iperf3 load")
    return int(round(line_rate_gbps * 1_000_000_000 * float(percent) / 100.0))


def effective_load_rate(args, target, percent):
    if args.load_rate and args.load_rate.lower() != "auto":
        return args.load_rate, ""
    bps = compute_load_bps(target, percent)
    return str(bps), bps


def require_root():
    if os.geteuid() != 0:
        raise SystemExit("ERROR: run with sudo. This script may create network namespaces and assign interfaces.")


def require_command(name):
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"ERROR: required command not found: {name}")
    return path


def run_cmd(cmd, *, check=True, capture=True, text=True, timeout=None):
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=text,
        check=False,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        raise CommandError(
            "command failed: " + " ".join(shlex.quote(str(x)) for x in cmd) +
            f"\nexit_code={proc.returncode}\nstdout={stdout}\nstderr={stderr}"
        )
    return proc


def ns_prefix(ns_name):
    if not ns_name or ns_name == "root":
        return []
    return ["ip", "netns", "exec", ns_name]


def ns_cmd(ns_name, command):
    return ns_prefix(ns_name) + list(command)


def namespace_exists(ns_name):
    if not ns_name or ns_name == "root":
        return True
    proc = run_cmd(["ip", "netns", "list"], check=False)
    names = []
    for line in (proc.stdout or "").splitlines():
        names.append(line.split()[0])
    return ns_name in names


def merge_target_with_overrides(base_target, args):
    target = dict(base_target)
    if args.lan_ip:
        target["lan_ip"] = args.lan_ip
    if args.lan_gw:
        target["lan_gw"] = args.lan_gw
    if args.wan_ip:
        target["wan_ip"] = args.wan_ip
    if args.wan_gw:
        target["wan_gw"] = args.wan_gw
        target["router_wan_ip"] = args.wan_gw
    if args.router_wan_ip:
        target["router_wan_ip"] = args.router_wan_ip
    return target


def normalize_pci_bdf(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", value):
        return value.lower()
    if re.fullmatch(r"[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", value):
        return f"0000:{value.lower()}"
    return ""



def pci_device_path(bdf):
    bdf = normalize_pci_bdf(bdf)
    if not bdf:
        return None
    return Path("/sys/bus/pci/devices") / bdf


def pci_current_driver(bdf):
    """Return the currently bound PCI driver name, or an empty string.

    Path.resolve() without strict checking can return a synthetic path ending in
    "driver" when the sysfs driver symlink is absent.  That bogus value caused
    later code to write to .../driver/unbind and fail.  Use the symlink target
    only when sysfs actually exposes a driver link.
    """
    dev_path = pci_device_path(bdf)
    if not dev_path:
        return ""
    driver_link = dev_path / "driver"
    try:
        if not driver_link.exists() and not driver_link.is_symlink():
            return ""
        return Path(os.readlink(driver_link)).name
    except OSError:
        try:
            return driver_link.resolve(strict=True).name
        except OSError:
            return ""


def pci_unbind_current_driver(bdf, current_driver, log_writer=None, required=False):
    bdf = normalize_pci_bdf(bdf)
    current_driver = str(current_driver or "").strip()
    if not bdf or not current_driver:
        if log_writer:
            log_writer.writerow([ts_iso(), "pci_unbind_skip_no_current_driver", bdf])
        return False
    candidates = [
        pci_device_path(bdf) / "driver" / "unbind",
        Path("/sys/bus/pci/drivers") / current_driver / "unbind",
    ]
    last_exc = None
    for unbind_path in candidates:
        if not unbind_path.exists():
            continue
        try:
            sysfs_write(unbind_path, bdf)
            if log_writer:
                log_writer.writerow([ts_iso(), "pci_unbind", f"{bdf} from {current_driver} via {unbind_path}"])
            return True
        except OSError as exc:
            last_exc = exc
    msg = f"{bdf} from {current_driver}: no usable unbind path"
    if last_exc is not None:
        msg = f"{msg}; last_error={last_exc}"
    if log_writer:
        log_writer.writerow([ts_iso(), "pci_unbind_warning", msg])
    if required:
        raise SystemExit(f"ERROR: failed to unbind {bdf} from {current_driver}: {msg}")
    return False


def sysfs_write(path, value):
    path = Path(path)
    with path.open("w", encoding="utf-8") as f:
        f.write(str(value))


def pci_netdev_names(bdf):
    bdf = normalize_pci_bdf(bdf)
    if not bdf:
        return []
    net_dir = Path("/sys/bus/pci/devices") / bdf / "net"
    try:
        return sorted(item.name for item in net_dir.iterdir() if item.is_dir())
    except FileNotFoundError:
        return []
    except PermissionError:
        return []


def wait_for_pci_netdev(bdf, timeout_sec=8.0):
    end = time.time() + float(timeout_sec)
    while time.time() < end:
        names = pci_netdev_names(bdf)
        if names:
            return names
        time.sleep(0.2)
    return pci_netdev_names(bdf)


def profile_port_index(target, side):
    port_key = f"{side}_port"
    try:
        return int(target[port_key])
    except (KeyError, TypeError, ValueError):
        return None


def profile_kernel_driver_entry(target, side):
    for key in (f"{side}_kernel_driver", f"{side}_driver"):
        if target.get(key):
            return str(target[key])
    drivers = target.get("kernel_drivers") or target.get("drivers") or []
    idx = profile_port_index(target, side)
    if idx is not None:
        try:
            return str(drivers[idx])
        except (IndexError, TypeError):
            pass
    if target.get("kernel_driver"):
        return str(target["kernel_driver"])
    return ""


def bind_pci_to_kernel_driver_if_needed(bdf, driver, log_writer):
    bdf = normalize_pci_bdf(bdf)
    if not bdf:
        return []
    dev_path = pci_device_path(bdf)
    if not dev_path or not dev_path.exists():
        raise SystemExit(f"ERROR: PCI device does not exist: {bdf}")

    existing_names = pci_netdev_names(bdf)
    current_driver = pci_current_driver(bdf)
    if existing_names:
        log_writer.writerow([ts_iso(), "kernel_netdev_ready", f"{bdf} driver={current_driver or '(none)'} netdev={','.join(existing_names)}"])
        return existing_names

    if not driver:
        raise SystemExit(
            f"ERROR: PCI device {bdf} does not expose a Linux netdev, and no kernel driver is defined. "
            "Set kernel_drivers in tests/target_profiles.py or specify --lan-iface/--wan-iface after preparing the interfaces."
        )

    log_writer.writerow([ts_iso(), "kernel_bind_start", f"{bdf} current_driver={current_driver or '(none)'} target_driver={driver}"])
    run_cmd(["modprobe", driver], check=False)

    override_path = dev_path / "driver_override"
    if override_path.exists():
        try:
            sysfs_write(override_path, driver)
            log_writer.writerow([ts_iso(), "driver_override_set", f"{bdf}->{driver}"])
        except OSError as exc:
            raise SystemExit(f"ERROR: failed to set driver_override for {bdf}: {exc}") from exc

    current_driver = pci_current_driver(bdf)
    if current_driver and current_driver != driver:
        pci_unbind_current_driver(bdf, current_driver, log_writer=log_writer, required=False)
    elif current_driver == driver:
        log_writer.writerow([ts_iso(), "pci_driver_already_bound_without_netdev", f"{bdf} driver={driver}"])
    else:
        log_writer.writerow([ts_iso(), "pci_unbound", bdf])

    bind_path = Path("/sys/bus/pci/drivers") / driver / "bind"
    if not bind_path.exists():
        raise SystemExit(f"ERROR: kernel driver bind path does not exist: {bind_path}. modprobe {driver} may have failed.")
    try:
        sysfs_write(bind_path, bdf)
        log_writer.writerow([ts_iso(), "pci_bind", f"{bdf}->{driver}"])
    except OSError as exc:
        # Binding to the same driver may return EBUSY on some systems; continue if a netdev appears.
        log_writer.writerow([ts_iso(), "pci_bind_warning", f"{bdf}->{driver}: {exc}"])

    names = wait_for_pci_netdev(bdf)
    if override_path.exists():
        try:
            sysfs_write(override_path, "")
            log_writer.writerow([ts_iso(), "driver_override_clear", bdf])
        except OSError:
            pass

    if not names:
        raise SystemExit(
            f"ERROR: PCI device {bdf} was bound to {driver}, but no Linux netdev appeared under /sys/bus/pci/devices/{bdf}/net/."
        )
    log_writer.writerow([ts_iso(), "kernel_netdev_ready", f"{bdf} driver={pci_current_driver(bdf) or '(none)'} netdev={','.join(names)}"])
    return names


def delete_namespace_if_exists(ns_name, log_writer, reason="delete_existing_namespace"):
    if not ns_name or ns_name == "root":
        return
    if namespace_exists(ns_name):
        log_writer.writerow([ts_iso(), reason, ns_name])
        run_cmd(["ip", "netns", "delete", ns_name], check=False)
        # When a namespace is deleted, physical interfaces inside it return to the init namespace.
        time.sleep(0.5)


def delete_existing_namespaces(args, log_writer):
    for ns_name in (args.lan_ns, args.wan_ns):
        delete_namespace_if_exists(ns_name, log_writer)


def profile_pci_interface_entry(target, side):
    """Return the PCI BDF profile entry for a LAN/WAN side.

    Linux netdev names may be declared separately as lan_iface/wan_iface.  Kernel
    rebinding still needs the PCI BDF, so this function intentionally ignores
    those netdev-name fields and reads the TRex-style interfaces/lan_port/wan_port
    mapping instead.
    """
    for key in (
        f"{side}_pci_bdf",
        f"{side}_bdf",
        f"{side}_pci",
    ):
        if target.get(key):
            return target[key]

    port_key = f"{side}_port"
    interfaces = target.get("interfaces") or []
    if port_key in target:
        try:
            return interfaces[int(target[port_key])]
        except (IndexError, TypeError, ValueError):
            return ""
    return ""


def prepare_kernel_interfaces_from_profile(args, target, log_writer):
    if not getattr(args, "auto_kernel_bind", True):
        return
    for side in ("lan", "wan"):
        entry = profile_pci_interface_entry(target, side)
        bdf = normalize_pci_bdf(entry)
        if not bdf:
            log_writer.writerow([ts_iso(), "kernel_bind_skip_no_pci_profile_entry", f"{side}={entry or '(not defined)'}"])
            continue
        driver = profile_kernel_driver_entry(target, side)
        bind_pci_to_kernel_driver_if_needed(bdf, driver, log_writer)

def root_iface_exists(iface):
    iface = str(iface or "").strip()
    if not iface:
        return False
    proc = run_cmd(["ip", "-o", "link", "show", "dev", iface], check=False)
    return proc.returncode == 0


def namespace_iface_exists(ns_name, iface):
    ns_name = str(ns_name or "").strip()
    iface = str(iface or "").strip()
    if not ns_name or not iface or not namespace_exists(ns_name):
        return False
    proc = run_cmd(["ip", "-n", ns_name, "-o", "link", "show", "dev", iface], check=False)
    return proc.returncode == 0


def direct_profile_iface_entry(target, side):
    direct_key_candidates = (
        f"{side}_iface",
        f"{side}_interface",
        f"{side}_linux_iface",
        f"{side}_linux_interface",
    )
    for key in direct_key_candidates:
        value = str(target.get(key) or "").strip()
        if value:
            return value
    return ""


def profile_interface_entry(target, side):
    direct = direct_profile_iface_entry(target, side)
    if direct:
        return direct

    port_key = f"{side}_port"
    interfaces = target.get("interfaces") or []
    if port_key in target:
        try:
            return interfaces[int(target[port_key])]
        except (IndexError, TypeError, ValueError):
            return ""
    return ""


def pci_netdev_names_for_profile_side(target, side):
    entry = profile_pci_interface_entry(target, side)
    bdf = normalize_pci_bdf(entry)
    if not bdf:
        return []
    net_dir = Path("/sys/bus/pci/devices") / bdf / "net"
    try:
        return sorted(item.name for item in net_dir.iterdir() if item.is_dir())
    except (FileNotFoundError, PermissionError):
        return []


def resolve_linux_iface_from_profile_value(value):
    """Resolve a PCI BDF profile value to a root-namespace Linux netdev name.

    This function is used only as a fallback when the profile does not declare
    lan_iface/wan_iface.  It never returns a netdev name that is not visible in
    the init namespace, because such a value fails later at ``ip link set ...
    netns``.
    """
    value = str(value or "").strip()
    if not value:
        return ""

    if root_iface_exists(value):
        return value

    bdf = normalize_pci_bdf(value)
    if not bdf:
        return ""

    net_dir = Path("/sys/bus/pci/devices") / bdf / "net"
    try:
        names = sorted(item.name for item in net_dir.iterdir() if item.is_dir())
    except FileNotFoundError:
        return ""
    except PermissionError:
        return ""

    visible_names = [name for name in names if root_iface_exists(name)]
    if len(visible_names) == 1:
        return visible_names[0]
    if len(visible_names) > 1:
        raise SystemExit(
            f"ERROR: PCI device {bdf} exposes multiple root-namespace Linux interfaces: {', '.join(visible_names)}. "
            "Specify lan_iface/wan_iface in tests/target_profiles.py."
        )
    return ""


def resolve_side_interface(args, target, side, log_writer):
    arg_name = f"{side}_iface"
    cli_iface = str(getattr(args, arg_name) or "").strip()
    if cli_iface:
        log_writer.writerow([ts_iso(), "iface_resolution_cli", f"{side}={cli_iface}"])
        return cli_iface

    desired = direct_profile_iface_entry(target, side)
    if desired:
        if root_iface_exists(desired):
            log_writer.writerow([ts_iso(), "iface_resolution_profile", f"{side}={desired}"])
            return desired

        # After TRex/DPDK, the NIC may return with a kernel-generated name.  If
        # the BDF mapping identifies exactly one root-namespace netdev, rename it
        # to the profile-declared name.  This keeps L4 tests deterministic and
        # prevents accidental use of values such as eth0 discovered through sysfs.
        pci_names = pci_netdev_names_for_profile_side(target, side)
        root_names = [name for name in pci_names if root_iface_exists(name)]
        if len(root_names) == 1:
            actual = root_names[0]
            if actual != desired:
                if root_iface_exists(desired):
                    raise SystemExit(
                        f"ERROR: cannot rename {side} interface {actual} to {desired}; {desired} already exists."
                    )
                log_writer.writerow([ts_iso(), "rename_iface_to_profile_name", f"{side}: {actual}->{desired}"])
                run_cmd(["ip", "link", "set", "dev", actual, "name", desired])
            if root_iface_exists(desired):
                return desired

        if len(root_names) > 1:
            raise SystemExit(
                f"ERROR: {side.upper()} PCI mapping exposes multiple root-namespace interfaces: {', '.join(root_names)}. "
                f"Profile-declared interface {desired} was not found."
            )

        known_ns = []
        for ns_name in (getattr(args, "lan_ns", ""), getattr(args, "wan_ns", "")):
            if namespace_iface_exists(ns_name, desired):
                known_ns.append(ns_name)
        ns_hint = f" It exists in namespace(s): {', '.join(known_ns)}." if known_ns else ""
        raise SystemExit(
            f"ERROR: profile-declared {side.upper()} interface was not found in the root namespace: {desired}."
            f" PCI netdev candidates={','.join(pci_names) or '(none)'} root-visible={','.join(root_names) or '(none)'}+.".replace("+.", ".")
            + ns_hint
            + " Confirm tests/target_profiles.py lan_iface/wan_iface or return the interface to the root namespace before L4 latency."
        )

    entry = profile_interface_entry(target, side)
    iface = resolve_linux_iface_from_profile_value(entry)
    if iface:
        log_writer.writerow([ts_iso(), "iface_resolution_pci_fallback", f"{side}: {entry}->{iface}"])
        return iface
    return ""


def resolve_profile_interfaces(args, target, log_writer=None):
    """Fill missing --lan-iface/--wan-iface from the selected target profile.

    Explicit CLI values always win.  Otherwise, lan_iface/wan_iface declared in
    the profile win.  PCI BDF auto-resolution is used only when the profile does
    not declare a Linux interface name.
    """
    if log_writer is None:
        log_writer = csv.writer(open(os.devnull, "w", newline=""))
    resolved = {}
    for side in ("lan", "wan"):
        arg_name = f"{side}_iface"
        iface = resolve_side_interface(args, target, side, log_writer)
        setattr(args, arg_name, iface)
        resolved[side] = iface
    return resolved


def require_profile_interfaces(args, target):
    missing = []
    for side in ("lan", "wan"):
        iface = getattr(args, f"{side}_iface")
        if not iface:
            entry = profile_interface_entry(target, side)
            missing.append(f"{side.upper()} profile entry={entry or '(not defined)'}")
        elif args.setup_namespaces and not root_iface_exists(iface):
            missing.append(f"{side.upper()} root namespace interface not found={iface}")
    if missing:
        raise SystemExit(
            "ERROR: --setup-namespaces could not resolve usable Linux interface names from the target profile. "
            + "; ".join(missing)
            + ". Update tests/target_profiles.py lan_iface/wan_iface, or specify --lan-iface/--wan-iface explicitly."
        )


def setup_namespaces(args, target, log_writer):
    if args.lan_ns == "root" or args.wan_ns == "root":
        raise SystemExit("ERROR: --setup-namespaces cannot be used with --lan-ns root or --wan-ns root")
    if not args.lan_iface or not args.wan_iface:
        raise SystemExit("ERROR: --setup-namespaces requires resolved LAN/WAN Linux interfaces. They are normally auto-resolved from --target; otherwise specify --lan-iface/--wan-iface.")
    if args.lan_iface == args.wan_iface:
        raise SystemExit("ERROR: --lan-iface and --wan-iface must be different")
    for ns_name in (args.lan_ns, args.wan_ns):
        if namespace_exists(ns_name):
            if not args.replace_namespaces:
                raise SystemExit(
                    f"ERROR: namespace already exists: {ns_name}. "
                    "Delete it, use a different --lan-ns/--wan-ns, or pass --replace-namespaces."
                )
            delete_namespace_if_exists(ns_name, log_writer)

    for ns_name in (args.lan_ns, args.wan_ns):
        log_writer.writerow([ts_iso(), "add_namespace", ns_name])
        run_cmd(["ip", "netns", "add", ns_name])
        run_cmd(["ip", "-n", ns_name, "link", "set", "lo", "up"])

    log_writer.writerow([ts_iso(), "move_iface_to_namespace", f"{args.lan_iface}->{args.lan_ns}"])
    run_cmd(["ip", "link", "set", args.lan_iface, "netns", args.lan_ns])
    log_writer.writerow([ts_iso(), "move_iface_to_namespace", f"{args.wan_iface}->{args.wan_ns}"])
    run_cmd(["ip", "link", "set", args.wan_iface, "netns", args.wan_ns])

    run_cmd(["ip", "-n", args.lan_ns, "addr", "flush", "dev", args.lan_iface])
    run_cmd(["ip", "-n", args.wan_ns, "addr", "flush", "dev", args.wan_iface])
    run_cmd(["ip", "-n", args.lan_ns, "addr", "add", f"{target['lan_ip']}/{args.lan_prefix}", "dev", args.lan_iface])
    run_cmd(["ip", "-n", args.wan_ns, "addr", "add", f"{target['wan_ip']}/{args.wan_prefix}", "dev", args.wan_iface])
    run_cmd(["ip", "-n", args.lan_ns, "link", "set", "dev", args.lan_iface, "up"])
    run_cmd(["ip", "-n", args.wan_ns, "link", "set", "dev", args.wan_iface, "up"])

    run_cmd(["ip", "-n", args.lan_ns, "route", "replace", "default", "via", target["lan_gw"], "dev", args.lan_iface])
    run_cmd(["ip", "-n", args.wan_ns, "route", "replace", "default", "via", target["router_wan_ip"], "dev", args.wan_iface])

    log_writer.writerow([ts_iso(), "lan_address", f"{target['lan_ip']}/{args.lan_prefix} gw {target['lan_gw']}"])
    log_writer.writerow([ts_iso(), "wan_address", f"{target['wan_ip']}/{args.wan_prefix} gw {target['router_wan_ip']}"])


def cleanup_namespaces(args, log_writer):
    if not args.setup_namespaces or args.keep_namespaces:
        return
    if args.lan_iface and namespace_exists(args.lan_ns):
        log_writer.writerow([ts_iso(), "return_iface_to_root", f"{args.lan_ns}:{args.lan_iface}"])
        run_cmd(["ip", "-n", args.lan_ns, "link", "set", args.lan_iface, "netns", "1"], check=False)
    if args.wan_iface and namespace_exists(args.wan_ns):
        log_writer.writerow([ts_iso(), "return_iface_to_root", f"{args.wan_ns}:{args.wan_iface}"])
        run_cmd(["ip", "-n", args.wan_ns, "link", "set", args.wan_iface, "netns", "1"], check=False)
    for ns_name in (args.lan_ns, args.wan_ns):
        if namespace_exists(ns_name):
            log_writer.writerow([ts_iso(), "delete_namespace", ns_name])
            run_cmd(["ip", "netns", "delete", ns_name], check=False)


def verify_namespaces(args):
    for ns_name in (args.lan_ns, args.wan_ns):
        if not namespace_exists(ns_name):
            raise SystemExit(
                f"ERROR: namespace does not exist: {ns_name}. "
                "Pass --setup-namespaces so this script can configure namespaces, or pre-create the namespace."
            )


def wait_namespace_interface_ready(ns_name, iface, timeout_sec):
    if not iface or ns_name == "root":
        return
    deadline = time.monotonic() + float(timeout_sec)
    last = ""
    while time.monotonic() < deadline:
        proc = run_cmd(["ip", "-n", ns_name, "-o", "link", "show", "dev", iface], check=False)
        last = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode == 0 and ("LOWER_UP" in last or "state UP" in last):
            return
        time.sleep(0.2)
    raise RuntimeError(f"interface did not become ready: ns={ns_name} iface={iface} last={last}")


def wait_test_interfaces_ready(args):
    if not args.wait_interfaces_ready:
        return
    wait_namespace_interface_ready(args.lan_ns, args.lan_iface, args.interface_ready_timeout_sec)
    wait_namespace_interface_ready(args.wan_ns, args.wan_iface, args.interface_ready_timeout_sec)


def make_namespace_arp_refresher(args, target, out_dir):
    endpoints = [
        {
            "namespace": args.lan_ns,
            "iface": args.lan_iface,
            "src_ip": target["lan_ip"],
            "target_ip": target["lan_gw"],
        },
        {
            "namespace": args.wan_ns,
            "iface": args.wan_iface,
            "src_ip": target["wan_ip"],
            "target_ip": target["router_wan_ip"],
        },
    ]
    return NamespaceArpRefresher(
        endpoints=endpoints,
        log_path=Path(out_dir) / "arp_refresh.csv",
        interval_sec=DEFAULT_ARP_REFRESH_INTERVAL_SEC,
    )


def percentile(values, pct):
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return ""
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * (float(pct) / 100.0)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def mean(values):
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else ""


def mdev(values):
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return ""
    avg = sum(vals) / len(vals)
    return math.sqrt(sum((v - avg) ** 2 for v in vals) / len(vals))


def make_payload(seq, payload_size):
    if payload_size < L4RR_HEADER.size:
        raise ValueError(f"payload size must be >= {L4RR_HEADER.size}")
    header = L4RR_HEADER.pack(int(seq), time.time())
    pad_len = payload_size - len(header)
    return header + (b"x" * pad_len)


def extract_seq(payload):
    if len(payload) < L4RR_HEADER.size:
        return None
    try:
        return L4RR_HEADER.unpack(payload[:L4RR_HEADER.size])[0]
    except Exception:
        return None


def recv_exact(sock_obj, size):
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = sock_obj.recv(remaining)
        if not chunk:
            raise ConnectionError("peer closed TCP connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def l4rr_tcp_server(bind_ip, port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind_ip, int(port)))
    srv.listen(32)
    srv.settimeout(0.5)
    print(f"l4rr TCP server listening on {bind_ip}:{port}", file=sys.stderr, flush=True)
    while True:
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        except KeyboardInterrupt:
            break
        conn.settimeout(1.0)
        with conn:
            print(f"accepted TCP client {addr}", file=sys.stderr, flush=True)
            while True:
                try:
                    data = conn.recv(65535)
                    if not data:
                        break
                    conn.sendall(data)
                except socket.timeout:
                    continue
                except (ConnectionError, OSError):
                    break
    srv.close()


def l4rr_udp_server(bind_ip, port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind_ip, int(port)))
    srv.settimeout(0.5)
    print(f"l4rr UDP server listening on {bind_ip}:{port}", file=sys.stderr, flush=True)
    while True:
        try:
            data, addr = srv.recvfrom(65535)
        except socket.timeout:
            continue
        except KeyboardInterrupt:
            break
        except OSError:
            break
        if data:
            try:
                srv.sendto(data, addr)
            except OSError as exc:
                print(f"UDP sendto failed for {addr}: {exc}", file=sys.stderr, flush=True)
    srv.close()


def connect_tcp_with_retry(server_ip, port, source_ip, connect_timeout_sec, retry_interval_sec=0.1):
    deadline = time.monotonic() + float(connect_timeout_sec)
    last_exc = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if last_exc is not None:
                raise TimeoutError(f"TCP connect retry timeout: {last_exc}")
            raise TimeoutError("TCP connect retry timeout")
        sock_obj = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if source_ip:
                sock_obj.bind((source_ip, 0))
            sock_obj.settimeout(min(0.5, max(0.05, remaining)))
            sock_obj.connect((server_ip, int(port)))
            return sock_obj
        except OSError as exc:
            last_exc = exc
            try:
                sock_obj.close()
            except Exception:
                pass
            time.sleep(min(float(retry_interval_sec), max(0.0, deadline - time.monotonic())))


def l4rr_client_tcp(server_ip, port, source_ip, duration_sec, interval_sec, payload_size, timeout_sec, connect_timeout_sec):
    samples = []
    requested = int(math.ceil(float(duration_sec) / float(interval_sec)))
    sock_obj = connect_tcp_with_retry(server_ip, port, source_ip, connect_timeout_sec)
    sock_obj.settimeout(float(timeout_sec))
    try:
        for seq in range(1, requested + 1):
            payload = make_payload(seq, payload_size)
            start_wall = time.time()
            start_perf = time.perf_counter()
            row = {
                "sample_index": seq,
                "seq": seq,
                "unix_ts_start": start_wall,
                "unix_ts_end": "",
                "rtt_ms": "",
                "payload_bytes": payload_size,
                "reply_bytes": 0,
                "result": "FAIL",
                "error": "",
            }
            try:
                sock_obj.sendall(payload)
                reply = recv_exact(sock_obj, payload_size)
                end_perf = time.perf_counter()
                row["unix_ts_end"] = time.time()
                row["reply_bytes"] = len(reply)
                if extract_seq(reply) != seq:
                    row["error"] = "unexpected TCP reply sequence"
                else:
                    row["rtt_ms"] = (end_perf - start_perf) * 1000.0
                    row["result"] = "PASS"
            except socket.timeout:
                row["unix_ts_end"] = time.time()
                row["error"] = "timeout"
            except Exception as exc:
                row["unix_ts_end"] = time.time()
                row["error"] = str(exc)
            samples.append(row)
            sleep_until = start_wall + float(interval_sec)
            delay = sleep_until - time.time()
            if delay > 0:
                time.sleep(delay)
    finally:
        sock_obj.close()
    return samples, requested


def l4rr_client_udp(server_ip, port, source_ip, duration_sec, interval_sec, payload_size, timeout_sec):
    samples = []
    requested = int(math.ceil(float(duration_sec) / float(interval_sec)))
    sock_obj = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if source_ip:
        sock_obj.bind((source_ip, 0))
    sock_obj.settimeout(float(timeout_sec))
    try:
        for seq in range(1, requested + 1):
            payload = make_payload(seq, payload_size)
            start_wall = time.time()
            start_perf = time.perf_counter()
            deadline = start_perf + float(timeout_sec)
            row = {
                "sample_index": seq,
                "seq": seq,
                "unix_ts_start": start_wall,
                "unix_ts_end": "",
                "rtt_ms": "",
                "payload_bytes": payload_size,
                "reply_bytes": 0,
                "result": "FAIL",
                "error": "",
            }
            try:
                sock_obj.sendto(payload, (server_ip, int(port)))
                while True:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        raise socket.timeout()
                    sock_obj.settimeout(remaining)
                    reply, _addr = sock_obj.recvfrom(max(65535, payload_size))
                    if extract_seq(reply) != seq:
                        continue
                    end_perf = time.perf_counter()
                    row["unix_ts_end"] = time.time()
                    row["reply_bytes"] = len(reply)
                    row["rtt_ms"] = (end_perf - start_perf) * 1000.0
                    row["result"] = "PASS"
                    break
            except socket.timeout:
                row["unix_ts_end"] = time.time()
                row["error"] = "timeout"
            except Exception as exc:
                row["unix_ts_end"] = time.time()
                row["error"] = str(exc)
            samples.append(row)
            sleep_until = start_wall + float(interval_sec)
            delay = sleep_until - time.time()
            if delay > 0:
                time.sleep(delay)
    finally:
        sock_obj.close()
    return samples, requested


def run_internal_l4rr_server(argv):
    parser = argparse.ArgumentParser(description="Internal TCP/UDP echo server for router benchmark.")
    parser.add_argument("--protocol", choices=["tcp", "udp"], required=True)
    parser.add_argument("--bind-ip", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    if args.protocol == "tcp":
        l4rr_tcp_server(args.bind_ip, args.port)
    else:
        l4rr_udp_server(args.bind_ip, args.port)


def run_internal_l4rr_client(argv):
    parser = argparse.ArgumentParser(description="Internal TCP/UDP request-response client for router benchmark.")
    parser.add_argument("--protocol", choices=["tcp", "udp"], required=True)
    parser.add_argument("--server-ip", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--source-ip", default="")
    parser.add_argument("--duration-sec", type=float, required=True)
    parser.add_argument("--interval-sec", type=float, required=True)
    parser.add_argument("--payload-size-bytes", type=int, required=True)
    parser.add_argument("--timeout-sec", type=float, required=True)
    parser.add_argument("--connect-timeout-sec", type=float, default=DEFAULT_LATENCY_CONNECT_TIMEOUT_SEC)
    args = parser.parse_args(argv)
    started = time.time()
    try:
        if args.protocol == "tcp":
            samples, requested = l4rr_client_tcp(
                args.server_ip, args.port, args.source_ip, args.duration_sec,
                args.interval_sec, args.payload_size_bytes, args.timeout_sec,
                args.connect_timeout_sec,
            )
        else:
            samples, requested = l4rr_client_udp(
                args.server_ip, args.port, args.source_ip, args.duration_sec,
                args.interval_sec, args.payload_size_bytes, args.timeout_sec,
            )
        result = "PASS" if any(sample.get("result") == "PASS" for sample in samples) else "FAIL"
        output = {
            "result": result,
            "protocol": args.protocol,
            "server_ip": args.server_ip,
            "source_ip": args.source_ip,
            "port": args.port,
            "duration_sec_requested": args.duration_sec,
            "duration_sec_elapsed": time.time() - started,
            "interval_sec": args.interval_sec,
            "payload_size_bytes": args.payload_size_bytes,
            "timeout_sec": args.timeout_sec,
            "samples_requested": requested,
            "samples": samples,
        }
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))
        return 0 if result == "PASS" else 1
    except Exception as exc:
        output = {
            "result": "FAIL",
            "protocol": args.protocol,
            "server_ip": args.server_ip,
            "source_ip": args.source_ip,
            "port": args.port,
            "duration_sec_requested": args.duration_sec,
            "duration_sec_elapsed": time.time() - started,
            "interval_sec": args.interval_sec,
            "payload_size_bytes": args.payload_size_bytes,
            "timeout_sec": args.timeout_sec,
            "samples_requested": int(math.ceil(float(args.duration_sec) / float(args.interval_sec))),
            "samples": [],
            "error": str(exc),
        }
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))
        return 1


def wait_for_process_exit(proc, timeout_sec, name):
    try:
        return proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGINT)
        try:
            return proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.communicate(timeout=5)


def start_l4rr_server(args, target, out_dir, phase):
    server_stdout_path = out_dir / f"l4rr_server_{phase}.stdout.log"
    server_stderr_path = out_dir / f"l4rr_server_{phase}.stderr.log"
    cmd = ns_cmd(args.wan_ns, [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_l4rr-server",
        "--protocol", args.latency_protocol,
        "--bind-ip", target["wan_ip"],
        "--port", str(args.latency_port),
    ])
    (out_dir / f"l4rr_server_{phase}.cmd.txt").write_text(
        " ".join(shlex.quote(str(x)) for x in cmd) + "\n",
        encoding="utf-8",
    )
    stdout = open(server_stdout_path, "w", encoding="utf-8")
    stderr = open(server_stderr_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, text=True)
    return proc, stdout, stderr


def build_l4rr_client_command(args, target, duration_sec=None, interval_sec=None):
    if duration_sec is None:
        duration_sec = args.latency_duration_sec
    if interval_sec is None:
        interval_sec = args.latency_interval_sec
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_l4rr-client",
        "--protocol", args.latency_protocol,
        "--server-ip", target["wan_ip"],
        "--port", str(args.latency_port),
        "--source-ip", target["lan_ip"],
        "--duration-sec", str(duration_sec),
        "--interval-sec", str(interval_sec),
        "--payload-size-bytes", str(args.latency_payload_size_bytes),
        "--timeout-sec", str(args.latency_timeout_sec),
        "--connect-timeout-sec", str(args.latency_connect_timeout_sec),
    ]
    return ns_cmd(args.lan_ns, cmd)


def parse_l4rr_json(path):
    if not path.exists() or path.stat().st_size == 0:
        return {"samples": [], "samples_requested": 0, "error": "empty l4rr client output"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"samples": [], "samples_requested": 0, "error": f"failed to parse l4rr JSON: {exc}"}


def ensure_process_running(proc, name):
    if proc.poll() is not None:
        raise RuntimeError(f"{name} exited before the measurement client started; exit_code={proc.returncode}")


def l4rr_client_result_ok(json_path):
    data = parse_l4rr_json(json_path)
    samples = data.get("samples", []) if isinstance(data, dict) else []
    if not isinstance(samples, list):
        return False
    return any(sample.get("result") == "PASS" for sample in samples)


def run_l4rr_preflight(args, target, out_dir, phase):
    if not args.l4rr_preflight:
        return
    deadline = time.monotonic() + float(args.l4rr_preflight_timeout_sec)
    attempt = 0
    last_error = ""
    while time.monotonic() < deadline:
        attempt += 1
        preflight_json = out_dir / f"l4rr_preflight_{phase}_attempt{attempt}.json"
        preflight_stderr = out_dir / f"l4rr_preflight_{phase}_attempt{attempt}.stderr.log"
        cmd = build_l4rr_client_command(
            args,
            target,
            duration_sec=args.l4rr_preflight_duration_sec,
            interval_sec=args.l4rr_preflight_interval_sec,
        )
        (out_dir / f"l4rr_preflight_{phase}_attempt{attempt}.cmd.txt").write_text(
            " ".join(shlex.quote(str(x)) for x in cmd) + "\n",
            encoding="utf-8",
        )
        with open(preflight_json, "w", encoding="utf-8") as out_f, open(preflight_stderr, "w", encoding="utf-8") as err_f:
            proc = subprocess.Popen(cmd, stdout=out_f, stderr=err_f, text=True)
            try:
                wait_for_process_exit(
                    proc,
                    args.l4rr_preflight_duration_sec + args.latency_timeout_sec + args.latency_connect_timeout_sec + 5.0,
                    "l4rr preflight client",
                )
            except Exception as exc:
                last_error = str(exc)
        if l4rr_client_result_ok(preflight_json):
            return
        data = parse_l4rr_json(preflight_json)
        if isinstance(data, dict):
            last_error = str(data.get("error") or data.get("result") or "preflight failed")
        time.sleep(0.2)
    message = (
        f"L4 request-response preflight failed before phase {phase}. "
        f"protocol={args.latency_protocol} server={target['wan_ip']}:{args.latency_port} "
        f"source={target['lan_ip']} last_error={last_error}"
    )
    if args.require_l4rr_preflight:
        raise RuntimeError(message)
    print(f"WARNING: {message}", file=sys.stderr, flush=True)


def run_l4rr_phase(phase, args, target, out_dir, *, tool_load="none", load_percent="", computed_load_bps=""):
    server_proc, server_stdout, server_stderr = start_l4rr_server(args, target, out_dir, phase)
    client_json = out_dir / f"l4rr_client_{phase}.json"
    client_stderr = out_dir / f"l4rr_client_{phase}.stderr.log"
    client_cmd = build_l4rr_client_command(args, target)
    (out_dir / f"l4rr_client_{phase}.cmd.txt").write_text(
        " ".join(shlex.quote(str(x)) for x in client_cmd) + "\n",
        encoding="utf-8",
    )
    proc = None
    try:
        time.sleep(args.latency_server_start_delay_sec)
        ensure_process_running(server_proc, "l4rr server")
        run_l4rr_preflight(args, target, out_dir, phase)
        with open(client_json, "w", encoding="utf-8") as out_f, open(client_stderr, "w", encoding="utf-8") as err_f:
            proc = subprocess.Popen(client_cmd, stdout=out_f, stderr=err_f, text=True)
            wait_for_process_exit(proc, args.latency_duration_sec + args.latency_timeout_sec + args.latency_connect_timeout_sec + 10.0, "l4rr client")
    finally:
        if server_proc.poll() is None:
            server_proc.terminate()
            wait_for_process_exit(server_proc, 5.0, "l4rr server")
        for handle in (server_stdout, server_stderr):
            try:
                handle.close()
            except Exception:
                pass

    data = parse_l4rr_json(client_json)
    samples = data.get("samples", []) if isinstance(data, dict) else []
    if not isinstance(samples, list):
        samples = []
    sample_csv = out_dir / f"latency_{phase}_samples.csv"
    with open(sample_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SAMPLE_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for sample in samples:
            row = dict(sample)
            row.update({"phase": phase, "latency_protocol": args.latency_protocol})
            writer.writerow(row)

    requested = int(data.get("samples_requested") or int(math.ceil(args.latency_duration_sec / args.latency_interval_sec))) if isinstance(data, dict) else 0
    ok_samples = [sample for sample in samples if sample.get("result") == "PASS" and sample.get("rtt_ms") not in ("", None)]
    timeout_count = sum(1 for sample in samples if sample.get("error") == "timeout")
    error_count = sum(1 for sample in samples if sample.get("result") != "PASS" and sample.get("error") != "timeout")
    rtts = [float(sample["rtt_ms"]) for sample in ok_samples]
    request_loss_pct = ((requested - len(ok_samples)) / requested * 100.0) if requested else ""
    error_text = ""
    if isinstance(data, dict) and data.get("error"):
        error_text = str(data.get("error"))
    if proc and proc.returncode not in (0, None) and ok_samples:
        error_text = (error_text + "; " if error_text else "") + f"l4rr client returned exit code {proc.returncode}"

    summary = {
        "phase": phase,
        "test_name": f"iperf3_l4rr_latency_{phase}",
        "result": "PASS" if ok_samples else "FAIL",
        "tool_latency": f"built-in Python socket {args.latency_protocol.upper()} request-response",
        "tool_load": tool_load,
        "target_name": args.target,
        "lan_ns": args.lan_ns,
        "wan_ns": args.wan_ns,
        "lan_iface": args.lan_iface,
        "wan_iface": args.wan_iface,
        "lan_ip": target["lan_ip"],
        "lan_gw": target["lan_gw"],
        "wan_ip": target["wan_ip"],
        "wan_gw": target["wan_gw"],
        "router_wan_ip": target["router_wan_ip"],
        "latency_protocol": args.latency_protocol,
        "latency_port": args.latency_port,
        "latency_interval_sec": args.latency_interval_sec,
        "latency_payload_size_bytes": args.latency_payload_size_bytes,
        "latency_timeout_sec": args.latency_timeout_sec,
        "load_protocol": "none",
        "load_direction": "none",
        "load_rate": "none",
        "load_percent": load_percent,
        "computed_load_bps": computed_load_bps,
        "iperf3_parallel": "",
        "duration_sec": data.get("duration_sec_elapsed", args.latency_duration_sec) if isinstance(data, dict) else args.latency_duration_sec,
        "samples_requested": requested,
        "samples_received": len(ok_samples),
        "timeout_count": timeout_count,
        "error_count": error_count,
        "request_loss_pct": request_loss_pct,
        "rtt_min_ms": min(rtts) if rtts else "",
        "rtt_avg_ms": mean(rtts),
        "rtt_p50_ms": percentile(rtts, 50),
        "rtt_p90_ms": percentile(rtts, 90),
        "rtt_p95_ms": percentile(rtts, 95),
        "rtt_p99_ms": percentile(rtts, 99),
        "rtt_max_ms": max(rtts) if rtts else "",
        "rtt_mdev_ms": mdev(rtts),
        "error": error_text if error_text else ("" if ok_samples else "no successful TCP/UDP request-response samples"),
    }
    return summary


def start_iperf3_server(args, target, out_dir, phase):
    server_json = out_dir / f"iperf3_server_{phase}.json"
    server_err = out_dir / f"iperf3_server_{phase}.stderr.log"
    cmd = ns_cmd(args.wan_ns, [
        "iperf3",
        "-s",
        "-1",
        "-B", target["wan_ip"],
        "-p", str(args.iperf3_port),
        "--json",
    ])
    (out_dir / f"iperf3_server_{phase}.cmd.txt").write_text(
        " ".join(shlex.quote(str(x)) for x in cmd) + "\n",
        encoding="utf-8",
    )
    stdout = open(server_json, "w", encoding="utf-8")
    stderr = open(server_err, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, text=True)
    return proc, stdout, stderr, server_json


def build_iperf3_client_command(args, target, duration_sec, load_rate):
    cmd = [
        "iperf3",
        "-c", target["wan_ip"],
        "-B", target["lan_ip"],
        "-p", str(args.iperf3_port),
        "-t", str(int(math.ceil(duration_sec))),
        "-P", str(args.iperf3_parallel),
        "--json",
    ]
    if args.load_protocol == "udp":
        cmd.append("-u")
        cmd.extend(["-b", str(load_rate)])
    else:
        cmd.extend(["-b", str(load_rate)])

    if args.load_direction == "download":
        cmd.append("-R")
    elif args.load_direction == "bidirectional":
        cmd.append("--bidir")

    if args.iperf3_extra_args:
        cmd.extend(shlex.split(args.iperf3_extra_args))
    return ns_cmd(args.lan_ns, cmd)


def parse_iperf3_json(path):
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    end = data.get("end") if isinstance(data, dict) else {}
    if not isinstance(end, dict):
        return {}

    result = {
        "iperf3_bits_per_second": "",
        "iperf3_retransmits": "",
        "iperf3_jitter_ms": "",
        "iperf3_lost_packets": "",
        "iperf3_packets": "",
        "iperf3_lost_percent": "",
    }

    sum_sent = end.get("sum_sent") if isinstance(end.get("sum_sent"), dict) else {}
    sum_received = end.get("sum_received") if isinstance(end.get("sum_received"), dict) else {}
    candidate = sum_received or sum_sent
    if candidate:
        result["iperf3_bits_per_second"] = candidate.get("bits_per_second", "")
    if sum_sent:
        result["iperf3_retransmits"] = sum_sent.get("retransmits", "")

    udp_sum = end.get("sum") if isinstance(end.get("sum"), dict) else {}
    if udp_sum:
        result["iperf3_bits_per_second"] = udp_sum.get("bits_per_second", result["iperf3_bits_per_second"])
        result["iperf3_jitter_ms"] = udp_sum.get("jitter_ms", "")
        result["iperf3_lost_packets"] = udp_sum.get("lost_packets", "")
        result["iperf3_packets"] = udp_sum.get("packets", "")
        result["iperf3_lost_percent"] = udp_sum.get("lost_percent", "")

    bidir_bits = []
    for key in ("sum_sent_bidir", "sum_received_bidir"):
        item = end.get(key)
        if isinstance(item, dict) and item.get("bits_per_second") is not None:
            bidir_bits.append(float(item.get("bits_per_second") or 0.0))
    if bidir_bits:
        result["iperf3_bits_per_second"] = sum(bidir_bits)
    return result


def build_l4rr_summary_from_json(phase, args, target, out_dir, data, proc_returncode=None, *, tool_load="none", load_rate="none", load_percent="", computed_load_bps=""):
    samples = data.get("samples", []) if isinstance(data, dict) else []
    if not isinstance(samples, list):
        samples = []
    sample_csv = out_dir / f"latency_{phase}_samples.csv"
    with open(sample_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SAMPLE_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for sample in samples:
            row = dict(sample)
            row.update({"phase": phase, "latency_protocol": args.latency_protocol})
            writer.writerow(row)

    requested = int(data.get("samples_requested") or int(math.ceil(args.latency_duration_sec / args.latency_interval_sec))) if isinstance(data, dict) else 0
    ok_samples = [sample for sample in samples if sample.get("result") == "PASS" and sample.get("rtt_ms") not in ("", None)]
    timeout_count = sum(1 for sample in samples if sample.get("error") == "timeout")
    error_count = sum(1 for sample in samples if sample.get("result") != "PASS" and sample.get("error") != "timeout")
    rtts = [float(sample["rtt_ms"]) for sample in ok_samples]
    request_loss_pct = ((requested - len(ok_samples)) / requested * 100.0) if requested else ""
    error_text = ""
    if isinstance(data, dict) and data.get("error"):
        error_text = str(data.get("error"))
    if proc_returncode not in (0, None) and ok_samples:
        error_text = (error_text + "; " if error_text else "") + f"l4rr client returned exit code {proc_returncode}"

    is_idle = phase == "idle"
    summary = {
        "phase": phase,
        "test_name": f"iperf3_l4rr_latency_{phase}",
        "result": "PASS" if ok_samples else "FAIL",
        "tool_latency": f"built-in Python socket {args.latency_protocol.upper()} request-response",
        "tool_load": tool_load,
        "target_name": args.target,
        "lan_ns": args.lan_ns,
        "wan_ns": args.wan_ns,
        "lan_iface": args.lan_iface,
        "wan_iface": args.wan_iface,
        "lan_ip": target["lan_ip"],
        "lan_gw": target["lan_gw"],
        "wan_ip": target["wan_ip"],
        "wan_gw": target["wan_gw"],
        "router_wan_ip": target["router_wan_ip"],
        "latency_protocol": args.latency_protocol,
        "latency_port": args.latency_port,
        "latency_interval_sec": args.latency_interval_sec,
        "latency_payload_size_bytes": args.latency_payload_size_bytes,
        "latency_timeout_sec": args.latency_timeout_sec,
        "load_protocol": "none" if is_idle else args.load_protocol,
        "load_direction": "none" if is_idle else args.load_direction,
        "load_rate": "none" if is_idle else load_rate,
        "load_percent": load_percent if load_percent != "" else (0 if is_idle else ""),
        "computed_load_bps": computed_load_bps if computed_load_bps != "" else (0 if is_idle else ""),
        "iperf3_parallel": "" if is_idle else args.iperf3_parallel,
        "duration_sec": data.get("duration_sec_elapsed", args.latency_duration_sec) if isinstance(data, dict) else args.latency_duration_sec,
        "samples_requested": requested,
        "samples_received": len(ok_samples),
        "timeout_count": timeout_count,
        "error_count": error_count,
        "request_loss_pct": request_loss_pct,
        "rtt_min_ms": min(rtts) if rtts else "",
        "rtt_avg_ms": mean(rtts),
        "rtt_p50_ms": percentile(rtts, 50),
        "rtt_p90_ms": percentile(rtts, 90),
        "rtt_p95_ms": percentile(rtts, 95),
        "rtt_p99_ms": percentile(rtts, 99),
        "rtt_max_ms": max(rtts) if rtts else "",
        "rtt_mdev_ms": mdev(rtts),
        "error": error_text if error_text else ("" if ok_samples else "no successful TCP/UDP request-response samples"),
    }
    return summary


def run_loaded_phase(phase, args, target, out_dir, *, load_rate, load_percent, computed_load_bps):
    l4_server_proc, l4_server_stdout, l4_server_stderr = start_l4rr_server(args, target, out_dir, phase)
    iperf_server_proc, iperf_server_stdout, iperf_server_stderr, _server_json = start_iperf3_server(args, target, out_dir, phase)
    client_json = out_dir / f"iperf3_client_{phase}.json"
    client_err = out_dir / f"iperf3_client_{phase}.stderr.log"
    iperf_client_stdout = None
    iperf_client_stderr = None
    iperf_client_proc = None
    summary = None
    try:
        time.sleep(max(args.latency_server_start_delay_sec, args.iperf3_server_start_delay_sec))
        ensure_process_running(l4_server_proc, "l4rr server")
        ensure_process_running(iperf_server_proc, "iperf3 server")
        run_l4rr_preflight(args, target, out_dir, phase)
        load_duration = args.latency_duration_sec + args.load_warmup_sec + args.load_cooldown_sec
        client_cmd = build_iperf3_client_command(args, target, load_duration, load_rate)
        (out_dir / f"iperf3_client_{phase}.cmd.txt").write_text(
            " ".join(shlex.quote(str(x)) for x in client_cmd) + "\n",
            encoding="utf-8",
        )
        iperf_client_stdout = open(client_json, "w", encoding="utf-8")
        iperf_client_stderr = open(client_err, "w", encoding="utf-8")
        iperf_client_proc = subprocess.Popen(client_cmd, stdout=iperf_client_stdout, stderr=iperf_client_stderr, text=True)
        time.sleep(args.load_warmup_sec)

        l4_client_json = out_dir / f"l4rr_client_{phase}.json"
        l4_client_stderr = out_dir / f"l4rr_client_{phase}.stderr.log"
        l4_client_cmd = build_l4rr_client_command(args, target)
        (out_dir / f"l4rr_client_{phase}.cmd.txt").write_text(
            " ".join(shlex.quote(str(x)) for x in l4_client_cmd) + "\n",
            encoding="utf-8",
        )
        with open(l4_client_json, "w", encoding="utf-8") as out_f, open(l4_client_stderr, "w", encoding="utf-8") as err_f:
            l4_client_proc = subprocess.Popen(l4_client_cmd, stdout=out_f, stderr=err_f, text=True)
            wait_for_process_exit(l4_client_proc, args.latency_duration_sec + args.latency_timeout_sec + args.latency_connect_timeout_sec + 10.0, "l4rr client")

        data = parse_l4rr_json(l4_client_json)
        summary = build_l4rr_summary_from_json(
            phase,
            args,
            target,
            out_dir,
            data,
            l4_client_proc.returncode,
            tool_load="iperf3",
            load_rate=load_rate,
            load_percent=load_percent,
            computed_load_bps=computed_load_bps,
        )
        wait_for_process_exit(iperf_client_proc, args.load_cooldown_sec + 15.0, "iperf3 client")
        wait_for_process_exit(iperf_server_proc, 10.0, "iperf3 server")
    finally:
        if iperf_client_proc and iperf_client_proc.poll() is None:
            iperf_client_proc.terminate()
            wait_for_process_exit(iperf_client_proc, 5.0, "iperf3 client")
        if iperf_server_proc.poll() is None:
            iperf_server_proc.terminate()
            wait_for_process_exit(iperf_server_proc, 5.0, "iperf3 server")
        if l4_server_proc.poll() is None:
            l4_server_proc.terminate()
            wait_for_process_exit(l4_server_proc, 5.0, "l4rr server")
        for handle in (iperf_client_stdout, iperf_client_stderr, iperf_server_stdout, iperf_server_stderr, l4_server_stdout, l4_server_stderr):
            try:
                if handle:
                    handle.close()
            except Exception:
                pass

    if summary is None:
        summary = {
            "phase": phase,
            "test_name": f"iperf3_l4rr_latency_{phase}",
            "result": "FAIL",
            "tool_latency": f"built-in Python socket {args.latency_protocol.upper()} request-response",
            "tool_load": "iperf3",
            "load_rate": load_rate,
            "load_percent": load_percent,
            "computed_load_bps": computed_load_bps,
            "error": "loaded phase failed before latency summary was created",
        }
    iperf3_metrics = parse_iperf3_json(client_json)
    summary.update(iperf3_metrics)
    if iperf_client_proc and iperf_client_proc.returncode not in (0, None):
        summary["result"] = "FAIL"
        current_error = summary.get("error") or ""
        summary["error"] = (current_error + "; " if current_error else "") + f"iperf3 client returned exit code {iperf_client_proc.returncode}"
    return summary


def write_summary_csv(path, rows):
    fields = list(SUMMARY_FIELDS)
    for row in rows:
        for key in row.keys():
            if key not in fields:
                fields.append(key)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def make_delta_row(idle_row, loaded_row, args, target):
    def as_float(value):
        try:
            if value == "" or value is None:
                return None
            return float(value)
        except Exception:
            return None

    idle_avg = as_float(idle_row.get("rtt_avg_ms")) if idle_row else None
    loaded_avg = as_float(loaded_row.get("rtt_avg_ms")) if loaded_row else None
    idle_p95 = as_float(idle_row.get("rtt_p95_ms")) if idle_row else None
    loaded_p95 = as_float(loaded_row.get("rtt_p95_ms")) if loaded_row else None
    idle_p99 = as_float(idle_row.get("rtt_p99_ms")) if idle_row else None
    loaded_p99 = as_float(loaded_row.get("rtt_p99_ms")) if loaded_row else None
    loaded_phase = loaded_row.get("phase", "loaded") if loaded_row else "loaded"

    row = {
        "phase": f"delta_vs_idle_{loaded_phase}",
        "test_name": f"iperf3_l4rr_latency_delta_vs_idle_{loaded_phase}",
        "result": "PASS" if idle_row and loaded_row and idle_row.get("result") == "PASS" and loaded_row.get("result") == "PASS" else "FAIL",
        "tool_latency": f"built-in Python socket {args.latency_protocol.upper()} request-response",
        "tool_load": "iperf3",
        "target_name": args.target,
        "lan_ns": args.lan_ns,
        "wan_ns": args.wan_ns,
        "lan_iface": args.lan_iface,
        "wan_iface": args.wan_iface,
        "lan_ip": target["lan_ip"],
        "lan_gw": target["lan_gw"],
        "wan_ip": target["wan_ip"],
        "wan_gw": target["wan_gw"],
        "router_wan_ip": target["router_wan_ip"],
        "latency_protocol": args.latency_protocol,
        "latency_port": args.latency_port,
        "latency_interval_sec": args.latency_interval_sec,
        "latency_payload_size_bytes": args.latency_payload_size_bytes,
        "latency_timeout_sec": args.latency_timeout_sec,
        "load_protocol": loaded_row.get("load_protocol", args.load_protocol) if loaded_row else args.load_protocol,
        "load_direction": loaded_row.get("load_direction", args.load_direction) if loaded_row else args.load_direction,
        "load_rate": loaded_row.get("load_rate", "") if loaded_row else "",
        "load_percent": loaded_row.get("load_percent", "") if loaded_row else "",
        "computed_load_bps": loaded_row.get("computed_load_bps", "") if loaded_row else "",
        "iperf3_parallel": args.iperf3_parallel,
        "duration_sec": args.latency_duration_sec,
        "idle_rtt_avg_ms": idle_avg if idle_avg is not None else "",
        "loaded_rtt_avg_ms": loaded_avg if loaded_avg is not None else "",
        "delta_rtt_avg_ms": (loaded_avg - idle_avg) if idle_avg is not None and loaded_avg is not None else "",
        "idle_rtt_p95_ms": idle_p95 if idle_p95 is not None else "",
        "loaded_rtt_p95_ms": loaded_p95 if loaded_p95 is not None else "",
        "delta_rtt_p95_ms": (loaded_p95 - idle_p95) if idle_p95 is not None and loaded_p95 is not None else "",
        "idle_rtt_p99_ms": idle_p99 if idle_p99 is not None else "",
        "loaded_rtt_p99_ms": loaded_p99 if loaded_p99 is not None else "",
        "delta_rtt_p99_ms": (loaded_p99 - idle_p99) if idle_p99 is not None and loaded_p99 is not None else "",
    }
    if loaded_row:
        for key in (
            "iperf3_bits_per_second",
            "iperf3_retransmits",
            "iperf3_jitter_ms",
            "iperf3_lost_packets",
            "iperf3_packets",
            "iperf3_lost_percent",
        ):
            row[key] = loaded_row.get(key, "")
    return row


def write_metadata(out_dir, args, target):
    metadata = {
        "created_at": ts_iso(),
        "mode": "iperf3_l4rr_latency",
        "target_name": args.target,
        "target": target,
        "args": vars(args),
        "tools": {
            "load_generation": "iperf3",
            "latency_measurement": f"built-in Python socket {args.latency_protocol.upper()} request-response",
            "namespace_setup": "ip (iproute2)",
            "kernel_rebind": "modprobe + sysfs PCI driver bind/unbind when needed",
        },
        "notes": [
            "TRex tests are not replaced by this script; this is an additional latency benchmark.",
            "Separate network namespaces are enabled by default so that Linux does not deliver LAN-to-WAN traffic locally on the test host.",
            "Existing benchmark namespaces are replaced by default; this makes launcher execution after TRex ASTF safe against stale namespace state.",
            "When Linux netdevs are missing after TRex/DPDK use, profile PCI devices are rebound to kernel drivers by default before namespace setup.",
            "Linux interface names are read from target profile lan_iface/wan_iface. PCI BDF auto-resolution is a fallback only for profiles that omit those fields.",
            "Latency is measured with TCP or UDP request-response RTT while iperf3 generates TCP or UDP load. The default L4 latency mode uses UDP for both latency request-response and iperf3 load.",
            "Default phases are 0%, 10%, 20%, 30%, 40%, 50%, 60%, 70%, 80%, 90%, 95%, and 99%. The 0% phase is recorded as idle. Percent-based load is calculated from target profile line_rate_gbps.",
            "ICMP ping is not used for latency measurement in this script.",
        ],
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Run IPv4 NAT latency benchmark using iperf3 plus TCP/UDP request-response, without TRex.")
    parser.add_argument("--target", choices=TARGETS.keys(), default="i226_2p5g")
    parser.add_argument("--out-dir", default="")

    parser.add_argument("--lan-iface", default="", help="Linux interface name connected to the DUT LAN side. Default: read lan_iface from --target profile. PCI BDF auto-resolution is fallback only when the profile omits lan_iface.")
    parser.add_argument("--wan-iface", default="", help="Linux interface name connected to the DUT WAN side. Default: read wan_iface from --target profile. PCI BDF auto-resolution is fallback only when the profile omits wan_iface.")
    parser.add_argument("--lan-ns", default=DEFAULT_LAN_NS, help="LAN network namespace name. Use root only for preconfigured non-namespace tests.")
    parser.add_argument("--wan-ns", default=DEFAULT_WAN_NS, help="WAN network namespace name. Use root only for preconfigured non-namespace tests.")
    parser.add_argument("--setup-namespaces", dest="setup_namespaces", action="store_true", default=True, help="Create namespaces, move LAN/WAN interfaces into them, assign addresses, and set routes. Enabled by default for this test; kept for compatibility.")
    parser.add_argument("--no-setup-namespaces", dest="setup_namespaces", action="store_false", help="Do not create or configure namespaces. Use only for a preconfigured topology.")
    parser.add_argument("--replace-namespaces", dest="replace_namespaces", action="store_true", default=True, help="Delete existing --lan-ns/--wan-ns before setup. Enabled by default for this test; kept for compatibility.")
    parser.add_argument("--no-replace-namespaces", dest="replace_namespaces", action="store_false", help="Fail instead of deleting existing --lan-ns/--wan-ns.")
    parser.add_argument("--keep-namespaces", action="store_true", help="Leave namespaces and interfaces in place after the test.")
    parser.add_argument("--auto-kernel-bind", dest="auto_kernel_bind", action="store_true", default=True, help="Before namespace setup, bind profile PCI devices back to their kernel drivers when Linux netdevs are missing. Enabled by default.")
    parser.add_argument("--no-auto-kernel-bind", dest="auto_kernel_bind", action="store_false", help="Do not attempt PCI driver rebinding before namespace setup.")
    parser.add_argument("--restore-dpdk-after", dest="restore_dpdk_after", action="store_true", default=True, help="After the L4 test, delete namespaces and bind profile PCI devices back to the profile DPDK driver. Enabled by default.")
    parser.add_argument("--no-restore-dpdk-after", dest="restore_dpdk_after", action="store_false", help="Leave the NICs on their kernel drivers after the L4 test.")
    parser.add_argument("--dpdk-driver", default="", help="Override the DPDK driver used when --restore-dpdk-after is enabled.")
    parser.add_argument("--lan-prefix", type=parse_positive_int, default=DEFAULT_LAN_PREFIX)
    parser.add_argument("--wan-prefix", type=parse_positive_int, default=DEFAULT_WAN_PREFIX)

    parser.add_argument("--lan-ip", default="")
    parser.add_argument("--lan-gw", default="")
    parser.add_argument("--wan-ip", default="")
    parser.add_argument("--wan-gw", default="")
    parser.add_argument("--router-wan-ip", default="")

    parser.add_argument("--latency-duration-sec", "--duration", dest="latency_duration_sec", type=parse_positive_float, default=DEFAULT_LATENCY_DURATION_SEC)
    parser.add_argument("--l4-transport", choices=["tcp", "udp"], default="", help="Set both --latency-protocol and --load-protocol unless either option is explicitly specified. Default behavior uses UDP for both latency and load.")
    parser.add_argument("--latency-protocol", choices=["tcp", "udp"], default="udp", help="Protocol used by the latency request-response measurement. Default: udp.")
    parser.add_argument("--latency-port", type=parse_positive_int, default=DEFAULT_LATENCY_PORT)
    parser.add_argument("--latency-interval-sec", dest="latency_interval_sec", type=parse_positive_float, default=DEFAULT_LATENCY_INTERVAL_SEC)
    parser.add_argument("--ping-interval-sec", dest="latency_interval_sec", type=parse_positive_float, help=argparse.SUPPRESS)
    parser.add_argument("--latency-payload-size-bytes", dest="latency_payload_size_bytes", type=parse_positive_int, default=DEFAULT_LATENCY_PAYLOAD_SIZE_BYTES)
    parser.add_argument("--ping-size-bytes", dest="latency_payload_size_bytes", type=parse_positive_int, help=argparse.SUPPRESS)
    parser.add_argument("--latency-timeout-sec", type=parse_positive_float, default=DEFAULT_LATENCY_TIMEOUT_SEC)
    parser.add_argument("--latency-connect-timeout-sec", type=parse_positive_float, default=DEFAULT_LATENCY_CONNECT_TIMEOUT_SEC)
    parser.add_argument("--latency-server-start-delay-sec", type=parse_non_negative_float, default=1.5)
    parser.add_argument("--l4rr-preflight", dest="l4rr_preflight", action="store_true", default=True, help="Run a short TCP/UDP request-response preflight before each measured phase. Enabled by default.")
    parser.add_argument("--no-l4rr-preflight", dest="l4rr_preflight", action="store_false", help="Disable L4 request-response preflight.")
    parser.add_argument("--require-l4rr-preflight", dest="require_l4rr_preflight", action="store_true", default=True, help="Fail the phase when preflight cannot pass. Enabled by default.")
    parser.add_argument("--no-require-l4rr-preflight", dest="require_l4rr_preflight", action="store_false", help="Continue even when preflight fails.")
    parser.add_argument("--l4rr-preflight-timeout-sec", type=parse_positive_float, default=DEFAULT_L4RR_PREFLIGHT_TIMEOUT_SEC)
    parser.add_argument("--l4rr-preflight-duration-sec", type=parse_positive_float, default=DEFAULT_L4RR_PREFLIGHT_DURATION_SEC)
    parser.add_argument("--l4rr-preflight-interval-sec", type=parse_positive_float, default=DEFAULT_L4RR_PREFLIGHT_INTERVAL_SEC)
    parser.add_argument("--post-namespace-settle-sec", type=parse_non_negative_float, default=DEFAULT_POST_NAMESPACE_SETTLE_SEC)
    parser.add_argument("--test-start-delay-sec", type=parse_non_negative_float, default=DEFAULT_TEST_START_DELAY_SEC, help="Seconds to wait after namespace interfaces report link-up and before testing. Default: 15.")
    parser.add_argument("--wait-interfaces-ready", dest="wait_interfaces_ready", action="store_true", default=True, help="Wait until namespace interfaces report UP/LOWER_UP. Enabled by default.")
    parser.add_argument("--no-wait-interfaces-ready", dest="wait_interfaces_ready", action="store_false", help="Do not wait for namespace interface readiness.")
    parser.add_argument("--interface-ready-timeout-sec", type=parse_positive_float, default=DEFAULT_INTERFACE_READY_TIMEOUT_SEC)
    parser.add_argument("--skip-idle", action="store_true", help="Skip no-load latency measurement.")
    parser.add_argument("--skip-loaded", action="store_true", help="Skip latency measurement under iperf3 load.")

    parser.add_argument("--iperf3-port", type=parse_positive_int, default=DEFAULT_IPERF3_PORT)
    parser.add_argument("--iperf3-parallel", type=parse_positive_int, default=1)
    parser.add_argument("--iperf3-server-start-delay-sec", type=parse_non_negative_float, default=1.5)
    parser.add_argument("--iperf3-extra-args", default="", help="Additional arguments appended to iperf3 client command. Quote as one string.")
    parser.add_argument("--load-protocol", choices=["tcp", "udp"], default="udp")
    parser.add_argument("--load-direction", choices=["upload", "download", "bidirectional"], default="download")
    parser.add_argument("--load-percentages", type=parse_load_percentages, default=list(DEFAULT_LOAD_PERCENTAGES), help="Comma-separated load percentages based on target line_rate_gbps. 0 means no-load baseline. Default: 0,10,20,30,40,50,60,70,80,90,95,99.")
    parser.add_argument("--load-rate", type=parse_load_rate, default="auto", help="Compatibility override for iperf3 -b. Default auto computes each loaded phase from --load-percentages.")
    parser.add_argument("--load-warmup-sec", type=parse_non_negative_float, default=DEFAULT_LOAD_WARMUP_SEC)
    parser.add_argument("--load-cooldown-sec", type=parse_non_negative_float, default=DEFAULT_LOAD_COOLDOWN_SEC)

    argv_for_explicit_check = sys.argv[1:]
    latency_protocol_explicit = any(a == "--latency-protocol" or a.startswith("--latency-protocol=") for a in argv_for_explicit_check)
    load_protocol_explicit = any(a == "--load-protocol" or a.startswith("--load-protocol=") for a in argv_for_explicit_check)

    args, unknown_args = parser.parse_known_args()
    if args.l4_transport:
        if not latency_protocol_explicit:
            args.latency_protocol = args.l4_transport
        if not load_protocol_explicit:
            args.load_protocol = args.l4_transport
    if unknown_args:
        print(f"WARNING: ignored unsupported options for iperf3 L4 latency script: {' '.join(unknown_args)}")

    if args.skip_idle and args.skip_loaded:
        raise SystemExit("ERROR: both --skip-idle and --skip-loaded were specified; no measurement remains")
    if args.lan_prefix > 32 or args.wan_prefix > 32:
        raise SystemExit("ERROR: --lan-prefix and --wan-prefix must be 1..32")
    if args.load_rate.lower() == "auto" and not args.load_percentages:
        raise SystemExit("ERROR: --load-percentages must contain at least one value")
    if args.latency_payload_size_bytes < L4RR_HEADER.size:
        raise SystemExit(f"ERROR: --latency-payload-size-bytes must be >= {L4RR_HEADER.size}")
    if args.latency_port == args.iperf3_port:
        raise SystemExit("ERROR: --latency-port and --iperf3-port must be different")

    require_root()
    if not args.skip_loaded:
        require_command("iperf3")
    require_command("ip")
    require_command(sys.executable)

    target = merge_target_with_overrides(TARGETS[args.target], args)
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_RESULT_ROOT / f"iperf3_l4rr_latency_{args.target}_{ts_compact()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    ns_log_path = out_dir / "namespace_setup.csv"
    with open(ns_log_path, "w", newline="") as ns_log_file:
        ns_log = csv.writer(ns_log_file)
        ns_log.writerow(["ts_local", "action", "detail"])

        if args.setup_namespaces and args.replace_namespaces:
            delete_existing_namespaces(args, ns_log)
        if args.setup_namespaces:
            prepare_kernel_interfaces_from_profile(args, target, ns_log)
        resolve_profile_interfaces(args, target, ns_log)
        if args.setup_namespaces:
            require_profile_interfaces(args, target)

        write_metadata(out_dir, args, target)

        print("============================================================")
        print("IPv4 NAT latency benchmark using iperf3 + TCP/UDP request-response")
        print("============================================================")
        print(f"Target          : {args.target}")
        print(f"Description     : {target['description']}")
        print(f"Topology        : {target.get('topology', 'router_nat')}")
        print(f"Tools           : iperf3(load), Python socket {args.latency_protocol.upper()} request-response(latency), ip(iproute2 namespaces)")
        print(f"LAN namespace   : {args.lan_ns}")
        print(f"WAN namespace   : {args.wan_ns}")
        print(f"LAN interface   : {args.lan_iface or '(preconfigured)'}")
        print(f"WAN interface   : {args.wan_iface or '(preconfigured)'}")
        print(f"Namespace setup : {'enabled' if args.setup_namespaces else 'disabled'} replace_existing={'yes' if args.replace_namespaces else 'no'} keep={'yes' if args.keep_namespaces else 'no'}")
        print(f"Kernel bind     : {'auto' if args.auto_kernel_bind else 'disabled'}")
        print(f"LAN endpoint    : {target['lan_ip']} gw {target['lan_gw']}")
        print(f"WAN endpoint    : {target['wan_ip']} gw {target['router_wan_ip']}")
        print(f"Latency protocol: {args.latency_protocol.upper()} port={args.latency_port}")
        print(f"Latency duration: {args.latency_duration_sec:g}s")
        print(f"Latency interval: {args.latency_interval_sec:g}s")
        print(f"Load            : {args.load_protocol} {args.load_direction} percentages={','.join(percent_label(p) for p in args.load_percentages)} rate_override={args.load_rate} parallel={args.iperf3_parallel}")
        print(f"Output          : {out_dir}")
        print("============================================================")

        arp_refresher = None
        try:
            if args.setup_namespaces:
                setup_namespaces(args, target, ns_log)
            verify_namespaces(args)
            wait_test_interfaces_ready(args)
            if args.post_namespace_settle_sec > 0:
                time.sleep(args.post_namespace_settle_sec)
            wait_before_test_start(args.test_start_delay_sec, "L4 latency benchmark")
            arp_refresher = make_namespace_arp_refresher(args, target, out_dir)
            arp_refresher.refresh_now("startup", required=True)
            arp_refresher.start()

            idle_requested, loaded_percentages = split_load_percentages(args.load_percentages)
            idle_row = None
            if idle_requested and not args.skip_idle:
                print("--- idle TCP/UDP request-response latency start: load_percent=0 ---", flush=True)
                arp_refresher.refresh_now("before_idle", required=True)
                idle_row = run_l4rr_phase("idle", args, target, out_dir, tool_load="none", load_percent=0, computed_load_bps=0)
                rows.append(idle_row)
                print(
                    f"idle latency: avg={idle_row.get('rtt_avg_ms')}ms "
                    f"p95={idle_row.get('rtt_p95_ms')}ms loss={idle_row.get('request_loss_pct')}%",
                    flush=True,
                )
            elif not idle_requested:
                print("--- idle phase skipped: 0 is not present in --load-percentages ---", flush=True)
            else:
                print("--- idle phase skipped by --skip-idle ---", flush=True)

            loaded_rows = []
            if not args.skip_loaded:
                for load_percent in loaded_percentages:
                    phase = phase_for_load_percent(load_percent)
                    load_rate, computed_load_bps = effective_load_rate(args, target, load_percent)
                    print(f"--- {phase} TCP/UDP request-response latency start: iperf3_rate={load_rate} ---", flush=True)
                    arp_refresher.refresh_now(f"before_{phase}", required=True)
                    loaded_row = run_loaded_phase(
                        phase,
                        args,
                        target,
                        out_dir,
                        load_rate=load_rate,
                        load_percent=load_percent,
                        computed_load_bps=computed_load_bps,
                    )
                    rows.append(loaded_row)
                    loaded_rows.append(loaded_row)
                    print(
                        f"{phase} latency: avg={loaded_row.get('rtt_avg_ms')}ms "
                        f"p95={loaded_row.get('rtt_p95_ms')}ms loss={loaded_row.get('request_loss_pct')}% "
                        f"iperf3_bps={loaded_row.get('iperf3_bits_per_second')}",
                        flush=True,
                    )
            elif loaded_percentages:
                print("--- loaded phases skipped by --skip-loaded ---", flush=True)

            if idle_row:
                for loaded_row in loaded_rows:
                    rows.append(make_delta_row(idle_row, loaded_row, args, target))
        finally:
            if arp_refresher is not None:
                arp_refresher.stop()
            cleanup_namespaces(args, ns_log)
            if getattr(args, "restore_dpdk_after", True):
                if prepare_trex_interfaces_from_profile is None:
                    ns_log.writerow([ts_iso(), "restore_dpdk_after_unavailable", "prepare_trex_interfaces_from_profile import failed"])
                else:
                    try:
                        prep_log = prepare_trex_interfaces_from_profile(
                            target,
                            out_dir,
                            auto_dpdk_bind=True,
                            dpdk_driver_override=getattr(args, "dpdk_driver", ""),
                        )
                        ns_log.writerow([ts_iso(), "restore_dpdk_after_done", str(prep_log)])
                    except Exception as exc:
                        ns_log.writerow([ts_iso(), "restore_dpdk_after_warning", str(exc)])
                        print(f"WARNING: failed to restore DPDK driver state after L4 latency: {exc}", flush=True)

    summary_csv = out_dir / "summary_all.csv"
    write_summary_csv(summary_csv, rows)

    print("============================================================")
    print("DONE")
    print("============================================================")
    print(f"Result directory : {out_dir}")
    print(f"Summary CSV      : {summary_csv}")
    idle_requested, loaded_percentages = split_load_percentages(args.load_percentages)
    if idle_requested and not args.skip_idle:
        print(f"Idle samples CSV : {out_dir / 'latency_idle_samples.csv'}")
    for load_percent in loaded_percentages:
        phase = phase_for_load_percent(load_percent)
        print(f"{phase} samples CSV: {out_dir / f'latency_{phase}_samples.csv'}")
        print(f"{phase} iperf3 JSON : {out_dir / f'iperf3_client_{phase}.json'}")
    print(f"Metadata         : {out_dir / 'metadata.json'}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--_l4rr-server":
        run_internal_l4rr_server(sys.argv[2:])
    elif len(sys.argv) >= 2 and sys.argv[1] == "--_l4rr-client":
        raise SystemExit(run_internal_l4rr_client(sys.argv[2:]))
    else:
        main()
