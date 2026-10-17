import csv
import tempfile
import unittest
from pathlib import Path

from tests.lib.common import json_cell, write_raw_stats, write_summary_csv


class CommonHelperTests(unittest.TestCase):
    def test_summary_csv_keeps_union_column_order_and_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "summary.csv"
            write_summary_csv(
                path,
                [
                    {"test": "one", "rate": 12, "extra": None},
                    {"test": "two", "loss": 0.25},
                ],
            )

            with path.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(
                rows,
                [
                    {"test": "one", "rate": "12", "extra": "", "loss": ""},
                    {"test": "two", "rate": "", "extra": "", "loss": "0.25"},
                ],
            )

    def test_empty_summary_still_creates_a_csv_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "empty.csv"
            write_summary_csv(path, [])

            self.assertTrue(path.is_file())

    def test_raw_stats_flatten_nested_values_and_cellize_special_values(self):
        rows = []

        class Writer:
            def writerow(self, row):
                rows.append(row)

        write_raw_stats(
            Writer(),
            "measure",
            "sample",
            1.23456,
            {"counts": [1, None], "payload": {"ok": True}},
            ts_local="2026-10-17T10:00:00",
        )

        self.assertEqual(
            rows,
            [
                ["2026-10-17T10:00:00", "1.235", "measure", "sample", "counts.0", 1],
                ["2026-10-17T10:00:00", "1.235", "measure", "sample", "counts.1", ""],
                ["2026-10-17T10:00:00", "1.235", "measure", "sample", "payload.ok", True],
            ],
        )
        self.assertEqual(json_cell({"latency": 1}), '{"latency": 1}')


if __name__ == "__main__":
    unittest.main()
