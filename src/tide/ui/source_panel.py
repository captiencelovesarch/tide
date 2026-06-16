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
from ..sources import MusicSource, registry as source_registry
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

        self.gear_btn = QPushButton("[⚙]")
        self.gear_btn.setObjectName("sourceGear")
        self.gear_btn.setFlat(True)
        self.gear_btn.setFixedWidth(36)
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

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._rows: dict[str, _SourceRow] = {}
        self._active_group = QButtonGroup(self)
        self._active_group.setExclusive(True)

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

        # Future-source placeholders.
        rows_col.addWidget(_PlaceholderRow("spotify", "v1.2.1 — premium via librespot"))
        rows_col.addWidget(_PlaceholderRow("apple music", "v1.2.2 — musickit js"))

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

    # ---------- slots ----------

    def _on_row_enable(self, slug: str, enabled: bool) -> None:
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
        self._settings.federated_search = checked
        self.settings_changed.emit()

    def _on_row_gear(self, slug: str) -> None:
        if slug == "local":
            self._configure_local()
        # ytmusic / soundcloud / bandcamp / mixcloud have nothing to configure
        # at the per-source level in v1.2.0. Future versions can dispatch.

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
