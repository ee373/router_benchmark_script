"""Shared benchmark result-directory layout helpers."""

from pathlib import Path


DETAILS_DIRNAME = "details"


def prepare_details_dir(out_dir):
    """Create and return the directory for non-summary benchmark artifacts."""
    details_dir = Path(out_dir) / DETAILS_DIRNAME
    details_dir.mkdir(parents=True, exist_ok=True)
    return details_dir
