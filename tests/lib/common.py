"""Small, test-independent helpers shared by benchmark scripts."""

import argparse
import csv
import datetime as dt
import json

from .config import default_result_root


DEFAULT_RESULT_ROOT = default_result_root()


def ts_compact():
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def ts_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


def parse_positive_float(value):
    try:
        number = float(str(value).replace("_", ""))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a number") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return number


def parse_non_negative_float(value):
    try:
        number = float(str(value).replace("_", ""))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a number") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return number


def parse_positive_int(value):
    try:
        number = int(str(value).replace("_", ""))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return number


def flatten(obj, prefix=""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            yield from flatten(value, name)
    elif isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            name = f"{prefix}.{index}" if prefix else str(index)
            yield from flatten(value, name)
    else:
        yield prefix, obj


def json_cell(value):
    if value is None:
        return ""
    if isinstance(value, (int, float, str, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def write_raw_stats(writer, phase, test_name, elapsed, stats, ts_local=None):
    now = ts_local or ts_iso()
    for metric, value in flatten(stats):
        writer.writerow([now, f"{elapsed:.3f}", phase, test_name, metric, json_cell(value)])


def write_summary_csv(path, summaries):
    keys = []
    for row in summaries:
        for key in row.keys():
            if key not in keys:
                keys.append(key)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for row in summaries:
            writer.writerow(row)
