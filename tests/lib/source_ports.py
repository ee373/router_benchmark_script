#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared LAN-side source-port range helpers."""

import argparse
import re


MIN_UNPRIVILEGED_PORT = 1024
MAX_PORT = 65535


def parse_lan_source_port_range(value):
    """Parse MIN-MAX, MIN:MAX, MIN,MAX, or a single port."""
    text = str(value or "").strip()
    if not text:
        raise argparse.ArgumentTypeError("LAN source-port range must not be empty")
    parts = [part.strip() for part in re.split(r"[-:,]", text) if part.strip()]
    if len(parts) == 1:
        parts = [parts[0], parts[0]]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            "LAN source-port range must be MIN-MAX, for example 38564-39563"
        )
    try:
        first, second = (int(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "LAN source-port range values must be integers"
        ) from exc
    low, high = sorted((first, second))
    if low < MIN_UNPRIVILEGED_PORT or high > MAX_PORT:
        raise argparse.ArgumentTypeError(
            f"LAN source ports must be {MIN_UNPRIVILEGED_PORT}..{MAX_PORT}"
        )
    return (low, high)


def format_lan_source_port_range(value):
    if value is None:
        return ""
    low, high = value
    return f"{int(low)}-{int(high)}"


def add_lan_source_port_range_argument(parser):
    parser.add_argument(
        "--lan-source-port-range",
        type=parse_lan_source_port_range,
        default=None,
        metavar="MIN-MAX",
        help=(
            "LAN-side TCP/UDP source-port range. Kernel socket tests use it as "
            "the namespace automatic-allocation range; TCP scratch uses the full "
            "range; STL uses MIN. ASTF and DHCP cannot override their source ports."
        ),
    )


def first_lan_source_port(value, default):
    if value is None:
        return int(default)
    return int(value[0])
