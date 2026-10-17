"""Shared support for repeating complete benchmark executions."""

import csv
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from .config import load_defaults
from .output import prepare_details_dir


DEFAULT_REPEAT = int(load_defaults("common").get("repeat", 1))


def parse_repeat(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("repeat must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError("repeat must be a positive integer")
    return parsed


def argparse_repeat(value):
    import argparse

    try:
        return parse_repeat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _remove_options(argv, option_names):
    """Remove value-taking long options from an argv list."""
    cleaned = []
    index = 0
    names = set(option_names)
    while index < len(argv):
        argument = argv[index]
        option = argument.split("=", 1)[0] if argument.startswith("--") else ""
        if option in names:
            if "=" not in argument and index + 1 < len(argv):
                index += 1
        else:
            cleaned.append(argument)
        index += 1
    return cleaned


def _write_repeat_metadata(path, repeat, runs):
    payload = {
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "repeat": int(repeat),
        "runs": runs,
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _aggregate_csv(out_dir, repeat_runs, filename):
    rows = []
    fields = ["repeat"]
    for run in repeat_runs:
        source = Path(run["out_dir"]) / filename
        if not source.is_file():
            continue
        with source.open("r", newline="", encoding="utf-8") as source_file:
            reader = csv.DictReader(source_file)
            for input_row in reader:
                row = dict(input_row)
                row["repeat"] = run["repeat"]
                rows.append(row)
                for key in row:
                    if key not in fields:
                        fields.append(key)
    if not rows:
        return None
    destination = Path(out_dir) / filename
    with destination.open("w", newline="", encoding="utf-8") as destination_file:
        writer = csv.DictWriter(destination_file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return destination


def run_repeated_subprocesses(
    repeat,
    out_dir,
    *,
    summary_filenames=("summary_all.csv",),
    argv=None,
    script_path=None,
    cwd=None,
):
    """Execute the current benchmark once per repeat in independent directories.

    Return ``None`` when no dispatch is needed, otherwise return the subprocess
    exit code. Each successful child is invoked with ``--repeat 1`` so the
    dispatcher cannot recurse.
    """
    repeat = parse_repeat(repeat)
    if repeat == 1:
        return None

    output_root = Path(out_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    details_root = prepare_details_dir(output_root)
    entry_script = Path(script_path or sys.argv[0]).resolve()
    base_argv = _remove_options(
        list(sys.argv[1:] if argv is None else argv),
        {"--repeat", "--out-dir"},
    )
    working_directory = Path(cwd).resolve() if cwd else entry_script.parent.parent
    runs = []

    print("============================================================")
    print(f"Repeated benchmark execution: {repeat} runs")
    print(f"Parent output directory      : {output_root}")
    print("============================================================")

    for repeat_index in range(1, repeat + 1):
        repeat_dir = details_root / f"repeat_{repeat_index:03d}"
        command = [
            sys.executable,
            str(entry_script),
            *base_argv,
            "--repeat",
            "1",
            "--out-dir",
            str(repeat_dir),
        ]
        print(
            f"--- repeat {repeat_index}/{repeat}: output={repeat_dir} ---",
            flush=True,
        )
        completed = subprocess.run(
            command,
            cwd=str(working_directory),
            check=False,
        )
        run_record = {
            "repeat": repeat_index,
            "out_dir": str(repeat_dir),
            "return_code": int(completed.returncode),
        }
        runs.append(run_record)
        _write_repeat_metadata(details_root / "repeat_metadata.json", repeat, runs)
        if completed.returncode != 0:
            return int(completed.returncode)

    for filename in summary_filenames:
        _aggregate_csv(output_root, runs, filename)
    _write_repeat_metadata(details_root / "repeat_metadata.json", repeat, runs)
    print("============================================================")
    print("Repeated benchmark execution completed")
    print(f"Parent output directory: {output_root}")
    print("============================================================")
    return 0
