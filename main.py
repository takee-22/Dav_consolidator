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

The application requires:
    • Python 3.11+
    • PySide6  (pip install PySide6)
    • FFmpeg + FFprobe on the system PATH  (or specify the path in the GUI)
"""

import sys
import os

# Ensure the project root is on sys.path so the sub-packages
# (gui, ffmpeg_wrapper, utils) are importable regardless of how
# the script is launched (python main.py, double-click, PyInstaller, etc.)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# On Windows, suppress the console window when launched via .pyw or .exe.
# This has no effect in a terminal session.
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("DAV Consolidator")
    except Exception:
        pass

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from gui.main_window import MainWindow


def main() -> int:
    """Initialise Qt, apply global settings, show the main window."""

    # High-DPI support (Qt 6 enables this by default, kept for clarity).
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    app = QApplication(sys.argv)
    app.setApplicationName("DAV Consolidator")
    app.setApplicationDisplayName("DAV Consolidator")
    app.setOrganizationName("DAVConsolidator")

    # Use a crisp system font.
    font = QFont("Segoe UI", 10)
    app.setFont(font)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())