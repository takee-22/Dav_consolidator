"""
utils/ffmpeg_utils.py
---------------------
FFmpeg binary resolution, file discovery, DAV filename parsing, and path helpers.

Binary resolution order
~~~~~~~~~~~~~~~~~~~~~~~
1. sys._MEIPASS   — PyInstaller --onefile extraction directory
2. Project root   — directory containing main.py (development mode)
3. System PATH    — last-resort bare-name fallback
"""

from __future__ import annotations

import logging
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
    if hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        logger.debug("PyInstaller mode: binary base = %s", base)
        return base
    base = Path(__file__).resolve().parent.parent
    logger.debug("Development mode: binary base = %s", base)
    return base


def _resolve_binary(name: str) -> str:
    base = _get_binary_base_dir()
    for candidate in (base / f"{name}.exe", base / name):
        if candidate.is_file():
            logger.debug("Resolved %s → %s", name, candidate)
            return str(candidate)
    logger.warning("%s not found in %s — falling back to system PATH", name, base)
    return name


def get_ffmpeg_path() -> str:
    return _resolve_binary("ffmpeg")


def get_ffprobe_path() -> str:
    return _resolve_binary("ffprobe")


def derive_ffprobe_from_ffmpeg(ffmpeg_path: str) -> str:
    p = Path(ffmpeg_path)
    if p.parent == Path("."):
        return get_ffprobe_path()
    return str(p.parent / p.name.replace("ffmpeg", "ffprobe"))


# ---------------------------------------------------------------------------
# DAV filename parsing
# ---------------------------------------------------------------------------

# Matches: 08.00.00-08.05.00[R][0@0][0].dav  (Dahua/IMOU naming convention)
_DAV_NAME_RE = re.compile(
    r"^(\d{2})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})"
)


def parse_dav_times(path: Path) -> Optional[tuple[str, str]]:
    """
    Extract (start_time, end_time) from a DAV filename.

    Returns ("08.00.00", "08.05.00") or None if the name doesn't match.
    """
    m = _DAV_NAME_RE.match(path.stem)
    if not m:
        return None
    sh, sm, ss, eh, em, es = m.groups()
    return f"{sh}.{sm}.{ss}", f"{eh}.{em}.{es}"


def build_output_filename(files: list[Path], output_dir: Path) -> Path:
    """
    Derive output filename from the first file's start time and last file's end time.

    Example:
        files[0]  = 08.00.00-08.05.00[R][0@0][0].dav
        files[-1] = 08.55.00-09.00.00[R][0@0][0].dav
        → output  = 08.00.00-09.00.00.mp4
    """
    if not files:
        return output_dir / "output.mp4"

    first = parse_dav_times(files[0])
    last  = parse_dav_times(files[-1])

    if first and last:
        name = f"{first[0]}-{last[1]}.mp4"
        logger.debug("Auto output name: %s", name)
        return output_dir / name

    logger.warning("Could not parse DAV times from filenames — using 'output.mp4'")
    return output_dir / "output.mp4"


# ---------------------------------------------------------------------------
# Natural sort
# ---------------------------------------------------------------------------

def _natural_key(path: Path) -> list:
    parts = re.split(r"(\d+)", path.name)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def natural_sorted(paths: list[Path]) -> list[Path]:
    return sorted(paths, key=_natural_key)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

_DEFAULT_EXTENSIONS: tuple[str, ...] = (".dav",)


def find_video_files(
    folder: str | Path,
    extensions: tuple[str, ...] = _DEFAULT_EXTENSIONS,
) -> list[Path]:
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder}")

    files: list[Path] = []
    for ext in extensions:
        files.extend(folder.rglob(f"*{ext}"))
        files.extend(folder.rglob(f"*{ext.upper()}"))

    seen: set[Path] = set()
    unique: list[Path] = []
    for f in files:
        resolved = f.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(f)

    return natural_sorted(unique)


def find_dav_files(folder: str | Path) -> list[Path]:
    return find_video_files(folder, extensions=(".dav",))


# ---------------------------------------------------------------------------
# Temp directory helpers
# ---------------------------------------------------------------------------

def make_temp_dir(prefix: str = "dav_tmp_") -> Path:
    path = Path(tempfile.mkdtemp(prefix=prefix))
    logger.debug("Temp dir created: %s", path)
    return path


def safe_unlink(path: Path, log: Optional[Callable[[str], None]] = None) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        msg = f"[WARN] Could not delete {path.name}: {exc}"
        if log:
            log(msg)
        logger.warning(msg)


def cleanup_files(paths: list[Path], log: Optional[Callable[[str], None]] = None) -> None:
    for p in paths:
        safe_unlink(p, log)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def ensure_mp4_extension(path: str | Path) -> Path:
    return Path(path).with_suffix(".mp4")
