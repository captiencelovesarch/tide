"""First-run sign-in wizard.

Google blocks credential entry inside embedded webviews, so the primary
path imports cookies from the user's real (trusted) browser. They sign
in to YouTube Music in chromium/chrome/brave like normal, then click
"import" in tide. No config files, no pastes.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from .. import auth, browser_import as bi


YT_MUSIC_URL = "https://music.youtube.com/"


class _ImportWorker(QObject):
    done = Signal(object)   # ImportResult
    failed = Signal(str)

    def __init__(self, profile: bi.BrowserProfile) -> None:
        super().__init__()
        self.profile = profile

    def run(self) -> None:
        try:
            self.done.emit(bi.import_cookies(self.profile))
        except Exception as exc:
            self.failed.emit(str(exc))


class SignInDialog(QDialog):
    """Modal sign-in. Imports cookies from a user-chosen browser."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("tide — sign in")
        self.setModal(True)
        self.setMinimumWidth(540)

        self._import_thread: QThread | None = None
        self._import_worker: _ImportWorker | None = None

        self._profiles = bi.available_profiles()

        self._headline = QLabel(
            "sign in to youtube music in your browser, then come back and click import."
        )
        self._headline.setWordWrap(True)

        self._step1 = QLabel("1.  open youtube music in your browser and sign in.")
        self._open_btn = QPushButton("open music.youtube.com")
        self._open_btn.clicked.connect(self._on_open)

        self._step2 = QLabel("2.  pick the browser you signed in with.")
        self._picker = QComboBox()
        if self._profiles:
            for p in self._profiles:
                self._picker.addItem(p.label, p)
        else:
            self._picker.addItem("(no supported browser found)")
            self._picker.setEnabled(False)

        self._step3 = QLabel("3.  import your session.")
        self._import_btn = QPushButton("import")
        self._import_btn.clicked.connect(self._on_import)
        self._import_btn.setEnabled(bool(self._profiles))

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: palette(mid);")

        self._cancel = QPushButton("cancel")
        self._cancel.clicked.connect(self.reject)

        row1 = QHBoxLayout()
        row1.addWidget(self._step1, stretch=1)
        row1.addWidget(self._open_btn)

        row2 = QHBoxLayout()
        row2.addWidget(self._step2, stretch=1)
        row2.addWidget(self._picker)

        row3 = QHBoxLayout()
        row3.addWidget(self._step3, stretch=1)
        row3.addWidget(self._import_btn)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self._cancel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 16)
        layout.setSpacing(14)
        layout.addWidget(self._headline)
        layout.addSpacing(4)
        layout.addLayout(row1)
        layout.addLayout(row2)
        layout.addLayout(row3)
        layout.addSpacing(4)
        layout.addWidget(self._status)
        layout.addStretch(1)
        layout.addLayout(bottom)

        if not self._profiles:
            self._status.setText(
                "no chromium-family browser profile found. install chromium, chrome, "
                "brave, vivaldi, or edge, sign in to music.youtube.com there, "
                "then run tide again."
            )

    # ---------- handlers ----------

    def _on_open(self) -> None:
        QDesktopServices.openUrl(QUrl(YT_MUSIC_URL))
        self._status.setText("opened music.youtube.com — sign in there, then click import.")

    def _on_import(self) -> None:
        profile: bi.BrowserProfile | None = self._picker.currentData()
        if profile is None:
            return
        self._import_btn.setEnabled(False)
        self._status.setText(f"reading cookies from {profile.label}…")

        thread = QThread(self)
        worker = _ImportWorker(profile)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._import_thread = thread
        self._import_worker = worker
        thread.start()

    def _on_done(self, result: bi.ImportResult) -> None:
        if not result.looks_signed_in:
            self._status.setText(
                f"no youtube music session in {result.profile.label}. open "
                f"music.youtube.com there, sign in, then click import again."
            )
            self._import_btn.setEnabled(True)
            return
        try:
            auth.save_browser_auth(result.cookies)
        except Exception as exc:
            self._status.setText(f"couldn't save session: {exc}")
            self._import_btn.setEnabled(True)
            return
        self._status.setText("signed in. opening tide…")
        self.accept()

    def _on_failed(self, msg: str) -> None:
        self._status.setText(f"import failed: {msg}")
        self._import_btn.setEnabled(True)
