#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for shared E12 load-step behavior."""

import unittest

from tests import run_router_bench_dslite_stl_throughput_pps as dslite_stl
from tests import run_router_bench_mape_stl_throughput_pps as mape_stl
from tests import run_router_bench_v4_stl_throughput_pps as basic_stl
from tests.lib.rate_steps import (
    format_kpps_steps,
    high_loss_streak_update,
    limit_positive_steps,
    make_e12_steps,
)


class RateStepTests(unittest.TestCase):
    def test_pps_steps_are_formatted_in_kpps_without_scientific_notation(self):
        self.assertEqual(
            format_kpps_steps([1000, 1200, 1000000, 1008060]),
            "[1, 1.2, 1000, 1008.06]",
        )

    def test_e12_sequence_reaches_exact_decade_maximum(self):
        self.assertEqual(
            make_e12_steps(1, 100),
            [
                1.0,
                1.2,
                1.5,
                1.8,
                2.2,
                2.7,
                3.3,
                3.9,
                4.7,
                5.6,
                6.8,
                8.2,
                10.0,
                12.0,
                15.0,
                18.0,
                22.0,
                27.0,
                33.0,
                39.0,
                47.0,
                56.0,
                68.0,
                82.0,
                100.0,
            ],
        )

    def test_e12_sequence_appends_non_e12_maximum(self):
        steps = make_e12_steps(1_000, 2_500)
        self.assertEqual(steps, [1_000.0, 1_200.0, 1_500.0, 1_800.0, 2_200.0, 2_500.0])

    def test_custom_steps_are_capped_and_append_maximum(self):
        self.assertEqual(
            limit_positive_steps([100, 1_000, 10_000], 2_500, "PPS"),
            [100.0, 1_000.0, 2_500.0],
        )

    def test_high_loss_streak_requires_consecutive_points(self):
        streak = 0
        for loss_rate, expected in ((0.5, 1), (0.7, 2), (0.49, 0), (0.5, 1), (0.8, 2), (1.0, 3)):
            streak = high_loss_streak_update(streak, loss_rate, 0.5)
            self.assertEqual(streak, expected)

    def test_all_profiles_use_requested_starts_and_exact_maxima(self):
        for module in (basic_stl, dslite_stl, mape_stl):
            throughput = module.make_default_rate_mbps_steps(2.5)
            self.assertEqual(throughput[0], 1.0)
            self.assertEqual(throughput[-1], 2_500.0)

        basic_pps = basic_stl.make_default_pps_steps(1.0)
        dslite_pps = dslite_stl.make_default_pps_steps(1.0, 40)
        mape_pps = mape_stl.make_default_pps_steps(1.0, 40)
        for values in (basic_pps, dslite_pps, mape_pps):
            self.assertEqual(values[0], 1_000.0)
        self.assertEqual(basic_pps[-1], basic_stl.theoretical_pps_per_port(1.0, 64))
        self.assertEqual(dslite_pps[-1], dslite_stl.theoretical_pps_per_port(1.0, 64, 40))
        self.assertEqual(mape_pps[-1], mape_stl.theoretical_pps_per_port(1.0, 64, 40))


if __name__ == "__main__":
    unittest.main()
