"""
core/gpu_detector.py
--------------------
Hardware encoder auto-detection for NVIDIA NVENC, Intel QSV, and AMD AMF.

Each detector runs a real (tiny) test encode — not just an encoder listing —
so we confirm the driver stack is functional, not merely installed.

Detection order: NVENC → QSV → AMF → CPU (libx264)
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

logger = logging.getLogger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Synthetic lavfi source: 1-frame black square, negligible overhead
_TEST_SRC = "color=black:s=64x64:r=1:d=0.04"


class Accelerator(Enum):
    NVENC = auto()   # NVIDIA NVENC
    QSV   = auto()   # Intel Quick Sync Video
    AMF   = auto()   # AMD Advanced Media Framework
    CPU   = auto()   # Software libx264


@dataclass(frozen=True)
class GPUInfo:
    accelerator: Accelerator
    encoder:     str    # ffmpeg encoder name, e.g. "h264_nvenc"
    label:       str    # human-readable, e.g. "NVIDIA NVENC"
    detected:    bool


def _test_encoder(ffmpeg: str, encoder: str) -> bool:
    """Return True if *encoder* successfully encodes a single frame."""
    try:
        r = subprocess.run(
            [
                ffmpeg, "-y",
                "-f", "lavfi", "-i", _TEST_SRC,
                "-c:v", encoder,
                "-frames:v", "1",
                "-f", "null", "-",
            ],
            capture_output=True,
            timeout=20,
            creationflags=_NO_WINDOW,
        )
        ok = r.returncode == 0
        logger.debug("Encoder test %s → %s", encoder, "OK" if ok else "FAIL")
        return ok
    except Exception as exc:
        logger.debug("Encoder test %s raised: %s", encoder, exc)
        return False


def detect_best(ffmpeg: str) -> GPUInfo:
    """
    Probe hardware encoders in priority order and return the best available.

    Priority: NVIDIA NVENC > Intel QSV > AMD AMF > CPU libx264
    The first successful probe wins; fallback is always CPU.
    """
    candidates = [
        (Accelerator.NVENC, "h264_nvenc", "NVIDIA NVENC"),
        (Accelerator.QSV,   "h264_qsv",   "Intel QSV"),
        (Accelerator.AMF,   "h264_amf",   "AMD AMF"),
    ]

    logger.info("Probing hardware encoders…")
    for accel, encoder, label in candidates:
        if _test_encoder(ffmpeg, encoder):
            logger.info("Selected accelerator: %s (%s)", label, encoder)
            return GPUInfo(accelerator=accel, encoder=encoder,
                           label=label, detected=True)

    logger.info("No hardware encoder found — using CPU (libx264)")
    return GPUInfo(accelerator=Accelerator.CPU, encoder="libx264",
                   label="CPU (libx264)", detected=False)


def detect_all(ffmpeg: str) -> list[GPUInfo]:
    """Return GPUInfo for every candidate (detected or not). Used for status display."""
    candidates = [
        (Accelerator.NVENC, "h264_nvenc", "NVIDIA NVENC"),
        (Accelerator.QSV,   "h264_qsv",   "Intel QSV"),
        (Accelerator.AMF,   "h264_amf",   "AMD AMF"),
    ]
    results = []
    for accel, encoder, label in candidates:
        ok = _test_encoder(ffmpeg, encoder)
        results.append(GPUInfo(accelerator=accel, encoder=encoder,
                               label=label, detected=ok))
    return results
