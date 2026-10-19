"""The settings dialog's plugins tab: every plugin tide found, its on
switch, and its settings from plugin.toml. Like the rest of the dialog,
nothing applies until save, and then it applies live (no restart).

Plugin settings are declared in plugin.toml, so this tab shows them even
while a plugin is off: you can fill them in, then switch it on.
"""
from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
    QWidget,
)

from .. import config
from .headings import Heading

_ORIGINS = {"bundled": "comes with tide", "system": "installed", "yours": "yours"}


def _dim(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("class", "dim")
    label.setWordWrap(True)
    return label


class _PluginSection:
    """one plugin's rows: switch, status, settings."""

    def __init__(self, host, rec) -> None:
        self.host, self.rec = host, rec
        m = rec.manifest
        self.slug = m.slug
        self.widgets: dict[str, QWidget] = {}
        self.start_on = host.is_enabled(m.slug)

        self.heading = Heading(m.name, 34)
        self.blurb = _dim(m.description)
        self.status = _dim("")
        self.switch = QCheckBox("on")
        self.switch.setChecked(self.start_on)
        self.switch.toggled.connect(self._sync_enabled)

        self.form = QFormLayout()
        for d in m.settings:
            w = self._widget(d)
            self.widgets[d.key] = w
            row: QWidget | QHBoxLayout = w
            if d.help_url:
                row = QHBoxLayout()
                row.addWidget(w, 1)
                helper = QPushButton("get one  →")
                helper.clicked.connect(lambda _c=False, url=d.help_url:
                                       QDesktopServices.openUrl(QUrl(url)))
                row.addWidget(helper)
            if d.kind == "bool":
                self.form.addRow("", row)
            else:
                self.form.addRow(f"{d.label}:", row)

        self.extra = QHBoxLayout()
        if m.origin == "yours":
            reload_btn = QPushButton("reload")
            reload_btn.setToolTip("stop it, read its code again, start it")
            reload_btn.clicked.connect(lambda: host.reload(self.slug))
            self.extra.addWidget(reload_btn)
        self.extra.addStretch(1)

        self.refresh_status()
        self._sync_enabled()

    def items(self) -> list:
        return [self.heading, self.blurb, self.status, self.switch, self.form, self.extra]

    def _widget(self, d) -> QWidget:
        value = None if d.kind == "action" else self.rec.settings[d.key]
        if d.kind == "bool":
            w = QCheckBox(d.label)
            w.setChecked(bool(value))
        elif d.kind == "choice":
            w = QComboBox()
            rows = d.choices if isinstance(d.choices, tuple) else ((value, str(value)),)
            for v, label in rows:
                w.addItem(label, v)
            idx = w.findData(value)
            w.setCurrentIndex(max(0, idx))
        elif d.kind == "int":
            w = QSpinBox()
            w.setRange(-1_000_000, 1_000_000)
            w.setValue(int(value))
        elif d.kind == "str":
            w = QLineEdit(str(value))
            if d.secret:
                w.setEchoMode(QLineEdit.Password)
        else:                                  # action
            w = QPushButton(d.label)
            w.clicked.connect(lambda _c=False, key=d.key: self._run_action(key))
        if d.tooltip:
            w.setToolTip(d.tooltip)
        return w

    def _run_action(self, key: str) -> None:
        handler = getattr(self.rec.instance, "on_action", None)
        if callable(handler):
            handler(key)

    def _sync_enabled(self) -> None:
        blocked = self.rec.blocked()
        self.switch.setEnabled(not blocked or self.switch.isChecked())
        on = self.switch.isChecked()
        for key, w in self.widgets.items():
            # settings stay editable while off (fill them in, then switch on);
            # actions need a running plugin
            is_action = self.rec.settings.desc(key).kind == "action"
            w.setEnabled(on and self.rec.running if is_action else True)

    def refresh_status(self) -> None:
        rec = self.rec
        bits = []
        if rec.running:
            bits.append(rec.detail or "on")
        else:
            blocked = rec.blocked()
            bits.append(blocked or (rec.status if rec.status != "on" else "off"))
        bits.append(_ORIGINS.get(rec.manifest.origin, rec.manifest.origin))
        if rec.overrides:
            bits.append(f"replaces the one that {_ORIGINS.get(rec.overrides, rec.overrides)}")
        if rec.manifest.uses:
            bits.append("uses " + ", ".join(rec.manifest.uses))
        self.status.setText(" · ".join(bits))
        self._sync_enabled()

    # ---- save ----

    def changed_settings(self) -> dict:
        out = {}
        for key, w in self.widgets.items():
            d = self.rec.settings.desc(key)
            if d.kind == "action":
                continue
            if d.kind == "bool":
                value = w.isChecked()
            elif d.kind == "choice":
                value = w.currentData()
            elif d.kind == "int":
                value = w.value()
            else:
                value = w.text()
            if value != self.rec.settings[key]:
                out[key] = value
        return out

    def commit(self) -> None:
        changed = self.changed_settings()
        keys = set()
        for key, value in changed.items():
            try:
                if self.rec.settings.set(key, value, save=False):
                    keys.add(key)
            except ValueError:
                pass
        if keys:
            self.rec.settings.save()
        on = self.switch.isChecked()
        was_running = self.rec.running
        if on != self.start_on or (on and not was_running):
            self.host.set_enabled(self.slug, on)
        if keys and was_running and self.rec.running:
            self.host.apply_settings(self.slug, keys)


class PluginsPage:
    """builds the tab's items and commits them on save."""

    def __init__(self, host) -> None:
        self.host = host
        self.sections = [_PluginSection(host, rec) for rec in host.records()]
        host.changed.connect(self._on_changed)

    def items(self) -> list:
        out: list = []
        if not self.sections:
            out.append(_dim("no plugins yet. a plugin is a folder with a plugin.toml; "
                            f"drop it in {config.USER_PLUGINS_DIR} and reopen settings."))
        for section in self.sections:
            out.extend(section.items())
        if self.host.broken:
            out.append(Heading("couldn't read", 34))
            for path, why in self.host.broken:
                out.append(_dim(f"{path.name}: {why}"))
        out.append(_dim("plugins are python that runs inside tide with your permissions. "
                        "only switch on ones you trust."))
        return out

    def _on_changed(self, slug: str) -> None:
        for section in self.sections:
            if section.slug == slug:
                try:
                    section.refresh_status()
                except RuntimeError:
                    pass      # the dialog closed; its widgets are gone

    def commit(self) -> None:
        for section in self.sections:
            section.commit()

    def close(self) -> None:
        try:
            self.host.changed.disconnect(self._on_changed)
        except (RuntimeError, TypeError):
            pass
