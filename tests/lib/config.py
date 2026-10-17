"""Load benchmark defaults and target profiles from external JSON files."""

from copy import deepcopy
from functools import lru_cache
import json
import os
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = TESTS_DIR / "config"
DEFAULTS_PATH = CONFIG_DIR / "benchmark_defaults.json"
TARGET_PROFILES_PATH = CONFIG_DIR / "target_profiles.json"
DSLITE_TARGET_PROFILES_PATH = CONFIG_DIR / "target_dslite_profiles.json"
MAPE_TARGET_PROFILES_PATH = CONFIG_DIR / "target_mape_profiles.json"


@lru_cache(maxsize=None)
def _read_json(path):
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def load_defaults(section):
    """Return a private copy of one defaults section."""
    defaults = _read_json(DEFAULTS_PATH)
    try:
        return deepcopy(defaults[section])
    except KeyError as exc:
        raise KeyError(f"unknown benchmark defaults section: {section}") from exc


def _load_target_profiles(path):
    """Return resolved target profiles from one JSON file."""
    raw_profiles = _read_json(path)
    resolved = {}
    resolving = set()

    def resolve(name):
        if name in resolved:
            return resolved[name]
        if name in resolving:
            raise ValueError(f"cyclic target profile inheritance: {name}")
        try:
            raw = raw_profiles[name]
        except KeyError as exc:
            raise KeyError(f"unknown target profile: {name}") from exc
        resolving.add(name)
        base_name = raw.get("extends")
        profile = deepcopy(resolve(base_name)) if base_name else {}
        profile.update({key: deepcopy(value) for key, value in raw.items() if key != "extends"})
        resolving.remove(name)
        resolved[name] = profile
        return profile

    for profile_name in raw_profiles:
        resolve(profile_name)
    return deepcopy(resolved)


def load_target_profiles():
    """Return router benchmark target profiles in their declared JSON order."""
    return _load_target_profiles(TARGET_PROFILES_PATH)


def target_for_ip_version(profile, ip_version):
    """Map a profile to the address keys consumed by a benchmark."""
    version = str(ip_version).lower().strip()
    if version in ("4", "ipv4"):
        version = "v4"
    elif version in ("6", "ipv6"):
        version = "v6"
    if version not in ("v4", "v6"):
        raise ValueError(f"unsupported IP version: {ip_version}")

    target = deepcopy(profile)
    target["ip_version"] = version
    if version == "v4":
        return target

    mapping = {
        "lan_ip": "lan_ipv6",
        "lan_gw": "lan_ipv6_gw",
        "wan_ip": "wan_ipv6",
        "wan_gw": "wan_ipv6_gw",
        "router_wan_ip": "router_wan_ipv6",
    }
    missing = [source for source in mapping.values() if not target.get(source)]
    if missing:
        raise ValueError("IPv6 target profile fields are missing: " + ", ".join(missing))
    for destination, source in mapping.items():
        target[destination] = target[source]
    target["description"] = str(target.get("description", "")).replace(
        "IPv4 NAT", "native IPv6 routing"
    )
    if target.get("topology") != "direct_l2_loop":
        target["topology"] = "native_ipv6_routing"
    return target


def load_dslite_target_profiles():
    """Return DS-Lite target profiles in their declared JSON order."""
    return _load_target_profiles(DSLITE_TARGET_PROFILES_PATH)


def load_mape_target_profiles():
    """Return MAP-E target profiles in their declared JSON order."""
    return _load_target_profiles(MAPE_TARGET_PROFILES_PATH)


def default_result_root():
    common = load_defaults("common")
    return Path(os.environ.get(common["result_root_env"], common["result_root"]))


TARGETS = load_target_profiles()
DSLITE_TARGETS = load_dslite_target_profiles()
MAPE_TARGETS = load_mape_target_profiles()
