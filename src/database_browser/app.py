"""Application setup and saved connection directory selection."""

import argparse
import sys
from pathlib import Path

from PyQt6.QtCore import QStandardPaths
from PyQt6.QtWidgets import QApplication

from .controller import ConnectionController
from .ui import MainWindow


def main() -> int:
    """Create the controller and window, then start the Qt event loop."""
    parser = argparse.ArgumentParser(description="Database Browser for SQLite")
    parser.add_argument("--data-dir", type=Path, help="Override the directory for saved connection profiles")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setOrganizationName("DatabaseBrowser")
    app.setApplicationName("DatabaseBrowser")
    data_dir = args.data_dir or Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))
    controller = ConnectionController(data_dir / "settings.sqlite3")
    window = MainWindow(controller)
    window.show()
    controller.refresh()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
