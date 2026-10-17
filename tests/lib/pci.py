"""Small PCI sysfs helpers shared by kernel and DPDK binding code."""

import os
import re
from pathlib import Path


PCI_SYSFS_ROOT = Path("/sys/bus/pci")


def normalize_pci_bdf(value):
    """Normalize valid PCI BDFs, preserving the kernel-network helper contract."""
    value = str(value or "").strip()
    if not value:
        return ""
    if re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", value):
        return value.lower()
    if re.fullmatch(r"[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", value):
        return f"0000:{value.lower()}"
    return ""


def normalize_trex_pci_bdf(value):
    """Keep TRex's historical domain-prefixing behavior, including invalid input."""
    value = str(value or "").strip()
    if not value:
        return ""
    if value.count(":") == 1:
        return f"0000:{value}"
    return value


def pci_device_path(bdf):
    if not bdf:
        return None
    return PCI_SYSFS_ROOT / "devices" / str(bdf)


def driver_unbind_paths(device_path, driver):
    return [
        Path(device_path) / "driver" / "unbind",
        PCI_SYSFS_ROOT / "drivers" / str(driver) / "unbind",
    ]


def write_sysfs_value(path, value):
    with Path(path).open("w", encoding="utf-8") as stream:
        stream.write(str(value))


def current_driver_from_device_path(dev_path):
    """Return the bound driver name for a resolved PCI device path."""
    if not dev_path:
        return ""
    driver_link = Path(dev_path) / "driver"
    try:
        if not driver_link.exists() and not driver_link.is_symlink():
            return ""
        return Path(os.readlink(driver_link)).name
    except OSError:
        try:
            return driver_link.resolve(strict=True).name
        except OSError:
            return ""
