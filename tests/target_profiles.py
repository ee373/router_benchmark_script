#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared target profiles for TRex router benchmark scripts.

This module is the single source of truth for hardware, port, and IPv4
addressing profiles used by both STL and ASTF tests.
"""

TARGETS = {
    "i226_1g": {
        "description": "Intel I226-V, 1GbE IPv4 NAT throughput/PPS",
        "line_rate_gbps": 1.0,
        "interfaces": ["05:00.0", "04:00.0"],
        "kernel_drivers": ["igc", "igc"],
        "dpdk_drivers": ["auto", "auto"],
        # Linux netdev names used by non-TRex L4 latency tests.
        # These names are preferred over PCI BDF auto-resolution.
        "lan_iface": "enp5s0",
        "wan_iface": "enp4s0",
        "lan_port": 0,
        "wan_port": 1,
        "lan_ip": "192.168.0.2",
        "lan_gw": "192.168.0.1",
        "wan_ip": "192.168.100.1",
        "wan_gw": "192.168.100.2",
        "router_wan_ip": "192.168.100.2",
        "trex_cores": 1,
    },
    "i226_2p5g": {
        "description": "Intel I226-V, 2.5GbE IPv4 NAT throughput/PPS",
        "line_rate_gbps": 2.5,
        "interfaces": ["05:00.0", "04:00.0"],
        "kernel_drivers": ["igc", "igc"],
        "dpdk_drivers": ["auto", "auto"],
        # Linux netdev names used by non-TRex L4 latency tests.
        # These names are preferred over PCI BDF auto-resolution.
        "lan_iface": "enp5s0",
        "wan_iface": "enp4s0",
        "lan_port": 0,
        "wan_port": 1,
        "lan_ip": "192.168.0.2",
        "lan_gw": "192.168.0.1",
        "wan_ip": "192.168.100.1",
        "wan_gw": "192.168.100.2",
        "router_wan_ip": "192.168.100.2",
        "trex_cores": 1,
    },
    "x540_10g": {
        "description": "Intel X540, 10GbE IPv4 NAT throughput/PPS",
        "line_rate_gbps": 10.0,
        "interfaces": ["01:00.0", "01:00.1"],
        "kernel_drivers": ["ixgbe", "ixgbe"],
        "dpdk_drivers": ["auto", "auto"],
        # Linux netdev names used by non-TRex L4 latency tests.
        # These names are preferred over PCI BDF auto-resolution.
        "lan_iface": "enp1s0f0",
        "wan_iface": "enp1s0f1",
        "lan_port": 0,
        "wan_port": 1,
        "lan_ip": "192.168.0.2",
        "lan_gw": "192.168.0.1",
        "wan_ip": "192.168.100.1",
        "wan_gw": "192.168.100.2",
        "router_wan_ip": "192.168.100.2",
        # STL needs two DP cores to cover 10GbE.  ASTF currently uses one
        # client/server address per side, so keep ASTF on one DP core like
        # the i226 profiles.  TRex requires each ASTF IP pool to contain
        # more addresses than the number of DP threads.
        "trex_cores": 2,
        "astf_cores": 1,
    },
}


def _loop_profile(name, base_key, description, line_rate_gbps):
    base = TARGETS[base_key]
    profile = dict(base)
    profile.update({
        "description": description,
        "line_rate_gbps": line_rate_gbps,
        "topology": "direct_l2_loop",
        "supported_tests": ["l4_latency"],
        # DUT/routerを挟まない直結測定用。LAN/WAN側NICを同一L2セグメントに置く。
        # これにより、測定器・NIC・Linux namespace・iperf3/L4RR自体の基準latencyを測定する。
        "lan_ip": "192.168.200.1",
        "lan_gw": "192.168.200.2",
        "wan_ip": "192.168.200.2",
        "wan_gw": "192.168.200.1",
        "router_wan_ip": "192.168.200.1",
    })
    return profile


TARGETS["i226_1g_loop"] = _loop_profile(
    "i226_1g_loop",
    "i226_1g",
    "Intel I226-V, 1GbE direct L2 loop baseline for L4 latency",
    1.0,
)

TARGETS["i226_2p5g_loop"] = _loop_profile(
    "i226_2p5g_loop",
    "i226_2p5g",
    "Intel I226-V, 2.5GbE direct L2 loop baseline for L4 latency",
    2.5,
)

TARGETS["x540_10g_loop"] = _loop_profile(
    "x540_10g_loop",
    "x540_10g",
    "Intel X540, 10GbE direct L2 loop baseline for L4 latency",
    10.0,
)

