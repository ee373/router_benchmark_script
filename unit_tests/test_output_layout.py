import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.lib.output import DETAILS_DIRNAME, prepare_details_dir
from tests.lib.repeat import run_repeated_subprocesses
from tests.run_router_bench_v4_tcp_scratch_max_sessions import (
    SERVER_TIMESERIES_FILENAME,
    resolve_server_timeseries_path,
)


class OutputLayoutTests(unittest.TestCase):
    def test_prepare_details_dir_creates_expected_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir) / "result"
            details_dir = prepare_details_dir(out_dir)

            self.assertEqual(details_dir, out_dir / DETAILS_DIRNAME)
            self.assertTrue(details_dir.is_dir())

    def test_prepare_details_dir_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir) / "result"

            self.assertEqual(
                prepare_details_dir(out_dir),
                prepare_details_dir(out_dir),
            )

    def test_repeated_runs_store_per_run_data_under_details(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir) / "result"

            def fake_run(command, **_kwargs):
                child_out_dir = Path(
                    command[command.index("--out-dir") + 1]
                )
                child_out_dir.mkdir(parents=True, exist_ok=True)
                repeat_index = int(child_out_dir.name.rsplit("_", 1)[1])
                (child_out_dir / "summary_all.csv").write_text(
                    f"value\n{repeat_index}\n",
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0)

            with mock.patch("tests.lib.repeat.subprocess.run", side_effect=fake_run):
                return_code = run_repeated_subprocesses(
                    2,
                    out_dir,
                    argv=[],
                    script_path=Path(__file__),
                    cwd=Path(temp_dir),
                )

            self.assertEqual(return_code, 0)
            self.assertFalse((out_dir / "repeat_001").exists())
            self.assertTrue(
                (out_dir / DETAILS_DIRNAME / "repeat_001" / "summary_all.csv").is_file()
            )
            self.assertTrue(
                (out_dir / DETAILS_DIRNAME / "repeat_002" / "summary_all.csv").is_file()
            )
            self.assertTrue(
                (out_dir / DETAILS_DIRNAME / "repeat_metadata.json").is_file()
            )
            with (out_dir / "summary_all.csv").open(
                newline="",
                encoding="utf-8",
            ) as summary_file:
                rows = list(csv.DictReader(summary_file))
            self.assertEqual(
                rows,
                [
                    {"repeat": "1", "value": "1"},
                    {"repeat": "2", "value": "2"},
                ],
            )
            metadata = json.loads(
                (out_dir / DETAILS_DIRNAME / "repeat_metadata.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(metadata["repeat"], 2)

    def test_primary_server_timeseries_path_can_be_outside_details(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result_dir = Path(temp_dir) / "result"
            details_dir = result_dir / DETAILS_DIRNAME
            args = SimpleNamespace(
                server_timeseries_path=str(
                    result_dir / SERVER_TIMESERIES_FILENAME
                )
            )

            path = resolve_server_timeseries_path(args, details_dir)

            self.assertEqual(path, result_dir / SERVER_TIMESERIES_FILENAME)
            self.assertTrue(result_dir.is_dir())

    def test_repeated_primary_server_timeseries_is_aggregated_at_top_level(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir) / "result"

            def fake_run(command, **_kwargs):
                child_out_dir = Path(
                    command[command.index("--out-dir") + 1]
                )
                child_out_dir.mkdir(parents=True, exist_ok=True)
                repeat_index = int(child_out_dir.name.rsplit("_", 1)[1])
                (child_out_dir / SERVER_TIMESERIES_FILENAME).write_text(
                    f"elapsed_sec,accept_total\n{repeat_index}.0,{repeat_index * 100}\n",
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0)

            with mock.patch(
                "tests.lib.repeat.subprocess.run",
                side_effect=fake_run,
            ):
                return_code = run_repeated_subprocesses(
                    2,
                    out_dir,
                    summary_filenames=(SERVER_TIMESERIES_FILENAME,),
                    argv=[],
                    script_path=Path(__file__),
                    cwd=Path(temp_dir),
                )

            self.assertEqual(return_code, 0)
            with (out_dir / SERVER_TIMESERIES_FILENAME).open(
                newline="",
                encoding="utf-8",
            ) as timeseries_file:
                rows = list(csv.DictReader(timeseries_file))
            self.assertEqual(
                rows,
                [
                    {
                        "repeat": "1",
                        "elapsed_sec": "1.0",
                        "accept_total": "100",
                    },
                    {
                        "repeat": "2",
                        "elapsed_sec": "2.0",
                        "accept_total": "200",
                    },
                ],
            )

    def test_tcp_scratch_repeat_configuration_uses_server_timeseries_as_summary(self):
        script_text = (
            Path(__file__).resolve().parents[1]
            / "tests"
            / "run_router_bench_v4_tcp_scratch_max_sessions.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "summary_filenames=(SERVER_TIMESERIES_FILENAME,)",
            script_text,
        )
        self.assertNotIn(
            'summary_filenames=("summary.csv", SERVER_TIMESERIES_FILENAME)',
            script_text,
        )


if __name__ == "__main__":
    unittest.main()
