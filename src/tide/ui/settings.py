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
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import auth, settings as settings_module, theming


DISCORD_HELP_URL = "https://discord.com/developers/applications"


class SettingsDialog(QDialog):
    """One-window settings. Saves on close; theme applies live."""

    def __init__(self, current_settings: settings_module.Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("tide — settings")
        self.setModal(True)
        self.setMinimumWidth(620)
        self.resize(680, 720)

        self._initial_theme = current_settings.theme
        self._initial_thumbnails = current_settings.show_thumbnails or "theme"
        # Copy known fields; defaults handle anything we don't mirror.
        self._settings = settings_module.Settings(
            theme=current_settings.theme,
            discord_enabled=current_settings.discord_enabled,
            discord_app_id=current_settings.discord_app_id,
            volume=current_settings.volume,
            sleep_preset_minutes=current_settings.sleep_preset_minutes,
            mini_mode_default=current_settings.mini_mode_default,
            show_thumbnails=current_settings.show_thumbnails or "theme",
            audio_device=current_settings.audio_device or "",
            listenbrainz_enabled=current_settings.listenbrainz_enabled,
            listenbrainz_token=current_settings.listenbrainz_token,
            layout=current_settings.layout or "classic",
            layout_overrides=dict(current_settings.layout_overrides or {}),
            adaptive_accent=current_settings.adaptive_accent,
            loading_indicator_style=current_settings.loading_indicator_style or "blocks",
            motion=current_settings.motion or "lite",
            ui_scale=current_settings.ui_scale or "normal",
            playback_speed=current_settings.playback_speed or 1.0,
            preserve_pitch=current_settings.preserve_pitch,
            adaptive_background=current_settings.adaptive_background,
            corner_style=current_settings.corner_style or "sharp",
            nav_icon_set=current_settings.nav_icon_set or "off",
            font_family_override=current_settings.font_family_override or "",
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

        self.thumbnails_picker = QComboBox()
        self.thumbnails_picker.addItem("from theme", "theme")
        self.thumbnails_picker.addItem("always show", "on")
        self.thumbnails_picker.addItem("never show", "off")
        self.thumbnails_picker.currentIndexChanged.connect(self._on_thumbnails_changed)

        # Audio device picker for the visualizer (backup; cog menu has it too).
        self.audio_device_picker = QComboBox()
        self.audio_device_picker.addItem("auto (default sink monitor)", "")
        try:
            from .. import audio_capture
            for name, label in audio_capture.list_monitor_sources():
                self.audio_device_picker.addItem(label, name)
        except Exception:
            pass

        # Layout preset picker.
        self.layout_picker = QComboBox()
        from .. import layout as layout_module
        for lay in layout_module.manager().list_layouts():
            self.layout_picker.addItem(lay.name, lay.slug)
        self.layout_picker.currentIndexChanged.connect(self._on_layout_changed)

        # Per-slot override pickers.
        from . import variants as variants_module
        self._slot_pickers: dict[str, QComboBox] = {}
        for slot, options in variants_module.all_variant_slugs().items():
            cb = QComboBox()
            cb.addItem("(from layout)", "")
            for opt in options:
                cb.addItem(opt, opt)
            cb.currentIndexChanged.connect(lambda _i=0, s=slot: self._on_slot_override(s))
            self._slot_pickers[slot] = cb

        # Adaptive accent.
        self.adaptive_toggle = QCheckBox("shift accent to album art")
        self.adaptive_toggle.toggled.connect(self._on_adaptive_toggled)

        # Adaptive background — tints the central content area with a soft
        # vertical gradient pulled from the album palette. Independent of
        # the accent shift above; either can be on without the other.
        self.adaptive_bg_toggle = QCheckBox(
            "tint central area with album-derived gradient"
        )

        # Corner softness — applies a sticky @radius override on the theming
        # manager (preserved across adaptive clears). Affects all corners
        # that use @radius (inputs, scrollbars, the central-area gradient).
        self.corner_picker = QComboBox()
        self.corner_picker.addItem("sharp · 0px", "sharp")
        self.corner_picker.addItem("soft · 6px", "soft")
        self.corner_picker.addItem("rounded · 12px", "rounded")

        # Nav-rail icon set. Each item label embeds one icon from the set
        # so the user previews the vibe in the picker itself.
        self.nav_icons_picker = QComboBox()
        self.nav_icons_picker.addItem("off · text only", "off")
        self.nav_icons_picker.addItem("svg · brutalist line-art icons", "svg")
        self.nav_icons_picker.addItem("classic · ⌂ ▤ ≡ ♪ ⌛ ♬ ⇄ ⚙", "classic")
        self.nav_icons_picker.addItem("emoji · 🏠 📚 📋 🎤 🕒 🎚 🔌 ⚙", "emoji")

        # Font family — overrides the active theme's typography.family.
        # Empty string = "use whatever the theme says". Populated below
        # from bundled tide fonts + system QFontDatabase enumeration.
        self.font_picker = QComboBox()
        self.font_picker.addItem("from theme", "")
        # Tide-bundled fonts always available regardless of system.
        for f in ("IBM Plex Mono", "JetBrains Mono", "Inter"):
            self.font_picker.addItem(f"{f} · bundled", f)
        # System families — filtered to a manageable list (mono fonts +
        # popular sans). Enumerating every font would be noisy; the user
        # can type in the box if their pick isn't shown.
        try:
            from PySide6.QtGui import QFontDatabase
            existing = {self.font_picker.itemData(i)
                        for i in range(self.font_picker.count())}
            system_families = QFontDatabase.families()
            # Prefer monospace families up top.
            mono = [f for f in system_families
                    if QFontDatabase.isFixedPitch(f) and f not in existing]
            for f in sorted(mono):
                self.font_picker.addItem(f, f)
                existing.add(f)
        except Exception:
            pass
        # Make editable so users can paste any family name.
        self.font_picker.setEditable(True)
        self.font_picker.setInsertPolicy(QComboBox.NoInsert)

        # Loading-indicator style. The labels include a tiny example of each
        # rendering so the user knows what they're picking without trial-and-error.
        self.loading_picker = QComboBox()
        self.loading_picker.addItem("off", "off")
        self.loading_picker.addItem("numbers · 42%", "numbers")
        self.loading_picker.addItem("blocks · █████░░░░░", "blocks")
        self.loading_picker.addItem("dots · ●●●●●○○○○○", "dots")
        self.loading_picker.addItem("ascii · [#####-----]", "ascii")

        # Motion intensity — gates every animation in the app via the motion
        # module. "lite" is the recommended default (signature + everyday
        # animations only); "full" enables atmospheric tier when it ships.
        self.motion_picker = QComboBox()
        self.motion_picker.addItem("off · instant transitions", "off")
        self.motion_picker.addItem("lite · signature + everyday", "lite")
        self.motion_picker.addItem("full · everything including ambient", "full")

        # UI scale — multiplies the active theme's typography size, which
        # cascades to every widget that uses self.font() / QFontMetrics.
        self.scale_picker = QComboBox()
        self.scale_picker.addItem("compact · 0.85×", "compact")
        self.scale_picker.addItem("normal · 1.00×", "normal")
        self.scale_picker.addItem("large · 1.15×", "large")
        self.scale_picker.addItem("huge · 1.30×", "huge")

        # Preserve-pitch toggle — when on, mpv's scaletempo filter keeps
        # pitch steady as speed changes (utility / audiobook mode). Default
        # off so the bottom-bar speed control gives the slowed/nightcore
        # aesthetic with no extra steps.
        self.preserve_pitch_toggle = QCheckBox(
            "preserve pitch when changing speed"
        )

        appearance_form = QFormLayout()
        appearance_form.addRow("theme:", self.theme_picker)
        appearance_form.addRow("layout:", self.layout_picker)
        appearance_form.addRow("  progress:", self._slot_pickers["progress"])
        appearance_form.addRow("  volume:", self._slot_pickers["volume"])
        appearance_form.addRow("  album art:", self._slot_pickers["album_art"])
        appearance_form.addRow("  controls:", self._slot_pickers["controls"])
        appearance_form.addRow("  label:", self._slot_pickers["now_label"])
        appearance_form.addRow("thumbnails:", self.thumbnails_picker)
        appearance_form.addRow("adaptive:", self.adaptive_toggle)
        appearance_form.addRow("", self.adaptive_bg_toggle)
        appearance_form.addRow("corners:", self.corner_picker)
        appearance_form.addRow("nav icons:", self.nav_icons_picker)
        appearance_form.addRow("font:", self.font_picker)
        appearance_form.addRow("loading bar:", self.loading_picker)
        appearance_form.addRow("motion:", self.motion_picker)
        appearance_form.addRow("ui scale:", self.scale_picker)
        appearance_form.addRow("speed:", self.preserve_pitch_toggle)
        appearance_form.addRow("visualizer audio:", self.audio_device_picker)

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

        # ---- listenbrainz ----
        lb_heading = QLabel("── listenbrainz scrobbling ──")
        lb_heading.setProperty("class", "dim")

        self.lb_toggle = QCheckBox("enable listenbrainz scrobbling")
        self.lb_token = QLineEdit()
        self.lb_token.setPlaceholderText("paste your listenbrainz user token")
        self.lb_token.setEchoMode(QLineEdit.Password)
        self.lb_token.setEnabled(False)
        self.lb_toggle.toggled.connect(self.lb_token.setEnabled)

        self.lb_help = QPushButton("get a token  →")
        self.lb_help.setFlat(True)
        self.lb_help.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://listenbrainz.org/profile/"))
        )

        lb_token_row = QHBoxLayout()
        lb_token_row.addWidget(self.lb_token, stretch=1)
        lb_token_row.addWidget(self.lb_help)

        lb_explainer = QLabel(
            "submits 'playing now' on track start and a 'listen' once you've heard "
            "the track for 30s (or 50% / 4 minutes). create an account at "
            "listenbrainz.org → settings → token."
        )
        lb_explainer.setWordWrap(True)
        lb_explainer.setProperty("class", "dim")

        lb_col = QVBoxLayout()
        lb_col.setSpacing(6)
        lb_col.addWidget(self.lb_toggle)
        lb_col.addLayout(lb_token_row)
        lb_col.addWidget(lb_explainer)

        # ---- advanced ----
        advanced_heading = QLabel("── advanced ──────────────────")
        advanced_heading.setProperty("class", "dim")

        self.sign_out_btn = QPushButton("sign out + re-import session")
        self.sign_out_btn.clicked.connect(self._on_sign_out)

        adv_col = QVBoxLayout()
        adv_col.setSpacing(6)
        adv_col.addWidget(self.sign_out_btn, alignment=Qt.AlignLeft)

        # ---- about ----
        about_heading = QLabel("── about ─────────────────────")
        about_heading.setProperty("class", "dim")

        from .. import __version__
        title = QLabel(f"tide  v{__version__}")
        title.setStyleSheet("font-weight: 600;")

        tagline = QLabel("a brutalist youtube music client.")
        tagline.setProperty("class", "dim")

        credits = QLabel(
            "built on:  pyside6 · mpv · ytmusicapi · yt-dlp · cryptography\n"
            "fonts:     ibm plex mono · ibm plex sans  (ofl)\n"
            "licensed:  gpl-3.0-or-later"
        )
        credits.setProperty("class", "dim")
        credits.setStyleSheet("font-family: monospace;")
        credits.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.repo_btn = QPushButton("github  →")
        self.repo_btn.setFlat(True)
        self.repo_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://github.com/captiencelovesarch/tide"))
        )

        self.issues_btn = QPushButton("report a bug  →")
        self.issues_btn.setFlat(True)
        self.issues_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://github.com/captiencelovesarch/tide/issues"))
        )

        about_links = QHBoxLayout()
        about_links.addWidget(self.repo_btn)
        about_links.addWidget(self.issues_btn)
        about_links.addStretch(1)

        about_col = QVBoxLayout()
        about_col.setSpacing(4)
        about_col.addWidget(title)
        about_col.addWidget(tagline)
        about_col.addSpacing(6)
        about_col.addWidget(credits)
        about_col.addLayout(about_links)

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

        # ---- assemble (scrollable so growing the dialog never breaks) ----
        content = QWidget()
        content_col = QVBoxLayout(content)
        content_col.setContentsMargins(0, 0, 0, 0)
        content_col.setSpacing(12)
        content_col.addWidget(appearance_heading)
        content_col.addLayout(appearance_form)
        content_col.addSpacing(6)
        content_col.addWidget(discord_heading)
        content_col.addLayout(discord_col)
        content_col.addSpacing(6)
        content_col.addWidget(lb_heading)
        content_col.addLayout(lb_col)
        content_col.addSpacing(6)
        content_col.addWidget(advanced_heading)
        content_col.addLayout(adv_col)
        content_col.addSpacing(6)
        content_col.addWidget(about_heading)
        content_col.addLayout(about_col)
        content_col.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 14)
        root.setSpacing(10)
        root.addWidget(scroll, stretch=1)
        root.addLayout(btn_row)

    def _populate(self) -> None:
        # Block currentIndexChanged on EVERY picker we're about to set, so
        # we don't trigger live-apply cascades (theme, layout, slots) just
        # for loading the saved values. Side-effect of apply happens on
        # explicit user change instead.
        all_pickers = [
            self.theme_picker, self.thumbnails_picker, self.audio_device_picker,
            self.layout_picker, *self._slot_pickers.values(),
        ]
        for cb in all_pickers:
            cb.blockSignals(True)
        try:
            self._populate_pickers()
        finally:
            for cb in all_pickers:
                cb.blockSignals(False)

    def _populate_pickers(self) -> None:
        themes = theming.discover_themes()
        # Sort by name for stable display.
        for slug, theme in sorted(themes.items(), key=lambda kv: kv[1].name):
            self.theme_picker.addItem(theme.name, slug)
        idx = self.theme_picker.findData(self._settings.theme)
        if idx >= 0:
            self.theme_picker.setCurrentIndex(idx)

        thumb_idx = self.thumbnails_picker.findData(self._settings.show_thumbnails or "theme")
        if thumb_idx >= 0:
            self.thumbnails_picker.setCurrentIndex(thumb_idx)

        dev_idx = self.audio_device_picker.findData(self._settings.audio_device or "")
        if dev_idx >= 0:
            self.audio_device_picker.setCurrentIndex(dev_idx)

        # Layout + overrides.
        lay_idx = self.layout_picker.findData(self._settings.layout or "classic")
        if lay_idx >= 0:
            self.layout_picker.setCurrentIndex(lay_idx)
        overrides = self._settings.layout_overrides or {}
        for slot, cb in self._slot_pickers.items():
            val = overrides.get(slot, "")
            idx = cb.findData(val)
            if idx >= 0:
                cb.setCurrentIndex(idx)
        self.adaptive_toggle.setChecked(self._settings.adaptive_accent)

        loading_idx = self.loading_picker.findData(
            self._settings.loading_indicator_style or "blocks"
        )
        if loading_idx >= 0:
            self.loading_picker.setCurrentIndex(loading_idx)

        motion_idx = self.motion_picker.findData(self._settings.motion or "lite")
        if motion_idx >= 0:
            self.motion_picker.setCurrentIndex(motion_idx)

        scale_idx = self.scale_picker.findData(self._settings.ui_scale or "normal")
        if scale_idx >= 0:
            self.scale_picker.setCurrentIndex(scale_idx)

        self.preserve_pitch_toggle.setChecked(bool(self._settings.preserve_pitch))
        self.adaptive_bg_toggle.setChecked(bool(self._settings.adaptive_background))
        corner_idx = self.corner_picker.findData(self._settings.corner_style or "sharp")
        if corner_idx >= 0:
            self.corner_picker.setCurrentIndex(corner_idx)
        nav_idx = self.nav_icons_picker.findData(self._settings.nav_icon_set or "off")
        if nav_idx >= 0:
            self.nav_icons_picker.setCurrentIndex(nav_idx)
        font_override = self._settings.font_family_override or ""
        font_idx = self.font_picker.findData(font_override)
        if font_idx >= 0:
            self.font_picker.setCurrentIndex(font_idx)
        else:
            # Custom value not in the preset list — show it in the editable
            # combo's text field directly.
            self.font_picker.setCurrentText(font_override)

        self.discord_toggle.setChecked(self._settings.discord_enabled)
        self.discord_app_id.setText(self._settings.discord_app_id)
        self.discord_app_id.setEnabled(self._settings.discord_enabled)

        self.lb_toggle.setChecked(self._settings.listenbrainz_enabled)
        self.lb_token.setText(self._settings.listenbrainz_token)
        self.lb_token.setEnabled(self._settings.listenbrainz_enabled)

    # ---------- handlers ----------

    def _on_theme_changed(self, _idx: int) -> None:
        slug = self.theme_picker.currentData()
        if slug:
            theming.manager().apply(slug)

    def _on_layout_changed(self, _idx: int) -> None:
        slug = self.layout_picker.currentData()
        if not slug:
            return
        from .. import layout as layout_module
        # Live-apply preview through the parent MainWindow.
        parent = self.parent()
        if parent is not None and hasattr(parent, "apply_layout"):
            effective = layout_module.manager().apply(slug, dict(self._gather_overrides()))
            if effective is not None:
                parent.apply_layout(effective)

    def _on_slot_override(self, slot: str) -> None:
        overrides = dict(self._gather_overrides())
        from .. import layout as layout_module
        effective = layout_module.manager().update_overrides(overrides)
        parent = self.parent()
        if parent is not None and hasattr(parent, "apply_layout"):
            parent.apply_layout(effective)

    def _gather_overrides(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for slot, cb in self._slot_pickers.items():
            val = cb.currentData() or ""
            if val:
                out[slot] = val
        return out

    def _on_adaptive_toggled(self, on: bool) -> None:
        # P5.3 wires the driver — here we just persist.
        pass

    def _on_thumbnails_changed(self, _idx: int) -> None:
        from .track_row import set_thumbnail_override
        value = self.thumbnails_picker.currentData() or "theme"
        set_thumbnail_override(value)
        # Force the live theme to re-emit so attached delegates repaint.
        current = theming.manager().current()
        if current is not None:
            theming.manager().theme_changed.emit(current)

    def _on_discord_toggle(self, on: bool) -> None:
        self.discord_app_id.setEnabled(on)

    def _on_save(self) -> None:
        self._settings.theme = self.theme_picker.currentData() or self._initial_theme
        self._settings.discord_enabled = self.discord_toggle.isChecked()
        self._settings.discord_app_id = self.discord_app_id.text().strip()
        self._settings.show_thumbnails = self.thumbnails_picker.currentData() or "theme"
        self._settings.audio_device = self.audio_device_picker.currentData() or ""
        self._settings.listenbrainz_enabled = self.lb_toggle.isChecked()
        self._settings.listenbrainz_token = self.lb_token.text().strip()
        self._settings.layout = self.layout_picker.currentData() or "classic"
        self._settings.layout_overrides = self._gather_overrides()
        self._settings.adaptive_accent = self.adaptive_toggle.isChecked()
        self._settings.loading_indicator_style = (
            self.loading_picker.currentData() or "blocks"
        )
        self._settings.motion = self.motion_picker.currentData() or "lite"
        self._settings.ui_scale = self.scale_picker.currentData() or "normal"
        self._settings.preserve_pitch = self.preserve_pitch_toggle.isChecked()
        self._settings.adaptive_background = self.adaptive_bg_toggle.isChecked()
        self._settings.corner_style = self.corner_picker.currentData() or "sharp"
        self._settings.nav_icon_set = self.nav_icons_picker.currentData() or "off"
        # Prefer the picker's data (preset family) if it's still selected;
        # fall back to the editable text for free-form entries.
        font_value = self.font_picker.currentData() or self.font_picker.currentText().strip()
        self._settings.font_family_override = font_value or ""
        settings_module.save(self._settings)
        self.accept()

    def _on_cancel(self) -> None:
        # Revert live previews.
        if self._initial_theme and self._initial_theme != self.theme_picker.currentData():
            theming.manager().apply(self._initial_theme)
        if self._initial_thumbnails != (self.thumbnails_picker.currentData() or "theme"):
            from .track_row import set_thumbnail_override
            set_thumbnail_override(self._initial_thumbnails)
            current = theming.manager().current()
            if current is not None:
                theming.manager().theme_changed.emit(current)
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
