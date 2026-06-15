"""Settings dialog — theme, discord, advanced auth re-import.

All persistent app options live here. Themes hot-swap on selection;
discord settings apply on save.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from .. import auth, settings as settings_module, theming


DISCORD_HELP_URL = "https://discord.com/developers/applications"


class SettingsDialog(QDialog):
    """One-window settings. Saves on close; theme applies live."""

    def __init__(self, current_settings: settings_module.Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("tide — settings")
        self.setModal(True)
        self.setMinimumWidth(540)

        self._initial_theme = current_settings.theme
        self._settings = settings_module.Settings(
            theme=current_settings.theme,
            discord_enabled=current_settings.discord_enabled,
            discord_app_id=current_settings.discord_app_id,
        )

        self._build_ui()
        self._populate()

    # ---------- build ----------

    def _build_ui(self) -> None:
        # ---- appearance ----
        appearance_heading = QLabel("── appearance ─────────────")
        appearance_heading.setProperty("class", "dim")

        self.theme_picker = QComboBox()
        self.theme_picker.currentIndexChanged.connect(self._on_theme_changed)

        appearance_form = QFormLayout()
        appearance_form.addRow("theme:", self.theme_picker)

        # ---- discord ----
        discord_heading = QLabel("── discord rich presence ──")
        discord_heading.setProperty("class", "dim")

        self.discord_toggle = QCheckBox("enable discord rich presence")
        self.discord_toggle.toggled.connect(self._on_discord_toggle)

        self.discord_app_id = QLineEdit()
        self.discord_app_id.setPlaceholderText("paste discord application id")
        self.discord_app_id.setEnabled(False)

        self.discord_help = QPushButton("get an app id  →")
        self.discord_help.setFlat(True)
        self.discord_help.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(DISCORD_HELP_URL))
        )

        discord_explainer = QLabel(
            "create a new application at the discord developer portal, "
            "copy its application id, paste it here. tide will show whatever "
            "name / image you gave that app."
        )
        discord_explainer.setWordWrap(True)
        discord_explainer.setProperty("class", "dim")

        discord_id_row = QHBoxLayout()
        discord_id_row.addWidget(self.discord_app_id, stretch=1)
        discord_id_row.addWidget(self.discord_help)

        discord_col = QVBoxLayout()
        discord_col.setSpacing(6)
        discord_col.addWidget(self.discord_toggle)
        discord_col.addLayout(discord_id_row)
        discord_col.addWidget(discord_explainer)

        # ---- advanced ----
        advanced_heading = QLabel("── advanced ──────────────────")
        advanced_heading.setProperty("class", "dim")

        self.sign_out_btn = QPushButton("sign out + re-import session")
        self.sign_out_btn.clicked.connect(self._on_sign_out)

        adv_col = QVBoxLayout()
        adv_col.setSpacing(6)
        adv_col.addWidget(self.sign_out_btn, alignment=Qt.AlignLeft)

        # ---- buttons ----
        self.save_btn = QPushButton("save")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._on_save)

        self.cancel_btn = QPushButton("cancel")
        self.cancel_btn.clicked.connect(self._on_cancel)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.save_btn)

        # ---- assemble ----
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 14)
        root.setSpacing(12)
        root.addWidget(appearance_heading)
        root.addLayout(appearance_form)
        root.addSpacing(6)
        root.addWidget(discord_heading)
        root.addLayout(discord_col)
        root.addSpacing(6)
        root.addWidget(advanced_heading)
        root.addLayout(adv_col)
        root.addStretch(1)
        root.addLayout(btn_row)

    def _populate(self) -> None:
        themes = theming.discover_themes()
        # Sort by name for stable display.
        for slug, theme in sorted(themes.items(), key=lambda kv: kv[1].name):
            self.theme_picker.addItem(theme.name, slug)
        idx = self.theme_picker.findData(self._settings.theme)
        if idx >= 0:
            self.theme_picker.setCurrentIndex(idx)

        self.discord_toggle.setChecked(self._settings.discord_enabled)
        self.discord_app_id.setText(self._settings.discord_app_id)
        self.discord_app_id.setEnabled(self._settings.discord_enabled)

    # ---------- handlers ----------

    def _on_theme_changed(self, _idx: int) -> None:
        slug = self.theme_picker.currentData()
        if slug:
            theming.manager().apply(slug)

    def _on_discord_toggle(self, on: bool) -> None:
        self.discord_app_id.setEnabled(on)

    def _on_save(self) -> None:
        self._settings.theme = self.theme_picker.currentData() or self._initial_theme
        self._settings.discord_enabled = self.discord_toggle.isChecked()
        self._settings.discord_app_id = self.discord_app_id.text().strip()
        settings_module.save(self._settings)
        self.accept()

    def _on_cancel(self) -> None:
        # Revert live theme preview if the user changed it.
        if self._initial_theme and self._initial_theme != self.theme_picker.currentData():
            theming.manager().apply(self._initial_theme)
        self.reject()

    def _on_sign_out(self) -> None:
        ok = QMessageBox.question(
            self, "tide",
            "this will sign out of youtube music and re-open the import "
            "wizard next time tide starts. continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok != QMessageBox.Yes:
            return
        auth.clear_saved_auth()
        QMessageBox.information(
            self, "tide",
            "signed out. quit tide and start it again to sign back in.",
        )

    # ---------- result ----------

    def updated_settings(self) -> settings_module.Settings:
        return self._settings
