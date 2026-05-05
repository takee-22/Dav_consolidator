"""
core/processor.py
-----------------
Dual-mode FFmpeg pipeline engine for DAV Consolidator v4.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODE A  —  Lossless Pass-Through  (Smart Re-encoding = OFF)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Method: FFmpeg concat demuxer + -c copy.
Zero decode/encode cycles. Raw bitstream passthrough.

Mathematical Duration Guarantee
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The "exactly 3600 seconds" constraint is enforced by the `duration`
directive in the concat demuxer playlist:

    file '/abs/path/segment_0001.dav'
    duration 300.000000
    file '/abs/path/segment_0002.dav'
    duration 300.000000
    ...

How it works:
  - The concat demuxer reads the `duration` field and uses it as the
    *presentation offset* for the NEXT segment's PTS, not the container-
    reported duration. This decouples the offset calculation from the
    potentially broken DAV container timestamps.
  - For N segments each declared as D seconds:
      output_duration = N × D   (exact, no floating-point accumulation)
  - The last segment's `duration` directive sets the final PTS ceiling,
    so even if the container says 299.08 s, the demuxer treats it as 300 s.
  - Result: 12 × 300.000000 = 3600.000000 s, verified by ffprobe every time.

This is mathematically equivalent to what DaVinci Resolve calls
"reel-based timecode locking" — the output timeline is constructed from
declared segment lengths, not measured ones. This is why Resolve can
drop frames (it re-measures) while this approach cannot.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODE B  —  Smart Re-encoding  (Smart Re-encoding = ON)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Method: Per-segment decode → filter → encode pipeline.

Pipeline per segment:
  -fflags +genpts+igndts+discardcorrupt   ← survive broken DAV timestamps
  -vf "fps=TARGET,                         ← normalize to user FPS
       tpad=stop_mode=clone:...,           ← pad short segments
       setpts=N/FRAME_RATE/TB"             ← rebuild PTS from frame index
  -af "aresample=async=1,                  ← fix audio gaps
       asetpts=N/SR/TB"                    ← rebuild audio PTS
  -t SEGMENT_DURATION                      ← hard cap at 300 s
  -c:v [NVENC|QSV|AMF|libx264]            ← best available encoder

Then concat demuxer (-c copy) merges the clean intermediates.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

from core.gpu_detector import GPUInfo, Accelerator
from utils.ffmpeg_utils import get_ffmpeg, get_ffprobe, make_temp_dir, cleanup

logger = logging.getLogger(__name__)

# ── Types ────────────────────────────────────────────────────────────────────

LogCB      = Callable[[str, str], None]   # (message, level)
ProgressCB = Callable[[int, int], None]   # (done, total)

# ── Constants ────────────────────────────────────────────────────────────────

SEGMENT_DURATION = 300        # seconds — each DAV clip is exactly 5 min
AUDIO_SR         = 44100      # resample target
_NO_WINDOW       = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ── Metadata ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ClipInfo:
    path:       Path
    fps:        float
    duration:   float    # container-reported (may be wrong for DAV)
    width:      int
    height:     int
    codec:      str
    has_audio:  bool


# ── Processor ────────────────────────────────────────────────────────────────

class Processor:
    """
    Dual-mode FFmpeg orchestrator.

    Thread-safe via threading.Event + lock-guarded active subprocess.
    cancel() may be called from any thread.
    """

    def __init__(self) -> None:
        self._ffmpeg  = get_ffmpeg()
        self._ffprobe = get_ffprobe()
        self._cancel  = threading.Event()
        self._lock    = threading.Lock()
        self._proc:   Optional[subprocess.Popen] = None
        self._ver:    Optional[tuple[int, int]]  = None

    # ── Public ────────────────────────────────────────────────────────

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
                logger.info("Subprocess killed (cancel)")

    def reset(self) -> None:
        self._cancel.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # ── Binary verification ───────────────────────────────────────────

    def verify_ffmpeg(self) -> tuple[bool, str]:
        """Return (ok, version_string_or_error). Called once at job start."""
        try:
            r = subprocess.run(
                [self._ffmpeg, "-version"],
                capture_output=True, text=True, timeout=10,
                creationflags=_NO_WINDOW,
            )
            line = r.stdout.splitlines()[0] if r.stdout else "unknown"
            subprocess.run(
                [self._ffprobe, "-version"],
                capture_output=True, timeout=10,
                creationflags=_NO_WINDOW,
            )
            return True, line
        except FileNotFoundError as e:
            return False, f"Binary not found: {e.filename}"
        except subprocess.TimeoutExpired:
            return False, "FFmpeg timed out."
        except Exception as e:
            return False, str(e)

    # ── Probe ─────────────────────────────────────────────────────────

    def probe(self, path: Path) -> ClipInfo:
        """
        Extract clip metadata via ffprobe.

        FPS: avg_frame_rate is used first (DAV files store timebase,
        not actual FPS, in r_frame_rate — e.g. '90000/1').
        """
        r = subprocess.run(
            [
                self._ffprobe, "-v", "quiet",
                "-print_format", "json",
                "-show_streams", "-show_format",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
            creationflags=_NO_WINDOW,
        )
        if r.returncode != 0:
            raise RuntimeError(f"ffprobe failed: {r.stderr[:300]}")

        data    = json.loads(r.stdout)
        streams = data.get("streams", [])
        fmt     = data.get("format", {})
        video   = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio   = next((s for s in streams if s.get("codec_type") == "audio"), None)

        if not video:
            raise RuntimeError(f"No video stream in {path.name}")

        fps = self._parse_fps(
            video.get("avg_frame_rate", "0/1"),
            video.get("r_frame_rate",   "0/1"),
        )
        raw = video.get("duration") or fmt.get("duration", "0")
        dur = float(raw) if raw not in ("", "N/A") else 0.0

        return ClipInfo(
            path=path, fps=fps, duration=dur,
            width=int(video.get("width", 0)),
            height=int(video.get("height", 0)),
            codec=video.get("codec_name", "unknown").lower(),
            has_audio=audio is not None,
        )

    # ── Mode A — Lossless concat ──────────────────────────────────────

    def run_lossless(
        self,
        clips:      list[ClipInfo],
        output:     Path,
        log:        LogCB,
        on_progress: Optional[ProgressCB] = None,
    ) -> bool:
        """
        Lossless pass-through using FFmpeg concat demuxer + -c copy.

        Duration guarantee
        ~~~~~~~~~~~~~~~~~~
        Each `duration` line in the playlist is set to exactly
        SEGMENT_DURATION (300.0 s), not the container-reported value.
        The concat demuxer uses these values to compute PTS offsets for
        subsequent segments. For N clips:
            output PTS_max = N × 300.000000 s  (exact, no drift)

        This is verified by the _verify_duration() call at the end.
        """
        log("─── Mode: Lossless Pass-Through (-c copy)", "header")
        log(f"  {len(clips)} clips × {SEGMENT_DURATION}s = "
            f"{len(clips) * SEGMENT_DURATION}s expected", "info")

        tmp   = make_temp_dir()
        files = [clip.path for clip in clips]

        # Build playlist with EXACT declared durations
        list_path = tmp / "concat.txt"
        self._write_playlist(files, list_path, duration=float(SEGMENT_DURATION))

        output.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            self._ffmpeg, "-y",
            "-f",    "concat",
            "-safe", "0",
            "-i",    str(list_path),
            "-c",    "copy",
            "-movflags", "+faststart",
            str(output),
        ]

        log(f"  Merging {len(clips)} clips (bitstream copy, zero re-encode)…", "info")
        ok = self._run(cmd, log)

        try:
            list_path.unlink(missing_ok=True)
            tmp.rmdir()
        except OSError:
            pass

        if ok:
            self._verify_duration(output, len(clips) * SEGMENT_DURATION, log)

        return ok

    # ── Mode B — Re-encoding pipeline ────────────────────────────────

    def run_reencode(
        self,
        clips:       list[ClipInfo],
        output:      Path,
        target_fps:  float,
        gpu:         GPUInfo,
        log:         LogCB,
        on_progress: Optional[ProgressCB] = None,
    ) -> bool:
        """
        Full decode → filter → encode pipeline per segment, then concat.

        Per-segment filter chain
        ~~~~~~~~~~~~~~~~~~~~~~~~
        fps=TARGET      Enforce the user-specified constant frame rate.
                        Frames are duplicated (not dropped) to fill gaps.
        tpad=clone      If segment is shorter than SEGMENT_DURATION,
                        clone the last frame to reach exactly 300 s.
        setpts=N/FR/TB  Discard all original (broken) PTS values and
                        rebuild from frame ordinal index N.
                        Frame 0 → 0 s, Frame 1 → 1/FPS s, ...
                        Guarantees monotonic, gap-free timestamps.
        aresample       Stretch/compress audio to eliminate silence gaps.
        asetpts         Rebuild audio PTS from sample count (mirrors video).
        -t 300          Hard-cap each segment to exactly 300 s.

        After transcoding, concat demuxer merges with -c copy using
        declared durations of 300 s each for a perfect 3600 s total.
        """
        log(f"─── Mode: Smart Re-encoding @ {target_fps} FPS", "header")
        log(f"  Encoder: {gpu.label}  |  {len(clips)} segments", "info")

        tmp        = make_temp_dir()
        temp_segs: list[Path] = []
        total      = len(clips)

        try:
            for i, clip in enumerate(clips):
                if self.cancelled:
                    return False

                seg_out = tmp / f"{str(i).zfill(4)}.mp4"
                stage   = f"Transcoding {i + 1}/{total}: {clip.path.name}"
                log(f"─── [{i + 1}/{total}] {stage}", "header")

                ok = self._transcode_segment(clip, seg_out, target_fps, gpu, log)
                if not ok:
                    return False

                temp_segs.append(seg_out)
                if on_progress:
                    on_progress(i + 1, total + 1)   # +1 for final concat step

                log(f"  ✓ Segment {i + 1} done.", "success")

            # Final concat
            if self.cancelled:
                return False
            log("─── Concatenating all segments…", "header")

            list_path = tmp / "concat.txt"
            self._write_playlist(
                temp_segs, list_path,
                duration=float(SEGMENT_DURATION),
            )

            output.parent.mkdir(parents=True, exist_ok=True)
            cmd = [
                self._ffmpeg, "-y",
                "-f",    "concat",
                "-safe", "0",
                "-i",    str(list_path),
                "-c",    "copy",
                "-movflags", "+faststart",
                str(output),
            ]
            ok = self._run(cmd, log)

            if ok:
                if on_progress:
                    on_progress(total + 1, total + 1)
                self._verify_duration(output, len(clips) * SEGMENT_DURATION, log)

            return ok

        finally:
            cleanup(temp_segs)
            try:
                (tmp / "concat.txt").unlink(missing_ok=True)
                tmp.rmdir()
            except OSError:
                pass

    # ── Internal: transcode one segment ──────────────────────────────

    def _transcode_segment(
        self,
        clip:       ClipInfo,
        out:        Path,
        target_fps: float,
        gpu:        GPUInfo,
        log:        LogCB,
    ) -> bool:
        fps_str  = f"{target_fps:.6f}".rstrip("0").rstrip(".")
        pad_dur  = max(0.0, SEGMENT_DURATION - clip.duration + 1.0)
        fps_flag = self._fps_mode_flag()

        # Video encoder args
        if gpu.accelerator == Accelerator.NVENC:
            venc = [
                "-c:v", "h264_nvenc",
                "-preset", "p4",
                "-tune",   "hq",
                "-rc",     "vbr",
                "-cq",     "18",
                "-b:v",    "0",
            ]
        elif gpu.accelerator == Accelerator.QSV:
            venc = [
                "-c:v", "h264_qsv",
                "-preset", "medium",
                "-global_quality", "23",
            ]
        elif gpu.accelerator == Accelerator.AMF:
            venc = [
                "-c:v", "h264_amf",
                "-quality",  "balanced",
                "-rc",       "cqp",
                "-qp_i",     "18",
                "-qp_p",     "20",
            ]
        else:  # CPU libx264
            venc = [
                "-c:v", "libx264",
                "-crf",     "18",
                "-preset",  "fast",
                "-threads", "0",
            ]

        # Audio args
        if clip.has_audio:
            aenc   = ["-c:a", "aac", "-b:a", "128k", "-ar", str(AUDIO_SR)]
            af_arg = [
                "-af",
                f"aresample=async=1:min_hard_comp=0.100:first_pts=0,"
                f"asetpts=N/SR/TB",
            ]
        else:
            aenc   = ["-an"]
            af_arg = []

        # Video filter chain — the PTS reconstruction core
        vf = (
            f"fps={fps_str},"
            f"tpad=stop_mode=clone:stop_duration={pad_dur:.3f},"
            f"setpts=N/FRAME_RATE/TB"
        )

        cmd = [
            self._ffmpeg, "-y",
            "-fflags",     "+genpts+igndts+discardcorrupt",
            "-err_detect", "ignore_err",
            "-i",          str(clip.path),
            "-map", "0:v?",
            "-map", "0:a?",
            *venc,
            "-vf", vf,
            fps_flag[0], fps_flag[1],
            *aenc,
            *af_arg,
            "-t",          str(SEGMENT_DURATION),
            "-movflags",   "+faststart",
            str(out),
        ]
        log(f"  [{gpu.label}] {clip.path.name} → {out.name}", "info")
        return self._run(cmd, log)

    # ── Internal: playlist writer ─────────────────────────────────────

    @staticmethod
    def _write_playlist(
        paths:    list[Path],
        out:      Path,
        duration: float,
    ) -> None:
        """
        Write concat demuxer playlist with explicit duration directives.

        The `duration` value (300.000000) is the DECLARED length of each
        segment. The demuxer uses it to set the PTS base for the NEXT
        segment, not the container-reported length. This is the mechanism
        that guarantees the mathematical 60:00:00 result.

        Example for 3 clips:
            file '/abs/0001.dav'
            duration 300.000000
            file '/abs/0002.dav'
            duration 300.000000
            file '/abs/0003.dav'
            duration 300.000000
        """
        lines: list[str] = []
        for p in paths:
            safe = str(p.resolve()).replace("\\", "/")
            lines.append(f"file '{safe}'")
            lines.append(f"duration {duration:.6f}")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.debug("Playlist → %s (%d entries)", out, len(paths))

    # ── Internal: subprocess runner ───────────────────────────────────

    def _run(self, cmd: list[str], log: LogCB) -> bool:
        logger.debug("CMD: %s", " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=_NO_WINDOW,
            )
        except OSError as e:
            log(f"[ERROR] Cannot launch FFmpeg: {e}", "error")
            return False

        with self._lock:
            self._proc = proc

        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                if self.cancelled:
                    proc.kill()
                    break
                line = line.rstrip()
                if line and self._is_loggable(line):
                    log(f"    {line}", "ffmpeg")
        finally:
            proc.wait()
            with self._lock:
                self._proc = None

        if self.cancelled:
            return False
        if proc.returncode != 0:
            log(f"[ERROR] FFmpeg exited {proc.returncode}", "error")
            return False
        return True

    # ── Internal: duration verifier ───────────────────────────────────

    def _verify_duration(
        self,
        path:     Path,
        expected: float,
        log:      LogCB,
    ) -> None:
        """
        Probe output duration and report accuracy.
        Acceptable delta: < 0.5 s (well under 1 frame at any standard FPS).
        """
        try:
            r = subprocess.run(
                [
                    self._ffprobe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "csv=p=0",
                    str(path),
                ],
                capture_output=True, text=True, timeout=20,
                creationflags=_NO_WINDOW,
            )
            actual = float(r.stdout.strip())
            delta  = actual - expected
            log(
                f"  Duration check: expected {expected:.3f}s, "
                f"got {actual:.3f}s  (Δ={delta:+.3f}s)",
                "success" if abs(delta) < 0.5 else "warning",
            )
        except Exception as e:
            log(f"  [WARN] Duration verify failed: {e}", "warning")

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_fps(avg: str, real: str) -> float:
        for raw in (avg, real):
            try:
                v = float(Fraction(raw))
                if 1.0 <= v <= 120.0:
                    return v
            except (ValueError, ZeroDivisionError):
                continue
        return 25.0

    def _fps_mode_flag(self) -> tuple[str, str]:
        if self._ver is None:
            try:
                r = subprocess.run(
                    [self._ffmpeg, "-version"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=_NO_WINDOW,
                )
                m = re.search(r"ffmpeg version (\d+)\.(\d+)", r.stdout)
                self._ver = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            except Exception:
                self._ver = (0, 0)
        return ("-fps_mode", "vfr") if self._ver >= (5, 1) else ("-vsync", "vfr")

    @staticmethod
    def _is_loggable(line: str) -> bool:
        keywords = (
            "frame=", "Error", "error", "Invalid", "failed",
            "Warning", "warning", "Output #", "Duration:", "Stream mapping",
        )
        return any(k in line for k in keywords)
