"""
main.py — DAV Consolidator v4.0
Entry point for development and PyInstaller packaged executable.
"""
from __future__ import annotations
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("DAV Consolidator v4")
    except Exception:
        pass

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont
from gui.main_window import MainWindow

logger = logging.getLogger("dav.main")


def main() -> int:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication(sys.argv)
    app.setApplicationName("DAV Consolidator")
    app.setFont(QFont("Segoe UI", 10))
    w = MainWindow()
    w.show()
    logger.info("Started — PID %d", os.getpid())
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
