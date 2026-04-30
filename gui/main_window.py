"""
gui/main_window.py
------------------
PyQt6 main window for DAV Consolidator v3.

Fixes applied in this version
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
BUG 5 – Wrong expected duration displayed/used:
    Old code used sum(v.duration for v in video_infos) — the broken probe
    values from DAV files. Fixed: SEGMENT_DURATION_SEC * len(infos) gives
    the correct expected total (e.g. 12 × 300 = 3600 s exactly).

BUG 6 – Wrong duration in concat list:
    Old code passed info.duration (probe value ≈ 299.08 s) to write_concat_list.
    Fixed: always pass float(SEGMENT_DURATION_SEC) = 300.0 so the concat
    demuxer offsets each segment by exactly 300 s, eliminating drift.

New features
~~~~~~~~~~~~
• Dark ↔ Light theme toggle (🌙 / ☀) in the header
• Auto output filename derived from DAV naming convention
  (first file start time → last file end time → e.g. 08.00.00-09.00.00.mp4)
• GPU status badge that shows detected / not detected
• Elapsed time counter in the status bar
• Drag-and-drop folder support on the input field
• Improved colour-coded log with horizontal rule dividers
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from pathlib import Path

from PyQt6.QtCore import (
    QThread, QTimer, Qt,
    pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import (
    QColor, QFont, QTextCharFormat, QTextCursor,
    QDragEnterEvent, QDropEvent,
)
from PyQt6.QtWidgets import (
    QApplication,
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

from core.processor import FFmpegProcessor, ProcessingPlan, SEGMENT_DURATION_SEC
from utils.ffmpeg_utils import (
    build_output_filename,
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
# Theme definitions
# ---------------------------------------------------------------------------

# ── Dark theme (Catppuccin Mocha) ──────────────────────────────────────────
_DARK = {
    "bg":          "#1e1e2e",
    "bg2":         "#181825",
    "surface":     "#313244",
    "surface2":    "#45475a",
    "overlay":     "#6c7086",
    "text":        "#cdd6f4",
    "subtext":     "#a6adc8",
    "blue":        "#89b4fa",
    "blue2":       "#b4befe",
    "green":       "#a6e3a1",
    "red":         "#f38ba8",
    "yellow":      "#f9e2af",
    "peach":       "#fab387",
    "mauve":       "#cba6f7",
    "log_bg":      "#11111b",
    "log_border":  "#313244",
    "sep":         "#313244",
}

# ── Light theme (Catppuccin Latte) ─────────────────────────────────────────
_LIGHT = {
    "bg":          "#eff1f5",
    "bg2":         "#e6e9ef",
    "surface":     "#dce0e8",
    "surface2":    "#bcc0cc",
    "overlay":     "#8c8fa1",
    "text":        "#4c4f69",
    "subtext":     "#6c6f85",
    "blue":        "#1e66f5",
    "blue2":       "#7287fd",
    "green":       "#40a02b",
    "red":         "#d20f39",
    "yellow":      "#df8e1d",
    "peach":       "#fe640b",
    "mauve":       "#8839ef",
    "log_bg":      "#dce0e8",
    "log_border":  "#bcc0cc",
    "sep":         "#bcc0cc",
}

# Log text colours per theme
_LOG_COLOURS_DARK: dict[str, str] = {
    "info":    "#cdd6f4",
    "success": "#a6e3a1",
    "warning": "#f9e2af",
    "error":   "#f38ba8",
    "ffmpeg":  "#89b4fa",
    "header":  "#cba6f7",
}
_LOG_COLOURS_LIGHT: dict[str, str] = {
    "info":    "#4c4f69",
    "success": "#40a02b",
    "warning": "#df8e1d",
    "error":   "#d20f39",
    "ffmpeg":  "#1e66f5",
    "header":  "#8839ef",
}


def _build_stylesheet(t: dict) -> str:
    return f"""
/* ── Base ── */
QMainWindow, QDialog  {{ background-color: {t["bg"]}; }}
QWidget#central        {{ background-color: {t["bg"]}; }}
QWidget                {{ color: {t["text"]}; }}

/* ── Header bar ── */
QWidget#header         {{ background-color: {t["bg2"]}; border-bottom: 2px solid {t["blue"]}; }}

/* ── Group boxes ── */
QGroupBox {{
    color: {t["text"]};
    border: 1px solid {t["surface2"]};
    border-radius: 8px;
    margin-top: 10px;
    font-weight: bold;
    font-size: 12px;
    padding: 6px 4px 4px 4px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {t["blue"]};
}}

/* ── Labels ── */
QLabel              {{ color: {t["text"]}; font-size: 12px; }}
QLabel#lbl_title    {{ color: {t["blue"]}; font-size: 17px; font-weight: bold; }}
QLabel#lbl_subtitle {{ color: {t["overlay"]}; font-size: 11px; }}
QLabel#lbl_stage    {{ color: {t["subtext"]}; font-size: 11px; }}
QLabel#lbl_file_info{{ color: {t["green"]}; font-size: 12px; font-weight: bold; }}
QLabel#lbl_plan     {{ color: {t["peach"]}; font-size: 11px; font-style: italic; }}
QLabel#lbl_elapsed  {{ color: {t["overlay"]}; font-size: 11px; }}
QLabel#badge_gpu_ok {{ color: {t["green"]}; font-size: 11px; font-weight: bold; }}
QLabel#badge_gpu_no {{ color: {t["overlay"]}; font-size: 11px; }}

/* ── Inputs ── */
QLineEdit {{
    background-color: {t["surface"]};
    color: {t["text"]};
    border: 1px solid {t["surface2"]};
    border-radius: 5px;
    padding: 5px 10px;
    font-size: 12px;
    selection-background-color: {t["blue"]};
}}
QLineEdit:focus   {{ border: 1px solid {t["blue"]}; }}
QLineEdit:disabled{{ color: {t["overlay"]}; background-color: {t["bg2"]}; }}

/* ── Checkbox ── */
QCheckBox         {{ color: {t["text"]}; font-size: 12px; spacing: 6px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 2px solid {t["surface2"]};
    border-radius: 3px;
    background: {t["surface"]};
}}
QCheckBox::indicator:checked {{
    background: {t["blue"]};
    border-color: {t["blue"]};
    image: none;
}}

/* ── General buttons ── */
QPushButton {{
    background-color: {t["surface"]};
    color: {t["text"]};
    border: 1px solid {t["surface2"]};
    border-radius: 5px;
    padding: 5px 14px;
    font-size: 12px;
}}
QPushButton:hover   {{ background-color: {t["surface2"]}; border-color: {t["blue"]}; }}
QPushButton:pressed {{ background-color: {t["bg2"]}; }}
QPushButton:disabled{{ color: {t["overlay"]}; background-color: {t["bg2"]}; border-color: {t["surface"]}; }}

/* ── Start button ── */
QPushButton#btn_start {{
    background-color: {t["blue"]};
    color: {t["bg"]};
    border: none;
    font-weight: bold;
    font-size: 13px;
    padding: 9px 28px;
    border-radius: 6px;
}}
QPushButton#btn_start:hover    {{ background-color: {t["blue2"]}; }}
QPushButton#btn_start:disabled {{ background-color: {t["surface2"]}; color: {t["overlay"]}; }}

/* ── Cancel button ── */
QPushButton#btn_cancel {{
    background-color: {t["red"]};
    color: {t["bg"]};
    border: none;
    font-weight: bold;
    font-size: 13px;
    padding: 9px 28px;
    border-radius: 6px;
}}
QPushButton#btn_cancel:hover    {{ background-color: #f5a3b8; }}
QPushButton#btn_cancel:disabled {{ background-color: {t["surface2"]}; color: {t["overlay"]}; }}

/* ── Theme toggle button ── */
QPushButton#btn_theme {{
    background-color: transparent;
    border: 1px solid {t["surface2"]};
    border-radius: 14px;
    padding: 3px 10px;
    font-size: 14px;
    color: {t["text"]};
}}
QPushButton#btn_theme:hover {{ background-color: {t["surface"]}; }}

/* ── Log view ── */
QPlainTextEdit {{
    background-color: {t["log_bg"]};
    color: {t["text"]};
    border: 1px solid {t["log_border"]};
    border-radius: 6px;
    font-family: "Consolas", "Cascadia Code", "Courier New", monospace;
    font-size: 11px;
    padding: 4px;
}}

/* ── Progress bar ── */
QProgressBar {{
    background-color: {t["surface"]};
    border: none;
    border-radius: 5px;
    height: 16px;
    text-align: center;
    color: {t["text"]};
    font-size: 11px;
    font-weight: bold;
}}
QProgressBar::chunk {{
    background-color: qlineargradient(
        x1:0, y1:0, x2:1, y2:0,
        stop:0 {t["blue"]}, stop:1 {t["mauve"]}
    );
    border-radius: 5px;
}}

/* ── Separator ── */
QFrame#sep {{ color: {t["sep"]}; }}

/* ── Scrollbar ── */
QScrollBar:vertical {{
    background: {t["bg2"]};
    width: 8px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background: {t["surface2"]};
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{ background: {t["overlay"]}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""


# ---------------------------------------------------------------------------
# Worker Thread
# ---------------------------------------------------------------------------

class ConversionWorker(QThread):
    """
    Background QThread for the full DAV → MP4 pipeline.

    All heavy work (FFmpeg subprocesses) runs here.
    Signals carry results back to the GUI thread.
    """

    log_message      = pyqtSignal(str, str)   # (text, level)
    progress_updated = pyqtSignal(int, int)   # (current, total)
    stage_changed    = pyqtSignal(str)
    plan_determined  = pyqtSignal(str)
    finished         = pyqtSignal(bool, str)  # (success, output_path_or_error)

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
            segment_duration=SEGMENT_DURATION_SEC,
        )
        self._start_time = 0.0

    def cancel(self) -> None:
        self._processor.cancel()

    def run(self) -> None:
        self._processor.reset()
        self._start_time = time.monotonic()

        def log(msg: str, level: str = "info") -> None:
            elapsed = time.monotonic() - self._start_time
            ts = str(timedelta(seconds=int(elapsed)))
            self.log_message.emit(f"[{ts}] {msg}", level)

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
                    "NVENC not available — falling back to CPU (libx264).",
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

        # ── FIX: use SEGMENT_DURATION_SEC, NOT sum of probe durations ──
        total_expected = float(SEGMENT_DURATION_SEC * len(video_infos))
        log(
            f"Segments: {len(video_infos)}  |  "
            f"Expected output: {timedelta(seconds=int(total_expected))} "
            f"({total_expected:.0f}s exactly)",
            "success",
        )

        # ── 3. Build encoding plan ────────────────────────────────────
        log("─── Determining encoding strategy…", "header")
        plan = self._processor.build_plan(video_infos, force_gpu=self._use_gpu)
        log(f"Strategy: {plan.reason}", "success")
        self.plan_determined.emit(f"⚙ {plan.reason}")

        # ── 4. Process each segment ───────────────────────────────────
        action = "Copying" if plan.use_stream_copy else "Transcoding"
        log(f"─── {action} segments…", "header")

        temp_dir    = make_temp_dir()
        temp_files: list[Path]              = []
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
            stage_msg = f"{action} {i + 1}/{total_steps}: {info.path.name}"
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
                    self.finished.emit(False, f"Processing failed: {info.path.name}")
                return

            temp_files.append(temp_out)
            # ── FIX: always use SEGMENT_DURATION_SEC, NOT info.duration ──
            segments.append((temp_out, float(SEGMENT_DURATION_SEC)))
            self.progress_updated.emit(i + 1, total_steps)
            log(f"  ✓ Segment {i + 1} complete.", "success")

        # ── 5. Write concat list ──────────────────────────────────────
        log("─── Writing concat playlist…", "header")
        self.stage_changed.emit("Writing concat playlist…")
        list_path = temp_dir / "concat_list.txt"
        self._processor.write_concat_list(segments, list_path)
        log(f"  Playlist written ({len(segments)} entries × {SEGMENT_DURATION_SEC}s each)", "info")

        # ── 6. Concatenate ────────────────────────────────────────────
        log("─── Concatenating all segments into final output…", "header")
        self.stage_changed.emit("Concatenating segments…")
        self.progress_updated.emit(0, 1)

        self._output_file.parent.mkdir(parents=True, exist_ok=True)
        ok = self._processor.concatenate_segments(list_path, self._output_file, log)

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
            f"    Expected  : {timedelta(seconds=int(total_expected))} "
            f"({total_expected:.0f}s — {len(video_infos)} × {SEGMENT_DURATION_SEC}s)\n"
            f"    Wall time : {timedelta(seconds=int(elapsed))}\n"
            f"    Strategy  : {plan.reason}\n"
            f"    Verify    : ffprobe -v error -show_entries format=duration "
            f'-of default=noprint_wrappers=1 "{self._output_file}"',
            "success",
        )
        self.stage_changed.emit(f"✓ Finished! → {self._output_file.name}")
        self.finished.emit(True, str(self._output_file))


# ---------------------------------------------------------------------------
# Drop-enabled QLineEdit
# ---------------------------------------------------------------------------

class DropLineEdit(QLineEdit):
    """QLineEdit that accepts folder/file drag-and-drop."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        urls = event.mimeData().urls()
        if urls:
            self.setText(urls[0].toLocalFile())
        else:
            super().dropEvent(event)


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    """Top-level application window for DAV Consolidator v3."""

    APP_TITLE  = "DAV Consolidator"
    APP_VER    = "v3.0"
    MIN_WIDTH  = 900
    MIN_HEIGHT = 760

    def __init__(self) -> None:
        super().__init__()
        self._worker:    ConversionWorker | None = None
        self._dark_mode: bool                   = True
        self._log_colours = _LOG_COLOURS_DARK

        # Elapsed-time counter
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._run_start = 0.0

        self._setup_ui()
        self._apply_theme()
        self._post_init()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        self.setWindowTitle(f"{self.APP_TITLE} {self.APP_VER}")
        self.setMinimumSize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.resize(1020, 860)
        self.setAcceptDrops(True)

        central = QWidget(objectName="central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(0, 0, 0, 12)

        # ── Header bar ────────────────────────────────────────────────
        root.addWidget(self._build_header())

        # ── Body ──────────────────────────────────────────────────────
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setSpacing(10)
        body_layout.setContentsMargins(16, 8, 16, 0)

        body_layout.addWidget(self._build_config_group())
        body_layout.addLayout(self._build_info_row())
        body_layout.addWidget(self._build_log_group(), stretch=1)
        body_layout.addLayout(self._build_progress_section())
        body_layout.addWidget(self._build_separator())
        body_layout.addLayout(self._build_action_row())

        root.addWidget(body, stretch=1)

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _build_header(self) -> QWidget:
        header = QWidget(objectName="header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 10, 16, 10)

        # Title + subtitle
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        lbl_title = QLabel(f"🎬  {self.APP_TITLE} {self.APP_VER}", objectName="lbl_title")
        lbl_title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        lbl_subtitle = QLabel(
            "Forensic-accurate DAV → MP4  ·  Zero frame loss  ·  GPU / CPU",
            objectName="lbl_subtitle",
        )
        title_col.addWidget(lbl_title)
        title_col.addWidget(lbl_subtitle)
        hl.addLayout(title_col)
        hl.addStretch()

        # GPU badge
        gpu_col = QVBoxLayout()
        gpu_col.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._gpu_badge = QLabel("⚡ GPU: detecting…", objectName="badge_gpu_no")
        gpu_col.addWidget(self._gpu_badge)
        hl.addLayout(gpu_col)
        hl.addSpacing(16)

        # Theme toggle
        self._btn_theme = QPushButton("☀", objectName="btn_theme")
        self._btn_theme.setFixedSize(36, 36)
        self._btn_theme.setToolTip("Toggle dark / light theme")
        self._btn_theme.clicked.connect(self._toggle_theme)
        hl.addWidget(self._btn_theme)

        return header

    # ------------------------------------------------------------------
    # Configuration group
    # ------------------------------------------------------------------

    def _build_config_group(self) -> QGroupBox:
        grp = QGroupBox("Configuration")
        layout = QVBoxLayout(grp)
        layout.setSpacing(8)

        self._input_edit, r1, _ = self._path_row(
            "Input Folder:", "Drag & drop or browse for folder containing .dav files…",
            browse_dir=True,
        )
        self._output_edit, r2, _ = self._path_row(
            "Output File:", "Select output .mp4 path (auto-filled from DAV filenames)…",
            browse_dir=False,
        )
        self._ffmpeg_edit, r3, ff_btn = self._path_row(
            "FFmpeg Path:", "auto-detected or system PATH",
            browse_dir=False, browse_label="Browse",
        )
        ff_btn.clicked.disconnect()
        ff_btn.clicked.connect(self._browse_ffmpeg)

        layout.addLayout(r1)
        layout.addLayout(r2)
        layout.addLayout(r3)

        # GPU row
        gpu_row = QHBoxLayout()
        self._gpu_check = QCheckBox(
            "Enable GPU acceleration (NVIDIA NVENC — auto-fallback to CPU)"
        )
        self._gpu_check.setToolTip(
            "Uses NVIDIA NVENC for faster H.264 encoding.\n"
            "Automatically falls back to libx264 if no NVENC GPU is found."
        )
        self._gpu_check.toggled.connect(self._on_gpu_toggled)
        gpu_row.addWidget(self._gpu_check)
        gpu_row.addStretch()
        layout.addLayout(gpu_row)

        # Wire input folder → auto-fill output
        self._input_edit.textChanged.connect(self._refresh_file_info)

        return grp

    def _path_row(
        self,
        label:        str,
        placeholder:  str,
        *,
        browse_dir:   bool,
        browse_label: str = "Browse…",
    ) -> tuple[DropLineEdit, QHBoxLayout, QPushButton]:
        row  = QHBoxLayout()
        lbl  = QLabel(label)
        lbl.setFixedWidth(110)
        edit = DropLineEdit()
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
    # Info row (file count + plan label)
    # ------------------------------------------------------------------

    def _build_info_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(2, 0, 2, 0)

        self._file_info_label = QLabel("No folder selected.")
        self._file_info_label.setObjectName("lbl_file_info")

        self._plan_label = QLabel("")
        self._plan_label.setObjectName("lbl_plan")
        self._plan_label.setWordWrap(True)

        row.addWidget(self._file_info_label)
        row.addStretch()
        row.addWidget(self._plan_label)
        return row

    # ------------------------------------------------------------------
    # Log group
    # ------------------------------------------------------------------

    def _build_log_group(self) -> QGroupBox:
        grp    = QGroupBox("Conversion Log")
        layout = QVBoxLayout(grp)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(12_000)
        self._log_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        layout.addWidget(self._log_view)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_clear = QPushButton("Clear Log")
        btn_clear.setFixedWidth(90)
        btn_clear.clicked.connect(self._log_view.clear)
        btn_row.addWidget(btn_clear)
        layout.addLayout(btn_row)

        return grp

    # ------------------------------------------------------------------
    # Progress section
    # ------------------------------------------------------------------

    def _build_progress_section(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setSpacing(4)

        row = QHBoxLayout()
        self._stage_label = QLabel("Idle")
        self._stage_label.setObjectName("lbl_stage")
        row.addWidget(self._stage_label, stretch=1)

        self._elapsed_label = QLabel("")
        self._elapsed_label.setObjectName("lbl_elapsed")
        self._elapsed_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(self._elapsed_label)
        layout.addLayout(row)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFormat("%p%  (%v / %m)")
        self._progress.setFixedHeight(18)
        layout.addWidget(self._progress)

        return layout

    def _build_separator(self) -> QFrame:
        sep = QFrame(objectName="sep")
        sep.setFrameShape(QFrame.Shape.HLine)
        return sep

    # ------------------------------------------------------------------
    # Action buttons
    # ------------------------------------------------------------------

    def _build_action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch()

        self._btn_start = QPushButton("▶  Start Conversion")
        self._btn_start.setObjectName("btn_start")
        self._btn_start.setFixedHeight(42)
        self._btn_start.clicked.connect(self._start_conversion)
        row.addWidget(self._btn_start)

        row.addSpacing(10)

        self._btn_cancel = QPushButton("✖  Cancel")
        self._btn_cancel.setObjectName("btn_cancel")
        self._btn_cancel.setFixedHeight(42)
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.clicked.connect(self._cancel_conversion)
        row.addWidget(self._btn_cancel)

        row.addStretch()
        return row

    # ------------------------------------------------------------------
    # Post-init
    # ------------------------------------------------------------------

    def _post_init(self) -> None:
        detected = get_ffmpeg_path()
        self._ffmpeg_edit.setText(detected)
        if detected != "ffmpeg":
            self._log(f"Auto-detected FFmpeg: {detected}", "success")
        else:
            self._log("ffmpeg.exe not found in project root — using system PATH.", "warning")
        self._log("DAV Consolidator ready.  Select an input folder to begin.", "success")
        self._log(
            f"Segment duration: {SEGMENT_DURATION_SEC}s per file  |  "
            "Files sorted in natural chronological order.",
            "info",
        )

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def _apply_theme(self) -> None:
        t = _DARK if self._dark_mode else _LIGHT
        self.setStyleSheet(_build_stylesheet(t))
        self._log_colours = _LOG_COLOURS_DARK if self._dark_mode else _LOG_COLOURS_LIGHT
        if hasattr(self, "_btn_theme"):
            self._btn_theme.setText("☀" if self._dark_mode else "🌙")

    def _toggle_theme(self) -> None:
        self._dark_mode = not self._dark_mode
        self._apply_theme()

    # ------------------------------------------------------------------
    # GPU badge
    # ------------------------------------------------------------------

    def _on_gpu_toggled(self, checked: bool) -> None:
        if checked:
            self._gpu_badge.setObjectName("badge_gpu_no")
            self._gpu_badge.setText("⚡ GPU: testing…")
            self._apply_theme()
            QApplication.processEvents()
            proc = FFmpegProcessor(ffmpeg_path=self._ffmpeg_edit.text().strip() or get_ffmpeg_path())
            if proc.detect_nvenc():
                self._gpu_badge.setObjectName("badge_gpu_ok")
                self._gpu_badge.setText("⚡ GPU: NVENC ✓")
            else:
                self._gpu_badge.setObjectName("badge_gpu_no")
                self._gpu_badge.setText("⚡ GPU: not found")
            self._apply_theme()
        else:
            self._gpu_badge.setObjectName("badge_gpu_no")
            self._gpu_badge.setText("⚡ GPU: off")
            self._apply_theme()

    # ------------------------------------------------------------------
    # Browse slots
    # ------------------------------------------------------------------

    def _browse_folder(self, edit: DropLineEdit) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select DAV Input Folder", edit.text() or ""
        )
        if folder:
            edit.setText(folder)

    def _browse_file(self, edit: DropLineEdit) -> None:
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
    # File info refresh + auto output filename
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
            total_s  = SEGMENT_DURATION_SEC * n
            total_td = timedelta(seconds=total_s)
            self._file_info_label.setText(
                f"✓  Found {n} .dav file(s)  →  "
                f"Expected output: {total_td} ({total_s}s exactly)"
            )
            # Auto-fill output filename if user hasn't typed one
            if not self._output_edit.text().strip():
                auto = build_output_filename(files, Path(folder))
                self._output_edit.setText(str(auto))
                self._log(f"Auto output filename: {auto.name}", "info")
        else:
            self._file_info_label.setText("⚠  No .dav files found in this folder.")

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
        self._elapsed_label.setText("")
        self._run_start = time.monotonic()
        self._elapsed_timer.start()

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
        logger.info("ConversionWorker started — input=%s  gpu=%s", input_folder, use_gpu)

    def _cancel_conversion(self) -> None:
        if self._worker and self._worker.isRunning():
            self._log("Cancellation requested — waiting for FFmpeg to stop…", "warning")
            self._worker.cancel()
            self._btn_cancel.setEnabled(False)

    # ------------------------------------------------------------------
    # Worker signal handlers
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
        self._elapsed_timer.stop()
        self._set_running(False)
        if success:
            self._progress.setValue(self._progress.maximum())
            QMessageBox.information(
                self,
                "Conversion Complete ✓",
                f"Output saved to:\n{message}\n\n"
                f"Verify duration:\n"
                f'ffprobe -v error -show_entries format=duration '
                f'-of default=noprint_wrappers=1 "{message}"',
            )
        elif message:
            QMessageBox.critical(self, "Conversion Failed", message)

    # ------------------------------------------------------------------
    # Elapsed timer
    # ------------------------------------------------------------------

    def _tick_elapsed(self) -> None:
        if self._run_start > 0:
            elapsed = int(time.monotonic() - self._run_start)
            self._elapsed_label.setText(f"Elapsed: {timedelta(seconds=elapsed)}")

    # ------------------------------------------------------------------
    # Log helper
    # ------------------------------------------------------------------

    def _log(self, message: str, level: str = "info") -> None:
        colour = self._log_colours.get(level, self._log_colours["info"])
        fmt    = QTextCharFormat()
        fmt.setForeground(QColor(colour))
        if level == "header":
            fmt.setFontWeight(700)

        cursor = self._log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(message + "\n", fmt)
        self._log_view.setTextCursor(cursor)
        self._log_view.ensureCursorVisible()

    # ------------------------------------------------------------------
    # UI state helpers
    # ------------------------------------------------------------------

    def _set_running(self, running: bool) -> None:
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
    # Close guard
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:   # type: ignore[override]
        if self._worker and self._worker.isRunning():
            reply = QMessageBox.question(
                self,
                "Conversion Running",
                "A conversion is in progress.\nCancel and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._elapsed_timer.stop()
                self._worker.cancel()
                self._worker.wait(5_000)
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()
