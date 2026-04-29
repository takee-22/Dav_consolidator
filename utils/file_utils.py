"""
utils/file_utils.py
-------------------
File discovery and natural sort utilities.

Natural sorting is critical here: IMOU cameras name files with
numeric suffixes (e.g., ch01_20240101000000.dav) that must be
sorted chronologically, not lexicographically.
"""

import re
import os
import tempfile
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Natural Sort
# ---------------------------------------------------------------------------

def _natural_key(path: Path) -> list:
    """
    Produce a sort key that orders numeric substrings by value, not by
    their string representation.

    Example ordering with natural sort:
        video_2.dav  →  video_9.dav  →  video_10.dav   ✓
    Example ordering with lexicographic sort:
        video_10.dav →  video_2.dav  →  video_9.dav    ✗
    """
    parts = re.split(r"(\d+)", path.name)
    return [int(part) if part.isdigit() else part.lower() for part in parts]


def natural_sorted(paths: list[Path]) -> list[Path]:
    """Return *paths* sorted in natural (human-friendly) order."""
    return sorted(paths, key=_natural_key)


# ---------------------------------------------------------------------------
# DAV File Discovery
# ---------------------------------------------------------------------------

def find_dav_files(folder: str | Path) -> list[Path]:
    """
    Recursively discover all .dav files under *folder* and return them in
    natural chronological order.

    Parameters
    ----------
    folder:
        Root directory to search.

    Returns
    -------
    list[Path]
        Naturally sorted list of .dav paths.  Empty list if none found.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder}")

    dav_files = list(folder.rglob("*.dav"))
    return natural_sorted(dav_files)


# ---------------------------------------------------------------------------
# Temp Directory Helpers
# ---------------------------------------------------------------------------

def make_temp_dir(prefix: str = "dav_tmp_") -> Path:
    """Create a temporary working directory and return its Path."""
    return Path(tempfile.mkdtemp(prefix=prefix))


def safe_unlink(path: Path, log: Callable[[str], None] | None = None) -> None:
    """Delete a file, silently ignoring errors (logs them if *log* is given)."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        if log:
            log(f"[WARN] Could not delete {path.name}: {exc}")


def cleanup_files(paths: list[Path], log: Callable[[str], None] | None = None) -> None:
    """Delete every path in *paths* via :func:`safe_unlink`."""
    for p in paths:
        safe_unlink(p, log)


# ---------------------------------------------------------------------------
# Filename Helpers
# ---------------------------------------------------------------------------

def stem_index(path: Path, index: int, zero_pad: int = 4) -> Path:
    """
    Return a sibling path whose stem is replaced with a zero-padded index.

    Example
    -------
    stem_index(Path("/tmp/foo.mp4"), 3) → Path("/tmp/0003.mp4")
    """
    return path.with_name(f"{str(index).zfill(zero_pad)}{path.suffix}")


def ensure_mp4_extension(path: str | Path) -> Path:
    """Force *.mp4* extension on *path*, preserving the stem."""
    path = Path(path)
    return path.with_suffix(".mp4")
