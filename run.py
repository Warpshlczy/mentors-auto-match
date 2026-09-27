"""入口：python run.py"""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from app.gui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("导师匹配助手")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
