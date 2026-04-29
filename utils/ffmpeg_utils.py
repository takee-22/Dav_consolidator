"""
utils/ffmpeg_utils.py
---------------------
FFmpeg binary discovery, file utilities, and natural-sort helpers.

Binary Resolution
~~~~~~~~~~~~~~~~~
Resolution order for ffmpeg.exe / ffprobe.exe:

  1. sys._MEIPASS   — PyInstaller --onefile extracts bundled binaries here
  2. Project root   — Development: the repo directory that contains main.py
  3. System PATH    — Last-resort fallback (string "ffmpeg" / "ffprobe")

This means the same code works transparently in both dev and packaged modes
without any conditional branching in calling code.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FFmpeg binary resolution
# ---------------------------------------------------------------------------

def _get_binary_base_dir() -> Path:
    """
    Return the directory expected to contain ffmpeg.exe / ffprobe.exe.

    Priority:
      1. sys._MEIPASS  ← PyInstaller one-file temp extraction directory
      2. Parent of *this* file's package root  ← project root in dev mode
    """
    if hasattr(sys, "_MEIPASS"):
        # Running inside a PyInstaller bundle
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        logger.debug("PyInstaller mode: binary base = %s", base)
        return base

    # Development: utils/ → parent → project root
    base = Path(__file__).resolve().parent.parent
    logger.debug("Development mode: binary base = %s", base)
    return base


def _resolve_binary(name: str) -> str:
    """
    Locate *name* (e.g. 'ffmpeg' or 'ffprobe') in the binary base directory.

    Checks '<name>.exe' first (Windows), then '<name>' (Linux/macOS).
    Falls back to the bare name (relies on PATH) if neither is found.

    Returns
    -------
    str
        Absolute path to the binary, or the bare name as a PATH fallback.
    """
    base = _get_binary_base_dir()
    for candidate in (base / f"{name}.exe", base / name):
        if candidate.is_file():
            logger.debug("Resolved %s → %s", name, candidate)
            return str(candidate)
    logger.warning(
        "%s not found in %s — falling back to system PATH", name, base
    )
    return name  # rely on PATH


def get_ffmpeg_path() -> str:
    """Return the absolute path to the ffmpeg binary (or 'ffmpeg' for PATH)."""
    return _resolve_binary("ffmpeg")


def get_ffprobe_path() -> str:
    """Return the absolute path to the ffprobe binary (or 'ffprobe' for PATH)."""
    return _resolve_binary("ffprobe")


def derive_ffprobe_from_ffmpeg(ffmpeg_path: str) -> str:
    """
    Given a concrete ffmpeg path, return the matching ffprobe path.

    If *ffmpeg_path* is just 'ffmpeg' (PATH fallback), return 'ffprobe'.
    Otherwise substitute 'ffmpeg' for 'ffprobe' in the filename.
    """
    p = Path(ffmpeg_path)
    if p.parent == Path("."):
        return get_ffprobe_path()
    return str(p.parent / p.name.replace("ffmpeg", "ffprobe"))


# ---------------------------------------------------------------------------
# Natural sort
# ---------------------------------------------------------------------------

def _natural_key(path: Path) -> list:
    """
    Produce a sort key that orders numeric substrings by integer value.

    Example
    -------
    Natural:      video_2.dav → video_9.dav → video_10.dav   ✓
    Lexicographic:video_10.dav → video_2.dav → video_9.dav   ✗
    """
    parts = re.split(r"(\d+)", path.name)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def natural_sorted(paths: list[Path]) -> list[Path]:
    """Return *paths* in natural (human-friendly) chronological order."""
    return sorted(paths, key=_natural_key)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

_DEFAULT_EXTENSIONS: tuple[str, ...] = (".dav",)


def find_video_files(
    folder: str | Path,
    extensions: tuple[str, ...] = _DEFAULT_EXTENSIONS,
) -> list[Path]:
    """
    Recursively discover video files under *folder*.

    Parameters
    ----------
    folder:
        Root directory to search.
    extensions:
        Lower-case extensions to include. Matching is case-insensitive.

    Returns
    -------
    list[Path]
        Naturally sorted list. Empty if none found.

    Raises
    ------
    NotADirectoryError
        When *folder* does not point to an existing directory.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder}")

    files: list[Path] = []
    for ext in extensions:
        files.extend(folder.rglob(f"*{ext}"))
        files.extend(folder.rglob(f"*{ext.upper()}"))

    # Deduplicate (case-insensitive filesystems may yield duplicates)
    seen: set[Path] = set()
    unique: list[Path] = []
    for f in files:
        resolved = f.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(f)

    return natural_sorted(unique)


def find_dav_files(folder: str | Path) -> list[Path]:
    """Convenience wrapper — discover only *.dav files under *folder*."""
    return find_video_files(folder, extensions=(".dav",))


# ---------------------------------------------------------------------------
# Temporary directory helpers
# ---------------------------------------------------------------------------

def make_temp_dir(prefix: str = "dav_tmp_") -> Path:
    """Create a temporary working directory and return its :class:`Path`."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    logger.debug("Temp dir created: %s", path)
    return path


def safe_unlink(
    path: Path,
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """
    Delete *path*, silently ignoring errors.

    Parameters
    ----------
    path:
        File to delete.
    log:
        Optional single-argument callable for warning messages.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        msg = f"[WARN] Could not delete {path.name}: {exc}"
        if log:
            log(msg)
        logger.warning(msg)


def cleanup_files(
    paths: list[Path],
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """Delete every path in *paths* via :func:`safe_unlink`."""
    for p in paths:
        safe_unlink(p, log)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def ensure_mp4_extension(path: str | Path) -> Path:
    """Return *path* with its extension replaced by ``.mp4``."""
    return Path(path).with_suffix(".mp4")
