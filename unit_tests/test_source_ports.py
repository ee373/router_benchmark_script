#!/usr/bin/env python3

import argparse
import unittest

from tests.lib.source_ports import (
    first_lan_source_port,
    format_lan_source_port_range,
    parse_lan_source_port_range,
)


class LanSourcePortRangeTests(unittest.TestCase):
    def test_ascending_range(self):
        self.assertEqual(parse_lan_source_port_range("38564-39563"), (38564, 39563))

    def test_descending_range_is_normalized(self):
        self.assertEqual(parse_lan_source_port_range("39563:38564"), (38564, 39563))

    def test_single_port(self):
        self.assertEqual(parse_lan_source_port_range("5209"), (5209, 5209))

    def test_out_of_range_value_is_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_lan_source_port_range("80-8080")
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_lan_source_port_range("65000-70000")

    def test_invalid_shape_is_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_lan_source_port_range("10000-20000-30000")

    def test_format_and_first_port(self):
        port_range = (38564, 39563)
        self.assertEqual(format_lan_source_port_range(port_range), "38564-39563")
        self.assertEqual(format_lan_source_port_range(None), "")
        self.assertEqual(first_lan_source_port(port_range, 5201), 38564)
        self.assertEqual(first_lan_source_port(None, 5201), 5201)


if __name__ == "__main__":
    unittest.main()
