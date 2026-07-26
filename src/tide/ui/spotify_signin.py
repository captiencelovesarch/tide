"""Spotify OAuth sign-in dialog.

Mirrors the YT Music ``SignInDialog`` pattern: a modal with a single
hero button. Clicking [connect] opens the user's default browser to
Spotify's authorize URL and starts a 127.0.0.1 loopback HTTP server to
catch the redirect. Returning code is exchanged for tokens which we
persist (AES-encrypted via ``auth_spotify``).

The blocking callback wait happens on a background thread so the dialog
stays responsive (user can hit cancel mid-flow).
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import auth_spotify, qthreads


# ---------- shelved-state confirmation ----------


def confirm_spotify_enable(parent: QWidget | None = None) -> bool:
    """Surface the broken-shipped-state warning before letting a user
    enable the Spotify source. Returns True iff they confirm anyway.

    Called from both the onboarding wizard (when the user toggles the
    Spotify card on) and the in-app source panel (when they flip the
    enable checkbox). Keeps the explanation in one place so the wording
    stays consistent.
    """
    dlg = QMessageBox(parent)
    dlg.setIcon(QMessageBox.Warning)
    dlg.setWindowTitle("spotify — shelved")
    dlg.setText("are you sure you want to enable spotify?")
    dlg.setInformativeText(
        "spotify ships with tide, but it's currently broken upstream.\n\n"
        "on 2026-02-06 spotify rolled out a platform-security update that "
        "closed librespot's audio-decryption path. every track plays as "
        "silence, regardless of how you authenticate. there is no "
        "client-side workaround. spotify will need to reopen the audio "
        "key endpoint OR librespot upstream will need a fix that "
        "satisfies the new policy — neither is on a known timeline.\n\n"
        "what still works:\n"
        "  · search (capped at 10 results per query, dev-mode cap)\n"
        "  · browsing your library + playlists\n"
        "  · tide showing up as a Spotify Connect device\n\n"
        "what does NOT work:\n"
        "  · playing any audio. clicking play sits silently.\n\n"
        "enable anyway?"
    )
    dlg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    dlg.setDefaultButton(QMessageBox.No)
    return dlg.exec() == QMessageBox.Yes


# The OAuth thread must NOT be parented to the dialog: callers destroy the
# dialog right after exec() returns, and destroying a QThread whose OS thread
# is still running is a Qt fatal abort. tide.qthreads holds the (thread,
# worker) pair instead, until the thread is destroyed. Late done/failed
# emissions into an already-destroyed dialog are auto-disconnected by Qt and
# dropped.


class _AuthWorker(QObject):
    done = Signal(object)        # SpotifyTokens
    failed = Signal(str)

    def __init__(self, flow: auth_spotify.AuthFlow) -> None:
        super().__init__()
        self.flow = flow

    def run(self) -> None:
        try:
            code = self.flow.run_callback(timeout_seconds=300.0)
            tokens = self.flow.exchange_code(code)
            auth_spotify.save_tokens(tokens)
            auth_spotify.set_cached(tokens)
            self.done.emit(tokens)
        except Exception as exc:
            self.failed.emit(str(exc))


class SpotifySignInDialog(QDialog):
    """Modal sign-in for Spotify. Runs the OAuth-PKCE flow end-to-end."""

    def __init__(self, parent=None, *, client_id_override: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("tide — connect spotify")
        self.setModal(True)
        self.setMinimumWidth(540)

        self._client_id_override = client_id_override
        self._thread: QThread | None = None
        self._worker: _AuthWorker | None = None
        self._flow: auth_spotify.AuthFlow | None = None

        self._headline = QLabel(
            "tide will open spotify in your browser. sign in there, accept "
            "the permissions, and tide picks up automatically."
        )
        self._headline.setWordWrap(True)

        self._step1 = QLabel("1.  click connect — tide opens spotify's sign-in page.")
        self._connect_btn = QPushButton("connect")
        self._connect_btn.clicked.connect(self._on_connect)

        self._step2 = QLabel("2.  approve the permissions in your browser.")
        self._step2.setWordWrap(True)

        self._step3 = QLabel("3.  switch back to tide. you're listening.")
        self._step3.setWordWrap(True)

        self._client_id_label = QLabel(
            "no shipped spotify app yet — paste a dev-app client_id below. "
            "see README · spotify for the 90-second setup."
        )
        self._client_id_label.setWordWrap(True)
        self._client_id_label.setStyleSheet("color: palette(mid);")
        self._client_id_edit = QLineEdit()
        self._client_id_edit.setPlaceholderText("client_id (from developer.spotify.com)")
        if client_id_override:
            self._client_id_edit.setText(client_id_override)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: palette(mid);")

        self._cancel = QPushButton("cancel")
        self._cancel.clicked.connect(self.reject)

        row1 = QHBoxLayout()
        row1.addWidget(self._step1, stretch=1)
        row1.addWidget(self._connect_btn)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self._cancel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 16)
        layout.setSpacing(14)
        layout.addWidget(self._headline)
        layout.addSpacing(4)
        layout.addLayout(row1)
        layout.addWidget(self._step2)
        layout.addWidget(self._step3)
        layout.addSpacing(8)
        # Hide the client-id row whenever tide-shipped default exists.
        # For now (no shipped id) it's always visible.
        if not auth_spotify.TIDE_DEFAULT_CLIENT_ID:
            layout.addWidget(self._client_id_label)
            layout.addWidget(self._client_id_edit)
        layout.addSpacing(4)
        layout.addWidget(self._status)
        layout.addStretch(1)
        layout.addLayout(bottom)

    # ---------- handlers ----------

    def _on_connect(self) -> None:
        client_id = self._client_id_edit.text().strip() or auth_spotify.effective_client_id()
        if not client_id:
            self._status.setText(
                "no client_id set. register a free spotify dev app and paste "
                "its client_id above, or set TIDE_SPOTIFY_CLIENT_ID env var."
            )
            return

        self._connect_btn.setEnabled(False)
        self._status.setText("opening browser…")

        flow = auth_spotify.AuthFlow(client_id=client_id)
        url = flow.authorize_url()
        self._flow = flow
        QDesktopServices.openUrl(QUrl(url))

        thread = QThread()   # unparented: must be able to outlive the dialog
        worker = _AuthWorker(flow)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        qthreads.retain(thread, worker)
        self._thread = thread
        self._worker = worker
        thread.start()
        self._status.setText(
            "waiting for you to approve in the browser…  (the page closes itself)"
        )

    def _on_done(self, _tokens) -> None:
        self._status.setText("connected. opening tide…")
        self.accept()

    def _on_failed(self, msg: str) -> None:
        self._status.setText(f"sign-in failed: {msg}")
        self._connect_btn.setEnabled(True)

    def done(self, result: int) -> None:
        # Every close path (cancel button, Esc, titlebar ✕, accept) funnels
        # through done(). If the OAuth thread is mid-wait on the loopback
        # server, cancel() wakes it so it raises and winds down promptly
        # instead of blocking for up to 300s; the qthreads registry keeps
        # the QThread safe to outlive the dialog either way. After a
        # successful flow cancel() is a no-op.
        if self._flow is not None:
            self._flow.cancel()
        super().done(result)
