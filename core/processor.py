"""
core/processor.py
-----------------
FFmpeg / FFprobe processing pipeline.

Architecture
~~~~~~~~~~~~
* ``VideoInfo``       — immutable metadata from a single ffprobe call.
* ``ProcessingPlan``  — encoding strategy chosen after analysing all segments.
* ``FFmpegProcessor`` — orchestrates subprocess calls; cancel-safe.

Processing strategy (in priority order)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
1. **Stream copy** (fastest, lossless):
   All segments share the same codec AND resolution AND the codec is
   known-copy-compatible (H.264, HEVC, VP8/9, AV1).
   Uses ``-c copy`` throughout — zero re-encoding.

2. **GPU transcode** (fast, near-lossless):
   User requested GPU acceleration AND NVIDIA NVENC is detected and
   functional.  Uses ``h264_nvenc`` with ``-rc vbr -cq 18``.

3. **CPU transcode** (universal fallback):
   ``libx264 -crf 18 -preset fast`` — always available.

VFR / frame-loss safety
~~~~~~~~~~~~~~~~~~~~~~~~
``-fps_mode vfr`` (FFmpeg ≥ 5.1) or ``-vsync vfr`` (older builds) is
applied during transcoding to preserve each frame's original PTS rather
than resampling to a fixed clock.  IMOU cameras mix 20 fps (day) and
15 fps (night) segments; this flag is the cornerstone of zero-frame-loss.

Thread-safety
~~~~~~~~~~~~~
:meth:`FFmpegProcessor.cancel` may be called from any thread.  It sets
a :class:`threading.Event` and immediately kills the active subprocess.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

LogCallback      = Callable[[str, str], None]   # (message, level)
ProgressCallback = Callable[[int, int], None]   # (current, total)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FFPROBE_NA = "N/A"

# Codecs safe for -c copy demux concat without re-encoding.
# (Raw formats like MJPEG are intentionally excluded — they need re-wrap.)
_COPY_SAFE_CODECS: frozenset[str] = frozenset({
    "h264", "hevc", "h265", "vp8", "vp9", "av1",
})

# NVENC preset: p4 = balanced quality/speed (NVENC SDK ≥ 11 presets)
_NVENC_PRESET = "p4"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VideoInfo:
    """Metadata extracted from a single video file via ffprobe."""

    path:        Path
    fps:         float
    duration:    float
    width:       int
    height:      int
    has_audio:   bool
    codec:       str          # lower-case video codec name, e.g. "h264"
    audio_codec: str = ""     # lower-case audio codec name, e.g. "aac"

    def resolution(self) -> tuple[int, int]:
        return (self.width, self.height)

    def __str__(self) -> str:
        return (
            f"{self.path.name}  {self.fps:.2f}fps  {self.duration:.2f}s  "
            f"{self.codec}  {self.width}x{self.height}"
        )


@dataclass(frozen=True)
class ProcessingPlan:
    """
    Encoding strategy determined after probing all source segments.

    Attributes
    ----------
    use_stream_copy:
        True → skip transcoding, use ``-c copy`` at segment-copy stage.
    use_gpu:
        True → use NVIDIA NVENC; False → libx264.
    needs_transcode:
        True when any re-encoding is required (i.e. not a pure stream copy).
    reason:
        Human-readable explanation for display in the GUI.
    """

    use_stream_copy: bool
    use_gpu:         bool
    needs_transcode: bool
    reason:          str

    @classmethod
    def stream_copy(cls, reason: str) -> "ProcessingPlan":
        return cls(
            use_stream_copy=True, use_gpu=False,
            needs_transcode=False, reason=reason,
        )

    @classmethod
    def transcode_gpu(cls, reason: str) -> "ProcessingPlan":
        return cls(
            use_stream_copy=False, use_gpu=True,
            needs_transcode=True, reason=reason,
        )

    @classmethod
    def transcode_cpu(cls, reason: str) -> "ProcessingPlan":
        return cls(
            use_stream_copy=False, use_gpu=False,
            needs_transcode=True, reason=reason,
        )


# ---------------------------------------------------------------------------
# FFmpegProcessor
# ---------------------------------------------------------------------------

class FFmpegProcessor:
    """
    Encapsulates all FFmpeg / FFprobe subprocess interactions.

    Parameters
    ----------
    ffmpeg_path:
        Absolute path to the ffmpeg binary (or bare 'ffmpeg' for PATH).
    ffprobe_path:
        Absolute path to the ffprobe binary (or bare 'ffprobe' for PATH).
    use_gpu:
        User preference: attempt NVENC.  Validated at runtime.
    max_workers:
        Thread pool size for parallel :meth:`probe_all_parallel` calls.
    """

    def __init__(
        self,
        ffmpeg_path:  str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        use_gpu:      bool = False,
        max_workers:  int  = 4,
    ) -> None:
        self.ffmpeg_path  = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self._prefer_gpu  = use_gpu
        self._max_workers = max(1, max_workers)

        self._cancel_event  = threading.Event()
        self._active_proc:  Optional[subprocess.Popen] = None
        self._proc_lock     = threading.Lock()

        # Cached results (reset on each :meth:`reset` call)
        self._ffmpeg_version: Optional[tuple[int, int]] = None
        self._nvenc_available: Optional[bool]           = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """
        Signal cancellation and immediately kill the active subprocess.
        Safe to call from any thread.
        """
        self._cancel_event.set()
        with self._proc_lock:
            if self._active_proc and self._active_proc.poll() is None:
                self._active_proc.kill()
                logger.info("Active FFmpeg subprocess killed (cancel requested)")

    def reset(self) -> None:
        """Clear the cancel flag and cached GPU state before a new run."""
        self._cancel_event.clear()
        self._nvenc_available = None
        logger.debug("FFmpegProcessor reset")

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    # ------------------------------------------------------------------
    # Binary detection
    # ------------------------------------------------------------------

    def detect_ffmpeg(self) -> tuple[bool, str]:
        """
        Verify that both ffmpeg and ffprobe binaries are reachable.

        Returns
        -------
        (ok: bool, message: str)
            *ok* is True when both tools respond.  *message* is the
            FFmpeg version string on success, or an error description.
        """
        try:
            r = subprocess.run(
                [self.ffmpeg_path, "-version"],
                capture_output=True, text=True, timeout=10,
                creationflags=self._no_window_flag(),
            )
            version_line = r.stdout.splitlines()[0] if r.stdout else "unknown"

            subprocess.run(
                [self.ffprobe_path, "-version"],
                capture_output=True, timeout=10,
                creationflags=self._no_window_flag(),
            )
            logger.info("FFmpeg detected: %s", version_line)
            return True, version_line

        except FileNotFoundError as exc:
            return False, f"Executable not found: {exc.filename}"
        except subprocess.TimeoutExpired:
            return False, "FFmpeg timed out during version check."
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def get_ffmpeg_version(self) -> tuple[int, int]:
        """
        Return (major, minor) FFmpeg version; (0, 0) on parse failure.
        Result is cached for the lifetime of this processor instance.
        """
        if self._ffmpeg_version is not None:
            return self._ffmpeg_version
        try:
            r = subprocess.run(
                [self.ffmpeg_path, "-version"],
                capture_output=True, text=True, timeout=10,
                creationflags=self._no_window_flag(),
            )
            m = re.search(r"ffmpeg version (\d+)\.(\d+)", r.stdout)
            if m:
                self._ffmpeg_version = (int(m.group(1)), int(m.group(2)))
                return self._ffmpeg_version
        except Exception:  # noqa: BLE001
            pass
        self._ffmpeg_version = (0, 0)
        return self._ffmpeg_version

    def detect_nvenc(self) -> bool:
        """
        Check NVENC availability by running a tiny test encode.

        The test encodes a single black frame (64×64, 0.1 s) to /dev/null
        (or NUL on Windows).  This confirms the driver/SDK are functional,
        not just that the encoder is listed in ``ffmpeg -encoders``.

        Result is cached until :meth:`reset` is called.
        """
        if self._nvenc_available is not None:
            return self._nvenc_available

        logger.info("Probing NVENC availability…")
        try:
            r = subprocess.run(
                [
                    self.ffmpeg_path, "-y",
                    "-f", "lavfi", "-i", "color=black:s=64x64:r=1:d=0.1",
                    "-c:v", "h264_nvenc",
                    "-frames:v", "1",
                    "-f", "null", "-",
                ],
                capture_output=True, text=True, timeout=20,
                creationflags=self._no_window_flag(),
            )
            available = r.returncode == 0
        except Exception:  # noqa: BLE001
            available = False

        self._nvenc_available = available
        logger.info("NVENC available: %s", available)
        return available

    # ------------------------------------------------------------------
    # FFprobe – metadata extraction
    # ------------------------------------------------------------------

    def get_video_info(self, input_path: Path) -> VideoInfo:
        """
        Probe *input_path* and return a :class:`VideoInfo`.

        Raises
        ------
        RuntimeError
            On ffprobe failure, JSON parse error, or missing video stream.
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
                cmd, capture_output=True, text=True, timeout=30,
                creationflags=self._no_window_flag(),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"ffprobe timed out on {input_path.name}")

        if result.returncode != 0:
            raise RuntimeError(
                f"ffprobe failed on {input_path.name}: {result.stderr[:400]}"
            )

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Could not parse ffprobe JSON: {exc}") from exc

        streams = data.get("streams", [])
        fmt     = data.get("format", {})

        # Video stream
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None:
            raise RuntimeError(f"No video stream found in {input_path.name}")

        fps      = self._parse_fps(video.get("r_frame_rate", "0/1"),
                                   video.get("avg_frame_rate", "0/1"))
        raw_dur  = video.get("duration") or fmt.get("duration", "0")
        duration = float(raw_dur) if raw_dur not in ("", _FFPROBE_NA) else 0.0
        width    = int(video.get("width",  0))
        height   = int(video.get("height", 0))
        codec    = video.get("codec_name", "unknown").lower()

        # Audio stream
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        has_audio   = audio is not None
        audio_codec = (audio.get("codec_name", "") if audio else "").lower()

        info = VideoInfo(
            path=input_path, fps=fps, duration=duration,
            width=width, height=height,
            has_audio=has_audio, codec=codec, audio_codec=audio_codec,
        )
        logger.debug("Probed: %s", info)
        return info

    def probe_all_parallel(
        self,
        paths:       list[Path],
        log:         LogCallback,
        on_progress: Optional[ProgressCallback] = None,
    ) -> list[VideoInfo]:
        """
        Probe *paths* in parallel using a :class:`ThreadPoolExecutor`.

        Files that fail to probe are logged as warnings and excluded.
        The returned list preserves the original sort order.

        Parameters
        ----------
        paths:
            Source video paths to probe.
        log:
            GUI log callback ``(message, level)``.
        on_progress:
            Optional ``(done, total)`` callback for the progress bar.

        Returns
        -------
        list[VideoInfo]
            Successfully probed infos in original path order.
        """
        results: dict[int, Optional[VideoInfo]] = {}
        total = len(paths)

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            future_to_idx = {
                pool.submit(self.get_video_info, p): i
                for i, p in enumerate(paths)
            }
            done = 0
            for future in as_completed(future_to_idx):
                if self.is_cancelled:
                    break
                idx = future_to_idx[future]
                try:
                    info = future.result()
                    results[idx] = info
                    log(f"  [{idx + 1:>3}] {info}", "info")
                except RuntimeError as exc:
                    log(f"  [WARN] Skipping {paths[idx].name}: {exc}", "warning")
                    logger.warning("Probe failed for %s: %s", paths[idx].name, exc)
                    results[idx] = None
                done += 1
                if on_progress:
                    on_progress(done, total)

        # Reconstruct in original order, drop failures
        return [results[i] for i in sorted(results) if results[i] is not None]

    # ------------------------------------------------------------------
    # Processing plan
    # ------------------------------------------------------------------

    def build_plan(
        self,
        infos:     list[VideoInfo],
        force_gpu: bool = False,
    ) -> ProcessingPlan:
        """
        Analyse probed metadata and choose the optimal encoding strategy.

        Decision tree
        -------------
        1. All same codec + resolution + codec is copy-safe → stream copy.
        2. GPU requested and NVENC functional → NVENC transcode.
        3. Otherwise → CPU libx264 transcode.

        Parameters
        ----------
        infos:
            Probed metadata for all source segments.
        force_gpu:
            Override the instance-level ``use_gpu`` preference.
        """
        if not infos:
            return ProcessingPlan.transcode_cpu("No input files")

        codecs      = {i.codec for i in infos}
        resolutions = {i.resolution() for i in infos}
        first_codec = infos[0].codec

        can_copy = (
            len(codecs)      == 1
            and len(resolutions) == 1
            and first_codec in _COPY_SAFE_CODECS
        )

        if can_copy:
            reason = (
                f"All {len(infos)} segments share codec={first_codec!r}, "
                f"resolution={infos[0].width}x{infos[0].height} "
                f"→ stream copy (zero re-encode, fastest)"
            )
            logger.info("Plan: %s", reason)
            return ProcessingPlan.stream_copy(reason)

        # Explain why copy was not possible
        if len(codecs) > 1:
            mismatch = f"mixed codecs {codecs}"
        elif len(resolutions) > 1:
            mismatch = f"mixed resolutions {resolutions}"
        else:
            mismatch = f"codec {first_codec!r} not copy-safe"

        want_gpu = force_gpu or self._prefer_gpu
        if want_gpu:
            if self.detect_nvenc():
                reason = f"{mismatch} → GPU transcode (NVENC h264_nvenc)"
                logger.info("Plan: %s", reason)
                return ProcessingPlan.transcode_gpu(reason)
            logger.warning("NVENC requested but unavailable; falling back to CPU")

        reason = f"{mismatch} → CPU transcode (libx264 -crf 18)"
        logger.info("Plan: %s", reason)
        return ProcessingPlan.transcode_cpu(reason)

    # ------------------------------------------------------------------
    # Step 1a – Transcode a single segment
    # ------------------------------------------------------------------

    def transcode_segment(
        self,
        info:        VideoInfo,
        output_path: Path,
        plan:        ProcessingPlan,
        log:         LogCallback,
    ) -> bool:
        """
        Transcode *info.path* to an intermediate H.264 MP4 at *output_path*.

        Codec selection
        ~~~~~~~~~~~~~~~
        * GPU  (NVENC): ``h264_nvenc -rc vbr -cq 18 -preset p4``
        * CPU (libx264): ``libx264 -crf 18 -preset fast``

        ``-fps_mode vfr`` / ``-vsync vfr`` preserves original frame PTS,
        preventing frame duplication or dropping at VFR boundaries.

        Returns True on success; False on cancellation or FFmpeg error.
        """
        if self.is_cancelled:
            return False

        fps_flag = self._fps_mode_flag()

        if plan.use_gpu:
            video_codec_args = [
                "-c:v", "h264_nvenc",
                "-rc",  "vbr",
                "-cq",  "18",
                "-preset", _NVENC_PRESET,
                "-b:v", "0",      # let -cq control quality, not target bitrate
            ]
            codec_label = "NVENC"
        else:
            video_codec_args = [
                "-c:v", "libx264",
                "-crf",    "18",
                "-preset", "fast",
            ]
            codec_label = "libx264"

        audio_args = (
            ["-c:a", "aac", "-b:a", "128k"] if info.has_audio else ["-an"]
        )

        cmd = [
            self.ffmpeg_path, "-y",
            "-i", str(info.path),
            "-map", "0:v?",       # include video if present; skip silently if absent
            "-map", "0:a?",       # include audio if present
            *video_codec_args,
            fps_flag[0], fps_flag[1],
            "-movflags", "+faststart",
            *audio_args,
            str(output_path),
        ]

        log(
            f"  Transcoding [{codec_label}]: {info.path.name} "
            f"({info.fps:.2f}fps, {info.duration:.1f}s) → {output_path.name}",
            "info",
        )
        logger.info("Transcoding %s via %s", info.path.name, codec_label)
        return self._run_ffmpeg(cmd, log, label=info.path.name)

    # ------------------------------------------------------------------
    # Step 1b – Stream copy a single segment (no transcode)
    # ------------------------------------------------------------------

    def copy_segment(
        self,
        info:        VideoInfo,
        output_path: Path,
        log:         LogCallback,
    ) -> bool:
        """
        Re-wrap *info.path* into an intermediate MP4 using ``-c copy``.

        Faster than transcoding (no decoding/encoding); used when all
        segments qualify for stream copy per :meth:`build_plan`.
        The MP4 container re-wrap ensures the concat demuxer can interleave
        PTS values correctly regardless of the original container format.

        Returns True on success.
        """
        if self.is_cancelled:
            return False

        cmd = [
            self.ffmpeg_path, "-y",
            "-i", str(info.path),
            "-map", "0:v?",
            "-map", "0:a?",
            "-c",    "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]
        log(f"  Copying (stream copy): {info.path.name} → {output_path.name}", "info")
        logger.info("Stream-copying %s", info.path.name)
        return self._run_ffmpeg(cmd, log, label=info.path.name)

    # ------------------------------------------------------------------
    # Step 2 – Write concat list
    # ------------------------------------------------------------------

    def write_concat_list(
        self,
        segments:  list[tuple[Path, float]],
        list_path: Path,
    ) -> None:
        """
        Write the FFmpeg concat demuxer playlist to *list_path*.

        The ``duration`` directive is included for every segment so FFmpeg
        can accurately compute the total duration even if the last segment's
        container duration is missing or slightly off.

        Example output::

            file '/abs/path/0000.mp4'
            duration 300.000000
            file '/abs/path/0001.mp4'
            duration 300.000000
        """
        lines: list[str] = []
        for path, duration in segments:
            safe_path = str(path.resolve()).replace("\\", "/")
            lines.append(f"file '{safe_path}'")
            lines.append(f"duration {duration:.6f}")

        list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.debug(
            "Wrote concat list → %s (%d segments)", list_path, len(segments)
        )

    # ------------------------------------------------------------------
    # Step 3 – Concatenate
    # ------------------------------------------------------------------

    def concatenate_segments(
        self,
        list_path:   Path,
        output_path: Path,
        log:         LogCallback,
    ) -> bool:
        """
        Merge all intermediate segments into *output_path* via the
        concat demuxer (``-f concat -c copy``).

        PTS values are offset by the cumulative duration of all preceding
        segments, ensuring:

            output_duration == Σ(segment_durations)

        No re-encoding occurs here regardless of the processing plan.

        Returns True on success.
        """
        if self.is_cancelled:
            return False

        cmd = [
            self.ffmpeg_path, "-y",
            "-f",    "concat",
            "-safe", "0",
            "-i",    str(list_path),
            "-c",    "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]
        log("  Running concat demuxer (bitstream copy, no re-encode)…", "info")
        logger.info("Concatenating %d segments → %s", 0, output_path)
        return self._run_ffmpeg(cmd, log, label="concat")

    # ------------------------------------------------------------------
    # Internal subprocess runner
    # ------------------------------------------------------------------

    def _run_ffmpeg(
        self,
        cmd:   list[str],
        log:   LogCallback,
        *,
        label: str = "",
    ) -> bool:
        """
        Launch *cmd* as a subprocess, streaming stderr to *log* in real time.

        FFmpeg writes all progress and diagnostics to stderr.  We capture
        it line-by-line and forward filtered lines to the GUI log pane so
        the user sees live progress without blocking the UI thread.

        Returns
        -------
        bool
            True on exit-code 0.  False on subprocess error, OS failure,
            or cancellation.
        """
        logger.debug("Launching: %s", " ".join(cmd))
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
            logger.error("OSError launching FFmpeg: %s", exc)
            return False

        with self._proc_lock:
            self._active_proc = proc

        try:
            for raw_line in proc.stderr:  # type: ignore[union-attr]
                if self.is_cancelled:
                    proc.kill()
                    break
                line = raw_line.rstrip()
                if not line:
                    continue
                logger.debug("[ffmpeg] %s", line)
                if self._is_loggable(line):
                    log(f"    {line}", "ffmpeg")
        finally:
            proc.wait()
            with self._proc_lock:
                self._active_proc = None

        if self.is_cancelled:
            return False

        if proc.returncode != 0:
            log(
                f"[ERROR] FFmpeg exited {proc.returncode} "
                f"while processing {label!r}",
                "error",
            )
            logger.error("FFmpeg exit %d for %r", proc.returncode, label)
            return False

        return True

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_fps(r_frame_rate: str, avg_frame_rate: str) -> float:
        """
        Parse a fractional frame-rate string (e.g. ``"20/1"``) to float.

        Tries ``r_frame_rate`` first (real base frame rate from the container
        header), then ``avg_frame_rate`` (averaged over the stream).
        Returns 25.0 as a safe fallback if both fail.
        """
        for raw in (r_frame_rate, avg_frame_rate):
            try:
                frac = Fraction(raw)
                val  = float(frac)
                if frac.denominator != 0 and val > 0:
                    return val
            except (ValueError, ZeroDivisionError):
                continue
        return 25.0

    def _fps_mode_flag(self) -> tuple[str, str]:
        """
        Return the correct FFmpeg VFR-preservation flag for the installed version.

        FFmpeg ≥ 5.1 renamed ``-vsync`` to ``-fps_mode``; both versions
        accept ``vfr`` as the argument.  We detect at runtime so the
        application works with older FFmpeg installations.
        """
        major, minor = self.get_ffmpeg_version()
        if (major, minor) >= (5, 1):
            return ("-fps_mode", "vfr")
        return ("-vsync", "vfr")

    @staticmethod
    def _is_loggable(line: str) -> bool:
        """
        Return True for FFmpeg stderr lines worth forwarding to the GUI.

        We show progress lines (``frame=``), error/warning keywords, and
        structural messages (Input/Output blocks, Duration, Stream mapping).
        We suppress the repetitive stream-dump lines that flood logs during
        concatenation of many segments.
        """
        important = (
            "frame=", "Error", "error", "Invalid", "failed",
            "Warning", "warning", "Output #", "Input #",
            "Duration:", "Stream mapping",
        )
        return any(kw in line for kw in important)

    @staticmethod
    def _no_window_flag() -> int:
        """
        Return Windows ``CREATE_NO_WINDOW`` (0x08000000) so FFmpeg
        subprocesses never flash a console window.  Returns 0 elsewhere.
        """
        try:
            import subprocess as _sp
            return _sp.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        except AttributeError:
            return 0
