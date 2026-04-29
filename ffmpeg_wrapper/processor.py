"""
ffmpeg_wrapper/processor.py
---------------------------
FFmpeg / FFprobe integration layer.

Design goals
~~~~~~~~~~~~
* Zero frame loss: every frame from every source segment must appear in the
  output exactly once, in the correct temporal position.
* VFR-safe concatenation: IMOU cameras produce 20 fps (day) and 15 fps
  (night) segments.  Forcing a single CFR on the concat would require
  duplicating or dropping frames at every day→night boundary.  Instead we:
    1. Transcode each segment preserving its native timestamps (``-fps_mode vfr``).
    2. Concatenate via the concat *demuxer* (not filter), which re-times each
       segment by offsetting its PTS so the output timeline is:
           t_out = t_in + Σ(durations of all prior segments)
       This guarantees total_duration = Σ(segment_durations).

FFmpeg flag reference (Step 1 - transcode)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
-c:v libx264          : H.264 video codec.
-crf 18               : Visually lossless quality (0–51, lower = better).
-preset fast          : Encoding speed vs. compression trade-off.
-fps_mode vfr         : [NEW flag ≥ FFmpeg 5.1]  Emit frames with their
                        original PTS; never duplicate or drop.  On older
                        builds the equivalent is ``-vsync vfr``.
-movflags +faststart  : Move MOOV atom to file start (streaming friendly).
-c:a aac -b:a 128k    : Re-encode audio to AAC for broad compatibility.
-map 0:v? -map 0:a?   : Include video and audio if present, skip silently
                        if a stream is absent (the ``?`` makes it optional).

FFmpeg flag reference (Step 2 - concat)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
-f concat             : Use the concat demuxer (reads a text playlist).
-safe 0               : Allow absolute/relative paths in the playlist.
-c copy               : Bitstream copy — no re-encode.  PTS values from
                        each segment are preserved and offset by the
                        cumulative duration of all preceding segments.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from fractions import Fraction
from pathlib import Path
from typing import Callable

# Type alias for a log callback: (message: str, level: str) -> None
LogCallback = Callable[[str, str], None]

# Sentinel string used by ffprobe when a field is unavailable.
_FFPROBE_N_A = "N/A"


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

class VideoInfo:
    """Metadata extracted from a single video file via ffprobe."""

    __slots__ = ("path", "fps", "duration", "width", "height", "has_audio", "codec")

    def __init__(
        self,
        path: Path,
        fps: float,
        duration: float,
        width: int,
        height: int,
        has_audio: bool,
        codec: str,
    ) -> None:
        self.path = path
        self.fps = fps
        self.duration = duration
        self.width = width
        self.height = height
        self.has_audio = has_audio
        self.codec = codec

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"VideoInfo(name={self.path.name!r}, fps={self.fps:.3f}, "
            f"duration={self.duration:.3f}s, {self.width}x{self.height})"
        )


# ---------------------------------------------------------------------------
# FFmpegProcessor
# ---------------------------------------------------------------------------

class FFmpegProcessor:
    """
    Encapsulates all FFmpeg/FFprobe subprocess interactions.

    Thread-safety
    ~~~~~~~~~~~~~
    :meth:`cancel` may be called from any thread.  It sets an internal
    threading.Event and calls ``Popen.kill()`` on the active subprocess so
    the worker loop exits cleanly.
    """

    def __init__(
        self,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path

        self._cancel_event = threading.Event()
        self._active_proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Signal the processor to abort and kill the running subprocess."""
        self._cancel_event.set()
        with self._proc_lock:
            if self._active_proc and self._active_proc.poll() is None:
                self._active_proc.kill()

    def reset(self) -> None:
        """Clear the cancellation flag (call before a new conversion run)."""
        self._cancel_event.clear()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    # ------------------------------------------------------------------
    # FFmpeg / FFprobe detection
    # ------------------------------------------------------------------

    def detect_ffmpeg(self) -> tuple[bool, str]:
        """
        Verify that both *ffmpeg* and *ffprobe* executables are reachable.

        Returns
        -------
        (ok: bool, message: str)
            *ok* is True when both tools are found.  *message* contains the
            FFmpeg version string on success, or an error description on failure.
        """
        try:
            result = subprocess.run(
                [self.ffmpeg_path, "-version"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=self._no_window_flag(),
            )
            version_line = result.stdout.splitlines()[0] if result.stdout else "unknown"

            subprocess.run(
                [self.ffprobe_path, "-version"],
                capture_output=True,
                timeout=10,
                creationflags=self._no_window_flag(),
            )
            return True, version_line
        except FileNotFoundError as exc:
            return False, f"Executable not found: {exc.filename}"
        except subprocess.TimeoutExpired:
            return False, "FFmpeg timed out during version check."
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def get_ffmpeg_version_tuple(self) -> tuple[int, int]:
        """
        Return (major, minor) version of FFmpeg so we can pick the correct
        fps-mode flag.  Returns (0, 0) on parse failure.
        """
        try:
            result = subprocess.run(
                [self.ffmpeg_path, "-version"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=self._no_window_flag(),
            )
            # e.g. "ffmpeg version 6.1.1 Copyright ..."
            m = re.search(r"ffmpeg version (\d+)\.(\d+)", result.stdout)
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception:  # noqa: BLE001
            pass
        return (0, 0)

    # ------------------------------------------------------------------
    # FFprobe – metadata extraction
    # ------------------------------------------------------------------

    def get_video_info(self, input_path: Path) -> VideoInfo:
        """
        Probe *input_path* and return a :class:`VideoInfo` instance.

        Raises
        ------
        RuntimeError
            If ffprobe exits non-zero or the output cannot be parsed.
        """
        cmd = [
            self.ffprobe_path,
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            str(input_path),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=self._no_window_flag(),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"ffprobe timed out on {input_path.name}")

        if result.returncode != 0:
            raise RuntimeError(
                f"ffprobe failed on {input_path.name}: {result.stderr[:300]}"
            )

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Could not parse ffprobe JSON: {exc}") from exc

        streams = data.get("streams", [])
        fmt = data.get("format", {})

        # -- Video stream --
        video_stream = next(
            (s for s in streams if s.get("codec_type") == "video"), None
        )
        if video_stream is None:
            raise RuntimeError(f"No video stream found in {input_path.name}")

        fps = self._parse_fps(
            video_stream.get("r_frame_rate", "0/1"),
            video_stream.get("avg_frame_rate", "0/1"),
        )

        # Duration: prefer stream-level, fall back to container-level.
        raw_dur = video_stream.get("duration") or fmt.get("duration", "0")
        duration = float(raw_dur) if raw_dur not in ("", _FFPROBE_N_A) else 0.0

        width = int(video_stream.get("width", 0))
        height = int(video_stream.get("height", 0))
        codec = video_stream.get("codec_name", "unknown")

        # -- Audio stream --
        has_audio = any(s.get("codec_type") == "audio" for s in streams)

        return VideoInfo(
            path=input_path,
            fps=fps,
            duration=duration,
            width=width,
            height=height,
            has_audio=has_audio,
            codec=codec,
        )

    # ------------------------------------------------------------------
    # Step 1 – Transcode each segment
    # ------------------------------------------------------------------

    def transcode_to_intermediate(
        self,
        info: VideoInfo,
        output_path: Path,
        log: LogCallback,
    ) -> bool:
        """
        Transcode a single source file to an intermediate H.264 MP4.

        The ``-fps_mode vfr`` flag (≥ FFmpeg 5.1) or ``-vsync vfr`` (older)
        instructs the muxer to write each frame with its original PTS rather
        than resampling to a fixed clock.  This is the cornerstone of
        zero-frame-loss VFR preservation.

        Returns
        -------
        bool
            True on success, False if cancelled or FFmpeg returned non-zero.
        """
        if self.is_cancelled:
            return False

        fps_flag = self._fps_mode_flag()

        # Build the command.  ``-map 0:v? -map 0:a?`` silently skips missing
        # streams rather than aborting — important for audio-less night clips.
        cmd = [
            self.ffmpeg_path,
            "-y",                      # overwrite without asking
            "-i", str(info.path),
            "-map", "0:v?",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-crf", "18",              # near-lossless quality
            "-preset", "fast",
            fps_flag[0], fps_flag[1],  # VFR preservation flag
            "-movflags", "+faststart",
            "-c:a", "aac",
            "-b:a", "128k",
            str(output_path),
        ]

        log(
            f"  Transcoding: {info.path.name} "
            f"({info.fps:.2f} fps, {info.duration:.1f}s) → {output_path.name}",
            "info",
        )
        return self._run_ffmpeg(cmd, log, label=info.path.name)

    # ------------------------------------------------------------------
    # Step 2 – Write concat list
    # ------------------------------------------------------------------

    def write_concat_list(
        self,
        segments: list[tuple[Path, float]],
        list_path: Path,
    ) -> None:
        """
        Write the concat demuxer playlist to *list_path*.

        Format
        ------
        The ``duration`` directive tells FFmpeg the exact length of each
        segment.  This is critical for the *last* segment: without it FFmpeg
        may under-report the total duration by a few milliseconds.

        ::

            file '/abs/path/to/0000.mp4'
            duration 300.000000
            file '/abs/path/to/0001.mp4'
            duration 300.000000
        """
        lines: list[str] = []
        for path, duration in segments:
            # Use forward slashes; FFmpeg on Windows accepts both styles.
            safe_path = str(path.resolve()).replace("\\", "/")
            lines.append(f"file '{safe_path}'")
            lines.append(f"duration {duration:.6f}")

        list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------
    # Step 3 – Concatenate
    # ------------------------------------------------------------------

    def concatenate_segments(
        self,
        list_path: Path,
        output_path: Path,
        log: LogCallback,
    ) -> bool:
        """
        Merge all segments listed in *list_path* into *output_path*.

        Uses the concat *demuxer* (``-f concat``) with ``-c copy`` so no
        re-encoding occurs.  Each segment's PTS values are offset by the
        cumulative duration of all prior segments, guaranteeing:

            output_duration == Σ(segment_durations)

        Returns
        -------
        bool
            True on success.
        """
        if self.is_cancelled:
            return False

        cmd = [
            self.ffmpeg_path,
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",             # bitstream copy — zero re-encode
            "-movflags", "+faststart",
            str(output_path),
        ]

        log("  Running concat demuxer (bitstream copy, no re-encode)…", "info")
        return self._run_ffmpeg(cmd, log, label="concat")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_ffmpeg(
        self,
        cmd: list[str],
        log: LogCallback,
        *,
        label: str = "",
    ) -> bool:
        """
        Execute *cmd* as a subprocess, streaming stderr line-by-line to *log*.

        FFmpeg writes all progress / diagnostic output to stderr.  We capture
        it in real-time so the GUI log window updates continuously.

        Returns True on exit-code 0, False otherwise (or on cancellation).
        """
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=self._no_window_flag(),
            )
        except OSError as exc:
            log(f"[ERROR] Could not launch FFmpeg: {exc}", "error")
            return False

        with self._proc_lock:
            self._active_proc = proc

        # Stream stderr — FFmpeg writes progress here.
        try:
            for raw_line in proc.stderr:  # type: ignore[union-attr]
                if self.is_cancelled:
                    proc.kill()
                    break
                line = raw_line.rstrip()
                if not line:
                    continue
                # Suppress overly verbose lines; keep meaningful ones.
                if self._is_loggable_line(line):
                    log(f"    {line}", "ffmpeg")
        finally:
            proc.wait()
            with self._proc_lock:
                self._active_proc = None

        if self.is_cancelled:
            return False

        if proc.returncode != 0:
            log(
                f"[ERROR] FFmpeg exited with code {proc.returncode} "
                f"while processing {label!r}",
                "error",
            )
            return False

        return True

    @staticmethod
    def _parse_fps(r_frame_rate: str, avg_frame_rate: str) -> float:
        """
        Parse a fractional frame-rate string (e.g. ``"20/1"``) to float.

        Prefers ``r_frame_rate`` (real base framerate) over ``avg_frame_rate``.
        Falls back gracefully on malformed or zero-denominator values.
        """
        for raw in (r_frame_rate, avg_frame_rate):
            try:
                frac = Fraction(raw)
                if frac.denominator != 0 and float(frac) > 0:
                    return float(frac)
            except (ValueError, ZeroDivisionError):
                continue
        return 25.0  # safe default

    def _fps_mode_flag(self) -> tuple[str, str]:
        """
        Return the appropriate FFmpeg fps-mode flag for the installed version.

        FFmpeg ≥ 5.1 renamed ``-vsync`` to ``-fps_mode``.  We detect the
        version at runtime so the application works with both old and new
        FFmpeg installations.
        """
        major, minor = self.get_ffmpeg_version_tuple()
        if (major, minor) >= (5, 1):
            return ("-fps_mode", "vfr")
        return ("-vsync", "vfr")

    @staticmethod
    def _is_loggable_line(line: str) -> bool:
        """
        Filter noisy FFmpeg output.  Returns True for lines worth displaying.

        We show progress lines (``frame=``) and any line containing keywords
        that indicate a meaningful event.  We suppress repetitive stream-info
        dumps that flood the log during concatenation.
        """
        important_keywords = (
            "frame=", "Error", "error", "Invalid", "failed",
            "Warning", "warning", "Output #", "Input #",
            "Duration:", "Stream mapping",
        )
        return any(kw in line for kw in important_keywords)

    @staticmethod
    def _no_window_flag() -> int:
        """
        Return the Windows ``CREATE_NO_WINDOW`` flag (0x08000000) so FFmpeg
        subprocesses never flash a console window in the background.

        Returns 0 on non-Windows platforms (harmless).
        """
        try:
            import subprocess as _sp
            return _sp.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        except AttributeError:
            return 0
