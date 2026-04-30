"""
core/processor.py
-----------------
FFmpeg / FFprobe processing pipeline — DAV forensic-accurate mode.

Key fixes over previous version
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
BUG 1 – Wrong FPS source:
    Old code tried r_frame_rate first. DAV files store the container
    timebase (e.g. "90000/1") there, NOT the frame rate. Fixed: try
    avg_frame_rate first and validate the result is in 1–120 fps range.

BUG 2 – Missing DAV timestamp reconstruction:
    Old transcode_segment had no -fflags +genpts+igndts, no setpts filter,
    no fps enforcement, no tpad. These caused the "59:49 instead of 60:00"
    frame-loss problem. Fixed: full filter chain applied on every segment.

BUG 3 – Stream copy unsafe for DAV:
    Old copy_segment used -c copy, which copies the broken DAV timestamps
    straight into the MP4. build_plan now NEVER returns stream_copy when
    any input file is a .dav (detected by extension). DAV files always need
    the PTS-reconstruction transcode path.

BUG 4 – Broken probe duration used in concat list:
    Old write_concat_list used info.duration (often ~299.08 s for a 300 s
    segment). The concat demuxer then computed cumulative offsets using those
    wrong values, producing drift. Fixed: always write SEGMENT_DURATION_SEC
    (300.0) as the duration directive so offsets are exact.

Watermark ↔ player-timeline alignment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The critical filter is:
    fps=FPS → enforce constant frame rate (no VFR gaps)
    setpts=N/FRAME_RATE/TB → rebuild every PTS from frame ordinal index
Frame 0 → PTS 0, Frame 1 → PTS 1/FPS, Frame N → PTS N/FPS.
Since the recording clock starts at t=0 for every segment, and there are
no gaps between consecutive frames, the player timeline matches the
burned-in watermark exactly throughout the concatenated output.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
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

# Every DAV segment is exactly this many seconds.
# Used for: PTS-cap (-t), tpad duration, concat list duration directive.
SEGMENT_DURATION_SEC: int = 300   # 5 minutes

_FFPROBE_NA = "N/A"

# Codecs that *would* be copy-safe in an ideal world — kept for reference
# but DAV files bypass copy entirely due to broken timestamps (see build_plan).
_COPY_SAFE_CODECS: frozenset[str] = frozenset({
    "h264", "hevc", "h265", "vp8", "vp9", "av1",
})

_NVENC_PRESET    = "p4"        # NVENC SDK ≥ 11 balanced preset
_AUDIO_SAMPLE_RATE = 44100     # target sample rate for aresample


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VideoInfo:
    """Immutable metadata from a single ffprobe call."""

    path:        Path
    fps:         float
    duration:    float   # container-reported; may be wrong for DAV — do NOT use for timing
    width:       int
    height:      int
    has_audio:   bool
    codec:       str
    audio_codec: str = ""

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
    Encoding strategy selected after probing all segments.

    NOTE: use_stream_copy is intentionally NEVER set for .dav inputs.
    DAV timestamps are always broken and must be reconstructed via
    the setpts filter (which requires decoding → cannot stream copy).
    """

    use_stream_copy: bool
    use_gpu:         bool
    needs_transcode: bool
    reason:          str

    @classmethod
    def stream_copy(cls, reason: str) -> "ProcessingPlan":
        return cls(use_stream_copy=True,  use_gpu=False, needs_transcode=False, reason=reason)

    @classmethod
    def transcode_gpu(cls, reason: str) -> "ProcessingPlan":
        return cls(use_stream_copy=False, use_gpu=True,  needs_transcode=True,  reason=reason)

    @classmethod
    def transcode_cpu(cls, reason: str) -> "ProcessingPlan":
        return cls(use_stream_copy=False, use_gpu=False, needs_transcode=True,  reason=reason)


# ---------------------------------------------------------------------------
# FFmpegProcessor
# ---------------------------------------------------------------------------

class FFmpegProcessor:
    """
    Orchestrates all FFmpeg / FFprobe subprocess calls.

    Parameters
    ----------
    ffmpeg_path / ffprobe_path:
        Absolute paths or bare names ('ffmpeg'/'ffprobe') for PATH fallback.
    use_gpu:
        Instance-level GPU preference; validated at runtime by detect_nvenc().
    max_workers:
        Thread-pool size for parallel probe_all_parallel() calls.
    segment_duration:
        Expected duration of each input segment in seconds (default 300 = 5 min).
        Used for -t cap and tpad.
    """

    def __init__(
        self,
        ffmpeg_path:      str  = "ffmpeg",
        ffprobe_path:     str  = "ffprobe",
        use_gpu:          bool = False,
        max_workers:      int  = 4,
        segment_duration: int  = SEGMENT_DURATION_SEC,
    ) -> None:
        self.ffmpeg_path      = ffmpeg_path
        self.ffprobe_path     = ffprobe_path
        self._prefer_gpu      = use_gpu
        self._max_workers     = max(1, max_workers)
        self._segment_duration = segment_duration

        self._cancel_event   = threading.Event()
        self._active_proc:   Optional[subprocess.Popen] = None
        self._proc_lock      = threading.Lock()

        self._ffmpeg_version:  Optional[tuple[int, int]] = None
        self._nvenc_available: Optional[bool]            = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Kill active subprocess and signal cancellation. Thread-safe."""
        self._cancel_event.set()
        with self._proc_lock:
            if self._active_proc and self._active_proc.poll() is None:
                self._active_proc.kill()
                logger.info("Active FFmpeg subprocess killed (cancel requested)")

    def reset(self) -> None:
        """Clear cancel flag and GPU cache before a new run."""
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
        """Verify both binaries are reachable. Returns (ok, version_or_error)."""
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
        except Exception as exc:
            return False, str(exc)

    def get_ffmpeg_version(self) -> tuple[int, int]:
        """Return (major, minor); (0,0) on failure. Cached per instance."""
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
        except Exception:
            pass
        self._ffmpeg_version = (0, 0)
        return self._ffmpeg_version

    def detect_nvenc(self) -> bool:
        """
        Test NVENC with a real tiny encode (not just -encoders listing).
        Cached until reset() is called.
        """
        if self._nvenc_available is not None:
            return self._nvenc_available
        logger.info("Probing NVENC availability…")
        try:
            r = subprocess.run(
                [
                    self.ffmpeg_path, "-y",
                    "-f", "lavfi", "-i", "color=black:s=64x64:r=1:d=0.1",
                    "-c:v", "h264_nvenc", "-frames:v", "1",
                    "-f", "null", "-",
                ],
                capture_output=True, timeout=20,
                creationflags=self._no_window_flag(),
            )
            available = r.returncode == 0
        except Exception:
            available = False
        self._nvenc_available = available
        logger.info("NVENC available: %s", available)
        return available

    # ------------------------------------------------------------------
    # FFprobe – metadata extraction
    # ------------------------------------------------------------------

    def get_video_info(self, input_path: Path) -> VideoInfo:
        """
        Probe *input_path* and return a VideoInfo.

        FIX: avg_frame_rate is now tried BEFORE r_frame_rate.
        DAV files store the container timebase (e.g. 90000/1) in
        r_frame_rate, which is NOT the frame rate. avg_frame_rate
        correctly returns the actual fps (e.g. 20/1 or 25/1).
        A sanity check rejects values outside 1–120 fps range.
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

        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None:
            raise RuntimeError(f"No video stream found in {input_path.name}")

        # ── FIX: avg_frame_rate first, r_frame_rate second, range-validated ──
        fps = self._parse_fps(
            video.get("avg_frame_rate", "0/1"),   # ← correct source for DAV
            video.get("r_frame_rate",   "0/1"),   # ← fallback (often timebase)
        )

        raw_dur  = video.get("duration") or fmt.get("duration", "0")
        duration = float(raw_dur) if raw_dur not in ("", _FFPROBE_NA) else 0.0
        width    = int(video.get("width",  0))
        height   = int(video.get("height", 0))
        codec    = video.get("codec_name", "unknown").lower()

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
        Probe *paths* in parallel. Failed probes are logged and excluded.
        Returned list is in original sort order.
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
        Choose the optimal encoding strategy.

        FIX: DAV files (.dav extension) ALWAYS go through transcode.
        Stream copy cannot apply the setpts PTS-reconstruction filter
        (which requires decoding), so stream copy is never safe for DAV.

        Non-DAV inputs with matching codec/resolution still get stream copy.
        """
        if not infos:
            return ProcessingPlan.transcode_cpu("No input files")

        # ── FIX: force transcode for any DAV input ───────────────────
        has_dav = any(i.path.suffix.lower() == ".dav" for i in infos)
        if has_dav:
            # Explain the plan (GPU vs CPU), never stream copy
            want_gpu = force_gpu or self._prefer_gpu
            if want_gpu and self.detect_nvenc():
                reason = (
                    "DAV input → PTS reconstruction required "
                    "→ GPU transcode (NVENC h264_nvenc, setpts rebuild)"
                )
                logger.info("Plan: %s", reason)
                return ProcessingPlan.transcode_gpu(reason)
            reason = (
                "DAV input → PTS reconstruction required "
                "→ CPU transcode (libx264 -crf 18, setpts rebuild)"
            )
            logger.info("Plan: %s", reason)
            return ProcessingPlan.transcode_cpu(reason)

        # ── Non-DAV: stream copy when safe ────────────────────────────
        codecs      = {i.codec for i in infos}
        resolutions = {i.resolution() for i in infos}
        first_codec = infos[0].codec

        can_copy = (
            len(codecs) == 1
            and len(resolutions) == 1
            and first_codec in _COPY_SAFE_CODECS
        )

        if can_copy:
            reason = (
                f"All {len(infos)} segments share codec={first_codec!r}, "
                f"resolution={infos[0].width}x{infos[0].height} "
                f"→ stream copy (zero re-encode)"
            )
            logger.info("Plan: %s", reason)
            return ProcessingPlan.stream_copy(reason)

        if len(codecs) > 1:
            mismatch = f"mixed codecs {codecs}"
        elif len(resolutions) > 1:
            mismatch = f"mixed resolutions {resolutions}"
        else:
            mismatch = f"codec {first_codec!r} not copy-safe"

        want_gpu = force_gpu or self._prefer_gpu
        if want_gpu and self.detect_nvenc():
            reason = f"{mismatch} → GPU transcode (NVENC h264_nvenc)"
            return ProcessingPlan.transcode_gpu(reason)

        reason = f"{mismatch} → CPU transcode (libx264 -crf 18)"
        return ProcessingPlan.transcode_cpu(reason)

    # ------------------------------------------------------------------
    # Step 1a – Transcode a single segment (FULL DAV FIX)
    # ------------------------------------------------------------------

    def transcode_segment(
        self,
        info:        VideoInfo,
        output_path: Path,
        plan:        ProcessingPlan,
        log:         LogCallback,
    ) -> bool:
        """
        Decode and re-encode *info.path* to a frame-accurate intermediate MP4.

        DAV timestamp reconstruction pipeline
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        -fflags +genpts+igndts+discardcorrupt
            genpts   → regenerate PTS from DTS when PTS is missing/invalid
            igndts   → ignore non-monotonic DTS entirely
            discardcorrupt → skip corrupt packets rather than aborting

        -err_detect ignore_err
            Continue past broken NAL units (common in CCTV recordings).

        -vf "fps=FPS,tpad=...,setpts=N/FRAME_RATE/TB"
            fps=FPS    → enforce constant frame rate first.
                         Duplicates frames to fill gaps; ensures every
                         1/FPS interval has exactly one frame.
            tpad=clone → if the stream is short (<300 s), clone the last
                         frame to reach exactly SEGMENT_DURATION_SEC.
            setpts=N/FRAME_RATE/TB
                       → THE CRITICAL FIX. Discards all original PTS values
                         and rebuilds them purely from the frame index N.
                         Frame 0 → 0 s, Frame 1 → 1/FPS s, Frame N → N/FPS s.
                         Guarantees monotonic, gap-free, watermark-aligned PTS.

        -af "aresample=async=1,asetpts=N/SR/TB"
            aresample  → stretch/compress audio to fill timeline gaps.
            asetpts    → same ordinal-index rebuild for audio samples.

        -t SEGMENT_DURATION_SEC
            Hard-cap output at exactly 300 s. Combined with tpad this
            ensures every intermediate segment is precisely 300.000 s.
        """
        if self.is_cancelled:
            return False

        fps     = info.fps
        fps_str = f"{fps:.6f}".rstrip("0").rstrip(".")
        # Pad: give 1 extra second of headroom so tpad always reaches target
        pad_dur = max(0.0, self._segment_duration - info.duration + 1.0)

        if plan.use_gpu:
            vcodec_args = [
                "-c:v", "h264_nvenc",
                "-rc",  "vbr",
                "-cq",  "18",
                "-preset", _NVENC_PRESET,
                "-b:v", "0",
            ]
            codec_label = "NVENC"
        else:
            vcodec_args = [
                "-c:v", "libx264",
                "-crf",    "18",
                "-preset", "fast",
            ]
            codec_label = "libx264"

        if info.has_audio:
            audio_args = ["-c:a", "aac", "-b:a", "128k", "-ar", str(_AUDIO_SAMPLE_RATE)]
            af = f"aresample=async=1:min_hard_comp=0.100:first_pts=0,asetpts=N/SR/TB"
            audio_filter_args = ["-af", af]
        else:
            audio_args        = ["-an"]
            audio_filter_args = []

        # Video filter chain — the cornerstone of timestamp accuracy
        vf = (
            f"fps={fps_str},"                                           # 1. enforce CFR
            f"tpad=stop_mode=clone:stop_duration={pad_dur:.3f},"       # 2. pad if short
            f"setpts=N/FRAME_RATE/TB"                                   # 3. rebuild PTS
        )

        fps_flag = self._fps_mode_flag()

        cmd = [
            self.ffmpeg_path, "-y",
            # ── Input flags — critical for broken DAV timestamps ──────
            "-fflags",     "+genpts+igndts+discardcorrupt",
            "-err_detect", "ignore_err",
            "-i",          str(info.path),
            # ── Stream selection ──────────────────────────────────────
            "-map", "0:v?",
            "-map", "0:a?",
            # ── Video ────────────────────────────────────────────────
            *vcodec_args,
            "-vf", vf,
            # ── VFR mode ─────────────────────────────────────────────
            fps_flag[0], fps_flag[1],
            # ── Audio ────────────────────────────────────────────────
            *audio_args,
            *audio_filter_args,
            # ── Hard duration cap ─────────────────────────────────────
            "-t", str(self._segment_duration),
            # ── Container ────────────────────────────────────────────
            "-movflags", "+faststart",
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
    # Step 1b – Stream copy (non-DAV only)
    # ------------------------------------------------------------------

    def copy_segment(
        self,
        info:        VideoInfo,
        output_path: Path,
        log:         LogCallback,
    ) -> bool:
        """
        Re-wrap *info.path* into an intermediate MP4 using -c copy.

        Only called for non-DAV inputs where build_plan returned stream_copy.
        DAV files NEVER reach this code path.
        """
        if self.is_cancelled:
            return False

        cmd = [
            self.ffmpeg_path, "-y",
            "-i",    str(info.path),
            "-map",  "0:v?",
            "-map",  "0:a?",
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
        Write the FFmpeg concat demuxer playlist.

        FIX: The duration directive is always SEGMENT_DURATION_SEC (300.0),
        NOT the probe duration. This ensures the concat demuxer offsets each
        segment by exactly 300 s, preventing cumulative drift.

        The caller must pass (path, SEGMENT_DURATION_SEC) as the float value.
        """
        lines: list[str] = []
        for path, duration in segments:
            safe_path = str(path.resolve()).replace("\\", "/")
            lines.append(f"file '{safe_path}'")
            lines.append(f"duration {duration:.6f}")
        list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.debug("Wrote concat list → %s (%d segments)", list_path, len(segments))

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
        Merge intermediate segments via concat demuxer (-f concat -c copy).

        Safe here because all intermediates were produced by us with identical
        codec / FPS / resolution and correct monotonic PTS values.
        No re-encoding occurs.
        """
        if self.is_cancelled:
            return False

        n_segs = sum(
            1 for ln in list_path.read_text().splitlines()
            if ln.startswith("file")
        )

        cmd = [
            self.ffmpeg_path, "-y",
            "-f",    "concat",
            "-safe", "0",
            "-i",    str(list_path),
            "-c",    "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]
        log(f"  Concat demuxer: merging {n_segs} segments (bitstream copy)…", "info")
        logger.info("Concatenating %d segments → %s", n_segs, output_path.name)
        return self._run_ffmpeg(cmd, log, label="concat")

    # ------------------------------------------------------------------
    # Subprocess runner
    # ------------------------------------------------------------------

    def _run_ffmpeg(
        self,
        cmd:   list[str],
        log:   LogCallback,
        *,
        label: str = "",
    ) -> bool:
        """
        Launch *cmd*, stream stderr line-by-line to the log callback.
        Returns True on exit-code 0.
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
                f"[ERROR] FFmpeg exited {proc.returncode} processing {label!r}",
                "error",
            )
            logger.error("FFmpeg exit %d for %r", proc.returncode, label)
            return False

        return True

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_fps(avg_frame_rate: str, r_frame_rate: str) -> float:
        """
        Parse FPS from FFprobe strings.

        FIX: avg_frame_rate is now the PRIMARY source (parameter order changed).
        DAV files report r_frame_rate = "90000/1" (the timebase denominator,
        not the FPS). avg_frame_rate = "20/1" is the actual recording rate.

        Validation: rejects values outside 1–120 fps range, then falls back
        to the secondary source, then defaults to 25.0.
        """
        for raw in (avg_frame_rate, r_frame_rate):
            try:
                frac = Fraction(raw)
                val  = float(frac)
                if 1.0 <= val <= 120.0:
                    return val
            except (ValueError, ZeroDivisionError):
                continue
        logger.warning(
            "Could not parse FPS from avg=%r r=%r — defaulting to 25.0",
            avg_frame_rate, r_frame_rate,
        )
        return 25.0

    def _fps_mode_flag(self) -> tuple[str, str]:
        """
        Return the correct VFR flag for the installed FFmpeg version.
        FFmpeg ≥ 5.1: -fps_mode vfr
        FFmpeg < 5.1: -vsync vfr
        """
        major, minor = self.get_ffmpeg_version()
        if (major, minor) >= (5, 1):
            return ("-fps_mode", "vfr")
        return ("-vsync", "vfr")

    @staticmethod
    def _is_loggable(line: str) -> bool:
        """Filter FFmpeg stderr to lines worth showing in the GUI log."""
        important = (
            "frame=", "Error", "error", "Invalid", "failed",
            "Warning", "warning", "Output #", "Input #",
            "Duration:", "Stream mapping",
        )
        return any(kw in line for kw in important)

    @staticmethod
    def _no_window_flag() -> int:
        """Return CREATE_NO_WINDOW on Windows so FFmpeg never flashes a console."""
        try:
            import subprocess as _sp
            return _sp.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        except AttributeError:
            return 0
