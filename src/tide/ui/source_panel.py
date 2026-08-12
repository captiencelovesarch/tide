"""Source panel — the [source] nav view.

Lists every registered MusicSource as a row. Each row shows: status dot
(connected / not configured / disabled), name, brief status string, a
gear button for per-source settings, an enable checkbox, and a "make
active" radio. Disabled sources fade. Disabled-on-this-version sources
(spotify, apple — v1.2.1, v1.2.2) are shown as ghost placeholders.

Switching the active source emits ``active_changed`` on the registry, which
the window listens to so Search / Library / Explore retarget.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..settings import Settings
from ..sources import MusicSource, NotSupportedError, registry as source_registry
from ..theming import styled_case


class _StatusDot(QFrame):
    """Tiny coloured dot. Theme-tinted via objectName for QSS hooks."""

    SIZE = 10

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._state = "ok"   # "ok" | "warn" | "off"

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # Resolve color from object name → palette via theme overrides.
        colors = {
            "ok": "#5aaf6a",
            "warn": "#d4b95e",
            "off": "#555",
        }
        from PySide6.QtGui import QColor
        c = QColor(colors.get(self._state, "#555"))
        p.setBrush(c)
        p.setPen(Qt.NoPen)
        r = self.rect().adjusted(0, 0, -1, -1)
        p.drawEllipse(r)


class _SourceRow(QFrame):
    """One row in the Source panel."""

    enable_toggled = Signal(str, bool)       # slug, enabled
    activate_requested = Signal(str)         # slug
    gear_clicked = Signal(str)               # slug

    def __init__(self, source: MusicSource, *, enabled: bool, is_active: bool,
                 parent=None) -> None:
        super().__init__(parent)
        self.source = source
        self.slug = source.slug
        self.setObjectName("sourceRow")
        self.setFrameShape(QFrame.NoFrame)

        self.dot = _StatusDot()
        self.name_label = QLabel(styled_case(source.name))
        self.name_label.setObjectName("sourceName")
        self.status_label = QLabel(source.status_text())
        self.status_label.setObjectName("sourceStatus")
        self.status_label.setMinimumWidth(220)

        self.gear_btn = QPushButton(styled_case("[settings]"))
        self.gear_btn.setObjectName("sourceGear")
        self.gear_btn.setFlat(False)
        self.gear_btn.setMinimumWidth(96)
        self.gear_btn.setCursor(Qt.PointingHandCursor)
        self.gear_btn.clicked.connect(lambda: self.gear_clicked.emit(self.slug))

        self.enable_box = QCheckBox(styled_case("enabled"))
        self.enable_box.setChecked(enabled)
        self.enable_box.toggled.connect(
            lambda checked: self.enable_toggled.emit(self.slug, checked)
        )

        self.active_radio = QRadioButton(styled_case("make active"))
        self.active_radio.setChecked(is_active)
        self.active_radio.toggled.connect(self._on_radio)

        # Two-row layout: top row = dot + name + status + gear,
        # bottom row = enabled + make active.
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(self.dot, alignment=Qt.AlignVCenter)
        top.addWidget(self.name_label)
        top.addWidget(self.status_label, stretch=1)
        top.addWidget(self.gear_btn)

        bot = QHBoxLayout()
        bot.setContentsMargins(20, 0, 0, 0)
        bot.setSpacing(20)
        bot.addWidget(self.enable_box)
        bot.addWidget(self.active_radio)
        bot.addStretch(1)

        col = QVBoxLayout()
        col.setContentsMargins(12, 10, 12, 12)
        col.setSpacing(4)
        col.addLayout(top)
        col.addLayout(bot)
        self.setLayout(col)
        self._refresh_dot(enabled)

    def _on_radio(self, checked: bool) -> None:
        if checked:
            self.activate_requested.emit(self.slug)

    def set_enabled_state(self, enabled: bool) -> None:
        self.enable_box.blockSignals(True)
        self.enable_box.setChecked(enabled)
        self.enable_box.blockSignals(False)
        self._refresh_dot(enabled)

    def set_active_state(self, is_active: bool) -> None:
        self.active_radio.blockSignals(True)
        self.active_radio.setChecked(is_active)
        self.active_radio.blockSignals(False)

    def refresh_status(self) -> None:
        self.status_label.setText(self.source.status_text())

    def _refresh_dot(self, enabled: bool) -> None:
        if not enabled:
            self.dot.set_state("off")
        elif self.source.is_authenticated():
            self.dot.set_state("ok")
        else:
            self.dot.set_state("warn")


class _GenericSourceDialog(QDialog):
    """Per-source [⚙] sub-dialog for sources that don't have richer config.

    Shows: name, status, declared capabilities, and a sign-out button for
    sources that needs_auth. Mostly a confirmation that the gear works and
    a place to surface auth state.
    """

    sign_out_requested = Signal(str)   # slug
    sign_in_requested = Signal(str)    # slug

    def __init__(self, source, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(source.name)
        self._source = source

        heading = QLabel(styled_case(source.name))
        heading.setObjectName("sectionHeading")

        status_lbl = QLabel(styled_case(source.status_text()))
        status_lbl.setObjectName("sourceStatus")
        status_lbl.setWordWrap(True)

        caps = sorted(source.capabilities) if source.capabilities else ["search only"]
        caps_lbl = QLabel(styled_case("capabilities · " + ", ".join(caps)))
        caps_lbl.setObjectName("sourceStatus")
        caps_lbl.setWordWrap(True)

        col = QVBoxLayout()
        col.setContentsMargins(20, 18, 20, 18)
        col.setSpacing(10)
        col.addWidget(heading)
        col.addWidget(status_lbl)
        col.addWidget(caps_lbl)

        if source.needs_auth and source.is_authenticated():
            signout_btn = QPushButton(styled_case("[sign out]"))
            signout_btn.clicked.connect(self._on_signout)
            col.addWidget(signout_btn)
        elif source.needs_auth and getattr(source, "supports_in_app_auth", False):
            # Signed out (or never signed in) but able to re-import in-app.
            # Without this branch the dialog showed no button at all, so a
            # sign-out left the source permanently unusable until restart.
            signin_btn = QPushButton(styled_case("[sign in]"))
            signin_btn.clicked.connect(self._on_signin)
            col.addWidget(signin_btn)
        elif not source.needs_auth:
            note = QLabel(styled_case("public catalog — nothing to configure"))
            note.setObjectName("sourceStatus")
            col.addWidget(note)

        col.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        col.addWidget(buttons)
        self.setLayout(col)
        self.resize(380, 240)

    def _on_signout(self) -> None:
        self.sign_out_requested.emit(self._source.slug)
        self.accept()

    def _on_signin(self) -> None:
        self.sign_in_requested.emit(self._source.slug)
        self.accept()


class _LocalGearDialog(QDialog):
    """Sub-dialog launched by the [⚙] gear on the Local source row.

    Lets the user pick a different music directory and trigger a manual
    rescan. Closes immediately on Done; rescans run in the background.
    """

    dir_changed = Signal(str)
    rescan_requested = Signal()

    def __init__(self, local_source, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("local files")
        self._local = local_source

        heading = QLabel(styled_case("local files"))
        heading.setObjectName("sectionHeading")

        self.dir_label = QLabel(local_source.music_dir)
        self.dir_label.setObjectName("sourceStatus")
        self.dir_label.setWordWrap(True)

        pick_btn = QPushButton(styled_case("[change directory]"))
        pick_btn.clicked.connect(self._pick_dir)

        self.count_label = QLabel(styled_case(f"{local_source.track_count():,} tracks indexed"))
        self.count_label.setObjectName("sourceStatus")

        rescan_btn = QPushButton(styled_case("[rescan now]"))
        rescan_btn.clicked.connect(self._on_rescan)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)

        col = QVBoxLayout()
        col.setContentsMargins(20, 18, 20, 18)
        col.setSpacing(10)
        col.addWidget(heading)
        col.addWidget(QLabel(styled_case("directory")))
        col.addWidget(self.dir_label)
        col.addWidget(pick_btn)
        col.addSpacing(6)
        col.addWidget(self.count_label)
        col.addWidget(rescan_btn)
        col.addStretch(1)
        col.addWidget(buttons)
        self.setLayout(col)
        self.resize(420, 280)

    def _pick_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "pick music directory", self._local.music_dir
        )
        if not chosen:
            return
        self.dir_label.setText(chosen)
        self.dir_changed.emit(chosen)

    def _on_rescan(self) -> None:
        self.count_label.setText(styled_case("indexing…"))
        self.rescan_requested.emit()


class _PlaceholderRow(QFrame):
    """Greyed-out row for sources that ship later in v1.2."""

    def __init__(self, label: str, version_note: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("sourcePlaceholder")
        self.setEnabled(False)
        dot = _StatusDot()
        dot.set_state("off")
        name = QLabel(styled_case(label))
        name.setObjectName("sourceName")
        note = QLabel(styled_case(version_note))
        note.setObjectName("sourceStatus")
        row = QHBoxLayout()
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(8)
        row.addWidget(dot, alignment=Qt.AlignVCenter)
        row.addWidget(name)
        row.addStretch(1)
        row.addWidget(note)
        self.setLayout(row)


class SourcePanel(QWidget):
    """The [source] view. Reads/writes settings, drives the registry."""

    active_changed = Signal(str)
    enabled_changed = Signal(str, bool)
    settings_changed = Signal()              # ask host to persist + relayout
    local_dir_changed = Signal(str)          # for LocalSource rescan trigger
    # Emitted from pool threads when a background probe finishes; the queued
    # delivery marshals the row repaint onto the GUI thread. (The previous
    # QTimer.singleShot-from-a-pool-thread approach silently never fires for
    # plain callables — no event loop on QThreadPool threads.)
    _probe_done = Signal(str)                # slug

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._rows: dict[str, _SourceRow] = {}
        self._active_group = QButtonGroup(self)
        self._active_group.setExclusive(True)
        self._probe_done.connect(self._on_probe_done)

        heading = QLabel(styled_case("source"))
        heading.setObjectName("sectionHeading")
        sub = QLabel(styled_case(
            "the active source drives search, library, and explore. "
            "enable any to mix into the queue."
        ))
        sub.setObjectName("sectionSub")
        sub.setWordWrap(True)

        # Federated search toggle.
        self.federate_box = QCheckBox(styled_case("federated search (all enabled sources)"))
        self.federate_box.setChecked(bool(settings.federated_search))
        self.federate_box.toggled.connect(self._on_federate_toggle)

        rows_col = QVBoxLayout()
        rows_col.setContentsMargins(0, 0, 0, 0)
        rows_col.setSpacing(8)

        reg = source_registry()
        for source in reg.all():
            enabled = reg.is_enabled(source.slug)
            is_active = reg.active_slug == source.slug
            row = _SourceRow(source, enabled=enabled, is_active=is_active)
            self._rows[source.slug] = row
            self._active_group.addButton(row.active_radio)
            row.enable_toggled.connect(self._on_row_enable)
            row.activate_requested.connect(self._on_row_activate)
            row.gear_clicked.connect(self._on_row_gear)
            rows_col.addWidget(row)

        # Future-source placeholder. Spotify used to be a placeholder
        # here too — in v1.2.1 it became a real (registered, but shelved)
        # source so the row above renders it directly via _SourceRow.
        rows_col.addWidget(_PlaceholderRow("apple music", "shelved"))

        rows_wrap = QWidget()
        rows_wrap.setLayout(rows_col)
        scroll = QScrollArea()
        scroll.setWidget(rows_wrap)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        col = QVBoxLayout()
        col.setContentsMargins(28, 24, 28, 24)
        col.setSpacing(12)
        col.addWidget(heading)
        col.addWidget(sub)
        col.addWidget(self.federate_box)
        col.addWidget(scroll, stretch=1)
        self.setLayout(col)
        # Fire deferred reachability probes for sources whose
        # status depends on a network round-trip (currently Subsonic). The
        # source's is_authenticated() is now cheap so the row mounts
        # instantly with the dot in "unknown" state; the probe runs on a
        # background worker and we refresh statuses once it lands.
        QTimer.singleShot(0, self._probe_async_sources)

    # ---------- slots ----------

    def _ui_sound(self, key: str) -> None:
        """Walk up the parent chain looking for the top-level window's
        UiSoundPlayer. Silent no-op if it isn't there yet (e.g. during
        construction before app.py attaches it, or in tests)."""
        w = self.window()
        player = getattr(w, "ui_sounds", None) if w is not None else None
        if player is not None:
            try:
                player.play(key)
            except Exception:
                pass

    def _on_row_enable(self, slug: str, enabled: bool) -> None:
        self._ui_sound("toggle_on" if enabled else "toggle_off")
        # Any path that opens a modal (Spotify confirm, Subsonic connect form)
        # MUST defer past this toggled emission — opening a QDialog inside a
        # QCheckBox's signal segfaults on PySide6 + py3.14 (the crash the
        # onboarding code already guards against). The checkbox is visually ON
        # for one event-loop tick; the deferred gate rolls it back if the user
        # declines. See [[feedback-pyside-modal]].
        if slug == "spotify" and enabled:
            QTimer.singleShot(0, lambda: self._gate_enable(slug, self._confirm_spotify))
            return
        # Subsonic needs a server URL + credentials before it's useful; toggling
        # it on with empty config opens the connect dialog.
        if slug == "subsonic" and enabled and not self._subsonic_configured():
            QTimer.singleShot(0, lambda: self._gate_enable(slug, self._run_subsonic_setup))
            return
        self._commit_enable(slug, enabled)

    def _confirm_spotify(self) -> bool:
        from .spotify_signin import confirm_spotify_enable
        return bool(confirm_spotify_enable(self))

    def _gate_enable(self, slug: str, gate) -> None:
        """Run a (possibly modal) confirmation ``gate`` for enabling ``slug``,
        deferred out of the originating click/toggle. Commit on success; roll
        the row's checkbox back to off on cancel."""
        if gate():
            self._commit_enable(slug, True)
        else:
            row = self._rows.get(slug)
            if row is not None:
                row.set_enabled_state(False)

    def _commit_enable(self, slug: str, enabled: bool) -> None:
        reg = source_registry()
        reg.set_enabled(slug, enabled)
        self._settings.sources_enabled[slug] = enabled
        row = self._rows.get(slug)
        if row is not None:
            row.set_enabled_state(enabled)
        self.enabled_changed.emit(slug, enabled)
        self.settings_changed.emit()

    def _on_row_activate(self, slug: str) -> None:
        reg = source_registry()
        # Make-active also enables; gate Spotify/Subsonic behind their setup
        # modals. Same defer-out-of-the-toggle rule as _on_row_enable — the
        # radio is visually selected for one tick and rolled back on cancel.
        if slug == "spotify" and not reg.is_enabled(slug):
            QTimer.singleShot(0, lambda: self._gate_activate(slug, self._confirm_spotify))
            return
        # Same gate for Subsonic — make-active implies enable, but if
        # the source isn't configured there's nothing to make active.
        if slug == "subsonic" and not self._subsonic_configured():
            QTimer.singleShot(0, lambda: self._gate_activate(slug, self._run_subsonic_setup))
            return
        self._commit_activate(slug)

    def _gate_activate(self, slug: str, gate) -> None:
        """Deferred (possibly modal) gate for making ``slug`` active; roll the
        row's radio back on cancel."""
        if gate():
            self._commit_activate(slug)
        else:
            row = self._rows.get(slug)
            if row is not None:
                row.set_active_state(False)

    def _commit_activate(self, slug: str) -> None:
        reg = source_registry()
        if not reg.is_enabled(slug):
            reg.set_enabled(slug, True)
            self._settings.sources_enabled[slug] = True
            row = self._rows.get(slug)
            if row is not None:
                row.set_enabled_state(True)
        reg.set_active(slug)
        self._settings.active_source = slug
        for r in self._rows.values():
            r.set_active_state(r.slug == slug)
        self.active_changed.emit(slug)
        self.settings_changed.emit()

    def _on_federate_toggle(self, checked: bool) -> None:
        self._ui_sound("toggle_on" if checked else "toggle_off")
        self._settings.federated_search = checked
        self.settings_changed.emit()

    def _on_row_gear(self, slug: str) -> None:
        # Defer the modal past the gear button's click emission (PySide6 +
        # py3.14 segfault guard, see [[feedback-pyside-modal]]).
        QTimer.singleShot(0, lambda: self._do_row_gear(slug))

    def _do_row_gear(self, slug: str) -> None:
        if slug == "local":
            self._configure_local()
            return
        if slug == "subsonic":
            self._run_subsonic_setup()
            return
        reg = source_registry()
        source = reg.get(slug)
        if source is None:
            return
        dlg = _GenericSourceDialog(source, self)
        dlg.sign_out_requested.connect(self._sign_out_source)
        dlg.sign_in_requested.connect(self._sign_in_source)
        try:
            dlg.exec()
        finally:
            dlg.deleteLater()

    def _sign_out_source(self, slug: str) -> None:
        reg = source_registry()
        source = reg.get(slug)
        if source is None:
            return
        try:
            source.sign_out()
        except Exception:
            pass
        row = self._rows.get(slug)
        if row is not None:
            row.refresh_status()
            row._refresh_dot(reg.is_enabled(slug))

    def _sign_in_source(self, slug: str) -> None:
        """Run the source's in-app auth (the import wizard for YT Music) and,
        on success, re-enable + activate it so it's usable immediately. This
        is the recovery path that was missing — signing out used to be a
        one-way trip with no way back in short of restarting tide.

        Deferred: this fires from the gear dialog's [sign in] button emission,
        so opening the wizard inline would be a nested modal-from-click. The
        singleShot lets the gear dialog finish closing first."""
        QTimer.singleShot(0, lambda: self.reauth_source(slug))

    def reauth_source(self, slug: str) -> bool:
        """Public re-auth entry point: run ``slug``'s in-app sign-in flow and
        rewire enable/persist/row state on success. Shared by the gear
        dialog's [sign in] button and the session-expired toast action.
        Returns True iff auth now exists."""
        reg = source_registry()
        source = reg.get(slug)
        if source is None:
            return False
        try:
            ok = bool(source.begin_auth(self))
        except NotSupportedError:
            # Bespoke-flow sources (Spotify OAuth, Subsonic server form)
            # sign in from their gear dialog rather than a generic
            # begin_auth — open that instead so the session-expired
            # toast's [sign in] still lands somewhere useful.
            self._do_row_gear(slug)
            ok = bool(source.is_authenticated())
        except Exception:
            ok = False
        if ok:
            reg.set_enabled(slug, True)
            self._settings.sources_enabled[slug] = True
            self.settings_changed.emit()
        row = self._rows.get(slug)
        if row is not None:
            row.refresh_status()
            row.set_enabled_state(reg.is_enabled(slug))
            row._refresh_dot(reg.is_enabled(slug))
        return ok

    # ---------- subsonic ----------

    def _probe_async_sources(self) -> None:
        """Kick off non-blocking probes for sources whose status line
        depends on a network call — Subsonic's ping and Spotify's /me
        profile — so status_text() itself stays free of blocking I/O on
        the GUI thread."""
        reg = source_registry()
        jobs: list[tuple[str, object]] = []

        sub = reg.get("subsonic")
        if sub is not None and self._subsonic_configured():
            jobs.append(("subsonic", sub))
        # Only probe Spotify when it's actually enabled: the probe's /me
        # fetch refreshes the stored token, and a revoked grant then raised
        # the "spotify: token expired" toast at every launch — even for
        # users who had the source switched off.
        sp = reg.get("spotify")
        if sp is not None and hasattr(sp, "probe") and reg.is_enabled("spotify"):
            try:
                if sp.is_authenticated():
                    jobs.append(("spotify", sp))
            except Exception:
                pass
        if not jobs:
            return

        panel = self

        class _Job(QRunnable):
            def __init__(self_inner, slug: str, source) -> None:
                super().__init__()
                self_inner.slug = slug
                self_inner.source = source

            def run(self_inner):
                try:
                    self_inner.source.probe()
                except Exception:
                    pass
                # Queued signal — the only reliable way to reach the GUI
                # thread from a pool thread.
                panel._probe_done.emit(self_inner.slug)

        for slug, source in jobs:
            QThreadPool.globalInstance().start(_Job(slug, source))

    def _on_probe_done(self, slug: str) -> None:
        self.refresh_statuses()
        self._refresh_dot_for(slug)

    def _refresh_dot_for(self, slug: str) -> None:
        row = self._rows.get(slug)
        if row is not None:
            row._refresh_dot(source_registry().is_enabled(slug))

    def _subsonic_configured(self) -> bool:
        s = self._settings
        return bool(s.subsonic_url and s.subsonic_user and s.subsonic_pass)

    def _run_subsonic_setup(self) -> bool:
        """Open the Subsonic connect dialog seeded with the current
        settings. On save, persist + reconfigure the live source. Returns
        True iff the user accepted with a complete config.

        Deferred via QTimer.singleShot per the PySide6 + Python 3.14
        modal-from-click crash pattern documented elsewhere in this file
        (and observed first on the onboarding wizard) — but here we're
        invoked from a button click higher up the stack, not directly
        from a checkbox toggled emission, so we exec() inline and rely on
        the explicit deleteLater() to drain the dialog's QWidget tree.
        """
        from .subsonic_signin import SubsonicSignInDialog
        from ..sources.subsonic import SubsonicConfig, SubsonicSource

        s = self._settings
        seed = SubsonicConfig(
            url=s.subsonic_url, user=s.subsonic_user,
            password=s.subsonic_pass,
            auth_style=s.subsonic_auth_style or "salt",
        )
        dlg = SubsonicSignInDialog(self, initial=seed)
        try:
            accepted = dlg.exec() == QDialog.DialogCode.Accepted
            cfg = dlg.result_config() if accepted else None
        finally:
            dlg.deleteLater()
        if not accepted or cfg is None or not cfg.is_complete():
            return False

        # Persist the entered credentials.
        s.subsonic_url = cfg.url
        s.subsonic_user = cfg.user
        s.subsonic_pass = cfg.password
        s.subsonic_auth_style = cfg.auth_style

        reg = source_registry()
        source = reg.get("subsonic")
        if isinstance(source, SubsonicSource):
            source.set_config(cfg)
        else:
            # No source registered yet (e.g. app booted with empty config
            # and never instantiated one) — wire one up now.
            reg.register(SubsonicSource(cfg), enabled=False)

        row = self._rows.get("subsonic")
        if row is not None:
            row.refresh_status()
            row._refresh_dot(reg.is_enabled("subsonic"))
        self.settings_changed.emit()
        # Kick off a fresh probe so the dot + status line settle to the
        # post-save state without waiting for the next panel refresh.
        self._probe_async_sources()
        return True

    # ---------- local ----------

    def _configure_local(self) -> None:
        reg = source_registry()
        local = reg.get("local")
        if local is None:
            return
        dlg = _LocalGearDialog(local, self)
        dlg.dir_changed.connect(self._apply_new_local_dir)
        dlg.rescan_requested.connect(self._rescan_local)
        dlg.exec()

    def _apply_new_local_dir(self, new_dir: str) -> None:
        reg = source_registry()
        local = reg.get("local")
        if local is None:
            return
        self._settings.local_music_dir = new_dir
        local.set_music_dir(new_dir)
        self.settings_changed.emit()
        self.local_dir_changed.emit(new_dir)
        row = self._rows.get("local")
        if row is not None:
            row.status_label.setText(styled_case(f"{new_dir} · indexing…"))

    def _rescan_local(self) -> None:
        reg = source_registry()
        local = reg.get("local")
        if local is None:
            return

        panel = self
        row = self._rows.get("local")
        if row is not None:
            row.status_label.setText(styled_case(f"{local.music_dir} · indexing…"))

        class _Job(QRunnable):
            def run(self_inner):
                try:
                    local.rescan()
                except Exception:
                    pass

        QThreadPool.globalInstance().start(_Job())
        QTimer.singleShot(1500, panel.refresh_statuses)
        QTimer.singleShot(5000, panel.refresh_statuses)

    def refresh_statuses(self) -> None:
        for row in self._rows.values():
            row.refresh_status()

    def update_local_status_from_index(self) -> None:
        """Force a status refresh — useful after a rescan completes."""
        row = self._rows.get("local")
        if row is not None:
            row.refresh_status()

    def bind_settings(self, settings: Settings) -> None:
        """Adopt a different Settings instance after construction.

        ``MainWindow`` instantiates the panel before ``app.py`` attaches the
        real Settings, so we re-bind once the real one is available.
        """
        self._settings = settings
        # Re-sync the federated toggle to the bound settings state.
        self.federate_box.blockSignals(True)
        self.federate_box.setChecked(bool(settings.federated_search))
        self.federate_box.blockSignals(False)
        # The pre-bind probe ran against the placeholder Settings (empty
        # subsonic creds) so it short-circuited. Now that the real
        # settings are in, schedule a fresh probe for the real config.
        QTimer.singleShot(0, self._probe_async_sources)
