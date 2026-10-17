import csv
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tests.lib.preflight import (
    run_consecutive_preflight,
    run_preflight_with_optional_recovery_probe,
)


class ConsecutivePreflightTests(unittest.TestCase):
    def test_optional_recovery_probe_defaults_to_single_preflight(self):
        run_preflight = mock.Mock(return_value="preflight result")
        wait_for_recovery = mock.Mock(return_value="recovery result")

        result = run_preflight_with_optional_recovery_probe(
            run_preflight,
            wait_for_recovery,
            recovery_probe_enabled=False,
        )

        self.assertEqual(result, "preflight result")
        run_preflight.assert_called_once_with()
        wait_for_recovery.assert_not_called()

    def test_optional_recovery_probe_uses_recovery_loop_when_enabled(self):
        run_preflight = mock.Mock(return_value="preflight result")
        wait_for_recovery = mock.Mock(return_value="recovery result")

        result = run_preflight_with_optional_recovery_probe(
            run_preflight,
            wait_for_recovery,
            recovery_probe_enabled=True,
        )

        self.assertEqual(result, "recovery result")
        run_preflight.assert_not_called()
        wait_for_recovery.assert_called_once_with()

    def test_stl_recovery_probe_option_is_forwarded_by_each_launcher(self):
        repo_dir = Path(__file__).resolve().parents[1]
        launchers = (
            ("test_basic_speed.py", "i226_1g", ["--ip-version", "v4"]),
            ("test_dslite_speed.py", "i226_1g_dslite", []),
            ("test_mape_speed.py", "i226_1g_mape", []),
        )
        for launcher, target, extra_args in launchers:
            for recovery_enabled in (False, True):
                with self.subTest(
                    launcher=launcher,
                    recovery_enabled=recovery_enabled,
                ):
                    command = [
                        sys.executable,
                        str(repo_dir / launcher),
                        "--dry-run",
                        "--run-stl-throughput",
                        "--target",
                        target,
                        *extra_args,
                    ]
                    if recovery_enabled:
                        command.append("--stl-recovery-probe")
                    proc = subprocess.run(
                        command,
                        cwd=repo_dir,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        check=False,
                        timeout=30,
                    )
                    self.assertEqual(proc.returncode, 0, msg=proc.stdout)
                    self.assertEqual(
                        "--stl-recovery-probe" in proc.stdout,
                        recovery_enabled,
                    )

    def test_failure_resets_consecutive_successes(self):
        outcomes = iter([True, True, False, True, True, True])
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "preflight.csv"

            def probe(attempt):
                if not next(outcomes):
                    raise RuntimeError(f"failed attempt {attempt}")
                return attempt

            result = run_consecutive_preflight(
                probe,
                phase="unit",
                csv_path=csv_path,
                required_successes=3,
                interval_sec=0,
                timeout_sec=0,
            )

            self.assertEqual(result.attempts, 6)
            self.assertEqual(result.value, 6)
            with csv_path.open(newline="", encoding="utf-8") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(
                [row["consecutive_successes"] for row in rows],
                ["1", "2", "0", "1", "2", "3"],
            )
            self.assertEqual(rows[-1]["recovery_complete"], "1")
            self.assertEqual(
                {row["required_consecutive_successes"] for row in rows},
                {"3"},
            )

    def test_every_launcher_test_exposes_a_three_success_gate(self):
        repo_dir = Path(__file__).resolve().parents[1]
        defaults = json.loads(
            (repo_dir / "tests/config/benchmark_defaults.json").read_text(
                encoding="utf-8"
            )
        )
        scripts = []
        for section in ("launcher", "dslite_launcher", "mape_launcher"):
            for item in defaults[section]["test_plan"]:
                script = item["script"]
                if script not in scripts:
                    scripts.append(script)
        for script in scripts:
            proc = subprocess.run(
                [sys.executable, str(repo_dir / "tests" / script), "--help"],
                cwd=repo_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(proc.returncode, 0, msg=f"{script}: {proc.stdout}")
            self.assertTrue(
                "--preflight-success-consecutive" in proc.stdout
                or "--recovery-success-consecutive" in proc.stdout,
                msg=f"missing consecutive-success gate: {script}",
            )

        self.assertEqual(defaults["recovery"]["success_consecutive"], 3)
        for section in ("stl", "dslite_stl", "mape_stl"):
            self.assertEqual(defaults[section]["recovery_success_consecutive"], 3)
            self.assertEqual(defaults[section]["recovery_timeout_sec"], 300.0)
        self.assertEqual(
            defaults["dslite_source_port_scan"]["preflight_success_consecutive"],
            3,
        )
        self.assertEqual(
            defaults["dslite_source_port_scan"]["recovery_success_consecutive"],
            3,
        )


if __name__ == "__main__":
    unittest.main()
