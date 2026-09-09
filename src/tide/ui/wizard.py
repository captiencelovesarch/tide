"""First-run sign-in wizard.

Google blocks credential entry inside embedded webviews and rejects
third-party OAuth against the YouTube Music endpoints, so sign-in imports
cookies from the user's real (trusted) browser. They sign in to YouTube
Music in chromium, chrome, brave, vivaldi, edge, opera or firefox like
normal, then click "import" in tide. No config files. When tide can't read
a browser on its own, a headers paste (devtools → copy request headers)
covers it. The automatic reading is yt-dlp's cookie loader; see
tide.browser_import.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .. import auth, browser_import as bi, config, qthreads


YT_MUSIC_URL = "https://music.youtube.com/"


# The import thread must NOT be parented to the dialog: callers destroy the
# dialog right after exec() returns, and destroying a QThread whose OS thread
# is still busy copying/decrypting browser cookie DBs is a Qt fatal abort.
# tide.qthreads holds the (thread, worker) pair instead, until the thread is
# destroyed. Late done/failed emissions into an already-destroyed dialog are
# auto-disconnected by Qt and dropped.


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


class _RefreshWorker(QObject):
    """Silent re-import: no dialog, no browser round-trip, no user steps."""
    done = Signal(str)      # profile label, or "" when no live session found
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.done.emit(auth.refresh_from_browser() or "")
        except Exception as exc:
            self.failed.emit(str(exc))


def refresh_token_async(on_done, on_failed=None) -> QThread:
    """Kick off a background token refresh. ``on_done`` receives the browser
    profile label on success, or "" when no browser held a live session.

    Both callbacks MUST be bound methods, not lambdas — a lambda connected to
    a worker signal runs in the *emitting* thread, which is exactly how you
    get GUI calls off the GUI thread in this codebase.
    """
    thread = QThread()      # unparented — must outlive whatever started it
    worker = _RefreshWorker()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.done.connect(on_done)
    if on_failed is not None:
        worker.failed.connect(on_failed)
    worker.done.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.finished.connect(thread.deleteLater)
    qthreads.retain(thread, worker)
    thread.start()
    return thread


class _PasteHeadersDialog(QDialog):
    """Manual fallback: the user pastes request headers copied from devtools.

    Parsing lives in ``auth.parse_pasted_headers``; this is only the text box.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("tide — paste headers")
        self.setModal(True)
        self.setMinimumWidth(560)

        info = QLabel(
            "sign in at music.youtube.com, open devtools (f12) → network, click "
            "any request to music.youtube.com, then copy → copy request headers. "
            "paste them below."
        )
        info.setWordWrap(True)

        self._edit = QPlainTextEdit()
        self._edit.setPlaceholderText("paste request headers here")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 16)
        layout.setSpacing(12)
        layout.addWidget(info)
        layout.addWidget(self._edit, stretch=1)
        layout.addWidget(buttons)

    def text(self) -> str:
        return self._edit.toPlainText()


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
        if not self._profiles and config.in_flatpak():
            # The sandbox can't see browser profiles unless the user opens
            # it; the readme's flatpak section has the override command.
            self._status.setText(
                "the flatpak can't read your browser's profile. paste headers "
                "instead, or let it see your browser with flatpak override "
                "(the readme has the command) and open this window again."
            )

        # Always available, even when no browser profile was found — that's
        # exactly the case the paste path exists for.
        self._paste_btn = QPushButton("paste headers instead")
        self._paste_btn.clicked.connect(self._on_paste)

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
        bottom.addWidget(self._paste_btn)
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
                "no supported browser profile found. sign in to music.youtube.com "
                "in chromium, chrome, brave, vivaldi, edge, opera or firefox, "
                "then run tide again."
            )

    # ---------- handlers ----------

    def _on_open(self) -> None:
        QDesktopServices.openUrl(QUrl(YT_MUSIC_URL))
        self._status.setText("opened music.youtube.com. sign in there, then click import.")

    def _on_import(self) -> None:
        profile: bi.BrowserProfile | None = self._picker.currentData()
        if profile is None:
            return
        self._import_btn.setEnabled(False)
        self._status.setText(f"reading cookies from {profile.label}…")

        thread = QThread()   # unparented: must be able to outlive the dialog
        worker = _ImportWorker(profile)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        qthreads.retain(thread, worker)
        self._import_thread = thread
        self._import_worker = worker
        thread.start()

    def _on_done(self, result: bi.ImportResult) -> None:
        if not result.looks_signed_in:
            if result.note:
                # Not a sign-in problem: the cookies were there and tide
                # could not read them. Telling them to sign in again would
                # send them in circles.
                msg = f"couldn't read {result.profile.label}'s cookies. {result.note}"
            else:
                msg = (
                    f"no youtube music session in {result.profile.label}. open "
                    f"music.youtube.com there, sign in, then click import again."
                )
            self._status.setText(msg)
            self._import_btn.setEnabled(True)
            return
        try:
            auth.save_browser_auth(result.cookies, expires_at=result.expires_at)
        except Exception as exc:
            self._status.setText(f"couldn't save session: {exc}")
            self._import_btn.setEnabled(True)
            return
        self._status.setText("signed in. opening tide…")
        self.accept()

    def _on_failed(self, msg: str) -> None:
        self._status.setText(f"import failed: {msg}")
        self._import_btn.setEnabled(True)

    # ---------- manual paste fallback ----------

    def _on_paste(self) -> None:
        dlg = _PasteHeadersDialog(self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        self._import_pasted(dlg.text())

    def _import_pasted(self, raw: str) -> None:
        """Parse pasted headers and save the session. A paste carries no
        cookie expiry (headers don't), so the recorded expiry is unknown —
        which the countdown treats as 'no data', never as 'expired'."""
        try:
            cookies, user_agent = auth.parse_pasted_headers(raw)
        except ValueError as exc:
            self._status.setText(str(exc))
            return
        try:
            auth.save_browser_auth(cookies, user_agent=user_agent, expires_at=None)
        except Exception as exc:
            self._status.setText(f"couldn't save session: {exc}")
            return
        self._status.setText("signed in from pasted headers. opening tide…")
        self.accept()
