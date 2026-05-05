"""
utils/ffmpeg_utils.py
---------------------
Internal FFmpeg binary resolution — completely hidden from the user.

Resolution order (transparent in both dev and PyInstaller --onefile):
  1. sys._MEIPASS   → PyInstaller extraction dir
  2. Project root   → dev mode (alongside main.py)
  3. System PATH    → bare-name fallback

The user never sees, configures, or is aware of this logic.
"""
from __future__ import annotations

import logging
import re
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── Binary resolution ────────────────────────────────────────────────────────

def _base_dir() -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)          # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


def _resolve(name: str) -> str:
    base = _base_dir()
    for candidate in (base / f"{name}.exe", base / name):
        if candidate.is_file():
            logger.debug("Binary resolved: %s → %s", name, candidate)
            return str(candidate)
    logger.warning("%s not in bundle — trying system PATH", name)
    return name


def get_ffmpeg() -> str:
    return _resolve("ffmpeg")


def get_ffprobe() -> str:
    p = Path(get_ffmpeg())
    probe = p.parent / p.name.replace("ffmpeg", "ffprobe")
    if probe.is_file():
        return str(probe)
    return _resolve("ffprobe")


# ── File helpers ──────────────────────────────────────────────────────────────

def natural_sorted(paths: list[Path]) -> list[Path]:
    def key(p: Path) -> list:
        return [int(c) if c.isdigit() else c.lower()
                for c in re.split(r"(\d+)", p.name)]
    return sorted(paths, key=key)


def make_temp_dir(prefix: str = "dav4_") -> Path:
    p = Path(tempfile.mkdtemp(prefix=prefix))
    logger.debug("Temp dir: %s", p)
    return p


def safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Could not delete %s: %s", path.name, e)


def cleanup(paths: list[Path]) -> None:
    for p in paths:
        safe_unlink(p)
