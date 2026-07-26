"""Subsonic / Navidrome connect dialog.

A small form modal: server url + username + password + auth-style pick.
[test] runs a `ping.view` against the entered credentials on a background
thread so the dialog stays responsive; [save] closes the dialog with the
config available via ``result_config()`` for the caller to persist.

Mirrors the SpotifySignInDialog shape so the wizard's _do_setup path
treats both sign-in flows the same way (modal exec + result accessor).
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from .. import qthreads
from ..sources.subsonic import SubsonicConfig, SubsonicSource


# The ping thread must NOT be parented to the dialog: callers destroy the
# dialog right after exec() returns, and destroying a QThread whose OS thread
# is still inside the (up to 10s) urllib round-trip is a Qt fatal abort.
# tide.qthreads holds the (thread, worker) pair instead, until the thread is
# destroyed. A late done emission into an already-destroyed dialog is
# auto-disconnected by Qt and dropped.


class _PingWorker(QObject):
    """Background ping. The SubsonicSource constructor is cheap (just
    config); calling is_authenticated() does the actual HTTP round-trip
    against the user's server. Keeping it off the GUI thread means the
    [test] button doesn't freeze the dialog while it waits."""

    done = Signal(bool, str)   # ok, status_text

    def __init__(self, config: SubsonicConfig) -> None:
        super().__init__()
        self._config = config

    def run(self) -> None:
        try:
            src = SubsonicSource(self._config)
            ok = src.probe()
            msg = src.status_text()
            self.done.emit(bool(ok), msg)
        except Exception as exc:
            self.done.emit(False, f"error: {exc}")


class SubsonicSignInDialog(QDialog):
    """Modal connect for Subsonic. ``exec()`` returns Accepted iff the user
    pressed [save] (with all fields populated). Caller reads result_config()
    afterward — there's no async OAuth dance, just three text fields."""

    def __init__(self, parent=None, *, initial: SubsonicConfig | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("tide — connect subsonic")
        self.setModal(True)
        self.setMinimumWidth(540)

        self._thread: QThread | None = None
        self._worker: _PingWorker | None = None
        self._result_config = SubsonicConfig()

        seed = initial or SubsonicConfig()

        headline = QLabel(
            "point tide at your subsonic-compatible server — navidrome, "
            "airsonic, gonic, funkwhale, or the reference subsonic server. "
            "tide streams audio straight from the server's url, no proxy."
        )
        headline.setWordWrap(True)

        # ---- fields ----
        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText("https://music.example.com  (or http://nas.local:4533)")
        self._url_edit.setText(seed.url)
        self._url_edit.textChanged.connect(self._refresh_buttons)

        self._user_edit = QLineEdit()
        self._user_edit.setPlaceholderText("username")
        self._user_edit.setText(seed.user)
        self._user_edit.textChanged.connect(self._refresh_buttons)

        self._pass_edit = QLineEdit()
        self._pass_edit.setPlaceholderText("password")
        self._pass_edit.setEchoMode(QLineEdit.Password)
        self._pass_edit.setText(seed.password)
        self._pass_edit.textChanged.connect(self._refresh_buttons)

        # auth style — salt is the safer default (md5(pass+salt) over HTTP);
        # plain sends password directly in the query string and is only
        # appropriate on HTTPS deployments that hash credentials at rest.
        self._style_group = QButtonGroup(self)
        self._salt_radio = QRadioButton("salt + token (safe over http)")
        self._plain_radio = QRadioButton("plain password (https only)")
        self._style_group.addButton(self._salt_radio)
        self._style_group.addButton(self._plain_radio)
        if (seed.auth_style or "salt").lower() == "plain":
            self._plain_radio.setChecked(True)
        else:
            self._salt_radio.setChecked(True)

        style_row = QHBoxLayout()
        style_row.setSpacing(14)
        style_row.addWidget(self._salt_radio)
        style_row.addWidget(self._plain_radio)
        style_row.addStretch(1)

        # ---- status + actions ----
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: palette(mid);")

        self._test_btn = QPushButton("test connection")
        self._test_btn.clicked.connect(self._on_test)
        self._save_btn = QPushButton("save")
        self._save_btn.clicked.connect(self._on_save)
        self._cancel_btn = QPushButton("cancel")
        self._cancel_btn.clicked.connect(self.reject)

        bottom = QHBoxLayout()
        bottom.addWidget(self._test_btn)
        bottom.addStretch(1)
        bottom.addWidget(self._cancel_btn)
        bottom.addWidget(self._save_btn)

        # ---- layout ----
        url_lbl = QLabel("server url")
        url_lbl.setStyleSheet("color: palette(mid);")
        user_lbl = QLabel("username")
        user_lbl.setStyleSheet("color: palette(mid);")
        pass_lbl = QLabel("password")
        pass_lbl.setStyleSheet("color: palette(mid);")
        style_lbl = QLabel("auth style")
        style_lbl.setStyleSheet("color: palette(mid);")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 16)
        layout.setSpacing(8)
        layout.addWidget(headline)
        layout.addSpacing(6)
        layout.addWidget(url_lbl)
        layout.addWidget(self._url_edit)
        layout.addSpacing(4)
        layout.addWidget(user_lbl)
        layout.addWidget(self._user_edit)
        layout.addSpacing(4)
        layout.addWidget(pass_lbl)
        layout.addWidget(self._pass_edit)
        layout.addSpacing(4)
        layout.addWidget(style_lbl)
        layout.addLayout(style_row)
        layout.addSpacing(8)
        layout.addWidget(self._status)
        layout.addStretch(1)
        layout.addLayout(bottom)

        self._refresh_buttons()

    # ---------- field state ----------

    def _current_config(self) -> SubsonicConfig:
        return SubsonicConfig(
            url=self._url_edit.text().strip(),
            user=self._user_edit.text().strip(),
            password=self._pass_edit.text(),
            auth_style="plain" if self._plain_radio.isChecked() else "salt",
        )

    def _refresh_buttons(self) -> None:
        complete = self._current_config().is_complete()
        self._test_btn.setEnabled(complete)
        self._save_btn.setEnabled(complete)

    # ---------- actions ----------

    def _on_test(self) -> None:
        cfg = self._current_config()
        if not cfg.is_complete():
            return
        self._test_btn.setEnabled(False)
        self._save_btn.setEnabled(False)
        self._status.setText("testing connection…")

        thread = QThread()   # unparented: must be able to outlive the dialog
        worker = _PingWorker(cfg)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_test_done)
        worker.done.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        qthreads.retain(thread, worker)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _on_test_done(self, ok: bool, msg: str) -> None:
        self._status.setText(("✓ " if ok else "✗ ") + msg)
        self._refresh_buttons()

    def _on_save(self) -> None:
        cfg = self._current_config()
        if not cfg.is_complete():
            return
        self._result_config = cfg
        self.accept()

    # ---------- public ----------

    def result_config(self) -> SubsonicConfig:
        return self._result_config
