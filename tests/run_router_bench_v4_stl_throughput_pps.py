#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRex IPv4 NAT throughput/PPS benchmark - STL only final version.

- No ASTF
- No latency
- Throughput and PPS only
- Mbps-based rate search is enabled by default
- NDR/PDR summary rows are written to summary_all.csv

Port mapping:
  port 0 = LAN/client side
  port 1 = WAN/server side

Typical:
  cd /opt/trex/v3.08
  sudo python3 ./run_router_bench_stl_throughput_pps.py --target i226_2p5g --duration 20

Single fixed-load run:
  sudo python3 ./run_router_bench_stl_throughput_pps.py --target i226_2p5g --no-rate-search --duration 60
"""

import argparse
import csv
import datetime as dt
import json
import os
import signal
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

TREX_DIR = "/opt/trex/v3.08"
TREX_CFG_PATH = "/etc/trex_cfg.yaml"

FRAME_64 = 64
FRAME_LARGE = 1414
L1_OVERHEAD_BYTES = 20
UDP_PORT = 7770
RAW_INTERVAL_SEC = 1.0
DEFAULT_RATE_MBPS_STEPS = (100, 150, 220, 330, 470, 680, 1000, 1500, 2200, 3300, 4700, 6800, 10000)
DEFAULT_RESULT_ROOT = Path(os.environ.get("TREX_RESULT_ROOT", "/var/trex"))

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from tests.target_profiles import TARGETS  # type: ignore
except Exception:
    from target_profiles import TARGETS  # type: ignore



def ts_compact():
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def ts_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


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


def parse_rate_steps(value):
    try:
        steps = [float(x.strip()) for x in value.split(",") if x.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--rate-steps must be comma-separated numbers, e.g. 10,20,...,100") from exc
    if not steps:
        raise argparse.ArgumentTypeError("--rate-steps must not be empty")
    for step in steps:
        if step <= 0 or step > 100:
            raise argparse.ArgumentTypeError("--rate-steps values must be 0 < step <= 100")
    return steps


def parse_rate_mbps_steps(value):
    try:
        steps = [float(x.strip().replace("_", "")) for x in value.split(",") if x.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--rate-mbps-steps must be comma-separated numbers, e.g. 100,150,220,330") from exc
    if not steps:
        raise argparse.ArgumentTypeError("--rate-mbps-steps must not be empty")
    for step in steps:
        if step <= 0:
            raise argparse.ArgumentTypeError("--rate-mbps-steps values must be positive")
    return steps


def fmt_rate_value(value):
    value = float(value)
    if value.is_integer():
        return str(int(value))
    return f"{value:g}"


def make_default_rate_mbps_steps(line_rate_gbps):
    line_rate_mbps = float(line_rate_gbps) * 1000.0
    steps = [float(x) for x in DEFAULT_RATE_MBPS_STEPS if float(x) <= line_rate_mbps + 1e-9]
    if line_rate_mbps > 0 and (not steps or abs(steps[-1] - line_rate_mbps) > 1e-9):
        steps.append(line_rate_mbps)
    return steps


def limit_rate_mbps_steps(steps, line_rate_gbps):
    line_rate_mbps = float(line_rate_gbps) * 1000.0
    limited = sorted(set(float(x) for x in steps if float(x) <= line_rate_mbps + 1e-9))
    if not limited:
        raise SystemExit(
            f"ERROR: no Mbps rate steps are <= configured line rate ({fmt_rate_value(line_rate_mbps)} Mbps)"
        )
    return limited


def build_rate_points(line_rate_gbps, rate_steps=None, rate_mbps_steps=None):
    line_rate_mbps = float(line_rate_gbps) * 1000.0
    if rate_mbps_steps is not None:
        points = []
        for mbps in limit_rate_mbps_steps(rate_mbps_steps, line_rate_gbps):
            points.append({
                "label": f"{fmt_rate_value(mbps)}mbps",
                "rate_mbps": mbps,
                "rate_percent": (mbps / line_rate_mbps) * 100.0,
                "bps_l1": mbps * 1_000_000.0,
            })
        return points

    points = []
    for pct in sorted(set(float(x) for x in rate_steps)):
        mbps = line_rate_mbps * (pct / 100.0)
        points.append({
            "label": f"{pct:g}pct",
            "rate_mbps": mbps,
            "rate_percent": pct,
            "bps_l1": float(line_rate_gbps) * 1_000_000_000.0 * (pct / 100.0),
        })
    return points


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


def flatten(obj, prefix=""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            yield from flatten(value, name)
    elif isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            name = f"{prefix}.{index}" if prefix else str(index)
            yield from flatten(value, name)
    else:
        yield prefix, obj


def json_cell(value):
    if value is None:
        return ""
    if isinstance(value, (int, float, str, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def write_raw_stats(writer, phase, test_name, elapsed, stats):
    now = ts_iso()
    for metric, value in flatten(stats):
        writer.writerow([now, f"{elapsed:.3f}", phase, test_name, metric, json_cell(value)])


def kill_existing_trex():
    subprocess.run(["pkill", "-f", "_t-rex-64"], check=False)
    subprocess.run(["pkill", "-f", "t-rex-64"], check=False)
    subprocess.run(["pkill", "-f", "trex-console"], check=False)
    time.sleep(3)


DEFAULT_BENCHMARK_NAMESPACES = ("rb_lan", "rb_wan")


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
    with Path(path).open("w", encoding="utf-8") as f:
        f.write(str(value))


def normalize_pci_bdf(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if value.count(":") == 1:
        return f"0000:{value}"
    return value


def pci_device_path(bdf):
    bdf = normalize_pci_bdf(bdf)
    return Path("/sys/bus/pci/devices") / bdf if bdf else None


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
    for driver in ("igb_uio", "vfio-pci", "uio_pci_generic"):
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


def wait_rpc(timeout=60):
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


class TrexStlServer:
    def __init__(self, cores, out_dir):
        self.cores = cores
        self.out_dir = out_dir
        self.proc = None
        self.log_path = out_dir / "trex_server_stl.log"
        self.log_file = None

    def start(self):
        kill_existing_trex()
        cmd = ["./t-rex-64", "-i", "-c", str(self.cores)]
        self.log_file = open(self.log_path, "w")
        print("=== Starting TRex STL mode ===")
        print(" ".join(cmd))
        print(f"server log: {self.log_path}")
        self.proc = subprocess.Popen(
            cmd,
            cwd=TREX_DIR,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
        )
        try:
            wait_rpc(timeout=60)
        except Exception as exc:
            exit_code = self.proc.poll() if self.proc is not None else None
            if self.log_file:
                self.log_file.flush()
            log_tail = tail_text(self.log_path)
            detail = f"{exc}\nTRex process exit_code={exit_code}\nTRex server log: {self.log_path}"
            if log_tail:
                detail += "\n--- last TRex server log lines ---\n" + log_tail
            raise RuntimeError(detail) from exc

    def stop(self):
        print("=== Stopping TRex STL mode ===")
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                self.proc.wait(timeout=8)
            except Exception:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except Exception:
                    pass
        kill_existing_trex()
        if self.log_file:
            self.log_file.close()
            self.log_file = None


def payload_for_l2_size(frame_size):
    payload_len = frame_size - 14 - 20 - 8  # Ethernet + IPv4 + UDP
    if payload_len < 0:
        raise ValueError(f"frame_size too small: {frame_size}")
    return b"X" * payload_len


def theoretical_pps_per_port(line_rate_gbps, frame_size):
    return line_rate_gbps * 1_000_000_000 / ((frame_size + L1_OVERHEAD_BYTES) * 8)


def subtract_stats(before, after):
    if isinstance(before, dict) and isinstance(after, dict):
        out = {}
        for key in set(before.keys()) | set(after.keys()):
            if key in before and key in after:
                out[key] = subtract_stats(before[key], after[key])
            elif key in after:
                out[key] = after[key]
        return out
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return after - before
    return after


def metric_float(row, key, default=0.0):
    value = row.get(key, None)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except Exception:
        return default


def metric_int(row, key, default=0):
    value = row.get(key, None)
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except Exception:
        return default


def row_passes(row, loss_threshold):
    return (
        metric_float(row, "loss_rate", 1.0) <= loss_threshold
        and metric_int(row, "queue_full", 1) == 0
        and metric_int(row, "ierrors", 1) == 0
        and metric_int(row, "oerrors", 1) == 0
    )


def best_row_for_threshold(rows, loss_threshold):
    best = None
    for row in rows:
        if not row_passes(row, loss_threshold):
            continue
        if best is None or metric_float(row, "rate_percent", -1) > metric_float(best, "rate_percent", -1):
            best = row
    return best


def first_loss_row(rows, loss_threshold):
    for row in sorted(rows, key=lambda r: metric_float(r, "rate_percent", 0.0)):
        if not row_passes(row, loss_threshold):
            return row
    return None


def copy_best(prefix, row, out):
    for suffix in ("percent", "mbps", "rx_pps", "l1_bps", "l2_bps", "loss_rate", "pps_reach_rate", "l1_reach_rate"):
        out[f"{prefix}_{suffix}"] = ""
    if row is None:
        return
    out[f"{prefix}_percent"] = row.get("rate_percent", "")
    out[f"{prefix}_mbps"] = row.get("rate_mbps_per_active_port", "")
    out[f"{prefix}_rx_pps"] = row.get("avg_rx_pps", "")
    out[f"{prefix}_l1_bps"] = row.get("avg_rx_l1_bps", "")
    out[f"{prefix}_l2_bps"] = row.get("avg_rx_l2_bps", "")
    out[f"{prefix}_loss_rate"] = row.get("loss_rate", "")
    out[f"{prefix}_pps_reach_rate"] = row.get("pps_reach_rate", "")
    out[f"{prefix}_l1_reach_rate"] = row.get("l1_reach_rate", "")


def make_rate_search_result(base_name, repeat_index, rows, lossless_threshold):
    first_loss = first_loss_row(rows, lossless_threshold)
    max_pps = max(rows, key=lambda r: metric_float(r, "avg_rx_pps", 0.0), default=None)
    max_l1 = max(rows, key=lambda r: metric_float(r, "avg_rx_l1_bps", 0.0), default=None)
    result = {
        "phase": "stl_rate_search_result",
        "repeat": repeat_index,
        "test_name": f"r{repeat_index}_{base_name}_result",
        "base_test_name": base_name,
        "lossless_threshold": lossless_threshold,
        "pdr_0p01_threshold": 0.0001,
        "pdr_0p1_threshold": 0.001,
        "first_loss_percent": "",
        "first_loss_mbps": "",
        "first_loss_loss_rate": "",
        "first_loss_ierrors": "",
        "first_loss_oerrors": "",
        "first_loss_queue_full": "",
        "max_observed_rx_pps": "",
        "max_observed_l1_bps": "",
        "max_observed_l2_bps": "",
    }
    copy_best("ndr", best_row_for_threshold(rows, lossless_threshold), result)
    copy_best("pdr_0p01", best_row_for_threshold(rows, 0.0001), result)
    copy_best("pdr_0p1", best_row_for_threshold(rows, 0.001), result)
    if first_loss is not None:
        result.update({
            "first_loss_percent": first_loss.get("rate_percent", ""),
            "first_loss_mbps": first_loss.get("rate_mbps_per_active_port", ""),
            "first_loss_loss_rate": first_loss.get("loss_rate", ""),
            "first_loss_ierrors": first_loss.get("ierrors", ""),
            "first_loss_oerrors": first_loss.get("oerrors", ""),
            "first_loss_queue_full": first_loss.get("queue_full", ""),
        })
    if max_pps is not None:
        result["max_observed_rx_pps"] = max_pps.get("avg_rx_pps", "")
    if max_l1 is not None:
        result["max_observed_l1_bps"] = max_l1.get("avg_rx_l1_bps", "")
        result["max_observed_l2_bps"] = max_l1.get("avg_rx_l2_bps", "")
    return result


def build_refine_steps(rows, lossless_threshold, step):
    if step <= 0:
        return []
    sorted_rows = sorted(rows, key=lambda r: metric_float(r, "rate_percent", 0.0))
    last_ok = None
    first_bad = None
    for row in sorted_rows:
        if row_passes(row, lossless_threshold):
            last_ok = row
        else:
            first_bad = row
            break
    if last_ok is None or first_bad is None:
        return []
    low = metric_float(last_ok, "rate_percent", 0.0)
    high = metric_float(first_bad, "rate_percent", 0.0)
    steps = []
    current = low + step
    while current < high:
        steps.append(round(current, 6))
        current += step
    return steps


def run_stl_tests(target, out_dir, duration, repeat, rate_search, rate_steps, rate_mbps_steps,
                  refine_step, lossless_threshold, stop_on_first_loss, single_load_percent):
    add_trex_api_path()
    from trex.stl.api import STLClient, Ether, IP, UDP, Raw, STLStream, STLPktBuilder, STLTXCont

    lan_port = target["lan_port"]
    wan_port = target["wan_port"]
    ports = [lan_port, wan_port]
    line_rate_gbps = float(target["line_rate_gbps"])

    def pkt_up(frame_size):
        return Ether()/IP(src=target["lan_ip"], dst=target["wan_ip"])/UDP(sport=UDP_PORT, dport=UDP_PORT)/Raw(load=payload_for_l2_size(frame_size))

    def pkt_down(frame_size):
        return Ether()/IP(src=target["wan_ip"], dst=target["router_wan_ip"])/UDP(sport=UDP_PORT, dport=UDP_PORT)/Raw(load=payload_for_l2_size(frame_size))

    def make_stream(name, direction, frame_size, pps=None, bps_l1=None):
        if direction == "up":
            packet = pkt_up(frame_size)
        elif direction == "down":
            packet = pkt_down(frame_size)
        else:
            raise ValueError(direction)
        if bps_l1 is not None:
            mode = STLTXCont(bps_L1=bps_l1)
        elif pps is not None:
            mode = STLTXCont(pps=pps)
        else:
            mode = STLTXCont()
        return STLStream(name=name, packet=STLPktBuilder(pkt=packet), mode=mode)

    def safe_stat(stats, port, key, default=0):
        try:
            return stats[port].get(key, default)
        except Exception:
            return default

    def calc_summary(test, stats, duration_sec):
        direction = test["direction"]
        frame_size = test["frame_size"]
        if direction == "up":
            tx_ports, rx_ports, active_directions = [lan_port], [wan_port], 1
        elif direction == "down":
            tx_ports, rx_ports, active_directions = [wan_port], [lan_port], 1
        elif direction == "bidir":
            tx_ports, rx_ports, active_directions = [lan_port, wan_port], [lan_port, wan_port], 2
        else:
            raise ValueError(direction)
        tx_pkts = sum(safe_stat(stats, p, "opackets") for p in tx_ports)
        rx_pkts = sum(safe_stat(stats, p, "ipackets") for p in rx_ports)
        tx_bytes = sum(safe_stat(stats, p, "obytes") for p in tx_ports)
        rx_bytes = sum(safe_stat(stats, p, "ibytes") for p in rx_ports)
        loss_pkts = max(0, tx_pkts - rx_pkts)
        loss_rate = (loss_pkts / tx_pkts) if tx_pkts else 0.0
        avg_tx_pps = tx_pkts / duration_sec if duration_sec > 0 else 0.0
        avg_rx_pps = rx_pkts / duration_sec if duration_sec > 0 else 0.0
        avg_rx_l2_bps = (rx_bytes * 8) / duration_sec if duration_sec > 0 else 0.0
        avg_rx_l1_bps = avg_rx_pps * (frame_size + L1_OVERHEAD_BYTES) * 8
        theoretical_pps = theoretical_pps_per_port(line_rate_gbps, frame_size) * active_directions
        theoretical_l1_bps = line_rate_gbps * 1_000_000_000 * active_directions
        try:
            queue_full = stats["global"].get("queue_full", 0)
        except Exception:
            queue_full = 0
        return {
            "phase": "stl_rate_search" if rate_search else "stl_single_load",
            "test_name": test["name"],
            "base_test_name": test.get("base_name", test["name"]),
            "repeat": test.get("repeat", ""),
            "direction": direction,
            "frame_size": frame_size,
            "duration_sec": duration_sec,
            "rate_percent": test.get("rate_percent", ""),
            "rate_mbps_per_active_port": test.get("rate_mbps", ""),
            "bps_l1_per_active_port": test.get("bps_l1", ""),
            "line_rate_gbps": line_rate_gbps,
            "tx_pkts": tx_pkts,
            "rx_pkts": rx_pkts,
            "tx_bytes": tx_bytes,
            "rx_bytes": rx_bytes,
            "loss_pkts": loss_pkts,
            "loss_rate": loss_rate,
            "avg_tx_pps": avg_tx_pps,
            "avg_rx_pps": avg_rx_pps,
            "avg_rx_l2_bps": avg_rx_l2_bps,
            "avg_rx_l1_bps": avg_rx_l1_bps,
            "theoretical_pps": theoretical_pps,
            "pps_reach_rate": avg_rx_pps / theoretical_pps if theoretical_pps else 0.0,
            "theoretical_l1_bps": theoretical_l1_bps,
            "l1_reach_rate": avg_rx_l1_bps / theoretical_l1_bps if theoretical_l1_bps else 0.0,
            "queue_full": queue_full,
            "ierrors": sum(safe_stat(stats, p, "ierrors") for p in ports),
            "oerrors": sum(safe_stat(stats, p, "oerrors") for p in ports),
        }

    def try_arp(client):
        try:
            client.set_service_mode(ports=ports, enabled=True)
            client.arp(ports=ports)
        except Exception:
            print("WARNING: automatic ARP failed. Check link, IP/gateway, and router ARP behavior.")
            print(traceback.format_exc())
        finally:
            try:
                client.set_service_mode(ports=ports, enabled=False)
            except Exception:
                pass

    def warm_nat(client, seconds=3):
        client.reset(ports=ports)
        client.clear_stats()
        client.remove_all_streams(ports=ports)
        client.add_streams(make_stream("warm_nat_up", "up", FRAME_64, pps=1000), ports=[lan_port])
        client.start(ports=[lan_port], mult="1", duration=seconds)
        client.wait_on_traffic(ports=[lan_port], timeout=seconds + 10)
        client.remove_all_streams(ports=ports)

    def run_one_test(client, test, raw_writer):
        name = test["name"]
        direction = test["direction"]
        frame_size = test["frame_size"]
        bps_l1 = test.get("bps_l1", None)
        rate_mbps = test.get("rate_mbps", "")
        rate_percent = test.get("rate_percent", "")
        print(f"=== STL {name}: direction={direction}, frame={frame_size}, rate={fmt_rate_value(rate_mbps)}Mbps ({rate_percent:g}%), bps_L1={bps_l1}, duration={duration}s ===")
        if direction in ("down", "bidir"):
            warm_nat(client, seconds=3)
        client.reset(ports=ports)
        client.clear_stats()
        client.remove_all_streams(ports=ports)
        if direction == "up":
            client.add_streams(make_stream(f"{name}_up", "up", frame_size, bps_l1=bps_l1), ports=[lan_port])
            active_ports = [lan_port]
        elif direction == "down":
            # Keep NAT binding alive during downlink. This 1pps stream is not counted in downlink summary.
            client.add_streams(make_stream(f"{name}_keepalive", "up", FRAME_64, pps=1), ports=[lan_port])
            client.add_streams(make_stream(f"{name}_down", "down", frame_size, bps_l1=bps_l1), ports=[wan_port])
            active_ports = [lan_port, wan_port]
        elif direction == "bidir":
            client.add_streams(make_stream(f"{name}_up", "up", frame_size, bps_l1=bps_l1), ports=[lan_port])
            client.add_streams(make_stream(f"{name}_down", "down", frame_size, bps_l1=bps_l1), ports=[wan_port])
            active_ports = [lan_port, wan_port]
        else:
            raise ValueError(direction)
        time.sleep(0.2)
        baseline_stats = client.get_stats(ports=ports, sync_now=True)
        start_time = time.time()
        client.start(ports=active_ports, mult="1", duration=duration)
        while True:
            elapsed = time.time() - start_time
            stats = client.get_stats(ports=ports, sync_now=True)
            write_raw_stats(raw_writer, "stl", name, elapsed, stats)
            if elapsed >= duration:
                break
            time.sleep(RAW_INTERVAL_SEC)
        client.wait_on_traffic(ports=active_ports, timeout=duration + 10)
        final_stats = client.get_stats(ports=ports, sync_now=True)
        delta_stats = subtract_stats(baseline_stats, final_stats)
        try:
            client.stop(ports=active_ports)
        except Exception:
            pass
        client.remove_all_streams(ports=ports)
        return calc_summary(test, delta_stats, duration)

    base_tests = [
        {"base_name": "large_down_throughput", "direction": "down", "frame_size": FRAME_LARGE},
        {"base_name": "large_up_throughput", "direction": "up", "frame_size": FRAME_LARGE},
        {"base_name": "large_bidir_throughput", "direction": "bidir", "frame_size": FRAME_LARGE},
        {"base_name": "udp64_down_pps", "direction": "down", "frame_size": FRAME_64},
        {"base_name": "udp64_up_pps", "direction": "up", "frame_size": FRAME_64},
        {"base_name": "udp64_bidir_pps", "direction": "bidir", "frame_size": FRAME_64},
    ]

    raw_csv = out_dir / "stl_raw.csv"
    summaries = []
    client = STLClient(server="127.0.0.1")
    client.connect()
    try:
        client.acquire(ports=ports, force=True)
        client.reset(ports=ports)
        try_arp(client)
        with open(raw_csv, "w", newline="") as raw_file:
            raw_writer = csv.writer(raw_file)
            raw_writer.writerow(["ts_local", "elapsed_sec", "phase", "test_name", "metric", "value"])
            for r in range(1, repeat + 1):
                for base in base_tests:
                    if rate_search:
                        step_rows = []
                        for rate_point in build_rate_points(line_rate_gbps, rate_steps=rate_steps, rate_mbps_steps=rate_mbps_steps):
                            pct = rate_point["rate_percent"]
                            mbps = rate_point["rate_mbps"]
                            bps_l1 = rate_point["bps_l1"]
                            label = rate_point["label"]
                            test = {
                                "name": f"r{r}_{base['base_name']}_{label}",
                                "base_name": base["base_name"],
                                "repeat": r,
                                "direction": base["direction"],
                                "frame_size": base["frame_size"],
                                "duration": duration,
                                "rate_percent": pct,
                                "rate_mbps": mbps,
                                "bps_l1": bps_l1,
                            }
                            try:
                                row = run_one_test(client, test, raw_writer)
                                summaries.append(row)
                                step_rows.append(row)
                                if stop_on_first_loss and not row_passes(row, lossless_threshold):
                                    print(f"Stopping {base['base_name']} at {fmt_rate_value(mbps)}Mbps ({pct:g}%) because loss/error was detected.")
                                    break
                            except Exception:
                                error_row = {
                                    "phase": "stl_rate_search",
                                    "repeat": r,
                                    "test_name": test["name"],
                                    "base_test_name": base["base_name"],
                                    "rate_percent": pct,
                                    "rate_mbps_per_active_port": mbps,
                                    "error": traceback.format_exc(),
                                }
                                print(error_row["error"])
                                summaries.append(error_row)
                            raw_file.flush()
                            time.sleep(5)
                        # Optional refinement between last NDR and first lossy point.
                        for pct in build_refine_steps(step_rows, lossless_threshold, refine_step):
                            bps_l1 = line_rate_gbps * 1_000_000_000 * (pct / 100.0)
                            mbps = (line_rate_gbps * 1000.0) * (pct / 100.0)
                            test = {
                                "name": f"r{r}_{base['base_name']}_{pct:g}pct_refine",
                                "base_name": base["base_name"],
                                "repeat": r,
                                "direction": base["direction"],
                                "frame_size": base["frame_size"],
                                "duration": duration,
                                "rate_percent": pct,
                                "rate_mbps": mbps,
                                "bps_l1": bps_l1,
                            }
                            try:
                                row = run_one_test(client, test, raw_writer)
                                summaries.append(row)
                                step_rows.append(row)
                            except Exception:
                                error_row = {
                                    "phase": "stl_rate_search_refine",
                                    "repeat": r,
                                    "test_name": test["name"],
                                    "base_test_name": base["base_name"],
                                    "rate_percent": pct,
                                    "rate_mbps_per_active_port": mbps,
                                    "error": traceback.format_exc(),
                                }
                                print(error_row["error"])
                                summaries.append(error_row)
                            raw_file.flush()
                            time.sleep(5)
                        summaries.append(make_rate_search_result(base["base_name"], r, step_rows, lossless_threshold))
                    else:
                        pct = float(single_load_percent)
                        mbps = (line_rate_gbps * 1000.0) * (pct / 100.0)
                        bps_l1 = line_rate_gbps * 1_000_000_000 * (pct / 100.0)
                        test = {
                            "name": f"r{r}_{base['base_name']}_{pct:g}pct",
                            "base_name": base["base_name"],
                            "repeat": r,
                            "direction": base["direction"],
                            "frame_size": base["frame_size"],
                            "duration": duration,
                            "rate_percent": pct,
                            "rate_mbps": mbps,
                            "bps_l1": bps_l1,
                        }
                        try:
                            summaries.append(run_one_test(client, test, raw_writer))
                        except Exception:
                            error_row = {
                                "phase": "stl_single_load",
                                "repeat": r,
                                "test_name": test["name"],
                                "base_test_name": base["base_name"],
                                "rate_percent": pct,
                                "rate_mbps_per_active_port": mbps,
                                "error": traceback.format_exc(),
                            }
                            print(error_row["error"])
                            summaries.append(error_row)
                        raw_file.flush()
                        time.sleep(5)
    finally:
        try:
            client.stop(ports=ports)
        except Exception:
            pass
        try:
            client.reset(ports=ports)
        except Exception:
            pass
        client.disconnect()
    return summaries


def write_summary_csv(path, summaries):
    keys = []
    for row in summaries:
        for key in row.keys():
            if key not in keys:
                keys.append(key)
    with open(path, "w", newline="") as out_file:
        writer = csv.DictWriter(out_file, fieldnames=keys)
        writer.writeheader()
        for row in summaries:
            writer.writerow(row)


def write_metadata(out_dir, target_name, target, args):
    metadata = {
        "created_at": ts_iso(),
        "target_name": target_name,
        "target": target,
        "args": vars(args),
        "trex_dir": TREX_DIR,
        "trex_cfg_path": TREX_CFG_PATH,
        "mode": "stl_only_throughput_pps",
        "tests": [
            "large_down_throughput",
            "large_up_throughput",
            "large_bidir_throughput",
            "udp64_down_pps",
            "udp64_up_pps",
            "udp64_bidir_pps",
        ],
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Run TRex STL-only IPv4 NAT throughput/PPS benchmark.")
    parser.add_argument("--target", choices=TARGETS.keys(), default="i226_1g")
    parser.add_argument("--out-dir", default="", help="Result directory. Default: /var/trex/trex_stl_throughput_pps_<target>_<timestamp>/.")
    parser.add_argument("--duration", type=int, default=20)
    parser.add_argument("--repeat", type=int, default=1)

    parser.add_argument("--rate-search", dest="rate_search", action="store_true", help="Enable rate search. Default: enabled.")
    parser.add_argument("--stl-rate-search", dest="rate_search", action="store_true", help="Compatibility alias for --rate-search.")
    parser.add_argument("--no-rate-search", dest="rate_search", action="store_false", help="Run one fixed-load point only.")
    parser.set_defaults(rate_search=True)

    parser.add_argument("--rate-steps", type=parse_rate_steps, default=None,
                        help="Compatibility option: percent steps. If omitted, Mbps steps are used.")
    parser.add_argument("--rate-mbps-steps", "--rate-steps-mbps", dest="rate_mbps_steps",
                        type=parse_rate_mbps_steps, default=None,
                        help="L1 Mbps steps per active port. Default: 100,150,220,330,470,680,1000,1500,2200,(line rate),3300,4700,(line rate),6800,10000 up to the configured line rate.")
    parser.add_argument("--refine-step", type=float, default=0.0,
                        help="Optional refine step in percent between last NDR and first loss. 0 disables refine. Example: 1")
    parser.add_argument("--single-load-percent", type=float, default=100.0,
                        help="Used only with --no-rate-search.")
    parser.add_argument("--lossless-threshold", type=float, default=0.0,
                        help="Loss-rate threshold for NDR. Default: 0.0")
    parser.add_argument("--stop-on-first-loss", action="store_true",
                        help="Stop each coarse rate search item at first loss/error.")
    parser.add_argument("--no-install-cfg", action="store_true")
    parser.add_argument("--auto-dpdk-bind", dest="auto_dpdk_bind", action="store_true", default=True, help="Before starting TRex, delete stale benchmark namespaces and bind profile PCI devices to their DPDK driver. Enabled by default.")
    parser.add_argument("--no-auto-dpdk-bind", dest="auto_dpdk_bind", action="store_false", help="Do not attempt DPDK driver binding before starting TRex. Stale benchmark namespaces are still removed.")
    parser.add_argument("--dpdk-driver", default="", help="Override the DPDK driver used for all profile PCI devices, e.g. vfio-pci or igb_uio.")

    parser.add_argument("--interfaces", type=parse_interfaces, default=None)
    parser.add_argument("--lan-ip", default="")
    parser.add_argument("--lan-gw", default="")
    parser.add_argument("--wan-ip", default="")
    parser.add_argument("--wan-gw", default="")
    parser.add_argument("--router-wan-ip", default="")
    parser.add_argument("--line-rate-gbps", type=float, default=None)
    parser.add_argument("--cores", type=int, default=None)

    args = parser.parse_args()
    require_root()

    if args.duration <= 0:
        raise SystemExit("ERROR: --duration must be positive")
    if args.repeat <= 0:
        raise SystemExit("ERROR: --repeat must be positive")
    if args.single_load_percent <= 0 or args.single_load_percent > 100:
        raise SystemExit("ERROR: --single-load-percent must be 0 < value <= 100")
    if args.refine_step < 0:
        raise SystemExit("ERROR: --refine-step must be >= 0")

    target = merge_target_with_overrides(TARGETS[args.target], args)
    if args.rate_search and args.rate_steps is None:
        if args.rate_mbps_steps is None:
            args.rate_mbps_steps = make_default_rate_mbps_steps(target["line_rate_gbps"])
        else:
            args.rate_mbps_steps = limit_rate_mbps_steps(args.rate_mbps_steps, target["line_rate_gbps"])
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_RESULT_ROOT / f"trex_stl_throughput_pps_{args.target}_{ts_compact()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("============================================================")
    print("TRex STL-only IPv4 NAT throughput/PPS benchmark")
    print("============================================================")
    print(f"Target              : {args.target}")
    print(f"Description         : {target['description']}")
    print(f"Interfaces          : {target['interfaces']}")
    print(f"Line rate           : {target['line_rate_gbps']} Gbps")
    print(f"LAN                 : port {target['lan_port']} / {target['lan_ip']} gw {target['lan_gw']}")
    print(f"WAN                 : port {target['wan_port']} / {target['wan_ip']} gw {target['wan_gw']}")
    print(f"Router WAN IP       : {target['router_wan_ip']}")
    print(f"Rate search         : {args.rate_search}")
    if args.rate_search and args.rate_steps is None:
        print(f"Rate steps          : {[fmt_rate_value(x) for x in args.rate_mbps_steps]} Mbps")
    elif args.rate_search:
        print(f"Rate steps          : {args.rate_steps} %")
    else:
        print(f"Rate steps          : {[args.single_load_percent]} %")
    print(f"Refine step         : {args.refine_step}")
    print(f"Duration/test       : {args.duration}s")
    print(f"Repeat              : {args.repeat}")
    print(f"Output              : {out_dir}")
    print("============================================================")

    prepare_log = prepare_trex_interfaces_from_profile(
        target,
        out_dir,
        auto_dpdk_bind=args.auto_dpdk_bind,
        dpdk_driver_override=args.dpdk_driver,
    )
    print(f"TRex interface prep : {prepare_log}")

    install_trex_cfg(target, out_dir, args.no_install_cfg)
    write_metadata(out_dir, args.target, target, args)

    summaries = []
    server = TrexStlServer(cores=int(target["trex_cores"]), out_dir=out_dir)
    try:
        server.start()
        summaries.extend(
            run_stl_tests(
                target=target,
                out_dir=out_dir,
                duration=args.duration,
                repeat=args.repeat,
                rate_search=args.rate_search,
                rate_steps=args.rate_steps,
                rate_mbps_steps=args.rate_mbps_steps,
                refine_step=args.refine_step,
                lossless_threshold=args.lossless_threshold,
                stop_on_first_loss=args.stop_on_first_loss,
                single_load_percent=args.single_load_percent,
            )
        )
    finally:
        server.stop()

    summary_csv = out_dir / "summary_all.csv"
    write_summary_csv(summary_csv, summaries)

    print("============================================================")
    print("DONE")
    print("============================================================")
    print(f"Result directory : {out_dir}")
    print(f"Summary CSV      : {summary_csv}")
    print(f"Raw CSV          : {out_dir / 'stl_raw.csv'}")
    print(f"Metadata         : {out_dir / 'metadata.json'}")
    print(f"TRex server log  : {out_dir / 'trex_server_stl.log'}")


if __name__ == "__main__":
    main()
