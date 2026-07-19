#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Periodic IPv4 ARP refresh helpers for router benchmark namespaces.

The helper sends both a directed ARP request for the configured gateway and a
sender-IP gratuitous ARP request.  The directed request refreshes the router's
neighbor entry and verifies that the gateway can reply.  The gratuitous request
also announces the local IPv4/MAC mapping without depending on the current
neighbor-cache state.
"""

import argparse
import csv
import datetime as dt
import ipaddress
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

ETH_P_ARP = 0x0806
ARP_HTYPE_ETHERNET = 1
ARP_PTYPE_IPV4 = 0x0800
ARP_OP_REQUEST = 1
BROADCAST_MAC = b"\xff" * 6
ZERO_MAC = b"\x00" * 6
DEFAULT_ARP_REFRESH_INTERVAL_SEC = 60.0


def ts_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


def _mac_bytes(text):
    parts = str(text).strip().split(":")
    if len(parts) != 6:
        raise ValueError(f"invalid MAC address: {text}")
    return bytes(int(x, 16) for x in parts)


def interface_mac(iface):
    path = Path("/sys/class/net") / str(iface) / "address"
    return path.read_text(encoding="ascii").strip().lower()


def build_arp_request(src_mac, src_ip, target_ip):
    src_mac_b = _mac_bytes(src_mac)
    src_ip_b = ipaddress.IPv4Address(src_ip).packed
    target_ip_b = ipaddress.IPv4Address(target_ip).packed
    ethernet = BROADCAST_MAC + src_mac_b + struct.pack("!H", ETH_P_ARP)
    arp = struct.pack(
        "!HHBBH6s4s6s4s",
        ARP_HTYPE_ETHERNET,
        ARP_PTYPE_IPV4,
        6,
        4,
        ARP_OP_REQUEST,
        src_mac_b,
        src_ip_b,
        ZERO_MAC,
        target_ip_b,
    )
    return ethernet + arp


def send_arp_once(iface, src_ip, target_ip, count=2):
    """Send directed and gratuitous ARP requests in the current namespace."""
    src_mac = interface_mac(iface)
    directed = build_arp_request(src_mac, src_ip, target_ip)
    gratuitous = build_arp_request(src_mac, src_ip, src_ip)
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ARP))
    try:
        sock.bind((str(iface), 0))
        for _ in range(max(1, int(count))):
            sock.send(directed)
            if target_ip != src_ip:
                sock.send(gratuitous)
            time.sleep(0.02)
    finally:
        sock.close()
    return {"iface": iface, "src_ip": src_ip, "target_ip": target_ip, "src_mac": src_mac}


def send_namespace_arp(namespace, iface, src_ip, target_ip, count=2, timeout_sec=8.0):
    namespace = str(namespace or "root")
    if namespace == "root":
        return send_arp_once(iface, src_ip, target_ip, count=count)
    cmd = [
        "ip", "netns", "exec", namespace,
        sys.executable, str(Path(__file__).resolve()),
        "--once",
        "--iface", str(iface),
        "--src-ip", str(src_ip),
        "--target-ip", str(target_ip),
        "--count", str(int(count)),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=float(timeout_sec), check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"namespace ARP refresh failed: ns={namespace} iface={iface} src={src_ip} target={target_ip} "
            f"exit={proc.returncode} stdout={proc.stdout.strip()} stderr={proc.stderr.strip()}"
        )
    return {"namespace": namespace, "iface": iface, "src_ip": src_ip, "target_ip": target_ip}


class NamespaceArpRefresher:
    """Send ARP immediately on demand and every interval in a daemon thread."""

    def __init__(self, endpoints, log_path, interval_sec=DEFAULT_ARP_REFRESH_INTERVAL_SEC):
        self.endpoints = [dict(x) for x in endpoints if x]
        self.log_path = Path(log_path)
        self.interval_sec = max(1.0, float(interval_sec))
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.log_path.exists():
            with self.log_path.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["ts_local", "reason", "namespace", "iface", "src_ip", "target_ip", "result", "detail"])

    def _log(self, reason, endpoint, result, detail=""):
        with self._lock:
            with self.log_path.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    ts_iso(), reason, endpoint.get("namespace", "root"), endpoint.get("iface", ""),
                    endpoint.get("src_ip", ""), endpoint.get("target_ip", ""), result, detail,
                ])

    def refresh_now(self, reason="manual", required=False):
        failures = []
        for endpoint in self.endpoints:
            try:
                send_namespace_arp(
                    endpoint.get("namespace", "root"),
                    endpoint["iface"],
                    endpoint["src_ip"],
                    endpoint["target_ip"],
                    count=endpoint.get("count", 2),
                )
                self._log(reason, endpoint, "ok")
            except Exception as exc:
                failures.append(str(exc))
                self._log(reason, endpoint, "error", str(exc))
                print(f"WARNING: ARP refresh failed: {exc}", flush=True)
        if failures and required:
            raise RuntimeError("; ".join(failures))
        return not failures

    def _worker(self):
        while not self._stop.wait(self.interval_sec):
            self.refresh_now(reason="periodic_60s", required=False)

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._worker, name="routerbench-arp-refresh", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=min(5.0, self.interval_sec + 1.0))
            self._thread = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--iface", required=True)
    parser.add_argument("--src-ip", required=True)
    parser.add_argument("--target-ip", required=True)
    parser.add_argument("--count", type=int, default=2)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("ERROR: ARP refresh requires root privileges")
    result = send_arp_once(args.iface, args.src_ip, args.target_ip, count=args.count)
    print(result)


if __name__ == "__main__":
    main()
