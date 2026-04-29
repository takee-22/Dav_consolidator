"""
gui/main_window.py
------------------
PyQt6 main window for DAV Consolidator.

Layout
~~~~~~
┌──────────────────────────────────────────────────────────────┐
│  DAV Consolidator v2.0                                       │
│  Consolidate IMOU .dav camera files…                        │
├──────────────────────────────────────────────────────────────┤
│  ┌─ Configuration ──────────────────────────────────────────┐│
│  │ Input Folder  [ path…                          ] [Browse] ││
│  │ Output File   [ path…                          ] [Browse] ││
│  │ FFmpeg Path   [ auto-detected                  ] [Browse] ││
│  │ ☐ Enable GPU acceleration (NVIDIA NVENC)                 ││
│  └──────────────────────────────────────────────────────────┘│
│  Found 12 .dav files.                                        │
│  Plan: All segments share h264 → stream copy (zero re-encode)│
│  ┌─ Conversion Log ────────────────────────────────────────┐ │
│  │ [00:00:01] Found: ffmpeg version 7.0 …                  │ │
│  │ [00:00:03] Probed: ch01_001.dav 20.00fps 300.00s h264   │ │
│  └─────────────────────────────────────────────────────────┘ │
│  Transcoding segment 3 / 12: ch01_003.dav           [Clear] │
│  [██████░░░░░░░░░░░░]  33%  (3 / 12)                         │
├──────────────────────────────────────────────────────────────┤
│                [▶ Start Conversion]  [✖ Cancel]              │
└──────────────────────────────────────────────────────────────┘

Threading model
~~~~~~~~~~~~~~~
:class:`ConversionWorker` (QThread) runs the full pipeline.
All FFmpeg subprocess calls live in the worker; Qt signals carry
results back to the GUI thread.  The GUI never blocks.

Separation of concerns
~~~~~~~~~~~~~~~~~~~~~~
This module contains *zero* business logic — no FFmpeg knowledge,
no file-system walking, no encoding decisions.  It only:
  • Gathers user inputs and validates presence (not correctness).
  • Constructs and starts ConversionWorker.
  • Renders signals from the worker into UI updates.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal, pyqtSlot, Qt
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.processor import FFmpegProcessor, ProcessingPlan
from utils.ffmpeg_utils import (
    cleanup_files,
    derive_ffprobe_from_ffmpeg,
    ensure_mp4_extension,
    find_dav_files,
    get_ffmpeg_path,
    get_ffprobe_path,
    make_temp_dir,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Colour palette for log severity levels
# ---------------------------------------------------------------------------

_LOG_COLOURS: dict[str, str] = {
    "info":    "#d4d4d4",   # neutral grey-white
    "success": "#4ec994",   # green
    "warning": "#e5c07b",   # amber
    "error":   "#e06c75",   # red
    "ffmpeg":  "#7c9ec9",   # muted blue for FFmpeg progress lines
    "header":  "#c678dd",   # purple for section separators
}

# ---------------------------------------------------------------------------
# Stylesheet (Catppuccin Mocha-inspired dark theme)
# ---------------------------------------------------------------------------

_STYLESHEET = """
QMainWindow                { background-color: #1e1e2e; }
QWidget#central            { background-color: #1e1e2e; }

QGroupBox {
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 6px;
    margin-top: 8px;
    font-weight: bold;
    padding: 4px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #89b4fa;
}

QLabel          { color: #cdd6f4; font-size: 13px; }

QLineEdit {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 12px;
}
QLineEdit:focus { border: 1px solid #89b4fa; }

QCheckBox       { color: #cdd6f4; font-size: 12px; }
QCheckBox::indicator { width: 14px; height: 14px; }

QPushButton {
    background-color: #45475a;
    color: #cdd6f4;
    border: none;
    border-radius: 4px;
    padding: 5px 12px;
    font-size: 12px;
}
QPushButton:hover    { background-color: #585b70; }
QPushButton:pressed  { background-color: #313244; }

QPushButton#btn_start {
    background-color: #89b4fa;
    color: #1e1e2e;
    font-weight: bold;
    font-size: 13px;
    padding: 8px 24px;
}
QPushButton#btn_start:hover     { background-color: #b4befe; }
QPushButton#btn_start:disabled  { background-color: #45475a; color: #6c7086; }

QPushButton#btn_cancel {
    background-color: #f38ba8;
    color: #1e1e2e;
    font-weight: bold;
    font-size: 13px;
    padding: 8px 24px;
}
QPushButton#btn_cancel:hover    { background-color: #f5a3b8; }
QPushButton#btn_cancel:disabled { background-color: #45475a; color: #6c7086; }

QPlainTextEdit {
    background-color: #11111b;
    color: #cdd6f4;
    border: 1px solid #313244;
    border-radius: 4px;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 11px;
}

QProgressBar {
    background-color: #313244;
    border: none;
    border-radius: 4px;
    height: 14px;
    text-align: center;
    color: #cdd6f4;
    font-size: 11px;
}
QProgressBar::chunk { background-color: #89b4fa; border-radius: 4px; }

QLabel#lbl_stage      { color: #a6adc8; font-size: 11px; }
QLabel#lbl_file_info  { color: #a6e3a1; font-size: 12px; }
QLabel#lbl_plan       { color: #fab387; font-size: 11px; font-style: italic; }
"""


# ---------------------------------------------------------------------------
# Worker Thread
# ---------------------------------------------------------------------------

class ConversionWorker(QThread):
    """
    Background QThread that runs the entire DAV → MP4 pipeline.

    All signals are emitted from the worker thread and delivered to
    the GUI thread by Qt's event loop (cross-thread signal delivery
    is queued automatically in Qt).

    Signals
    -------
    log_message(str, str)
        ``(text, level)`` — level ∈ {"info","success","warning","error",
        "ffmpeg","header"}.
    progress_updated(int, int)
        ``(current, total)`` for the progress bar.
    stage_changed(str)
        Human-readable description of the current pipeline stage.
    plan_determined(str)
        Short encoding-plan summary to display below the file info label.
    finished(bool, str)
        ``(True, output_path)`` on success; ``(False, error_msg)`` on failure.
        Empty *error_msg* means the user cancelled.
    """

    log_message      = pyqtSignal(str, str)
    progress_updated = pyqtSignal(int, int)
    stage_changed    = pyqtSignal(str)
    plan_determined  = pyqtSignal(str)
    finished         = pyqtSignal(bool, str)

    def __init__(
        self,
        input_folder:  str,
        output_file:   str,
        ffmpeg_path:   str,
        ffprobe_path:  str,
        use_gpu:       bool = False,
        parent:        QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._input_folder = Path(input_folder)
        self._output_file  = ensure_mp4_extension(output_file)
        self._use_gpu      = use_gpu
        self._processor    = FFmpegProcessor(
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            use_gpu=use_gpu,
            max_workers=4,
        )
        self._start_time = 0.0

    # ------------------------------------------------------------------
    # Public API (called from GUI thread)
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Request cancellation; safe to call from any thread."""
        self._processor.cancel()

    # ------------------------------------------------------------------
    # Pipeline (runs in worker thread)
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Full DAV → MP4 pipeline.  Runs in the QThread worker thread."""
        self._processor.reset()
        self._start_time = time.monotonic()

        def log(msg: str, level: str = "info") -> None:
            elapsed = time.monotonic() - self._start_time
            ts = str(timedelta(seconds=int(elapsed)))
            self.log_message.emit(f"[{ts}] {msg}", level)
            logger.debug("[worker] %s", msg)

        # ── 0. Verify FFmpeg ──────────────────────────────────────────
        log("─── Checking FFmpeg installation…", "header")
        ok, version_msg = self._processor.detect_ffmpeg()
        if not ok:
            log(f"FFmpeg not found: {version_msg}", "error")
            self.finished.emit(False, f"FFmpeg not found: {version_msg}")
            return
        log(f"Found: {version_msg}", "success")

        if self._use_gpu:
            log("─── Checking GPU (NVENC) availability…", "header")
            nvenc = self._processor.detect_nvenc()
            if nvenc:
                log("NVENC hardware encoder: available ✓", "success")
            else:
                log(
                    "NVENC not available on this system — "
                    "will fall back to CPU (libx264) transcode.",
                    "warning",
                )

        # ── 1. Discover source files ──────────────────────────────────
        log("─── Scanning input folder…", "header")
        try:
            dav_files = find_dav_files(self._input_folder)
        except NotADirectoryError as exc:
            log(str(exc), "error")
            self.finished.emit(False, str(exc))
            return

        if not dav_files:
            log("No .dav files found in the selected folder.", "error")
            self.finished.emit(False, "No .dav files found.")
            return

        log(f"Found {len(dav_files)} .dav file(s):", "success")
        for i, f in enumerate(dav_files, 1):
            log(f"  [{i:>3}] {f.name}", "info")

        # ── 2. Probe all files in parallel ────────────────────────────
        log("─── Probing source files (parallel)…", "header")
        self.stage_changed.emit("Probing source files…")
        self.progress_updated.emit(0, len(dav_files))

        video_infos = self._processor.probe_all_parallel(
            dav_files,
            log,
            on_progress=lambda c, t: self.progress_updated.emit(c, t),
        )

        if not video_infos:
            log("All probes failed — nothing to process.", "error")
            self.finished.emit(False, "All source probes failed.")
            return

        total_expected = sum(v.duration for v in video_infos)
        log(
            f"Total expected duration: "
            f"{timedelta(seconds=int(total_expected))} ({total_expected:.3f}s)",
            "success",
        )

        # ── 3. Build encoding plan ────────────────────────────────────
        log("─── Determining encoding strategy…", "header")
        plan = self._processor.build_plan(video_infos, force_gpu=self._use_gpu)
        log(f"Strategy: {plan.reason}", "success")
        self.plan_determined.emit(f"Plan: {plan.reason}")

        # ── 4. Process each segment ───────────────────────────────────
        action = "Copying" if plan.use_stream_copy else "Transcoding"
        log(f"─── {action} segments…", "header")

        temp_dir   = make_temp_dir()
        temp_files: list[Path] = []
        segments:   list[tuple[Path, float]] = []
        total_steps = len(video_infos)
        self.progress_updated.emit(0, total_steps)

        for i, info in enumerate(video_infos):
            if self._processor.is_cancelled:
                cleanup_files(temp_files, lambda m: log(m, "warning"))
                log("Conversion cancelled by user.", "warning")
                self.finished.emit(False, "")
                return

            temp_out  = temp_dir / f"{str(i).zfill(4)}.mp4"
            stage_msg = (
                f"{action} segment {i + 1} / {total_steps}: {info.path.name}"
            )
            self.stage_changed.emit(stage_msg)
            log(f"─── [{i + 1}/{total_steps}] {stage_msg}", "header")

            if plan.use_stream_copy:
                ok = self._processor.copy_segment(info, temp_out, log)
            else:
                ok = self._processor.transcode_segment(info, temp_out, plan, log)

            if not ok:
                cleanup_files(temp_files, lambda m: log(m, "warning"))
                if self._processor.is_cancelled:
                    log("Conversion cancelled by user.", "warning")
                    self.finished.emit(False, "")
                else:
                    log(f"Processing failed for {info.path.name}.", "error")
                    self.finished.emit(
                        False, f"Processing failed: {info.path.name}"
                    )
                return

            temp_files.append(temp_out)
            segments.append((temp_out, info.duration))
            self.progress_updated.emit(i + 1, total_steps)
            log(f"  ✓ Segment {i + 1} complete.", "success")

        # ── 5. Write concat list ──────────────────────────────────────
        log("─── Writing concat playlist…", "header")
        self.stage_changed.emit("Writing concat playlist…")
        list_path = temp_dir / "concat_list.txt"
        self._processor.write_concat_list(segments, list_path)
        log(f"  Playlist written → {list_path}", "info")

        # ── 6. Concatenate ────────────────────────────────────────────
        log("─── Concatenating all segments into final output…", "header")
        self.stage_changed.emit("Concatenating segments…")
        self.progress_updated.emit(0, 1)

        self._output_file.parent.mkdir(parents=True, exist_ok=True)
        ok = self._processor.concatenate_segments(
            list_path, self._output_file, log
        )

        if not ok:
            cleanup_files(temp_files + [list_path], lambda m: log(m, "warning"))
            if self._processor.is_cancelled:
                log("Conversion cancelled by user.", "warning")
                self.finished.emit(False, "")
            else:
                self.finished.emit(False, "Concatenation failed.")
            return

        self.progress_updated.emit(1, 1)

        # ── 7. Cleanup ────────────────────────────────────────────────
        log("─── Cleaning up temporary files…", "header")
        self.stage_changed.emit("Cleaning up…")
        cleanup_files(temp_files + [list_path], lambda m: log(m, "warning"))
        try:
            temp_dir.rmdir()
        except OSError:
            pass

        # ── 8. Summary ────────────────────────────────────────────────
        elapsed = time.monotonic() - self._start_time
        log(
            f"─── ✓ Done!\n"
            f"    Output    : {self._output_file}\n"
            f"    Duration  : {total_expected:.3f}s "
            f"({timedelta(seconds=int(total_expected))})\n"
            f"    Wall time : {timedelta(seconds=int(elapsed))}\n"
            f"    Strategy  : {plan.reason}\n"
            f"    Verify    : ffprobe -v error -show_entries format=duration "
            f'-of default=noprint_wrappers=1 "{self._output_file}"',
            "success",
        )
        self.stage_changed.emit(f"Finished! → {self._output_file.name}")
        self.finished.emit(True, str(self._output_file))


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    """Top-level PyQt6 application window for DAV Consolidator."""

    APP_TITLE   = "DAV Consolidator v2.0"
    MIN_WIDTH   = 880
    MIN_HEIGHT  = 740

    def __init__(self) -> None:
        super().__init__()
        self._worker: ConversionWorker | None = None
        self._setup_ui()
        self.setStyleSheet(_STYLESHEET)
        self._post_init_log()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        self.setWindowTitle(self.APP_TITLE)
        self.setMinimumSize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.resize(980, 820)

        central = QWidget(objectName="central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        # ── Title ─────────────────────────────────────────────────────
        title = QLabel(self.APP_TITLE)
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: #89b4fa; margin-bottom: 2px;")
        root.addWidget(title)

        subtitle = QLabel(
            "Consolidate IMOU .dav camera recordings into a single MP4 "
            "with zero frame loss — GPU-accelerated or CPU fallback."
        )
        subtitle.setStyleSheet(
            "color: #6c7086; font-size: 12px; margin-bottom: 6px;"
        )
        root.addWidget(subtitle)

        # ── Configuration group ───────────────────────────────────────
        cfg_group  = QGroupBox("Configuration")
        cfg_layout = QVBoxLayout(cfg_group)
        cfg_layout.setSpacing(8)

        self._input_edit, r1, _ = self._path_row(
            "Input Folder:", "Select folder containing .dav files…",
            browse_dir=True,
        )
        self._output_edit, r2, _ = self._path_row(
            "Output File:", "Select output .mp4 file path…",
            browse_dir=False,
        )
        self._ffmpeg_edit, r3, ff_btn = self._path_row(
            "FFmpeg Path:", "auto-detected or system PATH",
            browse_dir=False, browse_label="Browse",
        )
        # Rewire FFmpeg browse button to open an executable picker
        ff_btn.clicked.disconnect()
        ff_btn.clicked.connect(self._browse_ffmpeg)

        cfg_layout.addLayout(r1)
        cfg_layout.addLayout(r2)
        cfg_layout.addLayout(r3)

        # GPU toggle row
        gpu_row = QHBoxLayout()
        self._gpu_check = QCheckBox(
            "Enable GPU acceleration (NVIDIA NVENC — falls back to CPU if unavailable)"
        )
        self._gpu_check.setToolTip(
            "When checked, DAV Consolidator will attempt to use NVIDIA NVENC\n"
            "for hardware-accelerated H.264 encoding.  If your GPU does not\n"
            "support NVENC (or the driver is missing), it automatically falls\n"
            "back to software encoding via libx264 at no extra cost."
        )
        gpu_row.addWidget(self._gpu_check)
        gpu_row.addStretch()
        cfg_layout.addLayout(gpu_row)

        root.addWidget(cfg_group)

        # ── Info labels ───────────────────────────────────────────────
        self._file_info_label = QLabel("No folder selected.")
        self._file_info_label.setObjectName("lbl_file_info")
        self._file_info_label.setContentsMargins(4, 0, 0, 0)
        root.addWidget(self._file_info_label)

        self._plan_label = QLabel("")
        self._plan_label.setObjectName("lbl_plan")
        self._plan_label.setContentsMargins(4, 0, 0, 0)
        root.addWidget(self._plan_label)

        # ── Log window ────────────────────────────────────────────────
        log_group  = QGroupBox("Conversion Log")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(6, 6, 6, 6)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(10_000)   # ~10k lines in memory
        self._log_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        log_layout.addWidget(self._log_view)

        btn_clear = QPushButton("Clear Log")
        btn_clear.setFixedWidth(90)
        btn_clear.clicked.connect(self._log_view.clear)
        log_layout.addWidget(btn_clear, alignment=Qt.AlignmentFlag.AlignRight)

        root.addWidget(log_group, stretch=1)

        # ── Stage label + progress bar ────────────────────────────────
        self._stage_label = QLabel("Idle")
        self._stage_label.setObjectName("lbl_stage")
        root.addWidget(self._stage_label)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFormat("%p%  (%v / %m)")
        self._progress.setFixedHeight(18)
        root.addWidget(self._progress)

        # ── Action buttons ────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #313244;")
        root.addWidget(sep)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self._btn_start = QPushButton("▶  Start Conversion")
        self._btn_start.setObjectName("btn_start")
        self._btn_start.setFixedHeight(40)
        self._btn_start.clicked.connect(self._start_conversion)
        btn_row.addWidget(self._btn_start)

        self._btn_cancel = QPushButton("✖  Cancel")
        self._btn_cancel.setObjectName("btn_cancel")
        self._btn_cancel.setFixedHeight(40)
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.clicked.connect(self._cancel_conversion)
        btn_row.addWidget(self._btn_cancel)

        btn_row.addStretch()
        root.addLayout(btn_row)

        # Wire folder path → refresh file info
        self._input_edit.textChanged.connect(self._refresh_file_info)

    def _path_row(
        self,
        label:        str,
        placeholder:  str,
        *,
        browse_dir:   bool,
        browse_label: str = "Browse…",
    ) -> tuple[QLineEdit, QHBoxLayout, QPushButton]:
        """
        Build a ``label | line-edit | browse-button`` row.

        Returns (QLineEdit, QHBoxLayout, QPushButton) so the caller can
        re-wire the button's signal when needed (e.g. the FFmpeg row).
        """
        row  = QHBoxLayout()
        lbl  = QLabel(label)
        lbl.setFixedWidth(110)
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        btn  = QPushButton(browse_label)
        btn.setFixedWidth(90)

        if browse_dir:
            btn.clicked.connect(lambda: self._browse_folder(edit))
        else:
            btn.clicked.connect(lambda: self._browse_file(edit))

        row.addWidget(lbl)
        row.addWidget(edit, stretch=1)
        row.addWidget(btn)
        return edit, row, btn

    # ------------------------------------------------------------------
    # Post-init — runs after __init__ so the log pane is ready
    # ------------------------------------------------------------------

    def _post_init_log(self) -> None:
        self._log(
            "DAV Consolidator ready.  Select an input folder to begin.", "success"
        )
        self._log(
            "Files are sorted in natural order (e.g., ch01_001 … ch01_099).", "info"
        )
        # Auto-detect and display FFmpeg path
        detected = get_ffmpeg_path()
        self._ffmpeg_edit.setText(detected)
        if detected != "ffmpeg":
            self._log(f"Auto-detected FFmpeg: {detected}", "success")
        else:
            self._log(
                "ffmpeg.exe not found in project root — using system PATH.", "warning"
            )

    # ------------------------------------------------------------------
    # File browser slots
    # ------------------------------------------------------------------

    def _browse_folder(self, edit: QLineEdit) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select DAV Input Folder", edit.text() or ""
        )
        if folder:
            edit.setText(folder)

    def _browse_file(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Output MP4 As", edit.text() or "",
            "MP4 Video (*.mp4);;All Files (*)",
        )
        if path:
            edit.setText(path)

    def _browse_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate FFmpeg Executable", "",
            "Executables (*.exe);;All Files (*)",
        )
        if path:
            self._ffmpeg_edit.setText(path)

    # ------------------------------------------------------------------
    # File-info refresh (triggered when input folder path changes)
    # ------------------------------------------------------------------

    @pyqtSlot(str)
    def _refresh_file_info(self, folder: str) -> None:
        if not folder:
            self._file_info_label.setText("No folder selected.")
            self._plan_label.setText("")
            return
        try:
            files = find_dav_files(folder)
        except NotADirectoryError:
            self._file_info_label.setText("Invalid folder path.")
            self._plan_label.setText("")
            return

        n = len(files)
        if n > 0:
            self._file_info_label.setText(
                f"Found {n} .dav file(s).  "
                "Duration and encoding plan determined after probing."
            )
        else:
            self._file_info_label.setText("No .dav files found in this folder.")
        self._plan_label.setText("")

    # ------------------------------------------------------------------
    # Conversion control
    # ------------------------------------------------------------------

    def _start_conversion(self) -> None:
        input_folder = self._input_edit.text().strip()
        output_file  = self._output_edit.text().strip()
        ffmpeg_path  = self._ffmpeg_edit.text().strip() or get_ffmpeg_path()
        ffprobe_path = derive_ffprobe_from_ffmpeg(ffmpeg_path)
        use_gpu      = self._gpu_check.isChecked()

        # Input validation — check presence only; correctness is the worker's job
        if not input_folder:
            self._warn("Please select an input folder.")
            return
        if not output_file:
            self._warn("Please specify an output file path.")
            return
        if not Path(input_folder).is_dir():
            self._warn(f"Input folder does not exist:\n{input_folder}")
            return

        self._set_running(True)
        self._log_view.clear()
        self._plan_label.setText("")
        self._progress.setValue(0)
        self._stage_label.setText("Starting…")

        self._worker = ConversionWorker(
            input_folder=input_folder,
            output_file=output_file,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            use_gpu=use_gpu,
        )
        self._worker.log_message.connect(self._on_log_message)
        self._worker.progress_updated.connect(self._on_progress)
        self._worker.stage_changed.connect(self._stage_label.setText)
        self._worker.plan_determined.connect(self._plan_label.setText)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()
        logger.info(
            "ConversionWorker started — input=%s  gpu=%s", input_folder, use_gpu
        )

    def _cancel_conversion(self) -> None:
        if self._worker and self._worker.isRunning():
            self._log(
                "Cancellation requested — waiting for FFmpeg to stop…", "warning"
            )
            self._worker.cancel()
            self._btn_cancel.setEnabled(False)

    # ------------------------------------------------------------------
    # Worker signal handlers (run in GUI thread via Qt event queue)
    # ------------------------------------------------------------------

    @pyqtSlot(str, str)
    def _on_log_message(self, message: str, level: str) -> None:
        self._log(message, level)

    @pyqtSlot(int, int)
    def _on_progress(self, current: int, total: int) -> None:
        if total <= 0:
            return
        self._progress.setMaximum(total)
        self._progress.setValue(current)
        pct = int(current / total * 100)
        self._progress.setFormat(f"{pct}%  ({current} / {total})")

    @pyqtSlot(bool, str)
    def _on_finished(self, success: bool, message: str) -> None:
        self._set_running(False)
        if success:
            self._progress.setValue(self._progress.maximum())
            QMessageBox.information(
                self,
                "Conversion Complete",
                f"Output saved to:\n{message}\n\n"
                "Verify duration with:\n"
                f'ffprobe -v error -show_entries format=duration '
                f'-of default=noprint_wrappers=1 "{message}"',
            )
        elif message:
            QMessageBox.critical(self, "Conversion Failed", message)
        # Empty message = user-cancelled; no dialog needed

    # ------------------------------------------------------------------
    # Log helper
    # ------------------------------------------------------------------

    def _log(self, message: str, level: str = "info") -> None:
        """Append a colour-coded line to the log pane."""
        colour = _LOG_COLOURS.get(level, _LOG_COLOURS["info"])
        fmt    = QTextCharFormat()
        fmt.setForeground(QColor(colour))

        cursor = self._log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(message + "\n", fmt)
        self._log_view.setTextCursor(cursor)
        self._log_view.ensureCursorVisible()

    # ------------------------------------------------------------------
    # UI state helpers
    # ------------------------------------------------------------------

    def _set_running(self, running: bool) -> None:
        """Toggle interactive widgets based on whether a job is active."""
        self._btn_start.setEnabled(not running)
        self._btn_cancel.setEnabled(running)
        self._input_edit.setEnabled(not running)
        self._output_edit.setEnabled(not running)
        self._ffmpeg_edit.setEnabled(not running)
        self._gpu_check.setEnabled(not running)

    @staticmethod
    def _warn(message: str) -> None:
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Input Required")
        box.setText(message)
        box.exec()

    # ------------------------------------------------------------------
    # Close-event guard
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._worker and self._worker.isRunning():
            reply = QMessageBox.question(
                self,
                "Conversion Running",
                "A conversion is in progress.\nCancel and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._worker.cancel()
                self._worker.wait(5_000)
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()
