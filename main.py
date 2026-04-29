"""
main.py
-------
Entry point for DAV Consolidator.

Usage (development)
-------------------
    python main.py

Usage (after PyInstaller bundle)
---------------------------------
    DAVConsolidator.exe

Requires:
    Python 3.11+
    PyQt6             (pip install PyQt6)
    ffmpeg.exe / ffprobe.exe in the project root  (NOT system-installed)
"""

from __future__ import annotations

import logging
import os
import sys

# ── Ensure project root is on sys.path ─────────────────────────────────────
# Required so that `gui`, `core`, `utils` sub-packages are importable
# regardless of how the script is launched (direct, PyInstaller, etc.).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Windows: set console title (harmless in GUI-only builds) ───────────────
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("DAV Consolidator")
    except Exception:
        pass

# ── Logging bootstrap ───────────────────────────────────────────────────────
# Root logger → stdout. GUI log pane mirrors this via signal callbacks.
_log_format = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
logging.basicConfig(
    level=logging.DEBUG,
    format=_log_format,
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# ── Qt imports ──────────────────────────────────────────────────────────────
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont

from gui.main_window import MainWindow

logger = logging.getLogger("dav_consolidator.main")


def main() -> int:
    """Initialise Qt, apply global settings, show the main window."""
    # High-DPI support (Qt 6 enables this by default; kept for explicitness)
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    app = QApplication(sys.argv)
    app.setApplicationName("DAV Consolidator")
    app.setApplicationDisplayName("DAV Consolidator")
    app.setOrganizationName("DAVConsolidator")
    app.setFont(QFont("Segoe UI", 10))

    window = MainWindow()
    window.show()

    logger.info("Application started — PID %d", os.getpid())
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
