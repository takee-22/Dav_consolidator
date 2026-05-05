"""
gui/main_window.py
------------------
DAV Consolidator v4 — Minimalist PyQt6 interface.

Design principles
~~~~~~~~~~~~~~~~~
• Expose ONLY what the spec requires: file list, output path,
  re-encoding toggle, target FPS (conditional), start/cancel.
• FFmpeg binary path is fully hidden — users never see it.
• Dark ↔ Light theme toggle in the top-right corner.
• GPU status badge auto-detects all three hardware accelerators.
• ToggleSwitch is a custom pill-style QWidget (no third-party deps).
• ConversionWorker (QThread) keeps the GUI fully responsive.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import timedelta
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve, QPoint, QPropertyAnimation,
    QSize, Qt, QThread, QTimer,
    pyqtProperty, pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import (
    QColor, QDragEnterEvent, QDropEvent,
    QFont, QPainter, QPen, QTextCharFormat, QTextCursor,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QDoubleSpinBox,
    QFileDialog, QFrame, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSizePolicy,
    QSpacerItem, QVBoxLayout, QWidget,
)

from core.gpu_detector import GPUInfo, detect_best, detect_all, Accelerator
from core.processor import Processor, ClipInfo, SEGMENT_DURATION
from utils.ffmpeg_utils import get_ffmpeg, natural_sorted

logger = logging.getLogger(__name__)

# ── Themes ───────────────────────────────────────────────────────────────────

_D = {  # Dark — Catppuccin Mocha
    "bg":       "#1e1e2e", "bg2":    "#181825", "surface": "#313244",
    "surf2":    "#45475a", "over":   "#6c7086", "text":    "#cdd6f4",
    "sub":      "#a6adc8", "blue":   "#89b4fa", "blue2":   "#b4befe",
    "green":    "#a6e3a1", "red":    "#f38ba8", "yellow":  "#f9e2af",
    "peach":    "#fab387", "mauve":  "#cba6f7", "log_bg":  "#11111b",
    "tog_on":   "#89b4fa", "tog_off":"#45475a", "tog_knob":"#cdd6f4",
}
_L = {  # Light — Catppuccin Latte
    "bg":       "#eff1f5", "bg2":    "#e6e9ef", "surface": "#dce0e8",
    "surf2":    "#bcc0cc", "over":   "#8c8fa1", "text":    "#4c4f69",
    "sub":      "#6c6f85", "blue":   "#1e66f5", "blue2":   "#7287fd",
    "green":    "#40a02b", "red":    "#d20f39", "yellow":  "#df8e1d",
    "peach":    "#fe640b", "mauve":  "#8839ef", "log_bg":  "#dce0e8",
    "tog_on":   "#1e66f5", "tog_off":"#bcc0cc", "tog_knob":"#ffffff",
}
_LOG_D = {"info":"#cdd6f4","success":"#a6e3a1","warning":"#f9e2af",
          "error":"#f38ba8","ffmpeg":"#89b4fa","header":"#cba6f7"}
_LOG_L = {"info":"#4c4f69","success":"#40a02b","warning":"#df8e1d",
          "error":"#d20f39","ffmpeg":"#1e66f5","header":"#8839ef"}


def _ss(t: dict) -> str:
    return f"""
QMainWindow,QDialog{{background:{t["bg"]};}}
QWidget#root{{background:{t["bg"]};}}
QWidget#header{{background:{t["bg2"]};border-bottom:2px solid {t["blue"]};}}
QGroupBox{{color:{t["text"]};border:1px solid {t["surf2"]};border-radius:8px;
    margin-top:10px;font-weight:bold;font-size:12px;padding:6px 4px 4px 4px;}}
QGroupBox::title{{subcontrol-origin:margin;left:12px;padding:0 6px;color:{t["blue"]};}}
QLabel{{color:{t["text"]};font-size:12px;}}
QLabel#title{{color:{t["blue"]};font-size:18px;font-weight:bold;}}
QLabel#sub{{color:{t["over"]};font-size:11px;}}
QLabel#stage{{color:{t["sub"]};font-size:11px;}}
QLabel#elapsed{{color:{t["over"]};font-size:11px;}}
QLabel#gpu_badge{{font-size:11px;font-weight:bold;}}
QLabel#fps_lbl{{color:{t["sub"]};font-size:12px;}}
QLineEdit{{background:{t["surface"]};color:{t["text"]};border:1px solid {t["surf2"]};
    border-radius:5px;padding:5px 10px;font-size:12px;}}
QLineEdit:focus{{border:1px solid {t["blue"]};}}
QLineEdit:disabled{{color:{t["over"]};background:{t["bg2"]};}}
QDoubleSpinBox{{background:{t["surface"]};color:{t["text"]};border:1px solid {t["surf2"]};
    border-radius:5px;padding:4px 8px;font-size:12px;}}
QDoubleSpinBox:focus{{border:1px solid {t["blue"]};}}
QDoubleSpinBox::up-button,QDoubleSpinBox::down-button{{
    background:{t["surf2"]};border:none;border-radius:3px;width:16px;}}
QPushButton{{background:{t["surface"]};color:{t["text"]};border:1px solid {t["surf2"]};
    border-radius:5px;padding:5px 14px;font-size:12px;}}
QPushButton:hover{{background:{t["surf2"]};border-color:{t["blue"]};}}
QPushButton:pressed{{background:{t["bg2"]};}}
QPushButton:disabled{{color:{t["over"]};background:{t["bg2"]};border-color:{t["surface"]};}}
QPushButton#start{{background:{t["blue"]};color:{t["bg"]};border:none;
    font-weight:bold;font-size:13px;padding:9px 30px;border-radius:6px;}}
QPushButton#start:hover{{background:{t["blue2"]};}}
QPushButton#start:disabled{{background:{t["surf2"]};color:{t["over"]};}}
QPushButton#cancel{{background:{t["red"]};color:{t["bg"]};border:none;
    font-weight:bold;font-size:13px;padding:9px 30px;border-radius:6px;}}
QPushButton#cancel:hover{{background:#f5a3b8;}}
QPushButton#cancel:disabled{{background:{t["surf2"]};color:{t["over"]};}}
QPushButton#theme_btn{{background:transparent;border:1px solid {t["surf2"]};
    border-radius:14px;padding:3px 10px;font-size:15px;color:{t["text"]};}}
QPushButton#theme_btn:hover{{background:{t["surface"]};}}
QPushButton#remove_btn{{background:transparent;border:none;
    color:{t["red"]};font-size:14px;padding:2px 6px;border-radius:3px;}}
QPushButton#remove_btn:hover{{background:{t["surface"]};}}
QListWidget{{background:{t["surface"]};color:{t["text"]};border:1px solid {t["surf2"]};
    border-radius:6px;font-size:11px;outline:none;}}
QListWidget::item{{padding:3px 6px;border-radius:3px;}}
QListWidget::item:selected{{background:{t["blue"]};color:{t["bg"]};}}
QListWidget::item:hover{{background:{t["surf2"]};}}
QPlainTextEdit{{background:{t["log_bg"]};color:{t["text"]};border:1px solid {t["surf2"]};
    border-radius:6px;font-family:"Consolas","Cascadia Code","Courier New",monospace;
    font-size:11px;padding:4px;}}
QProgressBar{{background:{t["surface"]};border:none;border-radius:5px;height:16px;
    text-align:center;color:{t["text"]};font-size:11px;font-weight:bold;}}
QProgressBar::chunk{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
    stop:0 {t["blue"]},stop:1 {t["mauve"]});border-radius:5px;}}
QFrame#sep{{color:{t["surf2"]};}}
QScrollBar:vertical{{background:{t["bg2"]};width:8px;border-radius:4px;}}
QScrollBar::handle:vertical{{background:{t["surf2"]};border-radius:4px;min-height:20px;}}
QScrollBar::handle:vertical:hover{{background:{t["over"]};}}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}
"""


# ── Custom toggle switch widget ───────────────────────────────────────────────

class ToggleSwitch(QWidget):
    """
    Animated pill-style toggle switch.
    Emits toggled(bool) when clicked.
    """

    toggled = pyqtSignal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._checked   = False
        self._anim_pos  = 0.0          # 0.0 = off, 1.0 = on
        self._on_color  = QColor("#89b4fa")
        self._off_color = QColor("#45475a")
        self._knob_col  = QColor("#cdd6f4")
        self.setFixedSize(48, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._anim = QPropertyAnimation(self, b"anim_pos", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)

    # Qt property for animation
    @pyqtProperty(float)
    def anim_pos(self) -> float:           # type: ignore[override]
        return self._anim_pos

    @anim_pos.setter                       # type: ignore[override]
    def anim_pos(self, v: float) -> None:
        self._anim_pos = v
        self.update()

    def set_colors(self, on: str, off: str, knob: str) -> None:
        self._on_color  = QColor(on)
        self._off_color = QColor(off)
        self._knob_col  = QColor(knob)
        self.update()

    @property
    def checked(self) -> bool:
        return self._checked

    def setChecked(self, v: bool) -> None:
        if v == self._checked:
            return
        self._checked = v
        self._anim.stop()
        self._anim.setStartValue(self._anim_pos)
        self._anim.setEndValue(1.0 if v else 0.0)
        self._anim.start()

    def mousePressEvent(self, _) -> None:  # type: ignore[override]
        self.setChecked(not self._checked)
        self.toggled.emit(self._checked)

    def paintEvent(self, _) -> None:       # type: ignore[override]
        p  = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        r    = h / 2

        # Interpolate track colour
        t   = self._anim_pos
        col = QColor(
            int(self._off_color.red()   + t * (self._on_color.red()   - self._off_color.red())),
            int(self._off_color.green() + t * (self._on_color.green() - self._off_color.green())),
            int(self._off_color.blue()  + t * (self._on_color.blue()  - self._off_color.blue())),
        )
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(0, 0, w, h, r, r)

        # Knob
        knob_d   = h - 6
        knob_x   = 3 + t * (w - knob_d - 6)
        p.setBrush(self._knob_col)
        p.drawEllipse(int(knob_x), 3, knob_d, knob_d)
        p.end()


# ── Drop-enabled file list ────────────────────────────────────────────────────

class FileListWidget(QListWidget):
    """QListWidget that accepts dropped .dav (and other video) files."""

    files_added = pyqtSignal(list)   # list[Path]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e) -> None:   # type: ignore[override]
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:
        paths: list[Path] = []
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_file():
                paths.append(p)
            elif p.is_dir():
                paths.extend(p.rglob("*.dav"))
                paths.extend(p.rglob("*.DAV"))
        if paths:
            self.files_added.emit(natural_sorted(paths))


# ── Worker thread ─────────────────────────────────────────────────────────────

class Worker(QThread):
    """Runs the full pipeline in a background thread."""

    log_msg    = pyqtSignal(str, str)   # (text, level)
    progress   = pyqtSignal(int, int)   # (done, total)
    stage      = pyqtSignal(str)
    finished   = pyqtSignal(bool, str)  # (success, output_or_error)

    def __init__(
        self,
        clips:      list[Path],
        output:     Path,
        reencode:   bool,
        target_fps: float,
        parent:     QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._clips      = clips
        self._output     = output
        self._reencode   = reencode
        self._target_fps = target_fps
        self._proc       = Processor()
        self._t0         = 0.0

    def cancel(self) -> None:
        self._proc.cancel()

    def run(self) -> None:
        self._proc.reset()
        self._t0 = time.monotonic()

        def log(msg: str, lv: str = "info") -> None:
            elapsed = timedelta(seconds=int(time.monotonic() - self._t0))
            self.log_msg.emit(f"[{elapsed}] {msg}", lv)

        # ── Verify FFmpeg ─────────────────────────────────────────────
        log("─── Verifying internal FFmpeg bundle…", "header")
        ok, ver = self._proc.verify_ffmpeg()
        if not ok:
            log(f"FFmpeg not found: {ver}", "error")
            self.finished.emit(False, f"FFmpeg not found: {ver}")
            return
        log(f"FFmpeg OK: {ver}", "success")

        # ── Probe all clips ───────────────────────────────────────────
        log(f"─── Probing {len(self._clips)} source file(s)…", "header")
        self.stage.emit("Probing files…")
        infos: list[ClipInfo] = []
        for i, path in enumerate(self._clips):
            if self._proc.cancelled:
                self.finished.emit(False, "")
                return
            try:
                info = self._proc.probe(path)
                log(f"  [{i+1:>3}] {path.name}  "
                    f"{info.fps:.2f}fps  {info.duration:.2f}s  "
                    f"{info.codec}  {info.width}×{info.height}", "info")
                infos.append(info)
            except RuntimeError as e:
                log(f"  [SKIP] {path.name}: {e}", "warning")

        if not infos:
            log("No valid clips found.", "error")
            self.finished.emit(False, "No valid clips to process.")
            return

        n           = len(infos)
        expected_s  = n * SEGMENT_DURATION
        log(f"  {n} clip(s) × {SEGMENT_DURATION}s = {expected_s}s expected "
            f"({timedelta(seconds=expected_s)})", "success")
        self.progress.emit(0, n)

        # ── Detect GPU (for re-encode mode) ───────────────────────────
        gpu: GPUInfo | None = None
        if self._reencode:
            log("─── Detecting hardware encoder…", "header")
            gpu = detect_best(get_ffmpeg())
            log(f"  Selected: {gpu.label}", "success" if gpu.detected else "warning")

        # ── Run pipeline ──────────────────────────────────────────────
        self._output.parent.mkdir(parents=True, exist_ok=True)

        if not self._reencode:
            log("─── Starting lossless pass-through…", "header")
            self.stage.emit("Merging (lossless copy)…")
            ok = self._proc.run_lossless(
                infos, self._output, log,
                on_progress=lambda c, t: self.progress.emit(c, t),
            )
        else:
            log(f"─── Starting re-encode @ {self._target_fps} FPS…", "header")
            self.stage.emit(f"Transcoding @ {self._target_fps} FPS…")
            ok = self._proc.run_reencode(
                infos, self._output, self._target_fps, gpu, log,  # type: ignore[arg-type]
                on_progress=lambda c, t: self.progress.emit(c, t),
            )

        if self._proc.cancelled:
            self.finished.emit(False, "")
            return

        elapsed = timedelta(seconds=int(time.monotonic() - self._t0))
        if ok:
            log(f"─── ✓ Done in {elapsed} → {self._output}", "success")
            self.finished.emit(True, str(self._output))
        else:
            self.finished.emit(False, "Processing failed. Check the log.")


# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    TITLE = "DAV Consolidator"
    VER   = "v4.0"

    def __init__(self) -> None:
        super().__init__()
        self._dark     = True
        self._log_col  = _LOG_D
        self._worker:  Worker | None = None
        self._gpu_info: list[GPUInfo] = []
        self._clips:   list[Path]    = []

        self._elapsed_t = QTimer(self)
        self._elapsed_t.setInterval(1000)
        self._elapsed_t.timeout.connect(self._tick)
        self._t0 = 0.0

        self._build_ui()
        self._apply_theme()
        self._post_init()

    # ── UI construction ────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.setWindowTitle(f"{self.TITLE} {self.VER}")
        self.setMinimumSize(860, 760)
        self.resize(960, 840)

        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        vbox = QVBoxLayout(root)
        vbox.setSpacing(0)
        vbox.setContentsMargins(0, 0, 0, 0)

        vbox.addWidget(self._build_header())

        body = QWidget()
        bl   = QVBoxLayout(body)
        bl.setSpacing(10)
        bl.setContentsMargins(18, 12, 18, 12)

        bl.addWidget(self._build_files_group())
        bl.addWidget(self._build_output_group())
        bl.addWidget(self._build_engine_group())
        bl.addWidget(self._build_log_group(), stretch=1)
        bl.addLayout(self._build_progress_row())
        bl.addWidget(self._hline())
        bl.addLayout(self._build_action_row())

        vbox.addWidget(body, stretch=1)

    # ── Header ────────────────────────────────────────────────────────

    def _build_header(self) -> QWidget:
        hdr = QWidget(objectName="header")
        hl  = QHBoxLayout(hdr)
        hl.setContentsMargins(18, 10, 18, 10)

        col = QVBoxLayout()
        col.setSpacing(2)
        t = QLabel(f"🎬  {self.TITLE} {self.VER}", objectName="title")
        t.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        s = QLabel("High-fidelity video merger  ·  Lossless or GPU-accelerated",
                   objectName="sub")
        col.addWidget(t)
        col.addWidget(s)
        hl.addLayout(col)
        hl.addStretch()

        # GPU badge
        self._gpu_badge = QLabel("⚡ detecting…", objectName="gpu_badge")
        hl.addWidget(self._gpu_badge)
        hl.addSpacing(14)

        # Theme toggle
        self._btn_theme = QPushButton("☀", objectName="theme_btn")
        self._btn_theme.setFixedSize(36, 36)
        self._btn_theme.setToolTip("Toggle dark / light theme")
        self._btn_theme.clicked.connect(self._toggle_theme)
        hl.addWidget(self._btn_theme)
        return hdr

    # ── File list group ────────────────────────────────────────────────

    def _build_files_group(self) -> QGroupBox:
        grp = QGroupBox("Source Files")
        vl  = QVBoxLayout(grp)
        vl.setSpacing(6)

        # List
        self._file_list = FileListWidget()
        self._file_list.setFixedHeight(160)
        self._file_list.files_added.connect(self._add_files)
        vl.addWidget(self._file_list)

        # Buttons row
        btn_row = QHBoxLayout()
        b_add   = QPushButton("＋ Add Files")
        b_add.clicked.connect(self._browse_files)
        b_dir   = QPushButton("📁 Add Folder")
        b_dir.clicked.connect(self._browse_folder)
        b_clr   = QPushButton("✕ Clear All")
        b_clr.clicked.connect(self._clear_files)
        b_rem   = QPushButton("Remove Selected")
        b_rem.clicked.connect(self._remove_selected)

        for b in (b_add, b_dir, b_clr, b_rem):
            btn_row.addWidget(b)
        btn_row.addStretch()

        self._file_count = QLabel("No files loaded.")
        self._file_count.setObjectName("stage")
        btn_row.addWidget(self._file_count)
        vl.addLayout(btn_row)

        return grp

    # ── Output group ───────────────────────────────────────────────────

    def _build_output_group(self) -> QGroupBox:
        grp = QGroupBox("Output File")
        hl  = QHBoxLayout(grp)
        hl.setSpacing(8)

        self._output_edit = QLineEdit()
        self._output_edit.setPlaceholderText("Select output .mp4 path…")
        b = QPushButton("Browse…")
        b.setFixedWidth(90)
        b.clicked.connect(self._browse_output)
        hl.addWidget(self._output_edit, stretch=1)
        hl.addWidget(b)
        return grp

    # ── Engine group ───────────────────────────────────────────────────

    def _build_engine_group(self) -> QGroupBox:
        grp = QGroupBox("Processing Engine")
        vl  = QVBoxLayout(grp)
        vl.setSpacing(10)

        # Toggle row
        tog_row = QHBoxLayout()
        lbl_off = QLabel("Lossless Pass-Through")
        lbl_off.setStyleSheet("font-weight:bold;")

        self._toggle = ToggleSwitch()
        self._toggle.toggled.connect(self._on_toggle)

        lbl_on = QLabel("Smart Re-encoding")
        lbl_on.setStyleSheet("font-weight:bold;")

        self._mode_desc = QLabel(
            "OFF — raw bitstream copy, zero decode/encode, "
            "mathematically exact duration."
        )
        self._mode_desc.setObjectName("stage")
        self._mode_desc.setWordWrap(True)

        tog_row.addWidget(lbl_off)
        tog_row.addSpacing(10)
        tog_row.addWidget(self._toggle)
        tog_row.addSpacing(10)
        tog_row.addWidget(lbl_on)
        tog_row.addStretch()
        vl.addLayout(tog_row)
        vl.addWidget(self._mode_desc)

        # FPS row (hidden when toggle OFF)
        self._fps_row_widget = QWidget()
        fps_hl = QHBoxLayout(self._fps_row_widget)
        fps_hl.setContentsMargins(0, 0, 0, 0)
        fps_lbl = QLabel("Target FPS:", objectName="fps_lbl")
        fps_lbl.setFixedWidth(80)
        self._fps_spin = QDoubleSpinBox()
        self._fps_spin.setRange(1.0, 120.0)
        self._fps_spin.setValue(25.0)
        self._fps_spin.setSingleStep(1.0)
        self._fps_spin.setDecimals(2)
        self._fps_spin.setFixedWidth(100)
        self._fps_spin.setToolTip(
            "Target frame rate for re-encoded output.\n"
            "Common values: 15, 20, 25, 30, 50, 60"
        )
        fps_hl.addWidget(fps_lbl)
        fps_hl.addWidget(self._fps_spin)
        fps_hl.addStretch()
        self._fps_row_widget.hide()
        vl.addWidget(self._fps_row_widget)

        return grp

    # ── Log group ──────────────────────────────────────────────────────

    def _build_log_group(self) -> QGroupBox:
        grp = QGroupBox("Log")
        vl  = QVBoxLayout(grp)
        vl.setContentsMargins(6, 6, 6, 6)
        vl.setSpacing(4)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(15_000)
        vl.addWidget(self._log)

        hl = QHBoxLayout()
        hl.addStretch()
        btn_clr = QPushButton("Clear")
        btn_clr.setFixedWidth(70)
        btn_clr.clicked.connect(self._log.clear)
        hl.addWidget(btn_clr)
        vl.addLayout(hl)
        return grp

    # ── Progress row ───────────────────────────────────────────────────

    def _build_progress_row(self) -> QVBoxLayout:
        vl = QVBoxLayout()
        vl.setSpacing(4)

        hl = QHBoxLayout()
        self._stage_lbl   = QLabel("Idle", objectName="stage")
        self._elapsed_lbl = QLabel("", objectName="elapsed")
        self._elapsed_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        hl.addWidget(self._stage_lbl, stretch=1)
        hl.addWidget(self._elapsed_lbl)
        vl.addLayout(hl)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFormat("%p%  (%v / %m)")
        self._progress.setFixedHeight(18)
        vl.addWidget(self._progress)
        return vl

    # ── Action row ─────────────────────────────────────────────────────

    def _build_action_row(self) -> QHBoxLayout:
        hl = QHBoxLayout()
        hl.addStretch()

        self._btn_start = QPushButton("▶  Start", objectName="start")
        self._btn_start.setFixedHeight(42)
        self._btn_start.clicked.connect(self._start)
        hl.addWidget(self._btn_start)

        hl.addSpacing(10)

        self._btn_cancel = QPushButton("✖  Cancel", objectName="cancel")
        self._btn_cancel.setFixedHeight(42)
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.clicked.connect(self._cancel)
        hl.addWidget(self._btn_cancel)

        hl.addStretch()
        return hl

    def _hline(self) -> QFrame:
        f = QFrame(objectName="sep")
        f.setFrameShape(QFrame.Shape.HLine)
        return f

    # ── Post-init ──────────────────────────────────────────────────────

    def _post_init(self) -> None:
        self._emit("DAV Consolidator v4.0 ready.", "success")
        self._emit("Drop .dav files or folders into the Source Files panel.", "info")
        self._emit("FFmpeg binary loaded from internal bundle (not system PATH).", "info")

        # Async GPU probe after UI is shown
        QTimer.singleShot(400, self._probe_gpu)

    def _probe_gpu(self) -> None:
        self._gpu_badge.setText("⚡ detecting…")
        QApplication.processEvents()
        gpu_list = detect_all(get_ffmpeg())
        self._gpu_info = gpu_list
        detected = [g for g in gpu_list if g.detected]
        if detected:
            names = ", ".join(g.label for g in detected)
            self._gpu_badge.setText(f"⚡ {names} ✓")
            self._gpu_badge.setStyleSheet("color:#a6e3a1;font-weight:bold;font-size:11px;")
            for g in detected:
                self._emit(f"GPU detected: {g.label} ({g.encoder})", "success")
        else:
            self._gpu_badge.setText("⚡ CPU only")
            self._gpu_badge.setStyleSheet("color:#6c7086;font-size:11px;")
            self._emit("No hardware GPU encoder detected — CPU (libx264) will be used.", "warning")

    # ── Theme ──────────────────────────────────────────────────────────

    def _apply_theme(self) -> None:
        t = _D if self._dark else _L
        self.setStyleSheet(_ss(t))
        self._log_col = _LOG_D if self._dark else _LOG_L
        if hasattr(self, "_btn_theme"):
            self._btn_theme.setText("☀" if self._dark else "🌙")
        if hasattr(self, "_toggle"):
            self._toggle.set_colors(t["tog_on"], t["tog_off"], t["tog_knob"])

    def _toggle_theme(self) -> None:
        self._dark = not self._dark
        self._apply_theme()

    # ── File management ────────────────────────────────────────────────

    def _add_files(self, paths: list[Path]) -> None:
        existing = {p for p in self._clips}
        new = [p for p in paths if p not in existing]
        self._clips.extend(new)
        self._clips = natural_sorted(self._clips)
        self._refresh_list()

    def _browse_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select DAV files", "",
            "Video files (*.dav *.DAV *.mp4 *.avi *.mkv);;All files (*)",
        )
        if paths:
            self._add_files([Path(p) for p in paths])

    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select folder")
        if folder:
            p = Path(folder)
            files = list(p.rglob("*.dav")) + list(p.rglob("*.DAV"))
            if files:
                self._add_files(natural_sorted(files))
            else:
                self._warn("No .dav files found in the selected folder.")

    def _clear_files(self) -> None:
        self._clips.clear()
        self._refresh_list()

    def _remove_selected(self) -> None:
        selected_rows = {self._file_list.row(item)
                         for item in self._file_list.selectedItems()}
        self._clips = [p for i, p in enumerate(self._clips)
                       if i not in selected_rows]
        self._refresh_list()

    def _refresh_list(self) -> None:
        self._file_list.clear()
        for p in self._clips:
            self._file_list.addItem(QListWidgetItem(p.name))
        n   = len(self._clips)
        dur = timedelta(seconds=n * SEGMENT_DURATION)
        self._file_count.setText(
            f"{n} file(s)  →  {dur} expected"
            if n else "No files loaded."
        )
        # Auto-set output filename from first/last clip
        if n and not self._output_edit.text().strip():
            self._auto_output()

    def _auto_output(self) -> None:
        """Derive output name from DAV naming convention if possible."""
        if not self._clips:
            return
        import re as _re
        pat = _re.compile(r"^(\d{2}\.\d{2}\.\d{2})-(\d{2}\.\d{2}\.\d{2})")
        m0 = pat.match(self._clips[0].stem)
        m1 = pat.match(self._clips[-1].stem)
        if m0 and m1:
            name = f"{m0.group(1)}-{m1.group(2)}.mp4"
        else:
            name = "output.mp4"
        out = self._clips[0].parent / name
        self._output_edit.setText(str(out))
        self._emit(f"Auto output: {name}", "info")

    # ── Engine toggle ──────────────────────────────────────────────────

    def _on_toggle(self, on: bool) -> None:
        self._fps_row_widget.setVisible(on)
        if on:
            self._mode_desc.setText(
                "ON — full decode → filter → encode pipeline. "
                "Normalizes FPS, rebuilds timestamps. Uses GPU if available."
            )
        else:
            self._mode_desc.setText(
                "OFF — raw bitstream copy, zero decode/encode, "
                "mathematically exact duration."
            )

    # ── Browse output ──────────────────────────────────────────────────

    def _browse_output(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save output as", self._output_edit.text() or "",
            "MP4 Video (*.mp4);;All Files (*)",
        )
        if path:
            self._output_edit.setText(path)

    # ── Start / Cancel ────────────────────────────────────────────────

    def _start(self) -> None:
        if not self._clips:
            self._warn("Add at least one source file.")
            return
        output = self._output_edit.text().strip()
        if not output:
            self._warn("Select an output file path.")
            return

        reencode   = self._toggle.checked
        target_fps = self._fps_spin.value()

        self._set_running(True)
        self._log.clear()
        self._progress.setValue(0)
        self._stage_lbl.setText("Starting…")
        self._elapsed_lbl.setText("")
        self._t0 = time.monotonic()
        self._elapsed_t.start()

        self._worker = Worker(
            clips=list(self._clips),
            output=Path(output),
            reencode=reencode,
            target_fps=target_fps,
        )
        self._worker.log_msg.connect(self._on_log)
        self._worker.progress.connect(self._on_progress)
        self._worker.stage.connect(self._stage_lbl.setText)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _cancel(self) -> None:
        if self._worker and self._worker.isRunning():
            self._emit("Cancelling…", "warning")
            self._worker.cancel()
            self._btn_cancel.setEnabled(False)

    # ── Worker slots ───────────────────────────────────────────────────

    @pyqtSlot(str, str)
    def _on_log(self, msg: str, lv: str) -> None:
        self._emit(msg, lv)

    @pyqtSlot(int, int)
    def _on_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        self._progress.setMaximum(total)
        self._progress.setValue(done)
        self._progress.setFormat(f"{int(done/total*100)}%  ({done} / {total})")

    @pyqtSlot(bool, str)
    def _on_finished(self, ok: bool, msg: str) -> None:
        self._elapsed_t.stop()
        self._set_running(False)
        if ok:
            self._progress.setValue(self._progress.maximum())
            QMessageBox.information(
                self, "Complete ✓",
                f"Output saved:\n{msg}\n\n"
                f"Verify:\nffprobe -v error -show_entries format=duration "
                f"-of default=noprint_wrappers=1 \"{msg}\"",
            )
        elif msg:
            QMessageBox.critical(self, "Failed", msg)

    # ── Elapsed timer ──────────────────────────────────────────────────

    def _tick(self) -> None:
        if self._t0:
            self._elapsed_lbl.setText(
                f"Elapsed: {timedelta(seconds=int(time.monotonic()-self._t0))}"
            )

    # ── Log helper ─────────────────────────────────────────────────────

    def _emit(self, msg: str, lv: str = "info") -> None:
        colour = self._log_col.get(lv, self._log_col["info"])
        fmt    = QTextCharFormat()
        fmt.setForeground(QColor(colour))
        if lv == "header":
            fmt.setFontWeight(700)
        cur = self._log.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.insertText(msg + "\n", fmt)
        self._log.setTextCursor(cur)
        self._log.ensureCursorVisible()

    # ── UI state ───────────────────────────────────────────────────────

    def _set_running(self, running: bool) -> None:
        self._btn_start.setEnabled(not running)
        self._btn_cancel.setEnabled(running)
        self._toggle.setEnabled(not running)
        self._fps_spin.setEnabled(not running)
        self._output_edit.setEnabled(not running)
        self._file_list.setEnabled(not running)

    @staticmethod
    def _warn(msg: str) -> None:
        b = QMessageBox()
        b.setIcon(QMessageBox.Icon.Warning)
        b.setWindowTitle("Input Required")
        b.setText(msg)
        b.exec()

    # ── Close guard ────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:   # type: ignore[override]
        if self._worker and self._worker.isRunning():
            r = QMessageBox.question(
                self, "Running",
                "A job is running. Cancel and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if r == QMessageBox.StandardButton.Yes:
                self._elapsed_t.stop()
                self._worker.cancel()
                self._worker.wait(5000)
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()
