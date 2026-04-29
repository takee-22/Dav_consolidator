"""
gui/main_window.py
------------------
PySide6 main window for DAV Consolidator.

Layout
~~~~~~
┌─────────────────────────────────────────────────────────┐
│  DAV Consolidator v1.0                                  │
├─────────────────────────────────────────────────────────┤
│  Input Folder  [ path…                          ] [📁]  │
│  Output File   [ path…                          ] [💾]  │
│  FFmpeg Path   [ ffmpeg (auto-detected)         ] [🔧]  │
├─────────────────────────────────────────────────────────┤
│  ┌─ Files found ──────────────────────────────────────┐ │
│  │  12 .dav files  ·  Total ≈ 60 min 0 s             │ │
│  └────────────────────────────────────────────────────┘ │
│  ┌─ Conversion Log ───────────────────────────────────┐ │
│  │  [00:00:01] Detected FFmpeg 6.1.1                  │ │
│  │  [00:00:01] Scanning folder…                       │ │
│  └────────────────────────────────────────────────────┘ │
│  Stage:  Transcoding segment 3 / 12                     │
│  [████████░░░░░░░░░░░░░░░]  33 %                        │
├─────────────────────────────────────────────────────────┤
│             [  Start Conversion  ]  [  Cancel  ]        │
└─────────────────────────────────────────────────────────┘

Threading model
~~~~~~~~~~~~~~~
:class:`ConversionWorker` runs the entire pipeline in a dedicated QThread
and communicates back to the GUI via Qt signals (thread-safe by design).
The GUI never blocks; all FFmpeg subprocess calls live in the worker.
"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Slot, Qt
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor, QIcon
from PySide6.QtWidgets import (
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

from ffmpeg_wrapper.processor import FFmpegProcessor
from utils.file_utils import (
    cleanup_files,
    ensure_mp4_extension,
    find_dav_files,
    make_temp_dir,
    stem_index,
)

# ---------------------------------------------------------------------------
# Colour palette for log levels
# ---------------------------------------------------------------------------
_LOG_COLOURS: dict[str, str] = {
    "info":    "#d4d4d4",   # neutral white-grey
    "success": "#4ec994",   # green
    "warning": "#e5c07b",   # amber
    "error":   "#e06c75",   # red
    "ffmpeg":  "#7c9ec9",   # muted blue for FFmpeg progress lines
    "header":  "#c678dd",   # purple for section headers
}

_STYLESHEET = """
QMainWindow {
    background-color: #1e1e2e;
}
QWidget#central {
    background-color: #1e1e2e;
}
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
QLabel {
    color: #cdd6f4;
    font-size: 13px;
}
QLineEdit {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 12px;
}
QLineEdit:focus {
    border: 1px solid #89b4fa;
}
QPushButton {
    background-color: #45475a;
    color: #cdd6f4;
    border: none;
    border-radius: 4px;
    padding: 5px 12px;
    font-size: 12px;
}
QPushButton:hover {
    background-color: #585b70;
}
QPushButton:pressed {
    background-color: #313244;
}
QPushButton#btn_start {
    background-color: #89b4fa;
    color: #1e1e2e;
    font-weight: bold;
    font-size: 13px;
    padding: 8px 24px;
}
QPushButton#btn_start:hover {
    background-color: #b4befe;
}
QPushButton#btn_start:disabled {
    background-color: #45475a;
    color: #6c7086;
}
QPushButton#btn_cancel {
    background-color: #f38ba8;
    color: #1e1e2e;
    font-weight: bold;
    font-size: 13px;
    padding: 8px 24px;
}
QPushButton#btn_cancel:hover {
    background-color: #f5a3b8;
}
QPushButton#btn_cancel:disabled {
    background-color: #45475a;
    color: #6c7086;
}
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
    height: 12px;
    text-align: center;
    color: #cdd6f4;
    font-size: 11px;
}
QProgressBar::chunk {
    background-color: #89b4fa;
    border-radius: 4px;
}
QLabel#lbl_stage {
    color: #a6adc8;
    font-size: 11px;
}
QLabel#lbl_file_info {
    color: #a6e3a1;
    font-size: 12px;
}
"""


# ---------------------------------------------------------------------------
# Worker Thread
# ---------------------------------------------------------------------------

class ConversionWorker(QThread):
    """
    Background worker that orchestrates the full DAV→MP4 pipeline.

    Signals
    -------
    log_message(str, str)
        A log line and its severity level (info / success / warning /
        error / ffmpeg / header).
    progress_updated(int, int)
        (current_step, total_steps) for the progress bar.
    stage_changed(str)
        Human-readable description of the current stage.
    finished(bool, str)
        Emitted when the pipeline completes.  (True, "") on success;
        (False, error_message) on failure.
    """

    log_message = Signal(str, str)
    progress_updated = Signal(int, int)
    stage_changed = Signal(str)
    finished = Signal(bool, str)

    def __init__(
        self,
        input_folder: str,
        output_file: str,
        ffmpeg_path: str,
        ffprobe_path: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._input_folder = Path(input_folder)
        self._output_file = ensure_mp4_extension(output_file)
        self._processor = FFmpegProcessor(ffmpeg_path, ffprobe_path)
        self._start_time = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Request cancellation from the GUI thread."""
        self._processor.cancel()

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    def run(self) -> None:  # noqa: C901  (complexity acceptable here)
        """Main pipeline executed in the worker thread."""
        self._processor.reset()
        self._start_time = time.monotonic()

        def log(msg: str, level: str = "info") -> None:
            elapsed = time.monotonic() - self._start_time
            ts = str(timedelta(seconds=int(elapsed)))
            self.log_message.emit(f"[{ts}] {msg}", level)

        # ── 0. Verify FFmpeg ─────────────────────────────────────────
        log("─── Checking FFmpeg installation…", "header")
        ok, version_msg = self._processor.detect_ffmpeg()
        if not ok:
            log(f"FFmpeg not found: {version_msg}", "error")
            self.finished.emit(False, f"FFmpeg not found: {version_msg}")
            return
        log(f"Found: {version_msg}", "success")

        # ── 1. Discover source files ─────────────────────────────────
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

        log(f"Found {len(dav_files)} .dav file(s) — processing in order:", "success")
        for i, f in enumerate(dav_files, 1):
            log(f"  [{i:>3}] {f.name}", "info")

        # ── 2. Probe each source file ────────────────────────────────
        log("─── Probing source files…", "header")
        self.stage_changed.emit("Probing source files…")

        video_infos = []
        for i, dav in enumerate(dav_files, 1):
            if self._processor.is_cancelled:
                self._abort(log)
                return
            try:
                info = self._processor.get_video_info(dav)
                video_infos.append(info)
                log(
                    f"  [{i:>3}] {dav.name}  "
                    f"{info.fps:.2f} fps  "
                    f"{info.duration:.2f}s  "
                    f"{info.codec}",
                    "info",
                )
            except RuntimeError as exc:
                log(f"  [WARN] Skipping {dav.name}: {exc}", "warning")

        if not video_infos:
            log("All probes failed — nothing to process.", "error")
            self.finished.emit(False, "All source probes failed.")
            return

        total_expected = sum(v.duration for v in video_infos)
        log(
            f"Total expected output duration: "
            f"{timedelta(seconds=int(total_expected))} "
            f"({total_expected:.3f}s)",
            "success",
        )

        # ── 3. Transcode each segment ────────────────────────────────
        log("─── Transcoding segments to intermediate MP4…", "header")
        temp_dir = make_temp_dir()
        temp_files: list[Path] = []
        segments: list[tuple[Path, float]] = []  # (path, duration)

        total_steps = len(video_infos)
        self.progress_updated.emit(0, total_steps)

        for i, info in enumerate(video_infos):
            if self._processor.is_cancelled:
                cleanup_files(temp_files, lambda m: log(m, "warning"))
                self._abort(log)
                return

            temp_out = temp_dir / f"{str(i).zfill(4)}.mp4"
            stage_msg = f"Transcoding segment {i + 1} / {total_steps}: {info.path.name}"
            self.stage_changed.emit(stage_msg)
            log(f"─── [{i + 1}/{total_steps}] {stage_msg}", "header")

            ok = self._processor.transcode_to_intermediate(info, temp_out, log)

            if not ok:
                if self._processor.is_cancelled:
                    cleanup_files(temp_files, lambda m: log(m, "warning"))
                    self._abort(log)
                    return
                log(f"Transcoding failed for {info.path.name}.", "error")
                cleanup_files(temp_files, lambda m: log(m, "warning"))
                self.finished.emit(False, f"Transcode failed: {info.path.name}")
                return

            temp_files.append(temp_out)
            segments.append((temp_out, info.duration))
            self.progress_updated.emit(i + 1, total_steps)
            log(f"  ✓ Segment {i + 1} complete.", "success")

        # ── 4. Write concat list ─────────────────────────────────────
        log("─── Writing concat playlist…", "header")
        self.stage_changed.emit("Writing concat playlist…")
        list_path = temp_dir / "concat_list.txt"
        self._processor.write_concat_list(segments, list_path)
        log(f"  Playlist written: {list_path}", "info")

        # ── 5. Concatenate ───────────────────────────────────────────
        log("─── Concatenating all segments into final output…", "header")
        self.stage_changed.emit("Concatenating segments…")
        self.progress_updated.emit(0, 1)

        # Ensure output directory exists.
        self._output_file.parent.mkdir(parents=True, exist_ok=True)

        ok = self._processor.concatenate_segments(list_path, self._output_file, log)

        if not ok:
            cleanup_files(temp_files + [list_path], lambda m: log(m, "warning"))
            if not self._processor.is_cancelled:
                self.finished.emit(False, "Concatenation failed.")
            else:
                self._abort(log)
            return

        self.progress_updated.emit(1, 1)

        # ── 6. Cleanup ───────────────────────────────────────────────
        log("─── Cleaning up temporary files…", "header")
        self.stage_changed.emit("Cleaning up…")
        cleanup_files(temp_files + [list_path], lambda m: log(m, "warning"))
        try:
            temp_dir.rmdir()
        except OSError:
            pass
        log("  Temporary files removed.", "info")

        # ── 7. Report ────────────────────────────────────────────────
        elapsed_total = time.monotonic() - self._start_time
        log(
            f"─── ✓ Done!  Output: {self._output_file}\n"
            f"    Expected duration : {total_expected:.3f}s\n"
            f"    Elapsed wall-time : {timedelta(seconds=int(elapsed_total))}\n"
            f"    Tip: Verify duration with:  ffprobe -v error "
            f'-show_entries format=duration -of default=noprint_wrappers=1 '
            f'"{self._output_file}"',
            "success",
        )
        self.stage_changed.emit(f"Finished!  Output: {self._output_file.name}")
        self.finished.emit(True, str(self._output_file))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _abort(log: object) -> None:
        # log is a local closure, mypy can't infer its type here.
        log("Conversion cancelled by user.", "warning")  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    """Top-level application window."""

    APP_TITLE = "DAV Consolidator v1.0"

    def __init__(self) -> None:
        super().__init__()
        self._worker: ConversionWorker | None = None
        self._setup_ui()
        self._apply_style()
        self._log("DAV Consolidator ready.  Select a folder to begin.", "success")
        self._log(
            "Tip: Files are sorted in natural order — e.g., ch01_001 … ch01_012.",
            "info",
        )

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        self.setWindowTitle(self.APP_TITLE)
        self.setMinimumSize(820, 680)
        self.resize(920, 740)

        central = QWidget(objectName="central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        # ── Title bar ────────────────────────────────────────────────
        title = QLabel(self.APP_TITLE)
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: #89b4fa; margin-bottom: 4px;")
        root.addWidget(title)

        subtitle = QLabel(
            "Consolidate IMOU .dav camera files into a single MP4 with zero frame loss."
        )
        subtitle.setStyleSheet("color: #6c7086; font-size: 12px; margin-bottom: 8px;")
        root.addWidget(subtitle)

        # ── Path configuration ────────────────────────────────────────
        paths_group = QGroupBox("Configuration")
        paths_layout = QVBoxLayout(paths_group)
        paths_layout.setSpacing(8)

        self._input_edit, row1, _ = self._path_row(
            "Input Folder:", "Select folder containing .dav files…", browse_dir=True
        )
        self._output_edit, row2, _ = self._path_row(
            "Output File:", "Select output .mp4 file path…", browse_dir=False
        )
        self._ffmpeg_edit, row3, ffmpeg_btn = self._path_row(
            "FFmpeg Path:", "ffmpeg  (uses system PATH by default)", browse_dir=False,
            browse_label="Browse",
        )
        self._ffmpeg_edit.setText("ffmpeg")

        # Rewire the FFmpeg browse button to open an executable picker.
        ffmpeg_btn.clicked.disconnect()
        ffmpeg_btn.clicked.connect(self._browse_ffmpeg)

        paths_layout.addLayout(row1)
        paths_layout.addLayout(row2)
        paths_layout.addLayout(row3)
        root.addWidget(paths_group)

        # ── File info banner ──────────────────────────────────────────
        self._file_info_label = QLabel("No folder selected.")
        self._file_info_label.setObjectName("lbl_file_info")
        self._file_info_label.setContentsMargins(4, 0, 0, 0)
        root.addWidget(self._file_info_label)

        # ── Log window ────────────────────────────────────────────────
        log_group = QGroupBox("Conversion Log")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(6, 6, 6, 6)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(5000)  # cap memory usage
        self._log_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        log_layout.addWidget(self._log_view)

        btn_clear = QPushButton("Clear Log")
        btn_clear.setFixedWidth(90)
        btn_clear.clicked.connect(self._log_view.clear)
        log_layout.addWidget(btn_clear, alignment=Qt.AlignmentFlag.AlignRight)

        root.addWidget(log_group, stretch=1)

        # ── Stage + progress ──────────────────────────────────────────
        self._stage_label = QLabel("Idle")
        self._stage_label.setObjectName("lbl_stage")
        root.addWidget(self._stage_label)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFormat("%p%  (%v / %m segments)")
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

        # ── Wire up folder change to update file info ─────────────────
        self._input_edit.textChanged.connect(self._refresh_file_info)

    def _path_row(
        self,
        label: str,
        placeholder: str,
        *,
        browse_dir: bool,
        browse_label: str = "Browse…",
    ) -> tuple[QLineEdit, QHBoxLayout, QPushButton]:
        """Build a label + line-edit + browse-button row.

        Returns (QLineEdit, QHBoxLayout, QPushButton) so callers can
        rewire the button's signal without relying on findChildren.
        """
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setFixedWidth(100)
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        btn = QPushButton(browse_label)
        btn.setFixedWidth(90)

        if browse_dir:
            btn.clicked.connect(lambda: self._browse_folder(edit))
        else:
            btn.clicked.connect(lambda: self._browse_file(edit))

        row.addWidget(lbl)
        row.addWidget(edit, stretch=1)
        row.addWidget(btn)
        return edit, row, btn

    def _apply_style(self) -> None:
        self.setStyleSheet(_STYLESHEET)

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
            "MP4 Video (*.mp4);;All Files (*)"
        )
        if path:
            edit.setText(path)

    def _browse_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate FFmpeg Executable", "",
            "Executables (*.exe);;All Files (*)"
        )
        if path:
            self._ffmpeg_edit.setText(path)

    # ------------------------------------------------------------------
    # File info refresh
    # ------------------------------------------------------------------

    @Slot(str)
    def _refresh_file_info(self, folder: str) -> None:
        if not folder:
            self._file_info_label.setText("No folder selected.")
            return
        try:
            files = find_dav_files(folder)
        except NotADirectoryError:
            self._file_info_label.setText("Invalid folder path.")
            return
        n = len(files)
        self._file_info_label.setText(
            f"Found {n} .dav file(s) in the selected folder.  "
            "(Duration will be determined after probing.)"
            if n > 0
            else "No .dav files found in this folder."
        )

    # ------------------------------------------------------------------
    # Conversion control
    # ------------------------------------------------------------------

    def _start_conversion(self) -> None:
        input_folder = self._input_edit.text().strip()
        output_file = self._output_edit.text().strip()
        ffmpeg_path = self._ffmpeg_edit.text().strip() or "ffmpeg"

        # Derive ffprobe from ffmpeg path.
        ffmpeg_p = Path(ffmpeg_path)
        if ffmpeg_p.parent != Path("."):
            ffprobe_path = str(ffmpeg_p.parent / ffmpeg_p.name.replace("ffmpeg", "ffprobe"))
        else:
            ffprobe_path = "ffprobe"

        # Validate inputs.
        if not input_folder:
            self._warn("Please select an input folder.")
            return
        if not output_file:
            self._warn("Please specify an output file path.")
            return
        if not Path(input_folder).is_dir():
            self._warn(f"Input folder does not exist:\n{input_folder}")
            return

        # Lock UI.
        self._set_running(True)
        self._log_view.clear()
        self._progress.setValue(0)
        self._stage_label.setText("Starting…")

        self._worker = ConversionWorker(
            input_folder=input_folder,
            output_file=output_file,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
        )
        self._worker.log_message.connect(self._on_log_message)
        self._worker.progress_updated.connect(self._on_progress)
        self._worker.stage_changed.connect(self._stage_label.setText)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _cancel_conversion(self) -> None:
        if self._worker and self._worker.isRunning():
            self._log("Cancellation requested — waiting for FFmpeg to stop…", "warning")
            self._worker.cancel()
            self._btn_cancel.setEnabled(False)

    # ------------------------------------------------------------------
    # Worker signal handlers
    # ------------------------------------------------------------------

    @Slot(str, str)
    def _on_log_message(self, message: str, level: str) -> None:
        self._log(message, level)

    @Slot(int, int)
    def _on_progress(self, current: int, total: int) -> None:
        if total <= 0:
            return
        self._progress.setMaximum(total)
        self._progress.setValue(current)
        pct = int(current / total * 100)
        self._progress.setFormat(f"{pct}%  ({current} / {total} segments)")

    @Slot(bool, str)
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
        else:
            if message:
                QMessageBox.critical(self, "Conversion Failed", message)

    # ------------------------------------------------------------------
    # Log helpers
    # ------------------------------------------------------------------

    def _log(self, message: str, level: str = "info") -> None:
        """Append a coloured line to the log widget."""
        colour = _LOG_COLOURS.get(level, _LOG_COLOURS["info"])
        fmt = QTextCharFormat()
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
        self._btn_start.setEnabled(not running)
        self._btn_cancel.setEnabled(running)
        self._input_edit.setEnabled(not running)
        self._output_edit.setEnabled(not running)
        self._ffmpeg_edit.setEnabled(not running)

    @staticmethod
    def _warn(message: str) -> None:
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Input Required")
        box.setText(message)
        box.exec()

    # ------------------------------------------------------------------
    # Window close guard
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._worker and self._worker.isRunning():
            reply = QMessageBox.question(
                self,
                "Conversion Running",
                "A conversion is in progress.  Cancel and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._worker.cancel()
                self._worker.wait(5000)
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()