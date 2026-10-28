#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared Linux network namespace and kernel-interface helpers."""

import csv
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from .arp import NamespaceArpRefresher, DEFAULT_ARP_REFRESH_INTERVAL_SEC
from .ndp import NamespaceNdpRefresher
from .common import parse_non_negative_float, parse_positive_float, parse_positive_int, ts_iso
from .config import load_defaults as _load_defaults
from .pci import (
    current_driver_from_device_path,
    driver_unbind_paths,
    normalize_pci_bdf as _normalize_pci_bdf,
    pci_device_path as _pci_device_path,
    write_sysfs_value,
)
from .source_ports import format_lan_source_port_range
from .trex import prepare_trex_interfaces_from_profile


_NETWORK_DEFAULTS = _load_defaults("network")
DEFAULT_INTERFACE_READY_TIMEOUT_SEC = float(_NETWORK_DEFAULTS["interface_ready_timeout_sec"])
DEFAULT_LAN_NS = _NETWORK_DEFAULTS["lan_namespace"]
DEFAULT_LAN_PREFIX = int(_NETWORK_DEFAULTS["lan_prefix"])
DEFAULT_POST_NAMESPACE_SETTLE_SEC = float(_NETWORK_DEFAULTS["post_namespace_settle_sec"])
DEFAULT_WAN_NS = _NETWORK_DEFAULTS["wan_namespace"]
DEFAULT_WAN_PREFIX = int(_NETWORK_DEFAULTS["wan_prefix"])


class CommandError(RuntimeError):
    pass


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
    return _normalize_pci_bdf(value)

def pci_device_path(bdf):
    bdf = normalize_pci_bdf(bdf)
    if not bdf:
        return None
    return _pci_device_path(bdf)


def pci_current_driver(bdf):
    """Return the currently bound PCI driver name, or an empty string.

    Path.resolve() without strict checking can return a synthetic path ending in
    "driver" when the sysfs driver symlink is absent.  That bogus value caused
    later code to write to .../driver/unbind and fail.  Use the symlink target
    only when sysfs actually exposes a driver link.
    """
    return current_driver_from_device_path(pci_device_path(bdf))


def pci_unbind_current_driver(bdf, current_driver, log_writer=None, required=False):
    bdf = normalize_pci_bdf(bdf)
    current_driver = str(current_driver or "").strip()
    if not bdf or not current_driver:
        if log_writer:
            log_writer.writerow([ts_iso(), "pci_unbind_skip_no_current_driver", bdf])
        return False
    candidates = driver_unbind_paths(pci_device_path(bdf), current_driver)
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
    write_sysfs_value(path, value)


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
            "Set kernel_drivers in tests/config/target_profiles.json or specify --lan-iface/--wan-iface after preparing the interfaces."
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
            "Specify lan_iface/wan_iface in tests/config/target_profiles.json."
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
            + " Confirm tests/config/target_profiles.json lan_iface/wan_iface or return the interface to the root namespace before L4 latency."
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
            + ". Update tests/config/target_profiles.json lan_iface/wan_iface, or specify --lan-iface/--wan-iface explicitly."
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
    is_ipv6 = target.get("ip_version") == "v6"
    lan_prefix = 64 if is_ipv6 and args.lan_prefix == DEFAULT_LAN_PREFIX else args.lan_prefix
    wan_prefix = 64 if is_ipv6 and args.wan_prefix == DEFAULT_WAN_PREFIX else args.wan_prefix
    run_cmd(["ip", "-n", args.lan_ns, "addr", "add", f"{target['lan_ip']}/{lan_prefix}", "dev", args.lan_iface])
    wan_iface_ip = target.get("wan_iface_ip", target["wan_ip"])
    run_cmd(["ip", "-n", args.wan_ns, "addr", "add", f"{wan_iface_ip}/{wan_prefix}", "dev", args.wan_iface])
    if target["wan_ip"] != wan_iface_ip:
        run_cmd(["ip", "-n", args.wan_ns, "addr", "add", f"{target['wan_ip']}/32", "dev", args.wan_iface])
    run_cmd(["ip", "-n", args.lan_ns, "link", "set", "dev", args.lan_iface, "up"])
    run_cmd(["ip", "-n", args.wan_ns, "link", "set", "dev", args.wan_iface, "up"])

    family_flag = "-6" if is_ipv6 else "-4"
    run_cmd(["ip", "-n", args.lan_ns, family_flag, "route", "replace", "default", "via", target["lan_gw"], "dev", args.lan_iface])
    run_cmd(["ip", "-n", args.wan_ns, family_flag, "route", "replace", "default", "via", target["router_wan_ip"], "dev", args.wan_iface])

    lan_source_port_range = getattr(args, "lan_source_port_range", None)
    if lan_source_port_range is not None:
        low, high = lan_source_port_range
        run_cmd([
            "ip", "netns", "exec", args.lan_ns,
            "sysctl", "-q", "-w",
            f"net.ipv4.ip_local_port_range={int(low)} {int(high)}",
        ])
        log_writer.writerow([
            ts_iso(),
            "lan_source_port_range",
            format_lan_source_port_range(lan_source_port_range),
        ])

    log_writer.writerow([ts_iso(), "lan_address", f"{target['lan_ip']}/{lan_prefix} gw {target['lan_gw']}"])
    wan_address_detail = f"{wan_iface_ip}/{wan_prefix}"
    if target["wan_ip"] != wan_iface_ip:
        wan_address_detail += f" service={target['wan_ip']}/32"
    log_writer.writerow([ts_iso(), "wan_address", f"{wan_address_detail} gw {target['router_wan_ip']}"])


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
            "src_ip": target.get("wan_iface_ip", target["wan_ip"]),
            "target_ip": target["router_wan_ip"],
        },
    ]
    return NamespaceArpRefresher(
        endpoints=endpoints,
        log_path=Path(out_dir) / "arp_refresh.csv",
        interval_sec=DEFAULT_ARP_REFRESH_INTERVAL_SEC,
    )


def make_namespace_neighbor_refresher(args, target, out_dir):
    if target.get("ip_version") != "v6":
        return make_namespace_arp_refresher(args, target, out_dir)
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
            "src_ip": target.get("wan_iface_ip", target["wan_ip"]),
            "target_ip": target["router_wan_ip"],
        },
    ]
    return NamespaceNdpRefresher(
        endpoints=endpoints,
        log_path=Path(out_dir) / "ndp_refresh.csv",
        interval_sec=DEFAULT_ARP_REFRESH_INTERVAL_SEC,
    )
