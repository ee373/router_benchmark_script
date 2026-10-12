#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DHCPv4 lease acquisition benchmark using a scratch raw-socket client.

This test uses a small in-process raw-socket DHCP client.  It does not use
TRex EMU.

The parent process prepares the LAN NIC for Linux, moves it into a network
namespace, and executes a helper inside that namespace.  The helper sends raw
Ethernet DHCP DISCOVER/REQUEST/RELEASE frames with arbitrary client MACs and
records DORA latency.
"""

import argparse
import csv
import datetime as dt
import json
import os
import random
import shlex
import socket
import struct
import subprocess
import sys
import time
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from tests.lib.config import TARGETS  # type: ignore
except Exception:
    from lib.config import TARGETS  # type: ignore

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
    from tests.lib.common import (  # type: ignore
        DEFAULT_RESULT_ROOT,
        ts_compact,
    )
    from tests.lib.config import load_defaults as _load_defaults  # type: ignore
    from tests.lib.output import prepare_details_dir  # type: ignore
    from tests.lib.preflight import (  # type: ignore
        add_consecutive_preflight_arguments,
        run_consecutive_preflight,
        validate_consecutive_preflight_arguments,
    )
    from tests.lib.repeat import (
        DEFAULT_REPEAT,
        argparse_repeat,
        run_repeated_subprocesses,
    )  # type: ignore
    from tests.lib.trex import parse_interfaces, prepare_trex_interfaces_from_profile  # type: ignore
    from tests.lib.network import (  # type: ignore
        CommandError,
        delete_namespace_if_exists,
        namespace_exists,
        prepare_kernel_interfaces_from_profile,
        require_root,
        resolve_profile_interfaces,
        root_iface_exists,
        run_cmd,
        wait_namespace_interface_ready,
    )
except Exception:
    from lib.common import (  # type: ignore
        DEFAULT_RESULT_ROOT,
        ts_compact,
    )
    from lib.config import load_defaults as _load_defaults  # type: ignore
    from lib.output import prepare_details_dir  # type: ignore
    from lib.preflight import (  # type: ignore
        add_consecutive_preflight_arguments,
        run_consecutive_preflight,
        validate_consecutive_preflight_arguments,
    )
    from lib.repeat import (
        DEFAULT_REPEAT,
        argparse_repeat,
        run_repeated_subprocesses,
    )  # type: ignore
    from lib.trex import parse_interfaces, prepare_trex_interfaces_from_profile  # type: ignore
    from lib.network import (  # type: ignore
        CommandError,
        delete_namespace_if_exists,
        namespace_exists,
        prepare_kernel_interfaces_from_profile,
        require_root,
        resolve_profile_interfaces,
        root_iface_exists,
        run_cmd,
        wait_namespace_interface_ready,
    )


_DHCP_DEFAULTS = _load_defaults("dhcp")

ETH_P_ALL = 0x0003
ETH_P_IP = 0x0800
ETH_P_ARP = 0x0806
ETH_P_8021Q = 0x8100
UDP_PROTO = 17
DHCP_CLIENT_PORT = 68
DHCP_SERVER_PORT = 67
DHCP_MAGIC = b"\x63\x82\x53\x63"
BROADCAST_MAC = b"\xff\xff\xff\xff\xff\xff"
ZERO_IP = "0.0.0.0"
BROADCAST_IP = "255.255.255.255"

DHCP_MSG = {
    1: "discover",
    2: "offer",
    3: "request",
    4: "decline",
    5: "ack",
    6: "nak",
    7: "release",
    8: "inform",
}


def ts_iso():
    return dt.datetime.now().isoformat(timespec="microseconds")


def safe_json(obj):
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


def write_csv(path, rows):
    path = Path(path)
    fieldnames = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    if not fieldnames:
        fieldnames = ["ts_local", "event"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def mac_to_bytes(mac):
    parts = mac.split(":")
    if len(parts) != 6:
        raise ValueError(f"invalid MAC address: {mac}")
    return bytes(int(x, 16) for x in parts)


def mac_to_str(raw):
    return ":".join(f"{b:02x}" for b in raw)


def ip_to_bytes(ip):
    return socket.inet_aton(ip)


def bytes_to_ip(raw):
    if not raw or len(raw) != 4:
        return ""
    return socket.inet_ntoa(raw)


def mac_from_base(base_mac, index):
    parts = [int(x, 16) for x in base_mac.split(":")]
    if len(parts) != 6:
        raise ValueError(f"invalid MAC address: {base_mac}")
    value = 0
    for p in parts:
        value = (value << 8) | p
    value = (value + int(index)) & ((1 << 48) - 1)
    value |= (1 << 41)   # locally administered
    value &= ~(1 << 40)  # unicast
    return ":".join(f"{(value >> shift) & 0xff:02x}" for shift in (40, 32, 24, 16, 8, 0))


def trial_mac_base(base_mac, trial_offset):
    return mac_from_base(base_mac, int(trial_offset) * 256)


def checksum(data):
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) + data[i + 1]
        total = (total & 0xffff) + (total >> 16)
    return (~total) & 0xffff


def build_ipv4_header(src_ip, dst_ip, payload_len, ident=None):
    if ident is None:
        ident = random.randint(0, 0xffff)
    ver_ihl = 0x45
    tos = 0
    total_len = 20 + int(payload_len)
    flags_frag = 0
    ttl = 64
    proto = UDP_PROTO
    header_checksum = 0
    src = ip_to_bytes(src_ip)
    dst = ip_to_bytes(dst_ip)
    header = struct.pack("!BBHHHBBH4s4s", ver_ihl, tos, total_len, ident, flags_frag, ttl, proto, header_checksum, src, dst)
    header_checksum = checksum(header)
    return struct.pack("!BBHHHBBH4s4s", ver_ihl, tos, total_len, ident, flags_frag, ttl, proto, header_checksum, src, dst)


def build_udp_header(src_port, dst_port, payload):
    # IPv4 UDP checksum may legally be zero.  This avoids pseudo-header handling
    # and improves compatibility across namespaces/offload settings.
    length = 8 + len(payload)
    return struct.pack("!HHHH", int(src_port), int(dst_port), length, 0)


def dhcp_option(code, data):
    data = bytes(data)
    if len(data) > 255:
        raise ValueError(f"DHCP option too long: {code}")
    return bytes([int(code), len(data)]) + data


def build_dhcp_payload(msg_type, mac, xid, hostname="", requested_ip="", server_id="", ciaddr=ZERO_IP, broadcast=True):
    mac_b = mac_to_bytes(mac)
    flags = 0x8000 if broadcast else 0x0000
    # BOOTP fixed header: op, htype, hlen, hops, xid, secs, flags, ciaddr, yiaddr, siaddr, giaddr, chaddr[16], sname[64], file[128]
    fixed = struct.pack(
        "!BBBBIHH4s4s4s4s16s64s128s",
        1, 1, 6, 0,
        int(xid) & 0xffffffff,
        0,
        flags,
        ip_to_bytes(ciaddr),
        b"\x00" * 4,
        b"\x00" * 4,
        b"\x00" * 4,
        mac_b + b"\x00" * 10,
        b"\x00" * 64,
        b"\x00" * 128,
    )
    opts = [DHCP_MAGIC, dhcp_option(53, [int(msg_type)])]
    opts.append(dhcp_option(61, [1] + list(mac_b)))  # client identifier
    if hostname:
        opts.append(dhcp_option(12, hostname.encode("ascii", "ignore")[:63]))
    if msg_type in (1, 3):
        opts.append(dhcp_option(55, [1, 3, 6, 15, 28, 51, 54, 58, 59]))
        opts.append(dhcp_option(57, struct.pack("!H", 1500)))
    if requested_ip:
        opts.append(dhcp_option(50, ip_to_bytes(requested_ip)))
    if server_id:
        opts.append(dhcp_option(54, ip_to_bytes(server_id)))
    opts.append(b"\xff")
    payload = fixed + b"".join(opts)
    if len(payload) < 300:
        payload += b"\x00" * (300 - len(payload))
    return payload


def add_vlan_headers(dst_mac, src_mac, vlan, qinq, inner_ethertype=ETH_P_IP):
    if vlan < 0:
        return dst_mac + src_mac + struct.pack("!H", inner_ethertype)
    if qinq < 0:
        return dst_mac + src_mac + struct.pack("!HHH", ETH_P_8021Q, int(vlan) & 0x0fff, inner_ethertype)
    return dst_mac + src_mac + struct.pack("!HHHHH", ETH_P_8021Q, int(vlan) & 0x0fff, ETH_P_8021Q, int(qinq) & 0x0fff, inner_ethertype)


def build_dhcp_frame(src_mac, dst_mac, dhcp_payload, src_ip=ZERO_IP, dst_ip=BROADCAST_IP, vlan=-1, qinq=-1):
    src_b = mac_to_bytes(src_mac)
    dst_b = mac_to_bytes(dst_mac) if isinstance(dst_mac, str) else dst_mac
    udp = build_udp_header(DHCP_CLIENT_PORT, DHCP_SERVER_PORT, dhcp_payload) + dhcp_payload
    ip = build_ipv4_header(src_ip, dst_ip, len(udp)) + udp
    eth = add_vlan_headers(dst_b, src_b, int(vlan), int(qinq), ETH_P_IP)
    return eth + ip


def build_arp_request_frame(src_mac, src_ip, target_ip, vlan=-1, qinq=-1):
    src_b = mac_to_bytes(src_mac)
    eth = add_vlan_headers(BROADCAST_MAC, src_b, int(vlan), int(qinq), ETH_P_ARP)
    arp = struct.pack(
        "!HHBBH6s4s6s4s",
        1, ETH_P_IP, 6, 4, 1,
        src_b, ip_to_bytes(src_ip), b"\x00" * 6, ip_to_bytes(target_ip),
    )
    return eth + arp


def helper_send_arp_refresh(sock, client, vlan, qinq, events):
    leased_ip = client.get("leased_ip") or client.get("offered_ip")
    if not leased_ip:
        return
    targets = []
    server_id = client.get("server_id") or client.get("server_ip")
    if server_id and server_id != ZERO_IP:
        targets.append(server_id)
    targets.append(leased_ip)
    for target_ip in dict.fromkeys(targets):
        try:
            frame = build_arp_request_frame(client["mac"], leased_ip, target_ip, vlan=vlan, qinq=qinq)
            sock.send(frame)
            events.append({
                "ts_local": ts_iso(), "event": "arp_refresh_sent", "mac": client["mac"],
                "leased_ip": leased_ip, "target_ip": target_ip,
            })
        except Exception as exc:
            events.append({
                "ts_local": ts_iso(), "event": "arp_refresh_error", "mac": client.get("mac", ""),
                "leased_ip": leased_ip, "target_ip": target_ip, "error": str(exc),
            })


def parse_dhcp_options(raw):
    opts = {}
    i = 0
    while i < len(raw):
        code = raw[i]
        i += 1
        if code == 0:
            continue
        if code == 255:
            break
        if i >= len(raw):
            break
        length = raw[i]
        i += 1
        data = raw[i:i + length]
        i += length
        opts.setdefault(code, []).append(data)
    return opts


def parse_dhcp_frame(frame):
    if len(frame) < 14:
        return None
    dst_mac = frame[0:6]
    src_mac = frame[6:12]
    offset = 12
    eth_type = struct.unpack("!H", frame[offset:offset + 2])[0]
    offset += 2
    vlan_tags = []
    while eth_type == ETH_P_8021Q and len(frame) >= offset + 4:
        tci = struct.unpack("!H", frame[offset:offset + 2])[0]
        vlan_tags.append(tci & 0x0fff)
        eth_type = struct.unpack("!H", frame[offset + 2:offset + 4])[0]
        offset += 4
    if eth_type != ETH_P_IP or len(frame) < offset + 20:
        return None
    ip_start = offset
    ver_ihl = frame[ip_start]
    ihl = (ver_ihl & 0x0f) * 4
    if (ver_ihl >> 4) != 4 or len(frame) < ip_start + ihl + 8:
        return None
    proto = frame[ip_start + 9]
    if proto != UDP_PROTO:
        return None
    src_ip = bytes_to_ip(frame[ip_start + 12:ip_start + 16])
    dst_ip = bytes_to_ip(frame[ip_start + 16:ip_start + 20])
    udp_start = ip_start + ihl
    src_port, dst_port, udp_len, _ = struct.unpack("!HHHH", frame[udp_start:udp_start + 8])
    if src_port != DHCP_SERVER_PORT or dst_port != DHCP_CLIENT_PORT:
        return None
    payload = frame[udp_start + 8:udp_start + max(8, udp_len)]
    if len(payload) < 240:
        return None
    try:
        op, htype, hlen, _hops, xid = struct.unpack("!BBBBI", payload[:8])
    except struct.error:
        return None
    if op != 2 or htype != 1 or hlen != 6:
        return None
    yiaddr = bytes_to_ip(payload[16:20])
    chaddr = payload[28:34]
    if payload[236:240] != DHCP_MAGIC:
        return None
    opts = parse_dhcp_options(payload[240:])
    msg_type = None
    if 53 in opts and opts[53] and opts[53][0]:
        msg_type = opts[53][0][0]
    server_id = ""
    if 54 in opts and opts[54] and len(opts[54][0]) == 4:
        server_id = bytes_to_ip(opts[54][0])
    lease_time = ""
    if 51 in opts and opts[51] and len(opts[51][0]) == 4:
        lease_time = struct.unpack("!I", opts[51][0])[0]
    return {
        "xid": xid,
        "msg_type": msg_type,
        "msg_name": DHCP_MSG.get(msg_type, str(msg_type)),
        "client_mac": mac_to_str(chaddr),
        "server_mac": mac_to_str(src_mac),
        "dst_mac": mac_to_str(dst_mac),
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "yiaddr": yiaddr,
        "server_id": server_id,
        "lease_time_sec": lease_time,
        "vlan_tags": ",".join(str(x) for x in vlan_tags),
    }


def helper_send_release(sock, iface, client, vlan, qinq, events):
    if not client.get("ack"):
        return
    leased_ip = client.get("leased_ip") or client.get("offered_ip")
    server_id = client.get("server_id") or client.get("server_ip") or BROADCAST_IP
    server_mac = client.get("server_mac") or "ff:ff:ff:ff:ff:ff"
    xid = random.randint(1, 0xffffffff)
    payload = build_dhcp_payload(
        7,
        client["mac"],
        xid,
        hostname=client.get("hostname", ""),
        server_id=client.get("server_id", ""),
        ciaddr=leased_ip or ZERO_IP,
        broadcast=False,
    )
    frame = build_dhcp_frame(
        src_mac=client["mac"],
        dst_mac=server_mac if server_mac else BROADCAST_MAC,
        dhcp_payload=payload,
        src_ip=leased_ip or ZERO_IP,
        dst_ip=server_id if server_id else BROADCAST_IP,
        vlan=vlan,
        qinq=qinq,
    )
    try:
        sock.send(frame)
        events.append({"ts_local": ts_iso(), "event": "dhcp_release_sent", "mac": client["mac"], "leased_ip": leased_ip, "server_id": server_id})
    except Exception as exc:
        events.append({"ts_local": ts_iso(), "event": "dhcp_release_error", "mac": client["mac"], "error": str(exc)})


def scratch_dhcp_exchange(iface, clients, mac_base, timeout_sec, poll_interval_sec, hostname_prefix, vlan=-1, qinq=-1, release_at_end=True):
    start = time.monotonic()
    events = []
    states = {}
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    sock.bind((iface, 0))
    sock.settimeout(float(poll_interval_sec))
    try:
        for i in range(int(clients)):
            mac = mac_from_base(mac_base, i)
            xid = random.randint(1, 0xffffffff)
            hostname = f"{hostname_prefix}{i + 1:06d}"
            payload = build_dhcp_payload(1, mac, xid, hostname=hostname, broadcast=True)
            frame = build_dhcp_frame(mac, BROADCAST_MAC, payload, src_ip=ZERO_IP, dst_ip=BROADCAST_IP, vlan=vlan, qinq=qinq)
            send_ts = time.monotonic()
            sock.send(frame)
            states[xid] = {
                "client_index": i,
                "mac": mac,
                "hostname": hostname,
                "xid": xid,
                "discover_sent_ts": send_ts,
                "offer": False,
                "request_sent": False,
                "ack": False,
                "nak": False,
                "offered_ip": "",
                "leased_ip": "",
                "server_id": "",
                "server_mac": "",
                "error": "",
            }
            events.append({"ts_local": ts_iso(), "event": "dhcp_discover_sent", "client_index": i, "mac": mac, "xid": xid})

        deadline = start + float(timeout_sec)
        while time.monotonic() < deadline:
            if all(s.get("ack") or s.get("nak") for s in states.values()):
                break
            try:
                frame = sock.recv(65535)
            except socket.timeout:
                continue
            except InterruptedError:
                continue
            now = time.monotonic()
            pkt = parse_dhcp_frame(frame)
            if not pkt:
                continue
            state = states.get(pkt["xid"])
            if not state:
                continue
            if pkt.get("client_mac") and pkt["client_mac"].lower() != state["mac"].lower():
                continue
            msg_type = pkt.get("msg_type")
            events.append({
                "ts_local": ts_iso(),
                "event": "dhcp_packet_rx",
                "client_index": state["client_index"],
                "mac": state["mac"],
                "xid": state["xid"],
                "msg_type": msg_type,
                "msg_name": pkt.get("msg_name"),
                "yiaddr": pkt.get("yiaddr"),
                "server_id": pkt.get("server_id"),
                "server_mac": pkt.get("server_mac"),
                "src_ip": pkt.get("src_ip"),
            })
            if msg_type == 2 and not state.get("request_sent"):
                state["offer"] = True
                state["offer_rx_ts"] = now
                state["offered_ip"] = pkt.get("yiaddr") or ""
                state["server_id"] = pkt.get("server_id") or pkt.get("src_ip") or ""
                state["server_ip"] = pkt.get("src_ip") or ""
                state["server_mac"] = pkt.get("server_mac") or ""
                payload = build_dhcp_payload(
                    3,
                    state["mac"],
                    state["xid"],
                    hostname=state["hostname"],
                    requested_ip=state["offered_ip"],
                    server_id=state["server_id"],
                    broadcast=True,
                )
                req_frame = build_dhcp_frame(state["mac"], BROADCAST_MAC, payload, ZERO_IP, BROADCAST_IP, vlan=vlan, qinq=qinq)
                state["request_sent_ts"] = time.monotonic()
                state["request_sent"] = True
                sock.send(req_frame)
                events.append({"ts_local": ts_iso(), "event": "dhcp_request_sent", "client_index": state["client_index"], "mac": state["mac"], "xid": state["xid"], "requested_ip": state["offered_ip"], "server_id": state["server_id"]})
            elif msg_type == 5:
                state["ack"] = True
                state["ack_rx_ts"] = now
                state["leased_ip"] = pkt.get("yiaddr") or state.get("offered_ip") or ""
                state["server_id"] = pkt.get("server_id") or state.get("server_id") or pkt.get("src_ip") or ""
                state["server_ip"] = pkt.get("src_ip") or state.get("server_ip") or ""
                state["server_mac"] = pkt.get("server_mac") or state.get("server_mac") or ""
                state["lease_time_sec"] = pkt.get("lease_time_sec", "")
                helper_send_arp_refresh(sock, state, vlan, qinq, events)
            elif msg_type == 6:
                state["nak"] = True
                state["nak_rx_ts"] = now
                state["error"] = "DHCPNAK"

        if release_at_end:
            for state in states.values():
                helper_send_release(sock, iface, state, vlan, qinq, events)
    finally:
        sock.close()

    end = time.monotonic()
    client_rows = []
    for state in sorted(states.values(), key=lambda x: x["client_index"]):
        ack = bool(state.get("ack"))
        offer = bool(state.get("offer"))
        discover_ts = state.get("discover_sent_ts")
        offer_ts = state.get("offer_rx_ts")
        request_ts = state.get("request_sent_ts")
        ack_ts = state.get("ack_rx_ts")
        client_rows.append({
            "client_index": state["client_index"],
            "mac": state["mac"],
            "xid": state["xid"],
            "ok": 1 if ack else 0,
            "offer_received": 1 if offer else 0,
            "request_sent": 1 if state.get("request_sent") else 0,
            "ack_received": 1 if ack else 0,
            "nak_received": 1 if state.get("nak") else 0,
            "offered_ip": state.get("offered_ip", ""),
            "leased_ip": state.get("leased_ip", ""),
            "server_id": state.get("server_id", ""),
            "server_mac": state.get("server_mac", ""),
            "lease_time_sec": state.get("lease_time_sec", ""),
            "discover_to_offer_sec": f"{(offer_ts - discover_ts):.6f}" if offer_ts and discover_ts else "",
            "request_to_ack_sec": f"{(ack_ts - request_ts):.6f}" if ack_ts and request_ts else "",
            "discover_to_ack_sec": f"{(ack_ts - discover_ts):.6f}" if ack_ts and discover_ts else "",
            "error": state.get("error", "") if not ack else "",
        })
    return {"client_rows": client_rows, "events": events, "elapsed_sec": end - start}


def helper_main(args):
    result = scratch_dhcp_exchange(
        iface=args.helper_iface,
        clients=args.helper_clients,
        mac_base=args.helper_mac_base,
        timeout_sec=args.helper_timeout_sec,
        poll_interval_sec=args.helper_poll_interval_sec,
        hostname_prefix=args.helper_hostname_prefix,
        vlan=args.helper_vlan,
        qinq=args.helper_qinq,
        release_at_end=(not args.helper_no_release),
    )
    print(json.dumps(result, ensure_ascii=False, default=str))


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
        target["trex_cores"] = args.cores
    return target


def setup_dhcp_namespace(args, target, log_writer):
    # L4 latency uses rb_lan/rb_wan.  Delete them here as a safety net so a
    # previous failed test does not keep the physical NIC outside the root ns.
    for ns in (args.dhcp_ns, "rb_lan", "rb_wan"):
        delete_namespace_if_exists(ns, log_writer, reason="delete_stale_namespace")

    # prepare_kernel_interfaces_from_profile and resolve_profile_interfaces come
    # from the L4 script.  They use profile PCI BDF and lan_iface/wan_iface.
    args.auto_kernel_bind = not args.no_auto_kernel_bind
    args.lan_ns = args.dhcp_ns
    args.wan_ns = "root"
    prepare_kernel_interfaces_from_profile(args, target, log_writer)
    resolve_profile_interfaces(args, target, log_writer)

    if not args.lan_iface:
        raise SystemExit("ERROR: DHCP scratch test could not resolve LAN interface. Set lan_iface in tests/config/target_profiles.json or pass --lan-iface.")
    if not root_iface_exists(args.lan_iface):
        raise SystemExit(f"ERROR: LAN interface is not visible in the root namespace: {args.lan_iface}")

    log_writer.writerow([ts_iso(), "add_namespace", args.dhcp_ns])
    run_cmd(["ip", "netns", "add", args.dhcp_ns])
    run_cmd(["ip", "-n", args.dhcp_ns, "link", "set", "lo", "up"])
    log_writer.writerow([ts_iso(), "move_iface_to_namespace", f"{args.lan_iface}->{args.dhcp_ns}"])
    run_cmd(["ip", "link", "set", args.lan_iface, "netns", args.dhcp_ns])
    run_cmd(["ip", "-n", args.dhcp_ns, "addr", "flush", "dev", args.lan_iface])
    run_cmd(["ip", "-n", args.dhcp_ns, "link", "set", "dev", args.lan_iface, "promisc", "on"])
    run_cmd(["ip", "-n", args.dhcp_ns, "link", "set", "dev", args.lan_iface, "up"])
    wait_namespace_interface_ready(args.dhcp_ns, args.lan_iface, args.interface_ready_timeout_sec)
    if args.post_namespace_settle_sec > 0:
        time.sleep(float(args.post_namespace_settle_sec))


def cleanup_dhcp_namespace(args, target, out_dir, log_writer):
    if getattr(args, "lan_iface", "") and namespace_exists(args.dhcp_ns):
        log_writer.writerow([ts_iso(), "return_iface_to_root", f"{args.dhcp_ns}:{args.lan_iface}"])
        run_cmd(["ip", "-n", args.dhcp_ns, "link", "set", args.lan_iface, "netns", "1"], check=False)
    if namespace_exists(args.dhcp_ns):
        log_writer.writerow([ts_iso(), "delete_namespace", args.dhcp_ns])
        run_cmd(["ip", "netns", "delete", args.dhcp_ns], check=False)
    if getattr(args, "restore_dpdk_after", True):
        try:
            prep_log = prepare_trex_interfaces_from_profile(
                target,
                out_dir,
                auto_dpdk_bind=True,
                dpdk_driver_override=getattr(args, "dpdk_driver", "auto"),
            )
            log_writer.writerow([ts_iso(), "restore_dpdk_after_done", str(prep_log)])
        except Exception as exc:
            log_writer.writerow([ts_iso(), "restore_dpdk_after_warning", str(exc)])
            print(f"WARNING: failed to restore DPDK driver state after DHCP scratch test: {exc}", flush=True)


def run_helper_trial(args, phase, trial, clients, mac_base):
    cmd = [
        "ip", "netns", "exec", args.dhcp_ns,
        sys.executable, str(Path(__file__).resolve()),
        "--_dhcp-helper",
        "--helper-iface", args.lan_iface,
        "--helper-clients", str(int(clients)),
        "--helper-mac-base", mac_base,
        "--helper-timeout-sec", str(float(args.dhcp_timeout_sec)),
        "--helper-poll-interval-sec", str(float(args.dhcp_poll_interval_sec)),
        "--helper-hostname-prefix", args.dhcp_hostname_prefix,
        "--helper-vlan", str(int(args.dhcp_vlan)),
        "--helper-qinq", str(int(args.dhcp_qinq)),
    ]
    if not args.dhcp_release_at_end:
        cmd.append("--helper-no-release")
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            "DHCP scratch helper failed: " + " ".join(shlex.quote(x) for x in cmd) +
            f"\nexit_code={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:
        raise RuntimeError(f"failed to parse DHCP helper JSON: {exc}\nstdout={proc.stdout}\nstderr={proc.stderr}") from exc

    client_rows = result.get("client_rows") or []
    events = result.get("events") or []
    for row in client_rows:
        row.update({"phase": phase, "trial": trial, "mac_base": mac_base})
    for ev in events:
        ev.update({"phase": phase, "trial": trial, "mac_base": mac_base})

    ok_count = sum(1 for row in client_rows if str(row.get("ok")) == "1")
    offer_count = sum(1 for row in client_rows if str(row.get("offer_received")) == "1")
    fail_count = int(clients) - ok_count
    latencies = []
    for row in client_rows:
        try:
            if row.get("discover_to_ack_sec") != "":
                latencies.append(float(row.get("discover_to_ack_sec")))
        except Exception:
            pass
    trial_latency = max(latencies) if latencies else float(result.get("elapsed_sec") or 0.0)
    trial_row = {
        "phase": phase,
        "trial": trial,
        "clients_requested": int(clients),
        "clients_offered": offer_count,
        "clients_bound": ok_count,
        "clients_failed": fail_count,
        "success_rate": (ok_count / int(clients)) if int(clients) else 0.0,
        "failure_rate": (fail_count / int(clients)) if int(clients) else 0.0,
        "latency_sec": f"{trial_latency:.6f}",
        "helper_elapsed_sec": f"{float(result.get('elapsed_sec') or 0.0):.6f}",
        "all_bound": 1 if ok_count == int(clients) else 0,
        "timeout_sec": args.dhcp_timeout_sec,
        "mac_base": mac_base,
        "measurement_note": "Scratch raw-socket DHCP DORA. Latency is discover-to-ACK, or helper elapsed time on failure.",
    }
    for row in client_rows:
        row["trial_latency_sec"] = trial_row["latency_sec"]
        row["trial_all_bound"] = trial_row["all_bound"]
    return trial_row, client_rows, events


def run_dhcp_preflight(args, details_dir, phase, trial):
    """Require three consecutive DHCP DORA successes before a measured trial."""

    preflight_args = argparse.Namespace(**vars(args))
    preflight_args.dhcp_release_at_end = True
    phase_offsets = {
        "single_client_latency": 100000,
        "burst_clients_latency": 200000,
        "reconnect": 300000,
    }
    phase_offset = phase_offsets[phase]

    def probe(attempt):
        mac_base = trial_mac_base(
            args.dhcp_client_mac_base,
            phase_offset + int(trial) * 100 + int(attempt),
        )
        trial_row, _client_rows, _events = run_helper_trial(
            preflight_args,
            phase=f"preflight_{phase}_attempt{attempt}",
            trial=trial,
            clients=1,
            mac_base=mac_base,
        )
        if int(trial_row.get("all_bound") or 0) != 1:
            raise RuntimeError(
                f"DHCP DORA failed: bound={trial_row.get('clients_bound')}/"
                f"{trial_row.get('clients_requested')}"
            )
        return trial_row

    return run_consecutive_preflight(
        probe,
        phase=f"dhcp_{phase}_trial_{trial}",
        csv_path=Path(details_dir) / "dhcp_preflight_recovery.csv",
        required_successes=args.preflight_success_consecutive,
        interval_sec=args.preflight_probe_interval_sec,
        timeout_sec=args.preflight_timeout_sec,
    )


def run_reconnect_trial(args, pair_index, mac_base):
    initial_trial, initial_clients, initial_events = run_helper_trial(
        args,
        phase="reconnect_initial",
        trial=pair_index,
        clients=1,
        mac_base=mac_base,
    )
    wait_start = time.time()
    time.sleep(float(args.dhcp_reconnect_wait_sec))
    reconnect_trial, reconnect_clients, reconnect_events = run_helper_trial(
        args,
        phase="reconnect_after_disconnect",
        trial=pair_index,
        clients=1,
        mac_base=mac_base,
    )
    wait_row = {
        "ts_local": ts_iso(),
        "event": "reconnect_wait_done",
        "phase": "reconnect",
        "trial": pair_index,
        "mac_base": mac_base,
        "requested_wait_sec": args.dhcp_reconnect_wait_sec,
        "actual_wait_sec": f"{(time.time() - wait_start):.6f}",
    }
    pair_row = {
        "phase": "reconnect_summary",
        "trial": pair_index,
        "mac_base": mac_base,
        "reconnect_wait_sec": args.dhcp_reconnect_wait_sec,
        "initial_success": initial_trial.get("all_bound", 0),
        "reconnect_success": reconnect_trial.get("all_bound", 0),
        "both_success": 1 if int(initial_trial.get("all_bound", 0)) == 1 and int(reconnect_trial.get("all_bound", 0)) == 1 else 0,
        "initial_latency_sec": initial_trial.get("latency_sec", ""),
        "reconnect_latency_sec": reconnect_trial.get("latency_sec", ""),
        "measurement_note": "Same MAC is used before and after DHCP RELEASE plus wait.",
    }
    return pair_row, [initial_trial, reconnect_trial], initial_clients + reconnect_clients, initial_events + [wait_row] + reconnect_events


def summarize_trials(args, trial_rows, elapsed_sec):
    total_clients = sum(int(r.get("clients_requested", 0)) for r in trial_rows)
    total_bound = sum(int(r.get("clients_bound", 0)) for r in trial_rows)
    total_failed = sum(int(r.get("clients_failed", 0)) for r in trial_rows)
    rows = [{
        "phase": "summary",
        "test_name": "dhcpv4_scratch_basic_latency",
        "tool": "python_raw_af_packet",
        "single_client_repeats": int(args.dhcp_single_repeats),
        "burst_clients": int(args.dhcp_burst_clients),
        "burst_repeats": int(args.dhcp_burst_repeats),
        "reconnect_repeats": int(args.dhcp_reconnect_repeats),
        "reconnect_wait_sec": float(args.dhcp_reconnect_wait_sec),
        "total_trials": len(trial_rows),
        "total_clients_requested": total_clients,
        "total_clients_bound": total_bound,
        "total_clients_failed": total_failed,
        "success_rate": (total_bound / total_clients) if total_clients else 0.0,
        "failure_rate": (total_failed / total_clients) if total_clients else 0.0,
        "elapsed_sec": f"{elapsed_sec:.6f}",
        "measurement_note": "DHCP is stateful; this is a basic DORA success/latency test, not a sustained QPS test.",
    }]
    rows.extend(trial_rows)
    return rows


def write_metadata(out_dir, target_name, target, args):
    data = {
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "script": Path(__file__).name,
        "target_name": target_name,
        "target": target,
        "args": vars(args),
        "tools": {
            "dhcp": "Python AF_PACKET raw socket DHCPv4 client",
            "namespace": "iproute2 ip netns",
            "driver_state": "kernel driver during DHCP; DPDK restore after test",
        },
        "notes": [
            "DHCPv4 lease acquisition is implemented from scratch and does not use TRex EMU.",
            "The test creates forged client MACs and performs DHCP DISCOVER/OFFER/REQUEST/ACK over raw Ethernet frames.",
            "The LAN NIC is moved into a dedicated namespace so the host DHCP configuration is not modified.",
        ],
    }
    (Path(out_dir) / "metadata.json").write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def run(args):
    require_root()
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_RESULT_ROOT / f"dhcp_scratch_{args.target}_{ts_compact()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    details_dir = prepare_details_dir(out_dir)

    target = merge_target_with_overrides(TARGETS[args.target], args)
    write_metadata(details_dir, args.target, target, args)

    events = []
    client_rows = []
    trial_rows = []
    reconnect_rows = []
    start_time = time.time()
    ns_log_path = details_dir / "namespace_setup.csv"
    with ns_log_path.open("w", newline="", encoding="utf-8") as f:
        ns_log = csv.writer(f)
        ns_log.writerow(["ts_local", "action", "detail"])
        print("============================================================")
        print("IPv4 DHCPv4 basic latency using scratch raw-socket client")
        print("============================================================")
        print(f"Target          : {args.target}")
        print(f"Description     : {target.get('description', '')}")
        print("Tools           : Python AF_PACKET raw socket DHCPv4 client, ip(iproute2 namespaces)")
        print(f"LAN namespace   : {args.dhcp_ns}")
        print(f"LAN profile iface: {target.get('lan_iface', '(not defined)')}")
        print(f"Repeat          : {args.repeat}")
        print(f"Single repeats  : {args.dhcp_single_repeats}")
        print(f"Burst           : clients={args.dhcp_burst_clients} repeats={args.dhcp_burst_repeats}")
        print(f"Reconnect       : repeats={args.dhcp_reconnect_repeats} wait={args.dhcp_reconnect_wait_sec}s")
        print(f"Timeout/trial   : {args.dhcp_timeout_sec}s")
        print(f"Output          : {out_dir}")
        print("============================================================")
        try:
            setup_dhcp_namespace(args, target, ns_log)
            wait_before_test_start(args.test_start_delay_sec, "DHCP scratch benchmark")
            print(f"LAN interface   : {args.lan_iface}", flush=True)
            trial_no = 0
            for rep in range(int(args.dhcp_single_repeats)):
                trial_no += 1
                mac_base = trial_mac_base(args.dhcp_client_mac_base, trial_no)
                print(f"--- DHCP single_client_latency trial={rep + 1} clients=1 ---", flush=True)
                run_dhcp_preflight(
                    args, details_dir, "single_client_latency", rep + 1
                )
                trial_row, rows, evs = run_helper_trial(args, "single_client_latency", rep + 1, 1, mac_base)
                trial_rows.append(trial_row)
                client_rows.extend(rows)
                events.extend(evs)
                print(f"single trial={rep + 1}: success={trial_row['clients_bound']}/{trial_row['clients_requested']} latency={trial_row['latency_sec']}s", flush=True)
                time.sleep(float(args.dhcp_between_trials_sec))

            for rep in range(int(args.dhcp_burst_repeats)):
                trial_no += 1
                mac_base = trial_mac_base(args.dhcp_client_mac_base, trial_no)
                print(f"--- DHCP burst_clients_latency trial={rep + 1} clients={args.dhcp_burst_clients} ---", flush=True)
                run_dhcp_preflight(
                    args, details_dir, "burst_clients_latency", rep + 1
                )
                trial_row, rows, evs = run_helper_trial(args, "burst_clients_latency", rep + 1, int(args.dhcp_burst_clients), mac_base)
                trial_rows.append(trial_row)
                client_rows.extend(rows)
                events.extend(evs)
                print(f"burst trial={rep + 1}: success={trial_row['clients_bound']}/{trial_row['clients_requested']} latency={trial_row['latency_sec']}s", flush=True)
                time.sleep(float(args.dhcp_between_trials_sec))

            for rep in range(int(args.dhcp_reconnect_repeats)):
                trial_no += 1
                mac_base = trial_mac_base(args.dhcp_client_mac_base, trial_no)
                print(f"--- DHCP reconnect trial={rep + 1} wait={args.dhcp_reconnect_wait_sec}s ---", flush=True)
                run_dhcp_preflight(args, details_dir, "reconnect", rep + 1)
                reconnect_row, pair_trials, rows, evs = run_reconnect_trial(args, rep + 1, mac_base)
                reconnect_rows.append(reconnect_row)
                trial_rows.extend(pair_trials)
                client_rows.extend(rows)
                events.extend(evs)
                print(f"reconnect trial={rep + 1}: initial={reconnect_row['initial_success']} reconnect={reconnect_row['reconnect_success']}", flush=True)
                time.sleep(float(args.dhcp_between_trials_sec))
        finally:
            cleanup_dhcp_namespace(args, target, details_dir, ns_log)

    elapsed = time.time() - start_time
    summary_rows = summarize_trials(args, trial_rows, elapsed)
    # New scratch names.
    write_csv(details_dir / "dhcp_scratch_trials.csv", trial_rows)
    write_csv(details_dir / "dhcp_scratch_reconnect.csv", reconnect_rows)
    write_csv(details_dir / "dhcp_scratch_clients.csv", client_rows)
    write_csv(details_dir / "dhcp_scratch_events.csv", events)
    # Legacy filenames kept so existing post-processing does not break.
    write_csv(details_dir / "dhcp_emu_trials.csv", trial_rows)
    write_csv(details_dir / "dhcp_emu_reconnect.csv", reconnect_rows)
    write_csv(details_dir / "dhcp_emu_clients.csv", client_rows)
    write_csv(details_dir / "dhcp_emu_events.csv", events)
    write_csv(out_dir / "summary_all.csv", summary_rows)
    print("=== DHCP scratch summary ===")
    for row in summary_rows:
        print(json.dumps(row, ensure_ascii=False, default=str))
    print(f"Results: {out_dir}")
    print(f"Details: {details_dir}")


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Scratch raw-socket DHCPv4 lease acquisition benchmark", allow_abbrev=False)
    parser.add_argument("--target", choices=TARGETS.keys(), default=_DHCP_DEFAULTS["target"])
    parser.add_argument("--out-dir", default="")
    parser.add_argument(
        "--repeat",
        type=argparse_repeat,
        default=DEFAULT_REPEAT,
        help="Repeat the complete DHCP benchmark. Default: 1.",
    )
    parser.add_argument("--interfaces", type=parse_interfaces, default=None)
    parser.add_argument("--lan-ip", default="")
    parser.add_argument("--lan-gw", default="")
    parser.add_argument("--wan-ip", default="")
    parser.add_argument("--wan-gw", default="")
    parser.add_argument("--router-wan-ip", default="")
    parser.add_argument("--line-rate-gbps", type=float, default=None)
    parser.add_argument("--cores", type=int, default=None)
    parser.add_argument("--lan-iface", default="", help="Linux interface connected to DUT LAN. Default: target profile lan_iface.")
    parser.add_argument("--wan-iface", default="", help="Linux interface connected to DUT WAN. Only used to keep shared profile interface resolver compatible; DHCP traffic uses LAN only.")
    parser.add_argument("--dhcp-ns", default=_DHCP_DEFAULTS["namespace"], help="Network namespace used for scratch DHCP client.")
    parser.add_argument("--interface-ready-timeout-sec", type=float, default=_DHCP_DEFAULTS["interface_ready_timeout_sec"])
    parser.add_argument("--post-namespace-settle-sec", type=float, default=_DHCP_DEFAULTS["post_namespace_settle_sec"])
    parser.add_argument("--test-start-delay-sec", type=float, default=DEFAULT_TEST_START_DELAY_SEC, help="Seconds to wait after the DHCP namespace interface reports link-up and before testing. Default: 15.")
    parser.add_argument("--no-auto-kernel-bind", action="store_true", help="Do not bind profile PCI devices back to kernel drivers before DHCP.")
    parser.add_argument("--restore-dpdk-after", dest="restore_dpdk_after", action="store_true", default=_DHCP_DEFAULTS["restore_dpdk_after"])
    parser.add_argument("--no-restore-dpdk-after", dest="restore_dpdk_after", action="store_false")
    parser.add_argument("--dpdk-driver", default=_DHCP_DEFAULTS["dpdk_driver"])

    parser.add_argument("--dhcp-clients", type=int, default=None, help="Compatibility alias. Use --dhcp-burst-clients.")
    parser.add_argument("--dhcp-client-mac-base", default=_DHCP_DEFAULTS["client_mac_base"])
    parser.add_argument("--dhcp-vlan", type=int, default=_DHCP_DEFAULTS["vlan"], help="-1 means untagged.")
    parser.add_argument("--dhcp-qinq", type=int, default=_DHCP_DEFAULTS["qinq"], help="-1 means no second VLAN tag.")
    parser.add_argument("--dhcp-hostname-prefix", default=_DHCP_DEFAULTS["hostname_prefix"])
    parser.add_argument("--dhcp-single-repeats", type=int, default=_DHCP_DEFAULTS["single_repeats"], help="Number of 1-client latency trials.")
    parser.add_argument("--dhcp-burst-clients", type=int, default=_DHCP_DEFAULTS["burst_clients"], help="Number of simultaneous clients in the burst trial.")
    parser.add_argument("--dhcp-burst-repeats", type=int, default=_DHCP_DEFAULTS["burst_repeats"], help="Number of burst trials.")
    parser.add_argument("--dhcp-reconnect-repeats", type=int, default=_DHCP_DEFAULTS["reconnect_repeats"], help="Number of same-MAC reconnect trials.")
    parser.add_argument("--dhcp-reconnect-wait-sec", type=float, default=_DHCP_DEFAULTS["reconnect_wait_sec"], help="Wait time between release and same-MAC reconnect.")
    parser.add_argument("--dhcp-timeout-sec", type=float, default=_DHCP_DEFAULTS["timeout_sec"], help="Maximum wait time per trial.")
    parser.add_argument("--dhcp-poll-interval-sec", type=float, default=_DHCP_DEFAULTS["poll_interval_sec"], help="Socket receive timeout while waiting for DORA packets.")
    parser.add_argument("--dhcp-between-trials-sec", type=float, default=_DHCP_DEFAULTS["between_trials_sec"], help="Pause between trials.")
    parser.add_argument("--dhcp-release-at-end", dest="dhcp_release_at_end", action="store_true", default=_DHCP_DEFAULTS["release_at_end"], help="Send DHCP RELEASE for ACKed clients at the end of each helper trial.")
    parser.add_argument("--no-dhcp-release-at-end", dest="dhcp_release_at_end", action="store_false")
    add_consecutive_preflight_arguments(parser)

    # Internal helper mode.  Kept visible in --help only as suppressed options.
    parser.add_argument("--_dhcp-helper", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--helper-iface", default="", help=argparse.SUPPRESS)
    parser.add_argument("--helper-clients", type=int, default=1, help=argparse.SUPPRESS)
    parser.add_argument("--helper-mac-base", default=_DHCP_DEFAULTS["client_mac_base"], help=argparse.SUPPRESS)
    parser.add_argument("--helper-timeout-sec", type=float, default=_DHCP_DEFAULTS["timeout_sec"], help=argparse.SUPPRESS)
    parser.add_argument("--helper-poll-interval-sec", type=float, default=_DHCP_DEFAULTS["poll_interval_sec"], help=argparse.SUPPRESS)
    parser.add_argument("--helper-hostname-prefix", default=_DHCP_DEFAULTS["hostname_prefix"], help=argparse.SUPPRESS)
    parser.add_argument("--helper-vlan", type=int, default=_DHCP_DEFAULTS["vlan"], help=argparse.SUPPRESS)
    parser.add_argument("--helper-qinq", type=int, default=_DHCP_DEFAULTS["qinq"], help=argparse.SUPPRESS)
    parser.add_argument("--helper-no-release", action="store_true", help=argparse.SUPPRESS)
    return parser


def validate_args(args):
    if args.dhcp_clients is not None:
        args.dhcp_burst_clients = args.dhcp_clients
    if args.dhcp_single_repeats < 0:
        raise SystemExit("ERROR: --dhcp-single-repeats must be >= 0")
    if args.dhcp_burst_repeats < 0:
        raise SystemExit("ERROR: --dhcp-burst-repeats must be >= 0")
    if args.dhcp_reconnect_repeats < 0:
        raise SystemExit("ERROR: --dhcp-reconnect-repeats must be >= 0")
    if args.dhcp_reconnect_wait_sec < 0:
        raise SystemExit("ERROR: --dhcp-reconnect-wait-sec must be >= 0")
    if args.test_start_delay_sec < 0:
        raise SystemExit("ERROR: --test-start-delay-sec must be >= 0")
    if args.dhcp_burst_clients <= 0:
        raise SystemExit("ERROR: --dhcp-burst-clients must be positive")
    if args.dhcp_timeout_sec <= 0:
        raise SystemExit("ERROR: --dhcp-timeout-sec must be positive")
    if args.dhcp_poll_interval_sec <= 0:
        raise SystemExit("ERROR: --dhcp-poll-interval-sec must be positive")
    if args.dhcp_between_trials_sec < 0:
        raise SystemExit("ERROR: --dhcp-between-trials-sec must be >= 0")
    if args.interface_ready_timeout_sec <= 0:
        raise SystemExit("ERROR: --interface-ready-timeout-sec must be positive")
    if args.post_namespace_settle_sec < 0:
        raise SystemExit("ERROR: --post-namespace-settle-sec must be >= 0")
    validate_consecutive_preflight_arguments(args)


def main():
    parser = build_arg_parser()
    args = parser.parse_args()
    if args._dhcp_helper:
        helper_main(args)
        return
    validate_args(args)
    repeat_out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else DEFAULT_RESULT_ROOT / f"dhcp_scratch_{args.target}_{ts_compact()}"
    )
    repeated_rc = run_repeated_subprocesses(
        args.repeat,
        repeat_out_dir,
        summary_filenames=("summary_all.csv",),
        script_path=sys.argv[0],
        cwd=REPO_DIR,
    )
    if repeated_rc is not None:
        if repeated_rc != 0:
            raise SystemExit(repeated_rc)
        return
    try:
        run(args)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        traceback.print_exc()
        raise SystemExit(f"ERROR: {exc}")


if __name__ == "__main__":
    main()
