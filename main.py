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

# ── Ensure project root is on sys.path ──────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Windows: suppress console flash ─────────────────────────────────────────
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("DAV Consolidator")
    except Exception:
        pass

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont

from gui.main_window import MainWindow

logger = logging.getLogger("dav_consolidator.main")


def main() -> int:
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
