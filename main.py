#!/usr/bin/env python3
"""
Nova Player — Entry Point
Launch with: python main.py
"""

import sys
import os
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from nova_player.ui.main_window import MainWindow

def main():
    # High-DPI support
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

    app = QApplication(sys.argv)
    app.setApplicationName("Nova Player")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("RAHUL-DevelopeRR")

    # Load dark theme
    qss_path = os.path.join(os.path.dirname(__file__), "assets", "dark_theme.qss")
    if os.path.exists(qss_path):
        with open(qss_path, "r") as f:
            app.setStyleSheet(f.read())

    window = MainWindow()
    window.show()

    # Open file from CLI argument
    if len(sys.argv) > 1:
        window.open_file(sys.argv[1])

    sys.exit(app.exec())

if __name__ == "__main__":
    main()
