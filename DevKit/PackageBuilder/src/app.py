from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv
from PySide6.QtWebEngineCore import qWebEngineChromiumVersion
from PySide6.QtWidgets import QApplication

from .config import DATA_DIR, ENV_FILE
from .main_window import MainWindow
from .theme import build_stylesheet

LOG_FILE = DATA_DIR / "pnc-migration.log"


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s.%(msecs)03d %(name)s %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"), logging.StreamHandler()],
    )
    logging.getLogger("pnc").info("app starting; Chromium %s; log at %s", qWebEngineChromiumVersion(), LOG_FILE)
    # Real environment variables take precedence over .env values.
    load_dotenv(ENV_FILE)
    # Teams preloads in a hidden pane; without these Chromium throttles it while it isn't shown.
    flags = "--disable-background-timer-throttling --disable-renderer-backgrounding " \
            "--disable-backgrounding-occluded-windows"
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')} {flags}".strip()
    app = QApplication(sys.argv)
    app.setApplicationName("PnC Migration Tool")
    app.setStyle("Fusion")
    app.setStyleSheet(build_stylesheet(DATA_DIR / "assets"))
    window = MainWindow()
    window.resize(1440, 920)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
