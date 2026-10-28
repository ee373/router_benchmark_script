#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRex DS-Lite IPv4 throughput/PPS benchmark - STL only.

- No ASTF
- No latency
- Throughput and PPS only
- Mbps-based rate search is enabled by default
- NDR/PDR summary rows are written to summary_all.csv
- Every rate step performs ARP refresh, explicit-MAC packet construction,
  NAT external IPv4/port discovery, and bidirectional connectivity probes
- Bidirectional measurements report combined, uplink, and downlink statistics
- Frame sizes include Ethernet FCS; 1478 bytes carries a 1460-byte inner IPv4 packet
- Inner IPv4 and estimated encapsulated outer-line bit rates are recorded

Port mapping:
  port 0 = LAN/client side
  port 1 = WAN/server side

Typical:
  cd /opt/trex/v3.08
  sudo python3 ./tests/run_router_bench_dslite_stl_throughput_pps.py \
    --target i226_2p5g_dslite --duration 20

Single fixed-load run:
  sudo python3 ./tests/run_router_bench_dslite_stl_throughput_pps.py \
    --target i226_2p5g_dslite --no-rate-search --duration 60
"""

import argparse
import csv
import json
import os
import signal
import socket
import subprocess
import sys
import time
import traceback
import zlib
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from tests.lib.config import DSLITE_TARGETS  # type: ignore
    from tests.lib.output import prepare_details_dir  # type: ignore
    from tests.lib.rate_steps import (  # type: ignore
        format_kpps_steps,
        high_loss_streak_update,
        limit_positive_steps,
        make_e12_steps,
    )
    from tests.lib.source_ports import (  # type: ignore
        add_lan_source_port_range_argument,
        first_lan_source_port,
        format_lan_source_port_range,
    )
except Exception:
    from lib.config import DSLITE_TARGETS  # type: ignore
    from lib.output import prepare_details_dir  # type: ignore
    from lib.rate_steps import format_kpps_steps, high_loss_streak_update, limit_positive_steps, make_e12_steps  # type: ignore
    from lib.source_ports import add_lan_source_port_range_argument, first_lan_source_port, format_lan_source_port_range  # type: ignore

try:
    from tests.lib.timing import (  # type: ignore
        DEFAULT_TEST_START_DELAY_SEC,
        wait_before_test_start,
    )
except Exception:
    from lib.timing import (  # type: ignore
        DEFAULT_TEST_START_DELAY_SEC,
        wait_before_test_start,
    )

try:
    from tests.lib.preflight import run_preflight_with_optional_recovery_probe  # type: ignore
except Exception:
    from lib.preflight import run_preflight_with_optional_recovery_probe  # type: ignore

try:
    from tests.lib.common import (  # type: ignore
        DEFAULT_RESULT_ROOT,
        flatten,
        json_cell,
        ts_compact,
        ts_iso,
        write_raw_stats as _write_raw_stats,
        write_summary_csv,
    )
    from tests.lib.config import load_defaults as _load_defaults  # type: ignore
    from tests.lib.dslite import (  # type: ignore
        build_trex_target,
        estimate_wire_rates,
        is_nat_pool_address,
        max_inner_frame_size,
        theoretical_inner_pps,
        udp_payload_bytes,
        validate_dslite_profile,
    )
except Exception:
    from lib.common import (  # type: ignore
        DEFAULT_RESULT_ROOT,
        flatten,
        json_cell,
        ts_compact,
        ts_iso,
        write_raw_stats as _write_raw_stats,
        write_summary_csv,
    )
    from lib.config import load_defaults as _load_defaults  # type: ignore
    from lib.dslite import (  # type: ignore
        build_trex_target,
        estimate_wire_rates,
        is_nat_pool_address,
        max_inner_frame_size,
        theoretical_inner_pps,
        udp_payload_bytes,
        validate_dslite_profile,
    )


try:
    from tests.lib.trex import (  # type: ignore
        TREX_CFG_PATH,
        TREX_DIR,
        RAW_INTERVAL_SEC,
        DEFAULT_BENCHMARK_NAMESPACES,
        _trex_link_up_from_attr,
        add_trex_api_path,
        available_dpdk_driver_candidates,
        bind_pci_to_driver,
        bind_pci_to_driver_once,
        cleanup_benchmark_namespaces,
        delete_netns_force,
        dpdk_driver_bind_path_exists,
        install_trex_cfg,
        kill_existing_trex,
        namespace_exists,
        normalize_pci_bdf,
        parse_interfaces,
        pci_current_driver,
        pci_device_path,
        pci_unbind_current_driver,
        pci_vendor_device_ids,
        prepare_trex_interfaces_from_profile,
        profile_dpdk_driver_entry,
        profile_pci_interface_entry,
        profile_port_index,
        render_trex_cfg,
        require_root,
        run_cmd_noexcept,
        sysfs_write,
        tail_text,
        try_modprobe_driver,
        wait_rpc,
        wait_trex_ports_link_up,
    )
except Exception:
    from lib.trex import (  # type: ignore
        TREX_CFG_PATH,
        TREX_DIR,
        RAW_INTERVAL_SEC,
        DEFAULT_BENCHMARK_NAMESPACES,
        _trex_link_up_from_attr,
        add_trex_api_path,
        available_dpdk_driver_candidates,
        bind_pci_to_driver,
        bind_pci_to_driver_once,
        cleanup_benchmark_namespaces,
        delete_netns_force,
        dpdk_driver_bind_path_exists,
        install_trex_cfg,
        kill_existing_trex,
        namespace_exists,
        normalize_pci_bdf,
        parse_interfaces,
        pci_current_driver,
        pci_device_path,
        pci_unbind_current_driver,
        pci_vendor_device_ids,
        prepare_trex_interfaces_from_profile,
        profile_dpdk_driver_entry,
        profile_pci_interface_entry,
        profile_port_index,
        render_trex_cfg,
        require_root,
        run_cmd_noexcept,
        sysfs_write,
        tail_text,
        try_modprobe_driver,
        wait_rpc,
        wait_trex_ports_link_up,
    )


_STL_DEFAULTS = _load_defaults("dslite_stl")
FRAME_64 = int(_STL_DEFAULTS["frame_64_bytes"])
FRAME_LARGE = int(_STL_DEFAULTS["frame_large_bytes"])
L1_OVERHEAD_BYTES = int(_STL_DEFAULTS["l1_overhead_bytes"])
UDP_PORT = int(_STL_DEFAULTS["udp_port"])
DEFAULT_THROUGHPUT_START_MBPS = float(_STL_DEFAULTS["throughput_start_mbps"])
DEFAULT_PPS_START = float(_STL_DEFAULTS["pps_start"])
DEFAULT_HIGH_LOSS_THRESHOLD = float(_STL_DEFAULTS["high_loss_threshold"])
DEFAULT_HIGH_LOSS_CONSECUTIVE = int(_STL_DEFAULTS["high_loss_consecutive"])
DEFAULT_RECOVERY_PROBE_INTERVAL_SEC = float(_STL_DEFAULTS["recovery_probe_interval_sec"])
DEFAULT_RECOVERY_SUCCESS_CONSECUTIVE = int(_STL_DEFAULTS["recovery_success_consecutive"])
DEFAULT_RECOVERY_TIMEOUT_SEC = float(_STL_DEFAULTS["recovery_timeout_sec"])


def write_raw_stats(writer, phase, test_name, elapsed, stats):
    return _write_raw_stats(writer, phase, test_name, elapsed, stats)


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


def parse_pps_steps(value):
    try:
        steps = [float(x.strip().replace("_", "")) for x in value.split(",") if x.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--pps-steps must be comma-separated numbers, e.g. 1000,1200,1500"
        ) from exc
    if not steps:
        raise argparse.ArgumentTypeError("--pps-steps must not be empty")
    if any(step <= 0 for step in steps):
        raise argparse.ArgumentTypeError("--pps-steps values must be positive")
    return steps


def fmt_rate_value(value):
    value = float(value)
    if value.is_integer():
        return str(int(value))
    return f"{value:g}"


def make_default_rate_mbps_steps(line_rate_gbps):
    line_rate_mbps = float(line_rate_gbps) * 1000.0
    return make_e12_steps(DEFAULT_THROUGHPUT_START_MBPS, line_rate_mbps)


def limit_rate_mbps_steps(steps, line_rate_gbps):
    line_rate_mbps = float(line_rate_gbps) * 1000.0
    return limit_positive_steps(steps, line_rate_mbps, "Mbps rate")


def make_default_pps_steps(line_rate_gbps, tunnel_overhead_bytes, frame_size=FRAME_64):
    maximum = theoretical_pps_per_port(line_rate_gbps, frame_size, tunnel_overhead_bytes)
    return make_e12_steps(DEFAULT_PPS_START, maximum)


def limit_pps_steps(steps, line_rate_gbps, tunnel_overhead_bytes, frame_size=FRAME_64):
    maximum = theoretical_pps_per_port(line_rate_gbps, frame_size, tunnel_overhead_bytes)
    return limit_positive_steps(steps, maximum, "PPS rate")


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


def build_pps_rate_points(line_rate_gbps, frame_size, tunnel_overhead_bytes,
                          rate_steps=None, pps_steps=None):
    maximum_pps = theoretical_pps_per_port(
        line_rate_gbps, frame_size, tunnel_overhead_bytes
    )
    if pps_steps is not None:
        values = limit_pps_steps(
            pps_steps, line_rate_gbps, tunnel_overhead_bytes, frame_size
        )
    else:
        values = [maximum_pps * (float(pct) / 100.0) for pct in sorted(set(rate_steps))]
    points = []
    for pps in values:
        inner_bps = estimate_wire_rates(
            pps, frame_size, L1_OVERHEAD_BYTES, tunnel_overhead_bytes
        )["inner_ipv4_bps"]
        points.append({
            "label": f"{fmt_rate_value(pps)}pps",
            "rate_pps": pps,
            "rate_mbps": inner_bps / 1_000_000.0,
            "rate_percent": (pps / maximum_pps) * 100.0,
            "bps_l1": inner_bps,
        })
    return points


def add_tunnel_capacity_rate_point(
    points,
    line_rate_gbps,
    frame_size,
    tunnel_overhead_bytes,
):
    """Add the calculated inner-rate ceiling to the default coarse grid."""
    capacity_pps = theoretical_pps_per_port(
        line_rate_gbps,
        frame_size,
        tunnel_overhead_bytes,
    )
    capacity_bps = estimate_wire_rates(
        capacity_pps,
        frame_size,
        L1_OVERHEAD_BYTES,
        tunnel_overhead_bytes,
    )["inner_ipv4_bps"]
    capacity_mbps = capacity_bps / 1_000_000.0
    if any(abs(float(point["rate_mbps"]) - capacity_mbps) <= 1e-6 for point in points):
        return list(points)
    line_rate_mbps = float(line_rate_gbps) * 1000.0
    augmented = list(points)
    augmented.append(
        {
            "label": f"tunnelcap_{fmt_rate_value(round(capacity_mbps, 6))}mbps",
            "rate_mbps": capacity_mbps,
            "rate_percent": (capacity_mbps / line_rate_mbps) * 100.0,
            "bps_l1": capacity_bps,
        }
    )
    return sorted(augmented, key=lambda point: float(point["rate_mbps"]))


class TrexStlServer:
    def __init__(self, cores, out_dir):
        self.cores = cores
        self.out_dir = out_dir
        self.proc = None
        self.log_path = out_dir / "trex_server_stl.log"
        self.log_file = None

    def start(self):
        kill_existing_trex()
        cmd = ["./t-rex-64", "-i", "-c", str(self.cores), "--arp-refresh-period", "60"]
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
    payload_len = udp_payload_bytes(frame_size)
    return b"X" * payload_len


def theoretical_pps_per_port(line_rate_gbps, frame_size, tunnel_overhead_bytes):
    return theoretical_inner_pps(
        line_rate_gbps,
        frame_size,
        L1_OVERHEAD_BYTES,
        tunnel_overhead_bytes,
    )


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
    if metric_int(row, "preflight_ok", 1) != 1:
        return False
    loss_keys = ["loss_rate"]
    if metric_int(row, "up_active", 0) == 1:
        loss_keys.append("up_loss_rate")
    if metric_int(row, "down_active", 0) == 1:
        loss_keys.append("down_loss_rate")
    return (
        all(metric_float(row, key, 1.0) <= loss_threshold for key in loss_keys)
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
    for suffix in (
        "percent",
        "mbps",
        "rx_pps",
        "l1_bps",
        "l2_bps",
        "inner_ipv4_bps",
        "outer_line_estimated_bps",
        "loss_rate",
        "worst_direction_loss_rate",
        "pps_reach_rate",
        "l1_reach_rate",
        "up_rx_pps",
        "up_l1_bps",
        "up_loss_rate",
        "down_rx_pps",
        "down_l1_bps",
        "down_loss_rate",
    ):
        out[f"{prefix}_{suffix}"] = ""
    if row is None:
        return
    out[f"{prefix}_percent"] = row.get("rate_percent", "")
    out[f"{prefix}_mbps"] = row.get("rate_mbps_per_active_port", "")
    out[f"{prefix}_rx_pps"] = row.get("avg_rx_pps", "")
    out[f"{prefix}_l1_bps"] = row.get("avg_rx_l1_bps", "")
    out[f"{prefix}_l2_bps"] = row.get("avg_rx_l2_bps", "")
    out[f"{prefix}_inner_ipv4_bps"] = row.get("inner_ipv4_bps", "")
    out[f"{prefix}_outer_line_estimated_bps"] = row.get("outer_line_estimated_bps", "")
    out[f"{prefix}_loss_rate"] = row.get("loss_rate", "")
    out[f"{prefix}_worst_direction_loss_rate"] = row.get("worst_direction_loss_rate", "")
    out[f"{prefix}_pps_reach_rate"] = row.get("pps_reach_rate", "")
    out[f"{prefix}_l1_reach_rate"] = row.get("l1_reach_rate", "")
    out[f"{prefix}_up_rx_pps"] = row.get("up_avg_rx_pps", "")
    out[f"{prefix}_up_l1_bps"] = row.get("up_avg_rx_l1_bps", "")
    out[f"{prefix}_up_loss_rate"] = row.get("up_loss_rate", "")
    out[f"{prefix}_down_rx_pps"] = row.get("down_avg_rx_pps", "")
    out[f"{prefix}_down_l1_bps"] = row.get("down_avg_rx_l1_bps", "")
    out[f"{prefix}_down_loss_rate"] = row.get("down_loss_rate", "")


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
        "first_loss_worst_direction_loss_rate": "",
        "first_loss_up_loss_rate": "",
        "first_loss_down_loss_rate": "",
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
            "first_loss_worst_direction_loss_rate": first_loss.get("worst_direction_loss_rate", ""),
            "first_loss_up_loss_rate": first_loss.get("up_loss_rate", ""),
            "first_loss_down_loss_rate": first_loss.get("down_loss_rate", ""),
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
                  pps_steps, refine_step, lossless_threshold, stop_on_first_loss,
                  high_loss_threshold, high_loss_consecutive, recovery_probe_interval_sec,
                  recovery_success_consecutive, recovery_timeout_sec,
                  lan_source_port_range, single_load_percent,
                  test_start_delay_sec=DEFAULT_TEST_START_DELAY_SEC,
                  recovery_probe_enabled=False):
    add_trex_api_path()
    from trex.stl.api import (
        STLClient,
        Ether,
        IP,
        UDP,
        Raw,
        STLStream,
        STLPktBuilder,
        STLTXCont,
        STLTXSingleBurst,
    )

    lan_port = target["lan_port"]
    wan_port = target["wan_port"]
    ports = [lan_port, wan_port]
    line_rate_gbps = float(target["line_rate_gbps"])
    tunnel_overhead_bytes = int(target["tunnel_overhead_bytes"])
    lan_source_port = first_lan_source_port(lan_source_port_range, UDP_PORT)

    class StepPreflightError(RuntimeError):
        pass

    preflight_fields = [
        "ts_local",
        "test_name",
        "status",
        "lan_src_mac",
        "lan_router_mac",
        "wan_src_mac",
        "wan_router_mac",
        "warm_nat_internal_udp_port",
        "warm_nat_external_ipv4",
        "warm_nat_external_udp_port",
        "warm_nat_tx_pkts",
        "warm_nat_wan_rx_pkts",
        "warm_nat_capture_matches",
        "probe_up_tx_pkts",
        "probe_up_wan_rx_pkts",
        "probe_up_capture_matches",
        "probe_up_success_rate",
        "probe_up_external_ipv4",
        "probe_up_external_udp_port",
        "nat_ipv4_changed_after_warm",
        "nat_port_changed_after_warm",
        "probe_down_tx_pkts",
        "probe_down_lan_rx_pkts",
        "probe_down_capture_matches",
        "probe_down_success_rate",
        "detail",
    ]
    recovery_fields = [
        "ts_local",
        "test_name",
        "attempt",
        "status",
        "consecutive_successes",
        "required_consecutive_successes",
        "elapsed_sec",
        "recovery_complete",
        "detail",
    ]

    def payload_with_marker(prefix, test_name, frame_size):
        payload_len = udp_payload_bytes(frame_size)
        if payload_len < 8:
            raise ValueError(f"frame_size too small for diagnostic marker: {frame_size}")
        digest = zlib.crc32(test_name.encode("utf-8")) & 0xFFFFFFFF
        marker = prefix + digest.to_bytes(4, "big")
        payload = marker + (b"_" * max(0, payload_len - len(marker)))
        return marker, payload[:payload_len]

    def normalize_mac(value):
        value = str(value or "").strip().lower().replace("-", ":")
        parts = value.split(":")
        if len(parts) != 6:
            return ""
        try:
            normalized = ":".join(f"{int(part, 16):02x}" for part in parts)
        except ValueError:
            return ""
        if normalized in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
            return ""
        return normalized

    def pkt_up(frame_size, mac_ctx, udp_sport=None, udp_dport=UDP_PORT, payload=None):
        if payload is None:
            payload = payload_for_l2_size(frame_size)
        if udp_sport is None:
            udp_sport = lan_source_port
        return (
            Ether(src=mac_ctx["lan_src_mac"], dst=mac_ctx["lan_router_mac"])
            / IP(src=target["lan_ip"], dst=target["server_ipv4"])
            / UDP(sport=int(udp_sport), dport=int(udp_dport))
            / Raw(load=payload)
        )

    def pkt_down(
        frame_size,
        mac_ctx,
        nat_external_ipv4,
        nat_external_port,
        udp_sport=UDP_PORT,
        payload=None,
    ):
        if payload is None:
            payload = payload_for_l2_size(frame_size)
        return (
            Ether(src=mac_ctx["wan_src_mac"], dst=mac_ctx["wan_router_mac"])
            / IP(src=target["server_ipv4"], dst=nat_external_ipv4)
            / UDP(sport=int(udp_sport), dport=int(nat_external_port))
            / Raw(load=payload)
        )

    def make_stream(
        name,
        direction,
        frame_size,
        mac_ctx,
        nat_external_ipv4=None,
        nat_external_port=None,
        pps=None,
        bps_l1=None,
        total_pkts=None,
        payload=None,
    ):
        if direction == "up":
            packet = pkt_up(frame_size, mac_ctx, payload=payload)
        elif direction == "down":
            if nat_external_ipv4 is None or nat_external_port is None:
                raise ValueError(
                    "nat_external_ipv4 and nat_external_port are required for downlink streams"
                )
            packet = pkt_down(
                frame_size,
                mac_ctx,
                nat_external_ipv4,
                nat_external_port,
                payload=payload,
            )
        else:
            raise ValueError(direction)

        if total_pkts is not None:
            mode = STLTXSingleBurst(total_pkts=int(total_pkts), pps=float(pps or 1000))
        elif bps_l1 is not None:
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

    def stop_and_remove_streams(client):
        try:
            client.stop(ports=ports)
        except Exception:
            pass
        try:
            client.remove_all_streams(ports=ports)
        except Exception:
            pass

    def parse_capture(captured, expected_port, marker):
        parsed = []
        for item in captured:
            try:
                if int(item.get("port", -1)) != int(expected_port):
                    continue
                packet = Ether(item["binary"])
                if IP not in packet or UDP not in packet:
                    continue
                udp_payload = bytes(packet[UDP].payload)
                if not udp_payload.startswith(marker):
                    continue
                parsed.append(packet)
            except Exception:
                continue
        return parsed

    def run_captured_stream(client, stream, tx_port, rx_port, capture_limit,
                            duration_sec=None, timeout_sec=15):
        stop_and_remove_streams(client)
        client.clear_stats(ports=ports)
        captured = []
        capture_id = None
        service_enabled = False
        primary_error = None
        capture_error = None
        try:
            client.set_service_mode(ports=[rx_port], enabled=True)
            service_enabled = True
            capture = client.start_capture(
                rx_ports=[rx_port],
                limit=int(capture_limit),
                mode="fixed",
                bpf_filter="udp",
            )
            capture_id = capture["id"]
            client.add_streams(stream, ports=[tx_port])
            time.sleep(0.1)
            baseline_stats = client.get_stats(ports=ports, sync_now=True)
            if duration_sec is None:
                client.start(ports=[tx_port], mult="1")
                client.wait_on_traffic(ports=[tx_port], timeout=timeout_sec)
            else:
                client.start(ports=[tx_port], mult="1", duration=duration_sec)
                client.wait_on_traffic(ports=[tx_port], timeout=duration_sec + timeout_sec)
            final_stats = client.get_stats(ports=ports, sync_now=True)
            delta_stats = subtract_stats(baseline_stats, final_stats)
        except Exception as exc:
            primary_error = exc
            raise
        finally:
            try:
                client.stop(ports=[tx_port])
            except Exception:
                pass
            try:
                client.remove_all_streams(ports=ports)
            except Exception:
                pass
            if capture_id is not None:
                try:
                    client.stop_capture(capture_id, captured)
                except Exception as exc:
                    capture_error = exc
            if service_enabled:
                try:
                    client.set_service_mode(ports=[rx_port], enabled=False)
                except Exception:
                    pass
            if capture_error is not None and primary_error is None:
                raise capture_error
        return delta_stats, captured

    def resolve_step_macs(client):
        wait_trex_ports_link_up(client, ports)
        client.set_service_mode(ports=ports, enabled=True)
        try:
            client.arp(ports=ports, retries=2)
        finally:
            try:
                client.set_service_mode(ports=ports, enabled=False)
            except Exception:
                pass

        mac_ctx = {}
        for side, port in (("lan", lan_port), ("wan", wan_port)):
            layer_cfg = client.get_port(port).get_layer_cfg()
            ether_cfg = layer_cfg.get("ether", {})
            ipv4_cfg = layer_cfg.get("ipv4", {})
            src_mac = normalize_mac(ether_cfg.get("src"))
            router_mac = normalize_mac(ether_cfg.get("dst"))
            if ipv4_cfg.get("state") != "resolved" or not src_mac or not router_mac:
                raise RuntimeError(
                    f"ARP resolution failed on {side} port {port}: "
                    f"ipv4_state={ipv4_cfg.get('state')} src_mac={src_mac or '(invalid)'} "
                    f"router_mac={router_mac or '(invalid)'}"
                )
            mac_ctx[f"{side}_src_mac"] = src_mac
            mac_ctx[f"{side}_router_mac"] = router_mac
        return mac_ctx

    def validate_wan_capture_macs(packets, mac_ctx, phase):
        router_macs = {normalize_mac(packet[Ether].src) for packet in packets}
        dst_macs = {normalize_mac(packet[Ether].dst) for packet in packets}
        router_macs.discard("")
        dst_macs.discard("")
        if len(router_macs) != 1:
            raise RuntimeError(f"{phase}: captured packets contain inconsistent WAN router MACs: {sorted(router_macs)}")
        observed_router_mac = next(iter(router_macs))
        if observed_router_mac != mac_ctx["wan_router_mac"]:
            raise RuntimeError(
                f"{phase}: WAN router MAC differs from ARP result: "
                f"arp={mac_ctx['wan_router_mac']} observed={observed_router_mac}"
            )
        if dst_macs != {mac_ctx["wan_src_mac"]}:
            raise RuntimeError(
                f"{phase}: captured WAN destination MAC differs from TRex WAN MAC: "
                f"expected={mac_ctx['wan_src_mac']} observed={sorted(dst_macs)}"
            )

    def warm_nat_and_discover_mapping(client, test_name, mac_ctx):
        marker, payload = payload_with_marker(b"RBWN", test_name, FRAME_64)
        stream = make_stream(
            f"{test_name}_warm_nat",
            "up",
            FRAME_64,
            mac_ctx,
            pps=1000,
            payload=payload,
        )
        stats, captured = run_captured_stream(
            client,
            stream,
            tx_port=lan_port,
            rx_port=wan_port,
            capture_limit=256,
            duration_sec=3,
            timeout_sec=10,
        )
        matched = []
        for packet in parse_capture(captured, wan_port, marker):
            if (
                is_nat_pool_address(packet[IP].src, target["nat_pool_cidr"])
                and packet[IP].dst == target["server_ipv4"]
                and int(packet[UDP].dport) == UDP_PORT
            ):
                matched.append(packet)

        tx_pkts = safe_stat(stats, lan_port, "opackets")
        wan_rx_pkts = safe_stat(stats, wan_port, "ipackets")
        nat_mappings = {
            (str(packet[IP].src), int(packet[UDP].sport))
            for packet in matched
        }
        if tx_pkts <= 0 or not matched:
            raise RuntimeError(
                f"warm_nat failed: tx_pkts={tx_pkts}, wan_rx_pkts={wan_rx_pkts}, "
                f"matching_capture_packets={len(matched)}"
            )
        if len(nat_mappings) != 1:
            raise RuntimeError(
                "warm_nat returned multiple or no translated IPv4/UDP mappings: "
                f"{sorted(nat_mappings)}"
            )
        validate_wan_capture_macs(matched, mac_ctx, "warm_nat")
        nat_external_ipv4, nat_external_udp_port = next(iter(nat_mappings))
        return {
            "nat_external_ipv4": nat_external_ipv4,
            "nat_external_udp_port": nat_external_udp_port,
            "tx_pkts": tx_pkts,
            "wan_rx_pkts": wan_rx_pkts,
            "capture_matches": len(matched),
        }

    def probe_up(client, test_name, mac_ctx):
        probe_packets = 32
        marker, payload = payload_with_marker(b"RBUP", test_name, FRAME_64)
        stream = make_stream(
            f"{test_name}_probe_up",
            "up",
            FRAME_64,
            mac_ctx,
            pps=1000,
            total_pkts=probe_packets,
            payload=payload,
        )
        stats, captured = run_captured_stream(
            client,
            stream,
            tx_port=lan_port,
            rx_port=wan_port,
            capture_limit=probe_packets * 4,
            duration_sec=None,
            timeout_sec=10,
        )
        matched = []
        for packet in parse_capture(captured, wan_port, marker):
            if (
                is_nat_pool_address(packet[IP].src, target["nat_pool_cidr"])
                and packet[IP].dst == target["server_ipv4"]
                and int(packet[UDP].dport) == UDP_PORT
            ):
                matched.append(packet)
        tx_pkts = safe_stat(stats, lan_port, "opackets")
        wan_rx_pkts = safe_stat(stats, wan_port, "ipackets")
        nat_mappings = {
            (str(packet[IP].src), int(packet[UDP].sport))
            for packet in matched
        }
        success_rate = len(matched) / probe_packets
        minimum_matches = (probe_packets * 9 + 9) // 10
        if len(matched) < minimum_matches:
            raise RuntimeError(
                f"up probe failed: sent={probe_packets}, tx_pkts={tx_pkts}, "
                f"wan_rx_pkts={wan_rx_pkts}, matching_capture_packets={len(matched)}"
            )
        if len(nat_mappings) != 1:
            raise RuntimeError(
                "up probe returned multiple or no translated IPv4/UDP mappings: "
                f"{sorted(nat_mappings)}"
            )
        validate_wan_capture_macs(matched, mac_ctx, "up probe")
        nat_external_ipv4, nat_external_udp_port = next(iter(nat_mappings))
        return {
            "nat_external_ipv4": nat_external_ipv4,
            "nat_external_udp_port": nat_external_udp_port,
            "tx_pkts": tx_pkts,
            "wan_rx_pkts": wan_rx_pkts,
            "capture_matches": len(matched),
            "success_rate": success_rate,
        }

    def probe_down(client, test_name, mac_ctx, nat_external_ipv4, nat_external_port):
        probe_packets = 32
        marker, payload = payload_with_marker(b"RBDN", test_name, FRAME_64)
        stream = make_stream(
            f"{test_name}_probe_down",
            "down",
            FRAME_64,
            mac_ctx,
            nat_external_ipv4=nat_external_ipv4,
            nat_external_port=nat_external_port,
            pps=1000,
            total_pkts=probe_packets,
            payload=payload,
        )
        stats, captured = run_captured_stream(
            client,
            stream,
            tx_port=wan_port,
            rx_port=lan_port,
            capture_limit=probe_packets * 4,
            duration_sec=None,
            timeout_sec=10,
        )
        matched = []
        for packet in parse_capture(captured, lan_port, marker):
            if (
                packet[IP].src == target["server_ipv4"]
                and packet[IP].dst == target["lan_ip"]
                and int(packet[UDP].sport) == UDP_PORT
                and int(packet[UDP].dport) == lan_source_port
            ):
                matched.append(packet)
        tx_pkts = safe_stat(stats, wan_port, "opackets")
        lan_rx_pkts = safe_stat(stats, lan_port, "ipackets")
        success_rate = len(matched) / probe_packets
        minimum_matches = (probe_packets * 9 + 9) // 10
        if len(matched) < minimum_matches:
            raise RuntimeError(
                f"down probe failed: sent={probe_packets}, tx_pkts={tx_pkts}, "
                f"lan_rx_pkts={lan_rx_pkts}, matching_capture_packets={len(matched)}, "
                f"nat_mapping={nat_external_ipv4}:{nat_external_port}"
            )
        src_macs = {normalize_mac(packet[Ether].src) for packet in matched}
        dst_macs = {normalize_mac(packet[Ether].dst) for packet in matched}
        src_macs.discard("")
        dst_macs.discard("")
        if src_macs != {mac_ctx["lan_router_mac"]}:
            raise RuntimeError(
                f"down probe: LAN router MAC differs from ARP result: "
                f"expected={mac_ctx['lan_router_mac']} observed={sorted(src_macs)}"
            )
        if dst_macs != {mac_ctx["lan_src_mac"]}:
            raise RuntimeError(
                f"down probe: LAN destination MAC differs from TRex LAN MAC: "
                f"expected={mac_ctx['lan_src_mac']} observed={sorted(dst_macs)}"
            )
        return {
            "tx_pkts": tx_pkts,
            "lan_rx_pkts": lan_rx_pkts,
            "capture_matches": len(matched),
            "success_rate": success_rate,
        }

    def run_preflight(client, test_name, preflight_writer):
        diag = {
            "ts_local": ts_iso(),
            "test_name": test_name,
            "status": "error",
            "warm_nat_internal_udp_port": lan_source_port,
        }
        try:
            client.reset(ports=ports)
            client.clear_stats(ports=ports)
            client.remove_all_streams(ports=ports)
            mac_ctx = resolve_step_macs(client)
            diag.update(mac_ctx)

            warm = warm_nat_and_discover_mapping(client, test_name, mac_ctx)
            warm_nat_ipv4 = warm["nat_external_ipv4"]
            warm_nat_port = warm["nat_external_udp_port"]
            diag.update({
                "warm_nat_external_ipv4": warm_nat_ipv4,
                "warm_nat_external_udp_port": warm_nat_port,
                "warm_nat_tx_pkts": warm["tx_pkts"],
                "warm_nat_wan_rx_pkts": warm["wan_rx_pkts"],
                "warm_nat_capture_matches": warm["capture_matches"],
            })

            up = probe_up(client, test_name, mac_ctx)
            nat_external_ipv4 = up["nat_external_ipv4"]
            nat_external_port = up["nat_external_udp_port"]
            diag.update({
                "probe_up_tx_pkts": up["tx_pkts"],
                "probe_up_wan_rx_pkts": up["wan_rx_pkts"],
                "probe_up_capture_matches": up["capture_matches"],
                "probe_up_success_rate": up["success_rate"],
                "probe_up_external_ipv4": nat_external_ipv4,
                "probe_up_external_udp_port": nat_external_port,
                "nat_ipv4_changed_after_warm": int(nat_external_ipv4 != warm_nat_ipv4),
                "nat_port_changed_after_warm": int(nat_external_port != warm_nat_port),
            })

            down = probe_down(
                client,
                test_name,
                mac_ctx,
                nat_external_ipv4,
                nat_external_port,
            )
            diag.update({
                "probe_down_tx_pkts": down["tx_pkts"],
                "probe_down_lan_rx_pkts": down["lan_rx_pkts"],
                "probe_down_capture_matches": down["capture_matches"],
                "probe_down_success_rate": down["success_rate"],
                "status": "ok",
                "detail": "",
            })
            preflight_writer.writerow(diag)
            return {
                **mac_ctx,
                "nat_external_ipv4": nat_external_ipv4,
                "nat_external_udp_port": nat_external_port,
                "diag": diag,
            }
        except Exception as exc:
            diag["detail"] = f"{type(exc).__name__}: {exc}"
            preflight_writer.writerow(diag)
            raise StepPreflightError(str(exc)) from exc

    def wait_for_recovery(client, test_name, preflight_writer, recovery_writer):
        started = time.monotonic()
        attempt = 0
        consecutive = 0
        while consecutive < recovery_success_consecutive:
            time.sleep(recovery_probe_interval_sec)
            attempt += 1
            detail = ""
            try:
                preflight = run_preflight(client, test_name, preflight_writer)
                consecutive += 1
                status = "ok"
            except StepPreflightError as exc:
                preflight = None
                consecutive = 0
                status = "error"
                detail = str(exc)
            elapsed = time.monotonic() - started
            complete = int(consecutive >= recovery_success_consecutive)
            recovery_writer.writerow({
                "ts_local": ts_iso(),
                "test_name": test_name,
                "attempt": attempt,
                "status": status,
                "consecutive_successes": consecutive,
                "required_consecutive_successes": recovery_success_consecutive,
                "elapsed_sec": round(elapsed, 6),
                "recovery_complete": complete,
                "detail": detail,
            })
            if complete:
                print(
                    f"--- Recovery confirmed for {test_name}: "
                    f"{consecutive} consecutive probes, {elapsed:.3f}s ---"
                )
                return preflight
            if recovery_timeout_sec > 0 and elapsed >= recovery_timeout_sec:
                raise StepPreflightError(
                    f"preflight recovery timed out after {elapsed:.3f}s for "
                    f"{test_name}; required={recovery_success_consecutive} "
                    f"consecutive successes"
                )
            print(
                f"--- Recovery probe {test_name}: status={status}, "
                f"consecutive={consecutive}/{recovery_success_consecutive}, "
                f"elapsed={elapsed:.3f}s ---"
            )

    def direction_metrics(stats, direction, frame_size, duration_sec, active):
        if not active:
            return {
                "active": 0,
                "tx_pkts": "",
                "rx_pkts": "",
                "tx_bytes": "",
                "rx_bytes": "",
                "loss_pkts": "",
                "loss_rate": "",
                "avg_tx_pps": "",
                "avg_rx_pps": "",
                "avg_rx_l2_bps": "",
                "avg_rx_l1_bps": "",
                "inner_ipv4_bps": "",
                "outer_line_estimated_bps": "",
                "theoretical_pps": "",
                "pps_reach_rate": "",
                "theoretical_l1_bps": "",
                "l1_reach_rate": "",
                "theoretical_outer_line_bps": "",
                "outer_line_reach_rate": "",
            }
        if direction == "up":
            tx_port, rx_port = lan_port, wan_port
        elif direction == "down":
            tx_port, rx_port = wan_port, lan_port
        else:
            raise ValueError(direction)
        tx_pkts = safe_stat(stats, tx_port, "opackets")
        rx_pkts = safe_stat(stats, rx_port, "ipackets")
        tx_bytes = safe_stat(stats, tx_port, "obytes")
        rx_bytes = safe_stat(stats, rx_port, "ibytes")
        loss_pkts = max(0, tx_pkts - rx_pkts)
        loss_rate = loss_pkts / tx_pkts if tx_pkts else 0.0
        avg_tx_pps = tx_pkts / duration_sec if duration_sec > 0 else 0.0
        avg_rx_pps = rx_pkts / duration_sec if duration_sec > 0 else 0.0
        avg_rx_l2_bps = (rx_bytes * 8) / duration_sec if duration_sec > 0 else 0.0
        wire_rates = estimate_wire_rates(
            avg_rx_pps,
            frame_size,
            L1_OVERHEAD_BYTES,
            tunnel_overhead_bytes,
        )
        avg_rx_l1_bps = wire_rates["inner_ipv4_bps"]
        theoretical_pps = theoretical_pps_per_port(
            line_rate_gbps,
            frame_size,
            tunnel_overhead_bytes,
        )
        theoretical_rates = estimate_wire_rates(
            theoretical_pps,
            frame_size,
            L1_OVERHEAD_BYTES,
            tunnel_overhead_bytes,
        )
        theoretical_l1_bps = theoretical_rates["inner_ipv4_bps"]
        theoretical_outer_line_bps = line_rate_gbps * 1_000_000_000
        return {
            "active": 1,
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
            "inner_ipv4_bps": wire_rates["inner_ipv4_bps"],
            "outer_line_estimated_bps": wire_rates["outer_line_estimated_bps"],
            "theoretical_pps": theoretical_pps,
            "pps_reach_rate": avg_rx_pps / theoretical_pps if theoretical_pps else 0.0,
            "theoretical_l1_bps": theoretical_l1_bps,
            "l1_reach_rate": avg_rx_l1_bps / theoretical_l1_bps if theoretical_l1_bps else 0.0,
            "theoretical_outer_line_bps": theoretical_outer_line_bps,
            "outer_line_reach_rate": (
                wire_rates["outer_line_estimated_bps"] / theoretical_outer_line_bps
                if theoretical_outer_line_bps
                else 0.0
            ),
        }

    def calc_summary(test, stats, duration_sec, preflight):
        direction = test["direction"]
        frame_size = test["frame_size"]
        up = direction_metrics(stats, "up", frame_size, duration_sec, direction in ("up", "bidir"))
        down = direction_metrics(stats, "down", frame_size, duration_sec, direction in ("down", "bidir"))
        active_metrics = [metrics for metrics in (up, down) if metrics["active"]]
        tx_pkts = sum(metrics["tx_pkts"] for metrics in active_metrics)
        rx_pkts = sum(metrics["rx_pkts"] for metrics in active_metrics)
        tx_bytes = sum(metrics["tx_bytes"] for metrics in active_metrics)
        rx_bytes = sum(metrics["rx_bytes"] for metrics in active_metrics)
        loss_pkts = max(0, tx_pkts - rx_pkts)
        loss_rate = loss_pkts / tx_pkts if tx_pkts else 0.0
        avg_tx_pps = sum(metrics["avg_tx_pps"] for metrics in active_metrics)
        avg_rx_pps = sum(metrics["avg_rx_pps"] for metrics in active_metrics)
        avg_rx_l2_bps = sum(metrics["avg_rx_l2_bps"] for metrics in active_metrics)
        avg_rx_l1_bps = sum(metrics["avg_rx_l1_bps"] for metrics in active_metrics)
        inner_ipv4_bps = sum(metrics["inner_ipv4_bps"] for metrics in active_metrics)
        outer_line_estimated_bps = sum(
            metrics["outer_line_estimated_bps"] for metrics in active_metrics
        )
        theoretical_pps = sum(metrics["theoretical_pps"] for metrics in active_metrics)
        theoretical_l1_bps = sum(metrics["theoretical_l1_bps"] for metrics in active_metrics)
        theoretical_outer_line_bps = sum(
            metrics["theoretical_outer_line_bps"] for metrics in active_metrics
        )
        worst_direction_loss_rate = max(metrics["loss_rate"] for metrics in active_metrics)
        try:
            queue_full = stats["global"].get("queue_full", 0)
        except Exception:
            queue_full = 0

        result = {
            "phase": "stl_rate_search" if rate_search else "stl_single_load",
            "test_name": test["name"],
            "base_test_name": test.get("base_name", test["name"]),
            "repeat": test.get("repeat", ""),
            "direction": direction,
            "frame_size": frame_size,
            "duration_sec": duration_sec,
            "rate_percent": test.get("rate_percent", ""),
            "rate_mode": test.get("rate_mode", "mbps"),
            "rate_mbps_per_active_port": test.get("rate_mbps", ""),
            "requested_pps_per_active_port": test.get("rate_pps", ""),
            "bps_l1_per_active_port": test.get("bps_l1", ""),
            "line_rate_gbps": line_rate_gbps,
            "tx_pkts": tx_pkts,
            "rx_pkts": rx_pkts,
            "tx_bytes": tx_bytes,
            "rx_bytes": rx_bytes,
            "loss_pkts": loss_pkts,
            "loss_rate": loss_rate,
            "worst_direction_loss_rate": worst_direction_loss_rate,
            "avg_tx_pps": avg_tx_pps,
            "avg_rx_pps": avg_rx_pps,
            "avg_rx_l2_bps": avg_rx_l2_bps,
            "avg_rx_l1_bps": avg_rx_l1_bps,
            "inner_ipv4_bps": inner_ipv4_bps,
            "outer_line_estimated_bps": outer_line_estimated_bps,
            "theoretical_pps": theoretical_pps,
            "pps_reach_rate": avg_rx_pps / theoretical_pps if theoretical_pps else 0.0,
            "theoretical_l1_bps": theoretical_l1_bps,
            "l1_reach_rate": avg_rx_l1_bps / theoretical_l1_bps if theoretical_l1_bps else 0.0,
            "theoretical_outer_line_bps": theoretical_outer_line_bps,
            "outer_line_reach_rate": (
                outer_line_estimated_bps / theoretical_outer_line_bps
                if theoretical_outer_line_bps
                else 0.0
            ),
            "queue_full": queue_full,
            "ierrors": sum(safe_stat(stats, port, "ierrors") for port in ports),
            "oerrors": sum(safe_stat(stats, port, "oerrors") for port in ports),
            "lan_ierrors": safe_stat(stats, lan_port, "ierrors"),
            "lan_oerrors": safe_stat(stats, lan_port, "oerrors"),
            "wan_ierrors": safe_stat(stats, wan_port, "ierrors"),
            "wan_oerrors": safe_stat(stats, wan_port, "oerrors"),
            "preflight_ok": 1,
            "lan_src_mac": preflight["lan_src_mac"],
            "lan_router_mac": preflight["lan_router_mac"],
            "wan_src_mac": preflight["wan_src_mac"],
            "wan_router_mac": preflight["wan_router_mac"],
            "nat_internal_udp_port": lan_source_port,
            "nat_external_ipv4": preflight["nat_external_ipv4"],
            "nat_external_udp_port": preflight["nat_external_udp_port"],
            "warm_nat_capture_matches": preflight["diag"].get("warm_nat_capture_matches", ""),
            "probe_up_success_rate": preflight["diag"].get("probe_up_success_rate", ""),
            "probe_down_success_rate": preflight["diag"].get("probe_down_success_rate", ""),
            "nat_ipv4_changed_after_warm": preflight["diag"].get(
                "nat_ipv4_changed_after_warm",
                "",
            ),
            "nat_port_changed_after_warm": preflight["diag"].get("nat_port_changed_after_warm", ""),
        }
        for prefix, metrics in (("up", up), ("down", down)):
            for key, value in metrics.items():
                result[f"{prefix}_{key}"] = value
        return result

    def run_one_test(client, test, raw_writer, preflight_writer, recovery_writer):
        name = test["name"]
        direction = test["direction"]
        frame_size = test["frame_size"]
        bps_l1 = test.get("bps_l1", None)
        rate_pps = test.get("rate_pps", None)
        rate_mbps = test.get("rate_mbps", "")
        rate_percent = test.get("rate_percent", "")
        if rate_pps is None:
            rate_text = f"{fmt_rate_value(rate_mbps)}Mbps"
        else:
            rate_text = f"{fmt_rate_value(rate_pps)}pps"
        print(
            f"=== STL {name}: direction={direction}, frame={frame_size}, "
            f"rate={rate_text} ({rate_percent:g}%), bps_L1={bps_l1}, "
            f"duration={duration}s ==="
        )
        print(
            f"--- Preflight {name}: ARP, explicit MAC, warm NAT, "
            "NAT IPv4/port discovery, up/down probe ---"
        )
        preflight = run_preflight_with_optional_recovery_probe(
            lambda: run_preflight(client, name, preflight_writer),
            lambda: wait_for_recovery(
                client, name, preflight_writer, recovery_writer
            ),
            recovery_probe_enabled,
        )
        nat_external_ipv4 = preflight["nat_external_ipv4"]
        nat_external_port = preflight["nat_external_udp_port"]
        rate_kwargs = {"pps": rate_pps} if rate_pps is not None else {"bps_l1": bps_l1}

        stop_and_remove_streams(client)
        client.clear_stats(ports=ports)
        if direction == "up":
            client.add_streams(
                make_stream(f"{name}_up", "up", frame_size, preflight, **rate_kwargs),
                ports=[lan_port],
            )
            active_ports = [lan_port]
        elif direction == "down":
            client.add_streams(
                make_stream(f"{name}_keepalive", "up", FRAME_64, preflight, pps=1),
                ports=[lan_port],
            )
            client.add_streams(
                make_stream(
                    f"{name}_down",
                    "down",
                    frame_size,
                    preflight,
                    nat_external_ipv4=nat_external_ipv4,
                    nat_external_port=nat_external_port,
                    **rate_kwargs,
                ),
                ports=[wan_port],
            )
            active_ports = [lan_port, wan_port]
        elif direction == "bidir":
            client.add_streams(
                make_stream(f"{name}_up", "up", frame_size, preflight, **rate_kwargs),
                ports=[lan_port],
            )
            client.add_streams(
                make_stream(
                    f"{name}_down",
                    "down",
                    frame_size,
                    preflight,
                    nat_external_ipv4=nat_external_ipv4,
                    nat_external_port=nat_external_port,
                    **rate_kwargs,
                ),
                ports=[wan_port],
            )
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
        return calc_summary(test, delta_stats, duration, preflight)

    base_tests = [
        {"base_name": "large_down_throughput", "direction": "down", "frame_size": FRAME_LARGE, "rate_mode": "mbps"},
        {"base_name": "large_up_throughput", "direction": "up", "frame_size": FRAME_LARGE, "rate_mode": "mbps"},
        {"base_name": "large_bidir_throughput", "direction": "bidir", "frame_size": FRAME_LARGE, "rate_mode": "mbps"},
        {"base_name": "udp64_down_pps", "direction": "down", "frame_size": FRAME_64, "rate_mode": "pps"},
        {"base_name": "udp64_up_pps", "direction": "up", "frame_size": FRAME_64, "rate_mode": "pps"},
        {"base_name": "udp64_bidir_pps", "direction": "bidir", "frame_size": FRAME_64, "rate_mode": "pps"},
    ]

    def rate_points_for_base(base):
        if base["rate_mode"] == "pps":
            return build_pps_rate_points(
                line_rate_gbps,
                base["frame_size"],
                tunnel_overhead_bytes,
                rate_steps=rate_steps,
                pps_steps=pps_steps,
            )
        return build_rate_points(
            line_rate_gbps,
            rate_steps=rate_steps,
            rate_mbps_steps=rate_mbps_steps,
        )

    def rate_point_for_percent(base, pct, label):
        pct = float(pct)
        if base["rate_mode"] == "pps":
            maximum_pps = theoretical_pps_per_port(
                line_rate_gbps, base["frame_size"], tunnel_overhead_bytes
            )
            pps = maximum_pps * (pct / 100.0)
            inner_bps = estimate_wire_rates(
                pps, base["frame_size"], L1_OVERHEAD_BYTES, tunnel_overhead_bytes
            )["inner_ipv4_bps"]
            return {
                "label": label,
                "rate_pps": pps,
                "rate_mbps": inner_bps / 1_000_000.0,
                "rate_percent": pct,
                "bps_l1": inner_bps,
            }
        mbps = (line_rate_gbps * 1000.0) * (pct / 100.0)
        return {
            "label": label,
            "rate_pps": None,
            "rate_mbps": mbps,
            "rate_percent": pct,
            "bps_l1": mbps * 1_000_000.0,
        }

    def make_test(base, repeat_index, rate_point):
        return {
            "name": f"r{repeat_index}_{base['base_name']}_{rate_point['label']}",
            "base_name": base["base_name"],
            "repeat": repeat_index,
            "direction": base["direction"],
            "frame_size": base["frame_size"],
            "duration": duration,
            "rate_mode": base["rate_mode"],
            "rate_percent": rate_point["rate_percent"],
            "rate_mbps": rate_point["rate_mbps"],
            "rate_pps": rate_point.get("rate_pps"),
            "bps_l1": rate_point["bps_l1"],
        }

    def measurement_error_row(test, base, repeat_index, exc, phase):
        return {
            "phase": phase,
            "repeat": repeat_index,
            "test_name": test["name"],
            "base_test_name": base["base_name"],
            "direction": base["direction"],
            "rate_mode": base["rate_mode"],
            "rate_percent": test["rate_percent"],
            "rate_mbps_per_active_port": test["rate_mbps"],
            "requested_pps_per_active_port": test.get("rate_pps", ""),
            "preflight_ok": 0 if isinstance(exc, StepPreflightError) else 1,
            "error_stage": "preflight" if isinstance(exc, StepPreflightError) else "measurement",
            "error": traceback.format_exc(),
        }

    raw_csv = out_dir / "stl_raw.csv"
    preflight_csv = out_dir / "stl_preflight.csv"
    recovery_csv = out_dir / "stl_recovery.csv"
    summaries = []
    client = STLClient(server="127.0.0.1")
    client.connect()
    try:
        client.acquire(ports=ports, force=True)
        wait_trex_ports_link_up(client, ports)
        client.reset(ports=ports)
        wait_before_test_start(test_start_delay_sec, "STL throughput/PPS benchmark")
        with open(raw_csv, "w", newline="") as raw_file, open(
            preflight_csv, "w", newline=""
        ) as preflight_file, open(recovery_csv, "w", newline="") as recovery_file:
            raw_writer = csv.writer(raw_file)
            raw_writer.writerow(["ts_local", "elapsed_sec", "phase", "test_name", "metric", "value"])
            preflight_writer = csv.DictWriter(preflight_file, fieldnames=preflight_fields, extrasaction="ignore")
            preflight_writer.writeheader()
            recovery_writer = csv.DictWriter(recovery_file, fieldnames=recovery_fields, extrasaction="ignore")
            recovery_writer.writeheader()
            for r in range(1, repeat + 1):
                for base in base_tests:
                    if rate_search:
                        step_rows = []
                        high_loss_streak = 0
                        stopped_for_high_loss = False
                        for rate_point in rate_points_for_base(base):
                            test = make_test(base, r, rate_point)
                            try:
                                row = run_one_test(client, test, raw_writer, preflight_writer, recovery_writer)
                                summaries.append(row)
                                step_rows.append(row)
                                high_loss_streak = high_loss_streak_update(
                                    high_loss_streak,
                                    metric_float(row, "worst_direction_loss_rate", metric_float(row, "loss_rate", 0.0)),
                                    high_loss_threshold,
                                )
                                if high_loss_streak >= high_loss_consecutive:
                                    print(
                                        f"Stopping {base['base_name']} after {high_loss_streak} consecutive "
                                        f"measurements with packet loss >= {high_loss_threshold:.1%}."
                                    )
                                    stopped_for_high_loss = True
                                    break
                                if stop_on_first_loss and not row_passes(row, lossless_threshold):
                                    print(
                                        f"Stopping {base['base_name']} at {rate_point['label']} "
                                        f"because loss/error was detected."
                                    )
                                    break
                            except Exception as exc:
                                high_loss_streak = 0
                                error_row = measurement_error_row(test, base, r, exc, "stl_rate_search")
                                print(error_row["error"])
                                summaries.append(error_row)
                            raw_file.flush()
                            preflight_file.flush()
                            recovery_file.flush()
                        if not stopped_for_high_loss:
                            for pct in build_refine_steps(step_rows, lossless_threshold, refine_step):
                                point = rate_point_for_percent(base, pct, f"{pct:g}pct_refine")
                                test = make_test(base, r, point)
                                try:
                                    row = run_one_test(client, test, raw_writer, preflight_writer, recovery_writer)
                                    summaries.append(row)
                                    step_rows.append(row)
                                except Exception as exc:
                                    error_row = measurement_error_row(test, base, r, exc, "stl_rate_search_refine")
                                    print(error_row["error"])
                                    summaries.append(error_row)
                                raw_file.flush()
                                preflight_file.flush()
                                recovery_file.flush()
                        summaries.append(make_rate_search_result(base["base_name"], r, step_rows, lossless_threshold))
                    else:
                        pct = float(single_load_percent)
                        point = rate_point_for_percent(base, pct, f"{pct:g}pct")
                        test = make_test(base, r, point)
                        try:
                            summaries.append(run_one_test(client, test, raw_writer, preflight_writer, recovery_writer))
                        except Exception as exc:
                            error_row = measurement_error_row(test, base, r, exc, "stl_single_load")
                            print(error_row["error"])
                            summaries.append(error_row)
                        raw_file.flush()
                        preflight_file.flush()
                        recovery_file.flush()
    finally:
        stop_and_remove_streams(client)
        try:
            client.reset(ports=ports)
        except Exception:
            pass
        client.disconnect()
    return summaries

def write_metadata(out_dir, target_name, target, args):
    metadata = {
        "created_at": ts_iso(),
        "target_name": target_name,
        "target": target,
        "args": vars(args),
        "trex_dir": TREX_DIR,
        "trex_cfg_path": TREX_CFG_PATH,
        "mode": "dslite_stl_only_throughput_pps",
        "frame_size_includes_fcs": True,
        "inner_ipv4_mtu": target["inner_ipv4_mtu"],
        "tunnel_overhead_bytes": target["tunnel_overhead_bytes"],
        "default_rate_grid_adds_per_frame_tunnel_capacity": False,
        "nat_discovery": {
            "external_ipv4_mode": target["nat_external_ip_mode"],
            "external_port_mode": target["nat_external_port_mode"],
            "nat_pool_cidr": target["nat_pool_cidr"],
        },
        "per_step_preflight": {
            "arp_refresh": True,
            "wan_arp_target": target["wan_arp_target"],
            "explicit_mac_headers": True,
            "warm_nat_seconds": 3,
            "nat_external_ipv4_and_udp_port_capture": True,
            "uplink_probe_packets": 32,
            "downlink_probe_packets": 32,
            "minimum_probe_capture_rate": 0.9,
            "output": "stl_preflight.csv",
        },
        "per_step_recovery": {
            "enabled": args.stl_recovery_probe,
            "probe_interval_sec": args.recovery_probe_interval_sec,
            "required_consecutive_successes": args.recovery_success_consecutive,
            "timeout_sec": args.recovery_timeout_sec,
            "output": "stl_recovery.csv",
        },
        "directional_statistics": ["combined", "up", "down"],
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


def merge_dslite_target_with_overrides(base_target, args):
    target = dict(base_target)
    if args.interfaces:
        target["interfaces"] = args.interfaces
    for argument_name, profile_key in (
        ("lan_ip", "lan_ip"),
        ("lan_gw", "lan_gw"),
        ("wan_link_ip", "wan_link_ip"),
        ("wan_gateway_ip", "wan_gateway_ip"),
        ("wan_arp_target", "wan_arp_target"),
        ("server_ip", "server_ip"),
        ("nat_pool_cidr", "nat_pool_cidr"),
    ):
        value = getattr(args, argument_name)
        if value:
            target[profile_key] = value
    if args.line_rate_gbps is not None:
        target["line_rate_gbps"] = args.line_rate_gbps
    if args.inner_ipv4_mtu is not None:
        target["inner_ipv4_mtu"] = args.inner_ipv4_mtu
    if args.tunnel_overhead_bytes is not None:
        target["tunnel_overhead_bytes"] = args.tunnel_overhead_bytes
    if args.cores is not None:
        target["trex_cores"] = args.cores
    return target


def main():
    parser = argparse.ArgumentParser(
        description="Run TRex STL-only DS-Lite IPv4 throughput/PPS benchmark."
    )
    parser.add_argument("--target", choices=DSLITE_TARGETS.keys(), default=_STL_DEFAULTS["target"])
    parser.add_argument("--out-dir", default="", help="Result directory. Default: /var/trex/trex_dslite_stl_throughput_pps_<target>_<timestamp>/.")
    parser.add_argument("--duration", type=int, default=_STL_DEFAULTS["duration_sec"])
    parser.add_argument("--repeat", type=int, default=_STL_DEFAULTS["repeat"])
    parser.add_argument("--test-start-delay-sec", type=float, default=DEFAULT_TEST_START_DELAY_SEC,
                        help="Seconds to wait after TRex ports report link-up and before the first measured test. Default: 15.")

    parser.add_argument("--rate-search", dest="rate_search", action="store_true", help="Enable rate search. Default: enabled.")
    parser.add_argument("--stl-rate-search", dest="rate_search", action="store_true", help="Compatibility alias for --rate-search.")
    parser.add_argument("--no-rate-search", dest="rate_search", action="store_false", help="Run one fixed-load point only.")
    parser.set_defaults(rate_search=_STL_DEFAULTS["rate_search"])

    parser.add_argument("--rate-steps", type=parse_rate_steps, default=None,
                        help="Compatibility option: percent steps. If omitted, Mbps steps are used.")
    parser.add_argument("--rate-mbps-steps", "--rate-steps-mbps", dest="rate_mbps_steps",
                        type=parse_rate_mbps_steps, default=None,
                        help="L1 Mbps steps per active port for large-frame throughput. Default: E12 from 1 Mbps through profile line rate.")
    parser.add_argument("--pps-steps", type=parse_pps_steps, default=None,
                        help="PPS steps per active port for 64-byte tests. Default: E12 from 1,000 pps through the encapsulated profile maximum PPS.")
    parser.add_argument("--refine-step", type=float, default=_STL_DEFAULTS["refine_step"],
                        help="Optional refine step in percent between last NDR and first loss. 0 disables refine. Example: 1")
    parser.add_argument("--single-load-percent", type=float, default=_STL_DEFAULTS["single_load_percent"],
                        help="Used only with --no-rate-search.")
    parser.add_argument("--lossless-threshold", type=float, default=_STL_DEFAULTS["lossless_threshold"],
                        help="Loss-rate threshold for NDR. Default: 0.0")
    parser.add_argument("--stop-on-first-loss", action="store_true",
                        help="Stop each coarse rate search item at first loss/error.")
    parser.add_argument("--high-loss-threshold", type=float, default=DEFAULT_HIGH_LOSS_THRESHOLD,
                        help="Stop threshold for consecutive packet-loss measurements. Default: 0.5 (50%%).")
    parser.add_argument("--high-loss-consecutive", type=int, default=DEFAULT_HIGH_LOSS_CONSECUTIVE,
                        help="Stop after this many consecutive measurements at or above --high-loss-threshold. Default: 3.")
    parser.add_argument("--recovery-probe-interval-sec", type=float, default=DEFAULT_RECOVERY_PROBE_INTERVAL_SEC,
                        help="Seconds between connectivity probes before every measurement. Default: 2.")
    parser.add_argument("--recovery-success-consecutive", type=int, default=DEFAULT_RECOVERY_SUCCESS_CONSECUTIVE,
                        help="Required consecutive successful connectivity probes before measurement. Default: 3.")
    parser.add_argument("--recovery-timeout-sec", type=float, default=DEFAULT_RECOVERY_TIMEOUT_SEC,
                        help="Maximum preflight recovery wait. Zero disables the timeout. Default: 300.")
    parser.add_argument(
        "--stl-recovery-probe",
        action="store_true",
        help="Require consecutive recovery probes before each STL measurement. Disabled by default; one preflight still runs.",
    )
    parser.add_argument("--no-install-cfg", action="store_true")
    parser.add_argument("--auto-dpdk-bind", dest="auto_dpdk_bind", action="store_true", default=_STL_DEFAULTS["auto_dpdk_bind"], help="Before starting TRex, delete stale benchmark namespaces and bind profile PCI devices to their DPDK driver. Enabled by default.")
    parser.add_argument("--no-auto-dpdk-bind", dest="auto_dpdk_bind", action="store_false", help="Do not attempt DPDK driver binding before starting TRex. Stale benchmark namespaces are still removed.")
    parser.add_argument("--dpdk-driver", default="", help="Override the DPDK driver used for all profile PCI devices, e.g. vfio-pci or igb_uio.")

    parser.add_argument("--interfaces", type=parse_interfaces, default=None)
    add_lan_source_port_range_argument(parser)
    parser.add_argument("--lan-ip", default="")
    parser.add_argument("--lan-gw", default="")
    parser.add_argument("--wan-link-ip", default="")
    parser.add_argument("--wan-gateway-ip", default="")
    parser.add_argument("--wan-arp-target", default="")
    parser.add_argument("--server-ip", default="")
    parser.add_argument("--nat-pool-cidr", default="")
    parser.add_argument("--inner-ipv4-mtu", type=int, default=None)
    parser.add_argument("--tunnel-overhead-bytes", type=int, default=None)
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
    if args.test_start_delay_sec < 0:
        raise SystemExit("ERROR: --test-start-delay-sec must be >= 0")
    if args.high_loss_threshold < 0 or args.high_loss_threshold > 1:
        raise SystemExit("ERROR: --high-loss-threshold must be between 0 and 1")
    if args.high_loss_consecutive <= 0:
        raise SystemExit("ERROR: --high-loss-consecutive must be positive")
    if args.recovery_probe_interval_sec < 0:
        raise SystemExit("ERROR: --recovery-probe-interval-sec must be >= 0")
    if args.recovery_success_consecutive <= 0:
        raise SystemExit("ERROR: --recovery-success-consecutive must be positive")
    if args.recovery_timeout_sec < 0:
        raise SystemExit("ERROR: --recovery-timeout-sec must be >= 0")
    if args.rate_steps is not None and (args.rate_mbps_steps is not None or args.pps_steps is not None):
        raise SystemExit("ERROR: --rate-steps cannot be combined with --rate-mbps-steps or --pps-steps")

    target = merge_dslite_target_with_overrides(DSLITE_TARGETS[args.target], args)
    validate_dslite_profile(args.target, target)
    configured_max_frame = max_inner_frame_size(target["inner_ipv4_mtu"])
    if FRAME_LARGE != configured_max_frame:
        raise SystemExit(
            "ERROR: dslite_stl.frame_large_bytes must equal inner_ipv4_mtu + "
            f"Ethernet header/FCS ({configured_max_frame} bytes for this profile)"
        )
    trex_target = build_trex_target(target)
    if args.rate_search and args.rate_steps is None:
        if args.rate_mbps_steps is None:
            args.rate_mbps_steps = make_default_rate_mbps_steps(target["line_rate_gbps"])
        else:
            args.rate_mbps_steps = limit_rate_mbps_steps(args.rate_mbps_steps, target["line_rate_gbps"])
        if args.pps_steps is None:
            args.pps_steps = make_default_pps_steps(
                target["line_rate_gbps"], target["tunnel_overhead_bytes"]
            )
        else:
            args.pps_steps = limit_pps_steps(
                args.pps_steps,
                target["line_rate_gbps"],
                target["tunnel_overhead_bytes"],
            )
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_RESULT_ROOT / f"trex_dslite_stl_throughput_pps_{args.target}_{ts_compact()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    details_dir = prepare_details_dir(out_dir)

    print("============================================================")
    print("TRex STL-only DS-Lite IPv4 throughput/PPS benchmark")
    print("============================================================")
    print(f"Target              : {args.target}")
    print(f"Description         : {target['description']}")
    print(f"Interfaces          : {target['interfaces']}")
    print(f"Line rate           : {target['line_rate_gbps']} Gbps")
    print(f"LAN                 : port {target['lan_port']} / {target['lan_ip']} gw {target['lan_gw']}")
    print(f"WAN link            : port {target['wan_port']} / {target['wan_link_ip']}")
    print(f"WAN gateway / ARP   : {target['wan_gateway_ip']} / {target['wan_arp_target']}")
    print(f"Server IP           : {target['server_ip']}")
    print(f"AFTR NAT pool       : {target['nat_pool_cidr']} (IPv4/port discovery)")
    print(f"Inner IPv4 MTU      : {target['inner_ipv4_mtu']} bytes")
    print(f"Tunnel overhead     : {target['tunnel_overhead_bytes']} bytes")
    print(f"Rate search         : {args.rate_search}")
    if args.rate_search and args.rate_steps is None:
        print(f"Throughput steps    : {[fmt_rate_value(x) for x in args.rate_mbps_steps]} Mbps")
        print(f"PPS steps           : {format_kpps_steps(args.pps_steps)} kpps")
    elif args.rate_search:
        print(f"Rate steps          : {args.rate_steps} %")
    else:
        print(f"Rate steps          : {[args.single_load_percent]} %")
    print(f"Refine step         : {args.refine_step}")
    print(f"High-loss stop      : >= {args.high_loss_threshold:.1%} x {args.high_loss_consecutive}")
    if args.stl_recovery_probe:
        print(f"Recovery probes     : every {args.recovery_probe_interval_sec:g}s, {args.recovery_success_consecutive} consecutive successes, timeout={args.recovery_timeout_sec:g}s")
    else:
        print("Recovery probes     : disabled (one preflight per measurement)")
    print(f"LAN source port     : {first_lan_source_port(args.lan_source_port_range, UDP_PORT)}"
          f" (range {format_lan_source_port_range(args.lan_source_port_range) or 'test default'})")
    print(f"Duration/test       : {args.duration}s")
    print(f"Repeat              : {args.repeat}")
    print(f"Output              : {out_dir}")
    print("============================================================")

    prepare_log = prepare_trex_interfaces_from_profile(
        trex_target,
        details_dir,
        auto_dpdk_bind=args.auto_dpdk_bind,
        dpdk_driver_override=args.dpdk_driver,
    )
    print(f"TRex interface prep : {prepare_log}")

    install_trex_cfg(trex_target, details_dir, args.no_install_cfg)
    write_metadata(details_dir, args.target, target, args)

    summaries = []
    server = TrexStlServer(cores=int(target["trex_cores"]), out_dir=details_dir)
    try:
        server.start()
        summaries.extend(
            run_stl_tests(
                target=target,
                out_dir=details_dir,
                duration=args.duration,
                repeat=args.repeat,
                rate_search=args.rate_search,
                rate_steps=args.rate_steps,
                rate_mbps_steps=args.rate_mbps_steps,
                pps_steps=args.pps_steps,
                refine_step=args.refine_step,
                lossless_threshold=args.lossless_threshold,
                stop_on_first_loss=args.stop_on_first_loss,
                high_loss_threshold=args.high_loss_threshold,
                high_loss_consecutive=args.high_loss_consecutive,
                recovery_probe_interval_sec=args.recovery_probe_interval_sec,
                recovery_success_consecutive=args.recovery_success_consecutive,
                recovery_timeout_sec=args.recovery_timeout_sec,
                lan_source_port_range=args.lan_source_port_range,
                single_load_percent=args.single_load_percent,
                test_start_delay_sec=args.test_start_delay_sec,
                recovery_probe_enabled=args.stl_recovery_probe,
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
    print(f"Details          : {details_dir}")
    print(f"Raw CSV          : {details_dir / 'stl_raw.csv'}")
    print(f"Preflight CSV    : {details_dir / 'stl_preflight.csv'}")
    print(f"Recovery CSV     : {details_dir / 'stl_recovery.csv'}")
    print(f"Metadata         : {details_dir / 'metadata.json'}")
    print(f"TRex server log  : {details_dir / 'trex_server_stl.log'}")


if __name__ == "__main__":
    main()
