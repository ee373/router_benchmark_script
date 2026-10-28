import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import test_basic_speed as basic_launcher
import test_dslite_speed as dslite_launcher
import test_mape_speed as mape_launcher
from tests.lib.launcher import (
    build_default_output_dir,
    filter_child_passthrough_args,
    launcher_test_entries,
    parse_child_long_options,
    validate_child_passthrough_args,
    write_launcher_metadata_file,
)


class LauncherHelperTests(unittest.TestCase):
    def test_default_output_directory_keeps_launcher_specific_prefixes(self):
        root = Path("/results")
        self.assertEqual(
            build_default_output_dir(root, "trex_basic_speed", "target", "stamp"),
            root / "trex_basic_speed_target_stamp",
        )
        self.assertEqual(
            build_default_output_dir(root, "trex_dslite_speed", "target", "stamp"),
            root / "trex_dslite_speed_target_stamp",
        )

    def test_long_option_parser_keeps_only_complete_long_options(self):
        self.assertEqual(
            parse_child_long_options(
                "usage: run --rate RATE --use-cache --x=value -r --not_a_long_name"
            ),
            {"--rate", "--use-cache", "--x", "--not_a_long_name"},
        )

    def test_validation_preserves_equals_values_and_hyphen_value_behavior(self):
        validate_child_passthrough_args(
            ["--rate=10", "--name", "router"],
            {"--rate", "--name"},
        )
        with self.assertRaises(SystemExit) as error:
            validate_child_passthrough_args(
                ["--rate", "-1"],
                {"--rate"},
            )
        self.assertIn("unknown option(s): -1", str(error.exception))

    def test_validation_reports_unknown_options_and_stray_arguments(self):
        with self.assertRaises(SystemExit) as error:
            validate_child_passthrough_args(
                ["--unknown", "bad", "stray"],
                {"--known"},
            )
        message = str(error.exception)
        self.assertIn("ERROR: unknown option(s): --unknown", message)
        self.assertIn("ERROR: stray argument(s): stray", message)
        self.assertIn("No benchmark was started.", message)

    def test_filter_keeps_only_supported_long_options_and_values(self):
        self.assertEqual(
            filter_child_passthrough_args(
                ["--rate=10", "--name", "router", "--skip", "discard", "positional", "-x"],
                {"--rate", "--name"},
            ),
            ["--rate=10", "--name", "router", "positional", "-x"],
        )
        self.assertEqual(
            filter_child_passthrough_args(["--unknown", "value"], set()),
            [],
        )

    def test_dslite_and_mape_wrappers_share_validation_and_filter_behavior(self):
        test = {"script": Path("child.py")}
        for launcher in (dslite_launcher, mape_launcher):
            with mock.patch.object(launcher, "child_long_options", return_value={"--rate", "--flag"}):
                launcher.validate_passthrough_args(
                    ["--rate=10", "--flag"],
                    [test],
                )
                self.assertEqual(
                    launcher.filter_passthrough_args(
                        test["script"],
                        ["--rate", "10", "--unused", "value"],
                    ),
                    ["--rate", "10"],
                )

    def test_metadata_test_entries_and_file_location_preserve_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script_dir = root / "scripts"
            out_dir = root / "results"
            script = script_dir / "tests" / "child.py"
            script.parent.mkdir(parents=True)
            script.touch()
            entries = launcher_test_entries(
                out_dir,
                [{
                    "key": "sample",
                    "name": "Sample test",
                    "script": script,
                    "out_dir_name": "01_sample",
                    "args": ["--server-ip-count", "4"],
                }],
                script_dir,
            )
            metadata = {
                "benchmark_mode": "test-mode",
                "tests": entries,
                "optional": None,
            }
            write_launcher_metadata_file(out_dir, metadata)

            metadata_path = out_dir / "details" / "launcher_metadata.json"
            self.assertTrue(metadata_path.is_file())
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(payload, metadata)
            self.assertEqual(entries[0]["script"], "tests/child.py")
            self.assertEqual(entries[0]["out_dir"], str((out_dir / "01_sample").resolve()))
            self.assertEqual(entries[0]["args"], ["--server-ip-count", "4"])

    def test_launcher_metadata_retains_mode_specific_keys_and_path_shape(self):
        enabled_tests = [{
            "key": "sample",
            "name": "Sample",
            "script": basic_launcher.TESTS_DIR / "sample.py",
            "out_dir_name": "01_sample",
        }]
        passthrough_args = ["--duration", "10"]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            basic_args = SimpleNamespace(lan_source_port_range=None)
            with mock.patch.object(
                basic_launcher,
                "format_lan_source_port_range",
                return_value="1024-65535",
            ):
                basic_launcher.write_launcher_metadata(
                    root / "basic",
                    "target",
                    "v4",
                    enabled_tests,
                    passthrough_args,
                    True,
                    "explicit",
                    2.0,
                    1,
                    3.0,
                    2,
                    basic_args.lan_source_port_range,
                    False,
                )
            basic = json.loads(
                (root / "basic" / "details" / "launcher_metadata.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(basic["ip_version"], "v4")
            self.assertTrue(basic["dry_run"])
            self.assertNotIn("benchmark_mode", basic)

            launcher_args = SimpleNamespace(
                target="target",
                repeat=1,
                test_start_delay_sec=2.0,
                inter_test_probe_interval_sec=3.0,
                inter_test_success_consecutive=2,
                stl_recovery_probe=False,
                lan_source_port_range=None,
            )
            for launcher, mode in (
                (dslite_launcher, "dslite"),
                (mape_launcher, "mape"),
            ):
                with mock.patch.object(
                    launcher,
                    "format_lan_source_port_range",
                    return_value="1024-65535",
                ):
                    launcher.write_launcher_metadata(
                        root / mode,
                        launcher_args,
                        enabled_tests,
                        passthrough_args,
                        "explicit",
                    )
                payload = json.loads(
                    (root / mode / "details" / "launcher_metadata.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(payload["benchmark_mode"], mode)
                self.assertNotIn("ip_version", payload)
                self.assertNotIn("dry_run", payload)
                self.assertEqual(payload["tests"][0]["out_dir"], str((root / mode / "01_sample").resolve()))


if __name__ == "__main__":
    unittest.main()
