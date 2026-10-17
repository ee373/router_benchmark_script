#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared E12 load-step helpers."""

import math


E12_MANTISSAS = (1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2)


def _rounded_step(value):
    if value == 0:
        return 0.0
    digits = max(0, 12 - int(math.floor(math.log10(abs(value)))) - 1)
    return round(float(value), digits)


def make_e12_steps(start, maximum):
    """Return inclusive E12 values from *start* through *maximum*."""
    start = float(start)
    maximum = float(maximum)
    if start <= 0:
        raise ValueError("start must be positive")
    if maximum <= 0:
        raise ValueError("maximum must be positive")
    if maximum < start:
        return [maximum]

    values = [start]
    first_decade = int(math.floor(math.log10(start))) - 1
    last_decade = int(math.floor(math.log10(maximum))) + 1
    for decade in range(first_decade, last_decade + 1):
        scale = 10.0 ** decade
        for mantissa in E12_MANTISSAS:
            value = _rounded_step(mantissa * scale)
            if start < value < maximum:
                values.append(value)
    values.append(maximum)
    return sorted(set(values))


def limit_positive_steps(steps, maximum, label):
    """Validate, de-duplicate and cap user-supplied positive load steps."""
    maximum = float(maximum)
    limited = sorted(
        set(float(value) for value in steps if 0 < float(value) <= maximum + 1e-9)
    )
    if not limited:
        raise SystemExit(
            f"ERROR: no {label} steps are <= configured maximum ({maximum:g})"
        )
    if abs(limited[-1] - maximum) > 1e-9:
        limited.append(maximum)
    return limited


def high_loss_streak_update(current_streak, loss_rate, threshold):
    """Update a consecutive high-loss counter."""
    if float(loss_rate) >= float(threshold):
        return int(current_streak) + 1
    return 0


def format_kpps_steps(steps):
    """Format PPS values as a compact kpps list for console output."""
    return "[" + ", ".join(f"{float(value) / 1000.0:.6g}" for value in steps) + "]"
