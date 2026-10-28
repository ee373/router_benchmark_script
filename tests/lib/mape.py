#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MAP-E profile validation, port-set handling, and wire-rate calculations."""

from ipaddress import ip_address, ip_network


ETHERNET_HEADER_BYTES = 14
ETHERNET_FCS_BYTES = 4
IPV4_HEADER_BYTES = 20
TCP_HEADER_BYTES = 20
UDP_HEADER_BYTES = 8
IPV6_HEADER_BYTES = 40

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
    "rule_ipv6_prefix",
    "ce_prefix",
    "ce_wan_ia_na",
    "ce_map_ipv6",
    "br_ipv6",
    "rule_ipv4_prefix",
    "shared_ipv4",
    "ea_bits_length",
    "ipv4_suffix_length",
    "psid_offset",
    "psid_length",
    "psid",
    "outer_ipv6_mtu",
    "inner_ipv4_mtu",
    "tcp_mss",
    "tunnel_overhead_bytes",
    "vpp_access_ipv6",
    "vpp_ipv4",
    "next_hop_ipv4",
    "server_network",
    "server_ipv4",
    "dns_ipv6",
    "api_ipv6",
    "api_hostname",
    "trex_cores",
)


def generate_mape_ports(psid=3, psid_offset=4, psid_length=8):
    """Return the non-well-known MAP-E port set for one PSID."""
    psid = int(psid)
    psid_offset = int(psid_offset)
    psid_length = int(psid_length)
    suffix_length = 16 - psid_offset - psid_length
    if psid_offset <= 0 or psid_length <= 0 or suffix_length < 0:
        raise ValueError("Invalid MAP-E port parameters")
    if psid < 0 or psid >= (1 << psid_length):
        raise ValueError("PSID does not fit psid_length")

    ports = []
    for a in range(1, 1 << psid_offset):
        for j in range(1 << suffix_length):
            ports.append(
                (a << (psid_length + suffix_length))
                | (psid << suffix_length)
                | j
            )
    return ports


def is_valid_mape_port(port, psid=3, psid_offset=4, psid_length=8):
    """Return whether a port is in the configured non-well-known MAP-E set."""
    port = int(port)
    psid = int(psid)
    psid_offset = int(psid_offset)
    psid_length = int(psid_length)
    suffix_length = 16 - psid_offset - psid_length
    if not 0 <= port <= 65535 or psid_offset <= 0 or psid_length <= 0 or suffix_length < 0:
        return False
    if not 0 <= psid < (1 << psid_length):
        return False
    a = port >> (psid_length + suffix_length)
    extracted_psid = (port >> suffix_length) & ((1 << psid_length) - 1)
    return a != 0 and extracted_psid == psid


def derive_ea_bits(shared_ipv4, rule_ipv4_prefix, psid, psid_length):
    """Build EA bits as IPv4 suffix followed by the PSID."""
    rule = ip_network(rule_ipv4_prefix, strict=True)
    address = ip_address(shared_ipv4)
    if rule.version != 4 or address.version != 4 or address not in rule:
        raise ValueError("shared_ipv4 must be inside rule_ipv4_prefix")
    suffix_length = 32 - rule.prefixlen
    ipv4_suffix = int(address) & ((1 << suffix_length) - 1)
    return (ipv4_suffix << int(psid_length)) | int(psid)


def derive_ce_prefix(rule_ipv6_prefix, ea_bits, ea_bits_length):
    """Return the CE prefix formed by appending EA bits to the rule prefix."""
    rule = ip_network(rule_ipv6_prefix, strict=True)
    ea_bits_length = int(ea_bits_length)
    if rule.version != 6 or ea_bits_length <= 0 or rule.prefixlen + ea_bits_length > 128:
        raise ValueError("Invalid MAP-E IPv6 rule or EA-bit length")
    if int(ea_bits) < 0 or int(ea_bits) >= (1 << ea_bits_length):
        raise ValueError("EA bits do not fit ea_bits_length")
    prefix_length = rule.prefixlen + ea_bits_length
    prefix_value = int(rule.network_address) | (
        int(ea_bits) << (128 - prefix_length)
    )
    return ip_network((prefix_value, prefix_length))


def validate_mape_profile(name, profile):
    """Validate one resolved MAP-E target profile."""
    missing = [key for key in REQUIRED_PROFILE_FIELDS if key not in profile]
    if missing:
        raise ValueError(f"MAP-E profile {name!r} is missing: {', '.join(missing)}")
    if profile["benchmark_mode"] != "mape":
        raise ValueError(f"MAP-E profile {name!r} must set benchmark_mode=mape")
    if len(profile["interfaces"]) != 2:
        raise ValueError(f"MAP-E profile {name!r} must define exactly two interfaces")
    if int(profile["lan_port"]) == int(profile["wan_port"]):
        raise ValueError(f"MAP-E profile {name!r} must use different LAN and WAN ports")
    if float(profile["line_rate_gbps"]) <= 0:
        raise ValueError(f"MAP-E profile {name!r} line_rate_gbps must be positive")

    for key in (
        "lan_ip",
        "lan_gw",
        "wan_link_ip",
        "wan_gateway_ip",
        "wan_arp_target",
        "shared_ipv4",
        "vpp_ipv4",
        "next_hop_ipv4",
        "server_ipv4",
    ):
        if ip_address(profile[key]).version != 4:
            raise ValueError(f"MAP-E profile {name!r} {key} must be IPv4")
    for key in ("ce_wan_ia_na", "br_ipv6", "vpp_access_ipv6", "dns_ipv6", "api_ipv6"):
        if ip_address(profile[key]).version != 6:
            raise ValueError(f"MAP-E profile {name!r} {key} must be IPv6")
    if profile["ce_map_ipv6"] is not None:
        raise ValueError(
            f"MAP-E profile {name!r} ce_map_ipv6 must be null; VPP owns MAP encapsulation"
        )

    rule_ipv4 = ip_network(profile["rule_ipv4_prefix"], strict=True)
    server_network = ip_network(profile["server_network"], strict=True)
    rule_ipv6 = ip_network(profile["rule_ipv6_prefix"], strict=True)
    ce_prefix = ip_network(profile["ce_prefix"], strict=True)
    if rule_ipv4.version != 4 or server_network.version != 4:
        raise ValueError(f"MAP-E profile {name!r} IPv4 prefixes must be IPv4")
    if rule_ipv6.version != 6 or ce_prefix.version != 6:
        raise ValueError(f"MAP-E profile {name!r} IPv6 prefixes must be IPv6")
    if ip_address(profile["shared_ipv4"]) not in rule_ipv4:
        raise ValueError(f"MAP-E profile {name!r} shared_ipv4 must be in rule_ipv4_prefix")
    if ip_address(profile["server_ipv4"]) not in server_network:
        raise ValueError(f"MAP-E profile {name!r} server_ipv4 must be in server_network")
    if profile["wan_link_ip"] != profile["next_hop_ipv4"]:
        raise ValueError(f"MAP-E profile {name!r} wan_link_ip must equal next_hop_ipv4")
    if profile["wan_gateway_ip"] != profile["vpp_ipv4"]:
        raise ValueError(f"MAP-E profile {name!r} wan_gateway_ip must equal vpp_ipv4")
    if profile["wan_arp_target"] != profile["vpp_ipv4"]:
        raise ValueError(f"MAP-E profile {name!r} wan_arp_target must equal vpp_ipv4")

    ipv4_suffix_length = 32 - rule_ipv4.prefixlen
    if int(profile["ipv4_suffix_length"]) != ipv4_suffix_length:
        raise ValueError(
            f"MAP-E profile {name!r} ipv4_suffix_length must be {ipv4_suffix_length}"
        )
    if int(profile["ea_bits_length"]) != ipv4_suffix_length + int(profile["psid_length"]):
        raise ValueError(
            f"MAP-E profile {name!r} ea_bits_length must equal IPv4 suffix length + PSID length"
        )

    ports = generate_mape_ports(
        profile["psid"], profile["psid_offset"], profile["psid_length"]
    )
    if not ports:
        raise ValueError(f"MAP-E profile {name!r} produced an empty port set")
    ea_bits = derive_ea_bits(
        profile["shared_ipv4"],
        profile["rule_ipv4_prefix"],
        profile["psid"],
        profile["psid_length"],
    )
    expected_ce_prefix = derive_ce_prefix(
        profile["rule_ipv6_prefix"], ea_bits, profile["ea_bits_length"]
    )
    if ce_prefix != expected_ce_prefix:
        raise ValueError(
            f"MAP-E profile {name!r} ce_prefix must be {expected_ce_prefix} for EA bits 0x{ea_bits:04x}"
        )
    if ip_address(profile["ce_wan_ia_na"]) not in ce_prefix:
        raise ValueError(f"MAP-E profile {name!r} ce_wan_ia_na must be in ce_prefix")
    if ip_address(profile["vpp_access_ipv6"]) not in ce_prefix:
        raise ValueError(f"MAP-E profile {name!r} vpp_access_ipv6 must be in ce_prefix")

    tunnel_overhead = int(profile["tunnel_overhead_bytes"])
    inner_mtu = int(profile["inner_ipv4_mtu"])
    outer_mtu = int(profile["outer_ipv6_mtu"])
    if tunnel_overhead != IPV6_HEADER_BYTES:
        raise ValueError(f"MAP-E profile {name!r} tunnel_overhead_bytes must be 40")
    if outer_mtu != inner_mtu + tunnel_overhead:
        raise ValueError(
            f"MAP-E profile {name!r} outer_ipv6_mtu must equal inner_ipv4_mtu + 40"
        )
    max_tcp_mss = inner_mtu - IPV4_HEADER_BYTES - TCP_HEADER_BYTES
    tcp_mss = int(profile["tcp_mss"])
    if tcp_mss <= 0 or tcp_mss > max_tcp_mss:
        raise ValueError(
            f"MAP-E profile {name!r} tcp_mss must be between 1 and "
            f"{max_tcp_mss} for inner_ipv4_mtu"
        )
    return profile


def build_trex_target(profile):
    """Add aliases required by the shared IPv4 TRex configuration writer."""
    target = dict(profile)
    target["wan_ip"] = profile["wan_link_ip"]
    target["wan_gw"] = profile["wan_arp_target"]
    target["router_wan_ip"] = profile["wan_gateway_ip"]
    return target


def frame_without_fcs_bytes(frame_size):
    size = int(frame_size)
    if size <= ETHERNET_FCS_BYTES:
        raise ValueError(f"frame_size is too small: {frame_size}")
    return size - ETHERNET_FCS_BYTES


def udp_payload_bytes(frame_size):
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
    return int(inner_ipv4_mtu) + ETHERNET_HEADER_BYTES + ETHERNET_FCS_BYTES


def outer_frame_size(inner_frame_size, tunnel_overhead_bytes=IPV6_HEADER_BYTES):
    """Return the outer Ethernet frame size including FCS."""
    return int(inner_frame_size) + int(tunnel_overhead_bytes)


def inner_l1_bytes(frame_size, l1_overhead_bytes):
    return int(frame_size) + int(l1_overhead_bytes)


def outer_l1_bytes(frame_size, l1_overhead_bytes, tunnel_overhead_bytes):
    return inner_l1_bytes(frame_size, l1_overhead_bytes) + int(tunnel_overhead_bytes)


def theoretical_inner_pps(line_rate_gbps, frame_size, l1_overhead_bytes, tunnel_overhead_bytes):
    outer_bytes = outer_l1_bytes(frame_size, l1_overhead_bytes, tunnel_overhead_bytes)
    return float(line_rate_gbps) * 1_000_000_000.0 / (outer_bytes * 8)


def estimate_wire_rates(pps, frame_size, l1_overhead_bytes, tunnel_overhead_bytes):
    pps = float(pps)
    return {
        "inner_ipv4_bps": pps * inner_l1_bytes(frame_size, l1_overhead_bytes) * 8,
        "outer_line_estimated_bps": pps
        * outer_l1_bytes(frame_size, l1_overhead_bytes, tunnel_overhead_bytes)
        * 8,
    }
