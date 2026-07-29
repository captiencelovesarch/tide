"""Settings dialog — theme, discord, advanced auth re-import.

All persistent app options live here. Themes hot-swap on selection;
discord settings apply on save.
"""
from __future__ import annotations

import copy

from PySide6.QtCore import Qt, QTimer, Signal
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
    QSpinBox,
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
        self._initial_font = current_settings.font_family_override or ""
        self._initial_font_size = int(current_settings.font_size_override_pt or 0)
        # Work on a full, independent copy. The dialog mutates only the
        # fields it surfaces (in _on_save) and persists the whole object, so
        # any field NOT mirrored here would be written back at its default —
        # silently wiping sources_enabled, the Spotify/Subsonic credentials,
        # audio_fx_state, ui_sounds_enabled, first_launch_complete, etc. A
        # deepcopy preserves every field (including dict fields) verbatim, so
        # new settings added later can never regress this dialog again.
        self._settings = copy.deepcopy(current_settings)

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
        self.adaptive_style_picker = QComboBox()
        self.adaptive_style_picker.addItem("living fields · layered ambience", "field")
        self.adaptive_style_picker.addItem("diagonal band · classic sweep", "band")
        self.adaptive_style_picker.addItem("bass arch · hill swells on bass", "vbeam")

        # Bass pulse — swells / brightens that gradient on heavy bass while
        # playing. Needs the monitor capture, so it's gated on the gradient
        # being on and costs a little constant CPU during playback.
        self.adaptive_pulse_toggle = QCheckBox(
            "pulse the gradient on heavy bass"
        )
        self.adaptive_bg_toggle.toggled.connect(self.adaptive_pulse_toggle.setEnabled)
        self.adaptive_bg_toggle.toggled.connect(self.adaptive_style_picker.setEnabled)

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
        # Empty string = "use whatever the theme says". Every system family
        # is listed and each row renders in its own face, so picking a font
        # is done by looking at it rather than knowing its name. Selection
        # live-applies; cancel reverts.
        self.font_picker = QComboBox()
        self.font_picker.addItem("from theme", "")
        # Tide-bundled fonts pinned up top, always available.
        for f in ("IBM Plex Mono", "JetBrains Mono", "Inter"):
            self.font_picker.addItem(f"{f} · bundled", f)
        try:
            from PySide6.QtGui import QFont, QFontDatabase
            existing = {self.font_picker.itemData(i)
                        for i in range(self.font_picker.count())}
            for i in range(1, self.font_picker.count()):
                self.font_picker.setItemData(
                    i, QFont(self.font_picker.itemData(i)), Qt.FontRole
                )
            for f in sorted(QFontDatabase.families()):
                if f in existing or f.startswith("."):
                    continue    # skip dupes + private system faces
                self.font_picker.addItem(f, f)
                self.font_picker.setItemData(
                    self.font_picker.count() - 1, QFont(f), Qt.FontRole
                )
        except Exception:
            pass
        # Make editable so users can paste any family name. Typed names
        # apply on save; picked rows apply live.
        self.font_picker.setEditable(True)
        self.font_picker.setInsertPolicy(QComboBox.NoInsert)
        self.font_picker.currentIndexChanged.connect(self._on_font_changed)

        # Font size — 0 pt = follow the theme. ui_scale still multiplies.
        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(0, 24)
        self.font_size_spin.setSpecialValueText("theme default")
        self.font_size_spin.setSuffix(" pt")
        self.font_size_spin.valueChanged.connect(self._on_font_size_changed)

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

        # UI sounds — short clicks on nav / modals / toggles. Auto-muted
        # while music is playing so they never compete with the player.
        self.ui_sounds_toggle = QCheckBox(
            "ui sounds (nav · modals · toggles · auto-mutes during playback)"
        )

        # ---- mini player (v1.2.7 redo — dedicated frameless window,
        # opened by clicking the now-playing art or Ctrl+M). Everything
        # here is also reachable from the mini's own right-click menu.
        self.mini_default_toggle = QCheckBox("start in mini player")
        self.mini_backdrop_picker = QComboBox()
        self.mini_backdrop_picker.addItem("follow main backdrop style", "follow")
        self.mini_backdrop_picker.addItem("living fields · layered ambience", "field")
        self.mini_backdrop_picker.addItem("diagonal band · classic sweep", "band")
        self.mini_backdrop_picker.addItem("bass arch · hill swells on bass", "vbeam")
        self.mini_backdrop_picker.addItem("off · flat card", "off")
        self.mini_progress_picker = QComboBox()
        self.mini_progress_picker.addItem("border ring · the window edge fills", "ring")
        self.mini_progress_picker.addItem("thin bar", "thin")
        self.mini_pin_toggle = QCheckBox("pin on top of other windows")
        self.mini_ticker_toggle = QCheckBox("live synced-lyric ticker line")
        self.mini_zen_toggle = QCheckBox("auto-hide controls when idle")
        self.mini_pulse_toggle = QCheckBox("backdrop breathes with the bass")
        self.mini_pulse_resize_toggle = QCheckBox(
            "window breathes too · resizes on bass"
        )
        # The resize rides the pulse envelope — no pulse, nothing to ride.
        self.mini_pulse_toggle.toggled.connect(
            self.mini_pulse_resize_toggle.setEnabled
        )
        self.mini_vis_toggle = QCheckBox("visualizer instead of album art")

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
        appearance_form.addRow("  style:", self.adaptive_style_picker)
        appearance_form.addRow("", self.adaptive_pulse_toggle)
        appearance_form.addRow("corners:", self.corner_picker)
        appearance_form.addRow("nav icons:", self.nav_icons_picker)
        appearance_form.addRow("font:", self.font_picker)
        appearance_form.addRow("  size:", self.font_size_spin)
        appearance_form.addRow("loading bar:", self.loading_picker)
        appearance_form.addRow("motion:", self.motion_picker)
        appearance_form.addRow("", self.ui_sounds_toggle)
        appearance_form.addRow("ui scale:", self.scale_picker)
        appearance_form.addRow("speed:", self.preserve_pitch_toggle)
        appearance_form.addRow("visualizer audio:", self.audio_device_picker)

        mini_heading = QLabel("── mini player ────────────")
        mini_heading.setProperty("class", "dim")
        appearance_form.addRow(mini_heading)
        appearance_form.addRow("", self.mini_default_toggle)
        appearance_form.addRow("  backdrop:", self.mini_backdrop_picker)
        appearance_form.addRow("  progress:", self.mini_progress_picker)
        appearance_form.addRow("", self.mini_pin_toggle)
        appearance_form.addRow("", self.mini_ticker_toggle)
        appearance_form.addRow("", self.mini_zen_toggle)
        appearance_form.addRow("", self.mini_pulse_toggle)
        appearance_form.addRow("", self.mini_pulse_resize_toggle)
        appearance_form.addRow("", self.mini_vis_toggle)

        # ---- playback ----
        playback_heading = QLabel("── playback ───────────────")
        playback_heading.setProperty("class", "dim")

        self.prefetch_hover_toggle = QCheckBox(
            "pre-resolve stream on hover"
        )
        self.prefetch_warm_picker = QComboBox()
        self.prefetch_warm_picker.addItem("off", 0)
        self.prefetch_warm_picker.addItem("top 2", 2)
        self.prefetch_warm_picker.addItem("top 3", 3)
        self.prefetch_warm_picker.addItem("top 5", 5)

        prefetch_explainer = QLabel(
            "gets stream urls ready before you click so playback starts "
            "instantly. hover pre-resolves the row under the mouse; warm "
            "results quietly resolves the top few search hits as they land."
        )
        prefetch_explainer.setWordWrap(True)
        prefetch_explainer.setProperty("class", "dim")

        playback_form = QFormLayout()
        playback_form.addRow("", self.prefetch_hover_toggle)
        playback_form.addRow("warm results:", self.prefetch_warm_picker)

        playback_col = QVBoxLayout()
        playback_col.setSpacing(6)
        playback_col.addLayout(playback_form)
        playback_col.addWidget(prefetch_explainer)

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

        self.discord_lyrics_toggle = QCheckBox("show live lyric in presence")
        self.discord_lyrics_toggle.setEnabled(False)

        discord_lyrics_explainer = QLabel(
            "when the playing track has synced lyrics, the current line "
            "replaces artist · album on your profile — visible to everyone, "
            "and song lyrics can be explicit. off by default."
        )
        discord_lyrics_explainer.setWordWrap(True)
        discord_lyrics_explainer.setProperty("class", "dim")

        discord_id_row = QHBoxLayout()
        discord_id_row.addWidget(self.discord_app_id, stretch=1)
        discord_id_row.addWidget(self.discord_help)

        discord_col = QVBoxLayout()
        discord_col.setSpacing(6)
        discord_col.addWidget(self.discord_toggle)
        discord_col.addLayout(discord_id_row)
        discord_col.addWidget(discord_explainer)
        discord_col.addWidget(self.discord_lyrics_toggle)
        discord_col.addWidget(discord_lyrics_explainer)

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

        # ---- audio fx ----
        audio_fx_heading = QLabel("── audio fx ──────────────────")
        audio_fx_heading.setProperty("class", "dim")
        fx_blurb = QLabel(
            "10-band eq + reverb + loudness norm + the rest of the rack.\n"
            "open the full panel with ctrl+9, or use the [fx] popover on the now-playing strip."
        )
        fx_blurb.setProperty("class", "dim")
        fx_blurb.setWordWrap(True)
        self.audio_fx_open_btn = QPushButton("open audio fx panel  →")
        self.audio_fx_open_btn.clicked.connect(self._on_open_audio_fx)
        audio_fx_col = QVBoxLayout()
        audio_fx_col.setSpacing(6)
        audio_fx_col.addWidget(fx_blurb)
        audio_fx_col.addWidget(self.audio_fx_open_btn, alignment=Qt.AlignLeft)

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
        content_col.addWidget(playback_heading)
        content_col.addLayout(playback_col)
        content_col.addSpacing(6)
        content_col.addWidget(discord_heading)
        content_col.addLayout(discord_col)
        content_col.addSpacing(6)
        content_col.addWidget(lb_heading)
        content_col.addLayout(lb_col)
        content_col.addSpacing(6)
        content_col.addWidget(audio_fx_heading)
        content_col.addLayout(audio_fx_col)
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
        self.ui_sounds_toggle.setChecked(bool(self._settings.ui_sounds_enabled))
        self.adaptive_bg_toggle.setChecked(bool(self._settings.adaptive_background))
        style_idx = self.adaptive_style_picker.findData(
            self._settings.adaptive_background_style or "field"
        )
        if style_idx >= 0:
            self.adaptive_style_picker.setCurrentIndex(style_idx)
        self.adaptive_pulse_toggle.setChecked(bool(self._settings.adaptive_pulse))
        self.adaptive_pulse_toggle.setEnabled(bool(self._settings.adaptive_background))
        self.adaptive_style_picker.setEnabled(bool(self._settings.adaptive_background))
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
        self.font_size_spin.setValue(int(self._settings.font_size_override_pt or 0))

        self.mini_default_toggle.setChecked(bool(self._settings.mini_mode_default))
        mini_bd_idx = self.mini_backdrop_picker.findData(
            self._settings.mini_backdrop_style or "follow"
        )
        if mini_bd_idx >= 0:
            self.mini_backdrop_picker.setCurrentIndex(mini_bd_idx)
        mini_pr_idx = self.mini_progress_picker.findData(
            self._settings.mini_progress_style or "ring"
        )
        if mini_pr_idx >= 0:
            self.mini_progress_picker.setCurrentIndex(mini_pr_idx)
        self.mini_pin_toggle.setChecked(bool(self._settings.mini_pin))
        self.mini_ticker_toggle.setChecked(bool(self._settings.mini_ticker))
        self.mini_zen_toggle.setChecked(bool(self._settings.mini_zen))
        self.mini_pulse_toggle.setChecked(bool(self._settings.mini_pulse))
        self.mini_pulse_resize_toggle.setChecked(
            bool(self._settings.mini_pulse_resize))
        self.mini_pulse_resize_toggle.setEnabled(bool(self._settings.mini_pulse))
        self.mini_vis_toggle.setChecked(
            bool(self._settings.mini_show_visualizer))

        self.prefetch_hover_toggle.setChecked(bool(self._settings.prefetch_hover))
        warm_idx = self.prefetch_warm_picker.findData(
            int(self._settings.prefetch_warm_results or 0)
        )
        if warm_idx >= 0:
            self.prefetch_warm_picker.setCurrentIndex(warm_idx)

        self.discord_toggle.setChecked(self._settings.discord_enabled)
        self.discord_app_id.setText(self._settings.discord_app_id)
        self.discord_app_id.setEnabled(self._settings.discord_enabled)
        self.discord_lyrics_toggle.setChecked(self._settings.discord_lyrics_enabled)
        self.discord_lyrics_toggle.setEnabled(self._settings.discord_enabled)

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
        self.discord_lyrics_toggle.setEnabled(on)

    def _on_save(self) -> None:
        self._settings.theme = self.theme_picker.currentData() or self._initial_theme
        self._settings.discord_enabled = self.discord_toggle.isChecked()
        self._settings.discord_app_id = self.discord_app_id.text().strip()
        self._settings.discord_lyrics_enabled = self.discord_lyrics_toggle.isChecked()
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
        self._settings.ui_sounds_enabled = self.ui_sounds_toggle.isChecked()
        self._settings.prefetch_hover = self.prefetch_hover_toggle.isChecked()
        self._settings.prefetch_warm_results = int(
            self.prefetch_warm_picker.currentData() or 0
        )
        self._settings.adaptive_background = self.adaptive_bg_toggle.isChecked()
        self._settings.adaptive_background_style = (
            self.adaptive_style_picker.currentData() or "field"
        )
        self._settings.adaptive_pulse = self.adaptive_pulse_toggle.isChecked()
        self._settings.mini_mode_default = self.mini_default_toggle.isChecked()
        self._settings.mini_backdrop_style = (
            self.mini_backdrop_picker.currentData() or "follow"
        )
        self._settings.mini_progress_style = (
            self.mini_progress_picker.currentData() or "ring"
        )
        self._settings.mini_pin = self.mini_pin_toggle.isChecked()
        self._settings.mini_ticker = self.mini_ticker_toggle.isChecked()
        self._settings.mini_zen = self.mini_zen_toggle.isChecked()
        self._settings.mini_pulse = self.mini_pulse_toggle.isChecked()
        self._settings.mini_pulse_resize = (
            self.mini_pulse_resize_toggle.isChecked()
        )
        self._settings.mini_show_visualizer = self.mini_vis_toggle.isChecked()
        self._settings.corner_style = self.corner_picker.currentData() or "sharp"
        self._settings.nav_icon_set = self.nav_icons_picker.currentData() or "off"
        # Prefer the picker's data (preset family) if it's still selected;
        # fall back to the editable text for free-form entries.
        font_value = self.font_picker.currentData() or self.font_picker.currentText().strip()
        self._settings.font_family_override = font_value or ""
        self._settings.font_size_override_pt = int(self.font_size_spin.value())
        settings_module.save(self._settings)
        self.accept()

    def _on_font_changed(self, _idx: int) -> None:
        """Live-preview a picked family. Idempotent (set_user_font no-ops on
        the same value), so the populate-time setCurrentIndex is harmless."""
        theming.manager().set_user_font(self.font_picker.currentData() or "")

    def _on_font_size_changed(self, value: int) -> None:
        theming.manager().set_user_font_size(int(value))

    def _on_cancel(self) -> None:
        # Revert live previews. Fonts preview live too — put back whatever
        # the session started with (no-ops when untouched).
        theming.manager().set_user_font(self._initial_font)
        theming.manager().set_user_font_size(self._initial_font_size)
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
        # Defer the confirm/inform message boxes past this button's click
        # emission — a nested modal opened synchronously from a click inside
        # an already-modal dialog is the PySide6 + py3.14 segfault pattern
        # ([[feedback-pyside-modal]]).
        QTimer.singleShot(0, self._do_sign_out)

    def _do_sign_out(self) -> None:
        ok = QMessageBox.question(
            self, "tide",
            "this will sign out of youtube music. you can sign back in any "
            "time from settings → sources → the youtube music gear. continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok != QMessageBox.Yes:
            return
        auth.clear_saved_auth()
        QMessageBox.information(
            self, "tide",
            "signed out. re-import from settings → sources whenever you like.",
        )

    def _on_open_audio_fx(self) -> None:
        """Close this dialog + jump to the full audio FX panel. Settings
        dialog is modal so we save first; the FX panel mutates state
        without needing this dialog reopened."""
        self._on_save()
        win = self.parent()
        while win is not None and not hasattr(win, "_switch_view"):
            win = win.parent()
        if win is not None:
            try:
                win._switch_view("audio_fx")
            except Exception:
                pass

    # ---------- result ----------

    def updated_settings(self) -> settings_module.Settings:
        return self._settings
