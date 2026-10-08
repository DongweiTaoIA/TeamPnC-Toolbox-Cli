from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEngineDownloadRequest, QWebEngineProfile

SIGN_IN_HOSTS = {"login.microsoftonline.com", "login.live.com", "login.windows.net"}


class SharePointDownloader(QObject):
    """Downloads a file through the embedded browser session, reusing its SharePoint sign-in."""

    finished = Signal(str)
    failed = Signal(str)
    sign_in_required = Signal()

    def __init__(self, profile: QWebEngineProfile, timeout_ms: int = 60_000, parent: QObject | None = None):
        super().__init__(parent)
        self._target = Path()
        self._active = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(timeout_ms)
        self._timer.timeout.connect(self._on_timeout)
        profile.downloadRequested.connect(self._on_download)

    def start(self, target: Path) -> None:
        self._target = target
        self._active = True
        self._timer.start()

    def watch_url(self, url: QUrl) -> None:
        if self._active and url.host().lower() in SIGN_IN_HOSTS:
            self._timer.stop()
            self.sign_in_required.emit()

    def _on_download(self, request: QWebEngineDownloadRequest) -> None:
        if not self._active:
            # Downloads the user starts in the browser go to the default Downloads folder.
            request.accept()
            return
        self._timer.stop()
        folder = self._target.parent / "downloads"
        folder.mkdir(parents=True, exist_ok=True)
        request.setDownloadDirectory(str(folder))
        request.setDownloadFileName(f"download-{time.time_ns()}{self._target.suffix}")
        request.isFinishedChanged.connect(lambda: self._on_finished(request))
        request.accept()

    def _on_finished(self, request: QWebEngineDownloadRequest) -> None:
        if not request.isFinished() or not self._active:
            return
        self._active = False
        if request.state() == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
            source = Path(request.downloadDirectory()) / request.downloadFileName()
            os.replace(source, self._target)
            self.finished.emit(str(self._target))
        else:
            self.failed.emit(request.interruptReasonString() or "The download was interrupted.")

    def _on_timeout(self) -> None:
        if self._active:
            self._active = False
            self.failed.emit("Timed out downloading from SharePoint.")
