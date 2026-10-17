#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared TRex process, configuration, and PCI-interface helpers."""

import argparse
import csv
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from .common import ts_compact, ts_iso
from .config import load_defaults as _load_defaults
from .pci import (
    current_driver_from_device_path,
    driver_unbind_paths,
    normalize_trex_pci_bdf,
    pci_device_path as _pci_device_path,
    write_sysfs_value,
)


_TREX_DEFAULTS = _load_defaults("trex")
TREX_DIR = _TREX_DEFAULTS["directory"]
TREX_CFG_PATH = _TREX_DEFAULTS["config_path"]
RAW_INTERVAL_SEC = float(_TREX_DEFAULTS["raw_interval_sec"])
DEFAULT_BENCHMARK_NAMESPACES = tuple(_TREX_DEFAULTS["benchmark_namespaces"])
DEFAULT_DPDK_DRIVER_CANDIDATES = tuple(_TREX_DEFAULTS["dpdk_driver_candidates"])
DEFAULT_RPC_TIMEOUT_SEC = int(_TREX_DEFAULTS["rpc_timeout_sec"])


def require_root():
    if os.geteuid() != 0:
        raise SystemExit("ERROR: run with sudo. This script starts TRex and writes /etc/trex_cfg.yaml.")


def add_trex_api_path():
    api_path = os.path.join(TREX_DIR, "automation", "trex_control_plane", "interactive")
    if api_path not in sys.path:
        sys.path.insert(0, api_path)


def parse_interfaces(value):
    parts = [x.strip() for x in value.split(",") if x.strip()]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("--interfaces must be two PCI addresses, e.g. 05:00.0,04:00.0")
    return parts


def merge_target_with_overrides(base_target, args):
    target = dict(base_target)
    if args.interfaces:
        target["interfaces"] = args.interfaces
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
    if args.line_rate_gbps is not None:
        target["line_rate_gbps"] = args.line_rate_gbps
    if args.cores is not None:
        # --cores is an explicit runtime override.  Apply it to both the
        # generic/STL core count and the ASTF-specific default when present.
        target["trex_cores"] = args.cores
        target["astf_cores"] = args.cores
    return target


def render_trex_cfg(target):
    if target.get("ip_version") == "v6":
        return (
            "- port_limit      : 2\n"
            "  version         : 2\n"
            f"  interfaces      : [\"{target['interfaces'][0]}\", \"{target['interfaces'][1]}\"]\n"
        )
    return (
        "- port_limit      : 2\n"
        "  version         : 2\n"
        f"  interfaces      : [\"{target['interfaces'][0]}\", \"{target['interfaces'][1]}\"]\n"
        "\n"
        "  port_info:\n"
        "      # Port 0: LAN side\n"
        f"      - ip         : {target['lan_ip']}\n"
        f"        default_gw : {target['lan_gw']}\n"
        "\n"
        "      # Port 1: WAN side\n"
        f"      - ip         : {target['wan_ip']}\n"
        f"        default_gw : {target['wan_gw']}\n"
    )


def resolve_ipv6_gateway_macs(client, target, *, set_l2=False, retries=3):
    """Resolve both router-facing MAC addresses with raw TRex IPv6 ND services.

    ServiceIPv6ND builds and transmits Neighbor Solicitation packets itself.
    Do not call client.conf_ipv6() here: that API configures the optional
    port-stack IPv6 implementation and fails when TRex is using its default
    legacy stack, although the raw ND service is fully usable.
    """
    add_trex_api_path()
    from trex.common.services.trex_service_IPv6ND import ServiceIPv6ND

    result = {}
    endpoints = (
        ("lan", int(target["lan_port"]), target["lan_ip"], target["lan_gw"]),
        ("wan", int(target["wan_port"]), target["wan_ip"], target["router_wan_ip"]),
    )
    for side, port, src_ip, dst_ip in endpoints:
        client.set_service_mode(ports=[port], enabled=True)
        try:
            ctx = client.create_service_ctx(port)
            service = ServiceIPv6ND(
                ctx,
                dst_ip=dst_ip,
                src_ip=src_ip,
                retries=int(retries),
            )
            ctx.run([service])
            record = service.get_record()
            if record is None or not record.is_resolved():
                raise RuntimeError(
                    f"IPv6 neighbor resolution failed on {side} port {port}: "
                    f"{src_ip} -> {dst_ip}"
                )
            src_mac = str(record.src_mac).lower()
            dst_mac = str(record.dst_mac).lower()
            result[f"{side}_src_mac"] = src_mac
            result[f"{side}_router_mac"] = dst_mac
            if set_l2:
                client.set_l2_mode(port, dst_mac)
        finally:
            client.set_service_mode(ports=[port], enabled=False)
    return result


def install_trex_cfg(target, out_dir, no_install_cfg):
    cfg_text = render_trex_cfg(target)
    generated_cfg = out_dir / "trex_cfg.yaml"
    generated_cfg.write_text(cfg_text, encoding="utf-8")
    if no_install_cfg:
        print(f"Generated TRex cfg only: {generated_cfg}")
        return generated_cfg
    if os.path.exists(TREX_CFG_PATH):
        backup = out_dir / f"trex_cfg.yaml.backup_{ts_compact()}"
        backup.write_text(Path(TREX_CFG_PATH).read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Backed up existing {TREX_CFG_PATH} to {backup}")
    Path(TREX_CFG_PATH).write_text(cfg_text, encoding="utf-8")
    print(f"Installed TRex cfg: {TREX_CFG_PATH}")
    return generated_cfg


def kill_existing_trex():
    subprocess.run(["pkill", "-f", "_t-rex-64"], check=False)
    subprocess.run(["pkill", "-f", "t-rex-64"], check=False)
    subprocess.run(["pkill", "-f", "trex-console"], check=False)
    time.sleep(3)


def run_cmd_noexcept(cmd, timeout=15):
    try:
        return subprocess.run(
            [str(x) for x in cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except Exception as exc:
        class Result:
            returncode = 999
            stdout = ""
            stderr = str(exc)
        return Result()


def sysfs_write(path, value):
    write_sysfs_value(path, value)


def normalize_pci_bdf(value):
    return normalize_trex_pci_bdf(value)


def pci_device_path(bdf):
    bdf = normalize_pci_bdf(bdf)
    return _pci_device_path(bdf)


def pci_current_driver(bdf):
    """Return the currently bound PCI driver name, or an empty string.

    Path.resolve() without strict checking can return a synthetic path ending in
    "driver" when the sysfs driver symlink is absent.  That bogus value caused
    later code to write to .../driver/unbind and fail.  Use the symlink target
    only when sysfs actually exposes a driver link.
    """
    return current_driver_from_device_path(pci_device_path(bdf))


def pci_unbind_current_driver(bdf, current_driver, writer=None, required=False):
    """Best-effort PCI unbind used before switching kernel/DPDK drivers.

    Some systems transiently report a PCI device without a usable
    /sys/bus/pci/devices/<BDF>/driver/unbind path.  Unbind is a recovery step,
    not a measurement step, so do not abort immediately.  The caller validates
    the final driver after bind.
    """
    bdf = normalize_pci_bdf(bdf)
    current_driver = str(current_driver or "").strip()
    if not bdf or not current_driver:
        if writer:
            writer.writerow([ts_iso(), "pci_unbind_skip_no_current_driver", bdf])
        return False

    candidates = driver_unbind_paths(pci_device_path(bdf), current_driver)
    last_exc = None
    for unbind_path in candidates:
        if not unbind_path.exists():
            continue
        try:
            sysfs_write(unbind_path, bdf)
            if writer:
                writer.writerow([ts_iso(), "pci_unbind", f"{bdf} from {current_driver} via {unbind_path}"])
            return True
        except OSError as exc:
            last_exc = exc

    msg = f"{bdf} from {current_driver}: no usable unbind path"
    if last_exc is not None:
        msg = f"{msg}; last_error={last_exc}"
    if writer:
        writer.writerow([ts_iso(), "pci_unbind_warning", msg])
    if required:
        raise RuntimeError(f"ERROR: failed to unbind {bdf} from {current_driver}: {msg}")
    return False


def namespace_exists(ns_name):
    ns_name = str(ns_name or "").strip()
    if not ns_name or ns_name == "root":
        return False
    proc = run_cmd_noexcept(["ip", "netns", "list"], timeout=10)
    if proc.returncode != 0:
        return False
    return any(line.split()[0] == ns_name for line in (proc.stdout or "").splitlines() if line.strip())


def delete_netns_force(ns_name, writer=None):
    ns_name = str(ns_name or "").strip()
    if not ns_name or ns_name == "root" or not namespace_exists(ns_name):
        return
    if writer:
        writer.writerow([ts_iso(), "delete_stale_namespace_start", ns_name])
    pids_proc = run_cmd_noexcept(["ip", "netns", "pids", ns_name], timeout=10)
    pids = []
    for item in (pids_proc.stdout or "").split():
        try:
            pids.append(int(item))
        except ValueError:
            pass
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    if pids:
        time.sleep(0.5)
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    proc = run_cmd_noexcept(["ip", "netns", "delete", ns_name], timeout=15)
    if writer:
        writer.writerow([ts_iso(), "delete_stale_namespace_done", f"{ns_name} rc={proc.returncode} stderr={(proc.stderr or '').strip()}"])
    time.sleep(0.7)


def cleanup_benchmark_namespaces(writer=None):
    for ns_name in DEFAULT_BENCHMARK_NAMESPACES:
        delete_netns_force(ns_name, writer)


def profile_port_index(target, side):
    try:
        return int(target.get(f"{side}_port"))
    except (TypeError, ValueError):
        return None


def profile_pci_interface_entry(target, side):
    for key in (f"{side}_pci_bdf", f"{side}_bdf", f"{side}_pci"):
        if target.get(key):
            return target[key]
    interfaces = target.get("interfaces") or []
    idx = profile_port_index(target, side)
    if idx is not None:
        try:
            return interfaces[idx]
        except (IndexError, TypeError):
            return ""
    return ""


def profile_dpdk_driver_entry(target, side, override_driver=""):
    override_driver = str(override_driver or "").strip()
    if override_driver:
        return override_driver
    for key in (f"{side}_dpdk_driver", f"{side}_trex_driver"):
        if target.get(key):
            return str(target[key])
    drivers = target.get("dpdk_drivers") or target.get("trex_drivers") or []
    idx = profile_port_index(target, side)
    if idx is not None:
        try:
            driver = str(drivers[idx])
            if driver:
                return driver
        except (IndexError, TypeError):
            pass
    for key in ("dpdk_driver", "trex_driver"):
        if target.get(key):
            return str(target[key])
    env_driver = str(os.environ.get("TREX_DPDK_DRIVER", "")).strip()
    return env_driver or "auto"


def pci_vendor_device_ids(bdf):
    dev_path = pci_device_path(bdf)
    if not dev_path:
        return "", ""
    try:
        vendor = (dev_path / "vendor").read_text(encoding="utf-8").strip()
    except OSError:
        vendor = ""
    try:
        device = (dev_path / "device").read_text(encoding="utf-8").strip()
    except OSError:
        device = ""
    return vendor, device


def available_dpdk_driver_candidates(preferred=""):
    preferred = str(preferred or "").strip()
    env_driver = str(os.environ.get("TREX_DPDK_DRIVER", "")).strip()
    candidates = []
    if preferred and preferred.lower() != "auto":
        candidates.append(preferred)
    if env_driver and env_driver not in candidates:
        candidates.append(env_driver)
    # Prefer the classic TRex driver when present; many lab TRex installs still
    # use igb_uio.  Then try vfio-pci, then the generic UIO driver.
    for driver in DEFAULT_DPDK_DRIVER_CANDIDATES:
        if driver not in candidates:
            candidates.append(driver)
    return candidates


def dpdk_driver_bind_path_exists(driver):
    return (Path("/sys/bus/pci/drivers") / driver / "bind").exists()


def try_modprobe_driver(driver, writer=None):
    proc = run_cmd_noexcept(["modprobe", driver], timeout=20)
    if writer:
        detail = f"driver={driver} rc={proc.returncode} stderr={(proc.stderr or '').strip()}"
        writer.writerow([ts_iso(), "modprobe_driver", detail])
    return proc.returncode == 0


def bind_pci_to_driver_once(bdf, driver, writer=None):
    bdf = normalize_pci_bdf(bdf)
    driver = str(driver or "").strip()
    dev_path = pci_device_path(bdf)
    if not dev_path or not dev_path.exists():
        raise RuntimeError(f"ERROR: PCI device does not exist: {bdf}")

    current = pci_current_driver(bdf)
    if current == driver:
        if writer:
            writer.writerow([ts_iso(), "dpdk_driver_already_bound", f"{bdf} driver={driver}"])
        return True

    if writer:
        writer.writerow([ts_iso(), "dpdk_bind_start", f"{bdf} current={current or '(none)'} target={driver}"])

    try_modprobe_driver(driver, writer=writer)

    if not dpdk_driver_bind_path_exists(driver):
        if writer:
            writer.writerow([ts_iso(), "dpdk_bind_path_missing", f"driver={driver}"])
        return False

    override_path = dev_path / "driver_override"
    override_set = False
    if override_path.exists():
        try:
            sysfs_write(override_path, driver)
            override_set = True
            if writer:
                writer.writerow([ts_iso(), "driver_override_set", f"{bdf}->{driver}"])
        except OSError as exc:
            if writer:
                writer.writerow([ts_iso(), "driver_override_warning", f"{bdf}->{driver}: {exc}"])

    current = pci_current_driver(bdf)
    if current and current != driver:
        pci_unbind_current_driver(bdf, current, writer=writer, required=False)

    bind_path = Path("/sys/bus/pci/drivers") / driver / "bind"
    probe_path = Path("/sys/bus/pci/drivers_probe")
    new_id_path = Path("/sys/bus/pci/drivers") / driver / "new_id"
    attempts = []

    def attempt(label, path, value):
        try:
            sysfs_write(path, value)
            attempts.append(f"{label}=ok")
            if writer:
                writer.writerow([ts_iso(), label, f"{value} -> {path}"])
        except OSError as exc:
            attempts.append(f"{label}={exc}")
            if writer:
                writer.writerow([ts_iso(), f"{label}_warning", f"{value} -> {path}: {exc}"])
        time.sleep(0.3)
        return pci_current_driver(bdf) == driver

    try:
        if attempt("pci_bind", bind_path, bdf):
            return True

        if override_set and probe_path.exists():
            if attempt("pci_drivers_probe", probe_path, bdf):
                return True

        vendor, device = pci_vendor_device_ids(bdf)
        if vendor and device and new_id_path.exists():
            # vfio-pci/igb_uio/uio_pci_generic may not match the device until a
            # dynamic PCI ID is registered.  This is a standard DPDK devbind
            # fallback and is essential after a previous failed bind left the
            # device with no active driver.
            if attempt("pci_new_id", new_id_path, f"{vendor} {device}"):
                return True
            if attempt("pci_bind_after_new_id", bind_path, bdf):
                return True
            if override_set and probe_path.exists():
                if attempt("pci_drivers_probe_after_new_id", probe_path, bdf):
                    return True
    finally:
        if override_path.exists():
            try:
                sysfs_write(override_path, "")
                if writer:
                    writer.writerow([ts_iso(), "driver_override_clear", bdf])
            except OSError:
                pass

    final_driver = pci_current_driver(bdf)
    if writer:
        writer.writerow([ts_iso(), "dpdk_bind_candidate_failed", f"{bdf} target={driver} final={final_driver or '(none)'} attempts={' | '.join(attempts)}"])
    return False


def bind_pci_to_driver(bdf, driver, writer=None, required=True):
    bdf = normalize_pci_bdf(bdf)
    driver = str(driver or "").strip() or "auto"
    if not bdf:
        return False

    dev_path = pci_device_path(bdf)
    if not dev_path or not dev_path.exists():
        raise RuntimeError(f"ERROR: PCI device does not exist: {bdf}")

    current = pci_current_driver(bdf)
    if current in ("vfio-pci", "igb_uio", "uio_pci_generic") and driver.lower() == "auto":
        if writer:
            writer.writerow([ts_iso(), "dpdk_driver_already_bound_auto", f"{bdf} driver={current}"])
        return True

    errors = []
    for candidate in available_dpdk_driver_candidates(driver):
        if not candidate:
            continue
        try:
            if bind_pci_to_driver_once(bdf, candidate, writer=writer):
                final_driver = pci_current_driver(bdf)
                if writer:
                    writer.writerow([ts_iso(), "dpdk_bind_done", f"{bdf} driver={final_driver}"])
                return True
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")
            if writer:
                writer.writerow([ts_iso(), "dpdk_bind_candidate_exception", f"{bdf} target={candidate}: {exc}"])

    final_driver = pci_current_driver(bdf)
    msg = (
        f"ERROR: PCI device {bdf} could not be bound to a TRex/DPDK driver. "
        f"final_driver={final_driver or '(none)'}, requested={driver}. "
        "Tried: " + ", ".join(available_dpdk_driver_candidates(driver))
    )
    if errors:
        msg += "; errors: " + " | ".join(errors)
    msg += " Set TREX_DPDK_DRIVER or pass --dpdk-driver if this host uses a specific driver."
    if required:
        raise RuntimeError(msg)
    if writer:
        writer.writerow([ts_iso(), "dpdk_bind_failed_non_required", msg])
    return False


def prepare_trex_interfaces_from_profile(target, out_dir, auto_dpdk_bind=True, dpdk_driver_override=""):
    """Prepare NIC state for TRex after L4 namespace/kernel-driver tests.

    L4 latency tests move Linux netdevs into namespaces and bind the NICs to
    kernel drivers.  TRex STL/ASTF requires the PCI devices to be back on the
    configured DPDK driver.  This function removes stale benchmark namespaces
    and, when enabled, binds the profile PCI devices to the profile DPDK driver.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "trex_interface_prepare.csv"
    with log_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ts_local", "action", "detail"])
        cleanup_benchmark_namespaces(writer)
        if not auto_dpdk_bind:
            writer.writerow([ts_iso(), "dpdk_bind_disabled", "--no-auto-dpdk-bind"])
            return log_path
        for side in ("lan", "wan"):
            bdf = profile_pci_interface_entry(target, side)
            driver = profile_dpdk_driver_entry(target, side, dpdk_driver_override)
            bind_pci_to_driver(bdf, driver, writer=writer, required=True)
    return log_path


def wait_rpc(timeout=DEFAULT_RPC_TIMEOUT_SEC):
    start = time.time()
    while time.time() - start < timeout:
        ok = True
        for port in (4500, 4501):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            try:
                sock.connect(("127.0.0.1", port))
            except OSError:
                ok = False
            finally:
                sock.close()
        if ok:
            return
        time.sleep(1)
    raise RuntimeError("TRex RPC ports did not become ready")


def tail_text(path, max_lines=80):
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-int(max_lines):])


def _trex_link_up_from_attr(attr):
    if not isinstance(attr, dict):
        return False
    for key in ("link_up", "is_link_up", "up"):
        value = attr.get(key)
        if isinstance(value, bool):
            return value
    link = attr.get("link")
    if isinstance(link, bool):
        return link
    if isinstance(link, str):
        return link.strip().upper() == "UP"
    if isinstance(link, dict):
        for key in ("up", "link_up", "is_up"):
            value = link.get(key)
            if isinstance(value, bool):
                return value
        status = link.get("status") or link.get("state")
        if isinstance(status, str):
            return status.strip().upper() == "UP"
    return False


def wait_trex_ports_link_up(
    client,
    ports,
    timeout_sec=_TREX_DEFAULTS["link_timeout_sec"],
    poll_interval_sec=_TREX_DEFAULTS["link_poll_interval_sec"],
):
    deadline = time.monotonic() + float(timeout_sec)
    last = {}
    while time.monotonic() < deadline:
        all_up = True
        last = {}
        for port in ports:
            try:
                attr = client.get_port_attr(port)
            except Exception as exc:
                attr = {"error": str(exc)}
            last[port] = attr
            if not _trex_link_up_from_attr(attr):
                all_up = False
        if all_up:
            return last
        time.sleep(float(poll_interval_sec))
    raise RuntimeError(f"TRex port link did not become UP within {timeout_sec:g}s: {last}")
