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
import ipaddress
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

if __package__:
    from .config import load_defaults as _load_defaults
    from .refresh import EndpointRefresher
else:
    # This file is also executed directly inside a network namespace by
    # send_namespace_arp().  In that mode Python does not set a package
    # context, so make the repository package importable explicitly.
    REPO_DIR = Path(__file__).resolve().parents[2]
    if str(REPO_DIR) not in sys.path:
        sys.path.insert(0, str(REPO_DIR))
    from tests.lib.config import load_defaults as _load_defaults
    from tests.lib.refresh import EndpointRefresher


_COMMON_DEFAULTS = _load_defaults("common")

ETH_P_ARP = 0x0806
ARP_HTYPE_ETHERNET = 1
ARP_PTYPE_IPV4 = 0x0800
ARP_OP_REQUEST = 1
BROADCAST_MAC = b"\xff" * 6
ZERO_MAC = b"\x00" * 6
DEFAULT_ARP_REFRESH_INTERVAL_SEC = float(_COMMON_DEFAULTS["arp_refresh_interval_sec"])


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
        self._lifecycle = EndpointRefresher(
            endpoints,
            log_path,
            interval_sec,
            self._refresh_endpoint,
            "WARNING: ARP refresh failed",
            "routerbench-arp-refresh",
        )

    @property
    def endpoints(self):
        return self._lifecycle.endpoints

    @property
    def log_path(self):
        return self._lifecycle.log_path

    @property
    def interval_sec(self):
        return self._lifecycle.interval_sec

    def _log(self, reason, endpoint, result, detail=""):
        self._lifecycle._log(reason, endpoint, result, detail)

    def _refresh_endpoint(self, endpoint):
        send_namespace_arp(
            endpoint.get("namespace", "root"),
            endpoint["iface"],
            endpoint["src_ip"],
            endpoint["target_ip"],
            count=endpoint.get("count", 2),
        )

    def refresh_now(self, reason="manual", required=False):
        return self._lifecycle.refresh_now(reason=reason, required=required)

    def _worker(self):
        self._lifecycle._worker()

    def start(self):
        self._lifecycle.start()

    def stop(self):
        self._lifecycle.stop()

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
