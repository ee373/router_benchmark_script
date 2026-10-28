#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DS-Lite profile validation and wire-rate calculations."""

from ipaddress import ip_address, ip_network


ETHERNET_HEADER_BYTES = 14
ETHERNET_FCS_BYTES = 4
IPV4_HEADER_BYTES = 20
UDP_HEADER_BYTES = 8

REQUIRED_PROFILE_FIELDS = (
    "description",
    "benchmark_mode",
    "line_rate_gbps",
    "interfaces",
    "kernel_drivers",
    "dpdk_drivers",
    "lan_iface",
    "wan_iface",
    "lan_port",
    "wan_port",
    "lan_ip",
    "lan_prefix_len",
    "lan_gw",
    "wan_link_ip",
    "wan_prefix_len",
    "wan_gateway_ip",
    "wan_arp_target",
    "server_network",
    "server_ipv4",
    "nat_external_ip_mode",
    "nat_pool_cidr",
    "nat_external_port_mode",
    "inner_ipv4_mtu",
    "tcp_mss",
    "tunnel_overhead_bytes",
    "b4_session_limit",
    "trex_cores",
)


def validate_dslite_profile(name, profile):
    """Validate one resolved DS-Lite target profile."""
    missing = [key for key in REQUIRED_PROFILE_FIELDS if key not in profile]
    if missing:
        raise ValueError(f"DS-Lite profile {name!r} is missing: {', '.join(missing)}")
    if profile["benchmark_mode"] != "dslite":
        raise ValueError(f"DS-Lite profile {name!r} must set benchmark_mode=dslite")
    if len(profile["interfaces"]) != 2:
        raise ValueError(f"DS-Lite profile {name!r} must define exactly two interfaces")
    if int(profile["lan_port"]) == int(profile["wan_port"]):
        raise ValueError(f"DS-Lite profile {name!r} must use different LAN and WAN ports")
    if float(profile["line_rate_gbps"]) <= 0:
        raise ValueError(f"DS-Lite profile {name!r} line_rate_gbps must be positive")
    for key in (
        "lan_ip",
        "lan_gw",
        "wan_link_ip",
        "wan_gateway_ip",
        "wan_arp_target",
        "server_ipv4",
    ):
        if ip_address(profile[key]).version != 4:
            raise ValueError(f"DS-Lite profile {name!r} {key} must be IPv4")
    server_network = ip_network(profile["server_network"], strict=True)
    if server_network.version != 4:
        raise ValueError(f"DS-Lite profile {name!r} server_network must be IPv4")
    server_address = ip_address(profile["server_ipv4"])
    if server_address not in server_network:
        raise ValueError(
            f"DS-Lite profile {name!r} server_ipv4 must be in server_network"
        )
    if server_address in (server_network.network_address, server_network.broadcast_address):
        raise ValueError(
            f"DS-Lite profile {name!r} server_ipv4 must be a usable host address"
        )
    if server_address == ip_address(profile["wan_gateway_ip"]):
        raise ValueError(
            f"DS-Lite profile {name!r} server_ipv4 must not be the WAN gateway"
        )
    pool = ip_network(profile["nat_pool_cidr"], strict=False)
    if pool.version != 4:
        raise ValueError(f"DS-Lite profile {name!r} nat_pool_cidr must be IPv4")
    if ip_address(profile["wan_arp_target"]) in pool:
        raise ValueError(
            f"DS-Lite profile {name!r} wan_arp_target must not be inside nat_pool_cidr"
        )
    if profile["nat_external_ip_mode"] != "discover":
        raise ValueError(f"DS-Lite profile {name!r} supports only nat_external_ip_mode=discover")
    if profile["nat_external_port_mode"] != "discover":
        raise ValueError(f"DS-Lite profile {name!r} supports only nat_external_port_mode=discover")
    if int(profile["inner_ipv4_mtu"]) <= IPV4_HEADER_BYTES + UDP_HEADER_BYTES:
        raise ValueError(f"DS-Lite profile {name!r} inner_ipv4_mtu is too small")
    if int(profile["tunnel_overhead_bytes"]) <= 0:
        raise ValueError(f"DS-Lite profile {name!r} tunnel_overhead_bytes must be positive")
    if int(profile["b4_session_limit"]) <= 0:
        raise ValueError(f"DS-Lite profile {name!r} b4_session_limit must be positive")
    return profile


def build_trex_target(profile):
    """Add aliases required by the shared TRex configuration writer."""
    target = dict(profile)
    target["wan_ip"] = profile["wan_link_ip"]
    target["wan_gw"] = profile["wan_arp_target"]
    target["router_wan_ip"] = profile["wan_gateway_ip"]
    target["server_ip"] = profile["server_ipv4"]
    return target


def frame_without_fcs_bytes(frame_size):
    """Return the packet-builder size for an Ethernet frame including FCS."""
    size = int(frame_size)
    if size <= ETHERNET_FCS_BYTES:
        raise ValueError(f"frame_size is too small: {frame_size}")
    return size - ETHERNET_FCS_BYTES


def udp_payload_bytes(frame_size):
    """Return UDP payload size for an on-wire Ethernet frame including FCS."""
    payload = (
        frame_without_fcs_bytes(frame_size)
        - ETHERNET_HEADER_BYTES
        - IPV4_HEADER_BYTES
        - UDP_HEADER_BYTES
    )
    if payload < 0:
        raise ValueError(f"frame_size is too small for Ethernet/IPv4/UDP: {frame_size}")
    return payload


def max_inner_frame_size(inner_ipv4_mtu):
    """Return the largest inner Ethernet frame, including FCS."""
    return int(inner_ipv4_mtu) + ETHERNET_HEADER_BYTES + ETHERNET_FCS_BYTES


def inner_l1_bytes(frame_size, l1_overhead_bytes):
    return int(frame_size) + int(l1_overhead_bytes)


def outer_l1_bytes(frame_size, l1_overhead_bytes, tunnel_overhead_bytes):
    return inner_l1_bytes(frame_size, l1_overhead_bytes) + int(tunnel_overhead_bytes)


def theoretical_inner_pps(line_rate_gbps, frame_size, l1_overhead_bytes, tunnel_overhead_bytes):
    """Return the inner IPv4 PPS allowed by the encapsulated outer line."""
    outer_bytes = outer_l1_bytes(frame_size, l1_overhead_bytes, tunnel_overhead_bytes)
    return float(line_rate_gbps) * 1_000_000_000.0 / (outer_bytes * 8)


def estimate_wire_rates(pps, frame_size, l1_overhead_bytes, tunnel_overhead_bytes):
    """Return inner IPv4 and estimated encapsulated outer L1 bit rates."""
    pps = float(pps)
    return {
        "inner_ipv4_bps": pps * inner_l1_bytes(frame_size, l1_overhead_bytes) * 8,
        "outer_line_estimated_bps": pps
        * outer_l1_bytes(frame_size, l1_overhead_bytes, tunnel_overhead_bytes)
        * 8,
    }


def is_nat_pool_address(value, nat_pool_cidr):
    try:
        return ip_address(value) in ip_network(nat_pool_cidr, strict=False)
    except ValueError:
        return False
