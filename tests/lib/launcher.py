"""Pure and shared launcher argument and metadata helpers."""

import json
import re

from .output import prepare_details_dir


_LONG_OPTION_RE = re.compile(r"(?<![A-Za-z0-9_-])--[A-Za-z0-9][A-Za-z0-9_-]*")


def parse_child_long_options(help_output):
    return set(_LONG_OPTION_RE.findall(help_output or ""))


def build_default_output_dir(result_root, prefix, target, timestamp):
    return result_root / f"{prefix}_{target}_{timestamp}"


def validate_child_passthrough_args(passthrough_args, accepted_options):
    accepted = set(accepted_options)
    unknown = []
    stray = []
    index = 0
    while index < len(passthrough_args):
        argument = passthrough_args[index]
        if argument.startswith("--"):
            option = argument.split("=", 1)[0]
            if option not in accepted:
                unknown.append(option)
            if (
                "=" not in argument
                and index + 1 < len(passthrough_args)
                and not passthrough_args[index + 1].startswith("-")
            ):
                index += 1
        elif argument.startswith("-"):
            unknown.append(argument)
        else:
            stray.append(argument)
        index += 1
    if not unknown and not stray:
        return
    lines = []
    if unknown:
        lines.append("ERROR: unknown option(s): " + ", ".join(sorted(set(unknown))))
    if stray:
        lines.append("ERROR: stray argument(s): " + ", ".join(stray))
    lines.append("No benchmark was started.")
    raise SystemExit("\n".join(lines))


def filter_child_passthrough_args(passthrough_args, accepted_options):
    accepted = set(accepted_options)
    filtered = []
    index = 0
    while index < len(passthrough_args):
        argument = passthrough_args[index]
        if argument.startswith("--"):
            option = argument.split("=", 1)[0]
            value = None
            if (
                "=" not in argument
                and index + 1 < len(passthrough_args)
                and not passthrough_args[index + 1].startswith("-")
            ):
                index += 1
                value = passthrough_args[index]
            if option in accepted:
                filtered.append(argument)
                if value is not None:
                    filtered.append(value)
        else:
            filtered.append(argument)
        index += 1
    return filtered


def launcher_test_entries(out_dir, enabled_tests, script_dir):
    return [
        {
            "key": item["key"],
            "name": item["name"],
            "script": str(item["script"].relative_to(script_dir)),
            "out_dir": str((out_dir / item["out_dir_name"]).resolve()),
        }
        for item in enabled_tests
    ]


def write_launcher_metadata_file(out_dir, metadata, default=None):
    details_dir = prepare_details_dir(out_dir)
    (details_dir / "launcher_metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False, default=default),
        encoding="utf-8",
    )
