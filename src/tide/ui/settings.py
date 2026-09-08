"""Settings dialog — generated from settings_schema.REGISTRY: tabs →
sections → one widget per descriptor kind. Adding an option: add the
Settings field, its OptionDesc, (maybe) a live applier.

The dialog reads the LIVE Settings object (the old whole-object save
raced the satellite savers). Edits are diffed against an opening
snapshot; accept writes ONLY the changed fields (save_fields) and
hands the changed-key set to window.run_live_appliers. preview=True
descriptors apply while the dialog is open and revert on cancel —
previews never commit.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import auth, settings as settings_module, theming
from . import settings_schema
from .headings import Heading, line_heading
from . import scale


DISCORD_HELP_URL = "https://discord.com/developers/applications"


# ---------------------------------------------------------------------------
# dialog-side chrome the schema doesn't carry
# ---------------------------------------------------------------------------

# Generated widgets keep their v1 attribute names (tests and muscle
# memory still find dlg.theme_picker etc.). Every REGISTRY key MUST
# have an entry — pinned by the engine tests.
_WIDGET_ATTRS: dict[str, str] = {
    "theme": "theme_picker",
    "theme_picker_show_all": "theme_show_all_toggle",
    "font_family_override": "font_picker",
    "font_size_override_pt": "font_size_spin",
    "text_case_override": "case_picker",
    "ui_scale": "scale_picker",
    "layout": "layout_picker",
    "corner_style": "corner_picker",
    "nav_icon_set": "nav_icons_picker",
    "csd_titlebar": "csd_toggle",
    "show_thumbnails": "thumbnails_picker",
    "loading_indicator_style": "loading_picker",
    "home_layout": "home_layout_picker",
    "adaptive_accent": "adaptive_toggle",
    "adaptive_background": "adaptive_bg_toggle",
    "adaptive_background_style": "adaptive_style_picker",
    "adaptive_pulse": "adaptive_pulse_toggle",
    "adaptive_text_contrast": "adaptive_text_contrast_toggle",
    "motion": "motion_picker",
    "text_transition": "text_transition_picker",
    "ui_sounds_enabled": "ui_sounds_toggle",
    "audio_device": "audio_device_picker",
    "prefetch_hover": "prefetch_hover_toggle",
    "prefetch_warm_results": "prefetch_warm_picker",
    "preserve_pitch": "preserve_pitch_toggle",
    "local_auto_index": "local_index_toggle",
    "spotify_bitrate": "spotify_bitrate_picker",
    "spotify_audio_device": "spotify_sink_edit",
    "spotify_connect_enabled": "spotify_connect_toggle",
    "discord_enabled": "discord_toggle",
    "discord_app_id": "discord_app_id",
    "discord_lyrics_enabled": "discord_lyrics_toggle",
    "discord_show_paused": "discord_paused_toggle",
    "discord_show_progress": "discord_progress_toggle",
    "discord_activity_type": "discord_activity_picker",
    "discord_details_template": "discord_details_edit",
    "discord_state_template": "discord_state_edit",
    "listenbrainz_enabled": "lb_toggle",
    "listenbrainz_token": "lb_token",
    "report_plays": "report_plays_toggle",
    "mini_mode_default": "mini_default_toggle",
    "mini_backdrop_style": "mini_backdrop_picker",
    "mini_progress_style": "mini_progress_picker",
    "mini_ticker": "mini_ticker_toggle",
    "mini_zen": "mini_zen_toggle",
    "mini_pulse": "mini_pulse_toggle",
    "mini_pulse_resize": "mini_pulse_resize_toggle",
    "mini_show_visualizer": "mini_vis_toggle",
    "fullscreen_backdrop_style": "fs_backdrop_picker",
    "fullscreen_pulse": "fs_pulse_toggle",
}

# Spin-box tuning for int descriptors (range / suffix / zero label).
_INT_SPECS: dict[str, dict] = {
    "font_size_override_pt": {
        "min": 0, "max": 24, "suffix": " pt", "special": "theme default",
    },
}

# Line-edit tuning for str descriptors (placeholder / secret echo).
_STR_SPECS: dict[str, dict] = {
    "discord_app_id": {"placeholder": "paste discord application id"},
    "discord_details_template": {"placeholder": "default · {title}"},
    "discord_state_template": {"placeholder": "default · {artists} · {album}"},
    "listenbrainz_token": {
        "placeholder": "paste your listenbrainz user token", "password": True,
    },
    "spotify_audio_device": {"placeholder": "default sink"},
}

# Controller key → dependent keys enabled only while the controller
# is on.
_ENABLE_RULES: dict[str, tuple[str, ...]] = {
    "adaptive_background": ("adaptive_background_style", "adaptive_pulse"),
    "mini_pulse": ("mini_pulse_resize",),
    "discord_enabled": (
        "discord_app_id",
        "discord_lyrics_enabled",
        "discord_show_paused",
        "discord_show_progress",
        "discord_activity_type",
        "discord_details_template",
        "discord_state_template",
    ),
    "listenbrainz_enabled": ("listenbrainz_token",),
}

# Keys whose change reshapes the DIALOG, not the app (neither `live`
# nor `preview`): key → niladic method run right after the pending
# diff is recorded.
_CHANGE_HOOKS: dict[str, str] = {
    "theme_picker_show_all": "refresh_theme_choices",
}

# kind == "custom" widget builders: key → callable(dialog) -> QWidget.
# Empty today (the font picker is its own first-class kind).
CUSTOM_BUILDERS: dict[str, object] = {}


def _dim(label: QLabel) -> QLabel:
    label.setProperty("class", "dim")
    label.setWordWrap(True)
    return label


def _page(*items) -> QScrollArea:
    """One tab page: a scrollable column of widgets/layouts, so no
    section is ever buried a full page-scroll away."""
    content = QWidget()
    col = QVBoxLayout(content)
    col.setContentsMargins(6, 10, 6, 10)
    col.setSpacing(12)
    for item in items:
        if isinstance(item, QWidget):
            col.addWidget(item)
        else:
            col.addLayout(item)
    col.addStretch(1)
    scroll = QScrollArea()
    scroll.setWidget(content)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    return scroll


class SettingsDialog(QDialog):
    """One window, every option — generated from the descriptor table.
    Saves the field-diff on accept; previews revert on cancel."""

    # The descriptors that live-preview while the dialog is open, pinned
    # against schema preview flags in tests. Everything here must have a
    # branch in _apply_preview AND a revert in _on_cancel.
    PREVIEW_KEYS = frozenset({
        "theme",
        "font_family_override",
        "font_size_override_pt",
        "text_case_override",
        "show_thumbnails",
        "layout",
    })

    def __init__(self, current_settings: settings_module.Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("tide — settings")
        self.setModal(True)
        scale.fit_dialog(self, 680, 720, min_width=620)
        # The scale can change from inside this very dialog; the theme
        # manager re-emits theme_changed after a scale change, so refit
        # then (grow only — a box the user dragged bigger stays bigger).
        theming.manager().theme_changed.connect(self._refit_for_scale)

        # The LIVE settings object; accept writes changed fields onto it.
        self._settings = current_settings
        self._defaults = settings_module.Settings()
        self._descs = settings_schema.REGISTRY
        self._by_key = settings_schema.by_key()

        self._widgets: dict[str, QWidget] = {}
        self._preset_markers: dict[str, QLabel] = {}
        self._pending: dict[str, object] = {}
        self._originals: dict[str, object] = {}
        self._accepted_changes: tuple[str, ...] = ()
        self._populating = False
        # reject() is every cancel path (button, Esc, window close);
        # these guard the revert to run at most once, never after accept.
        self._accepted = False
        self._reverted = False
        # A strip-builder pick made while the LAYOUT is only a preview
        # parks here — accept lands layout + bar together, cancel drops
        # both.
        self._pending_strip_overrides: dict | None = None
        self._pending_strip_base = ""

        # Preview snapshot — what cancel puts back.
        self._initial_theme = current_settings.theme
        self._initial_thumbnails = current_settings.show_thumbnails or "theme"
        self._initial_font = current_settings.font_family_override or ""
        self._initial_font_size = int(current_settings.font_size_override_pt or 0)
        self._initial_case = current_settings.text_case_override or ""
        self._initial_layout = current_settings.layout or "classic"
        self._initial_overrides = dict(current_settings.layout_overrides or {})

        self._build_ui()
        self._populate()

        # Manual session refresh reports through a window signal (worker
        # + dedup live on MainWindow). Qt drops the connection with the
        # dialog — no teardown bookkeeping.
        win = self._find_main_window()
        if win is not None:
            win.session_refresh_finished.connect(self._on_refresh_session_finished)

    # ---------- generated build ----------

    def _build_ui(self) -> None:
        section_order: dict[str, list[str]] = {
            tab: [] for tab in settings_schema.TABS
        }
        forms: dict[tuple[str, str], QFormLayout] = {}
        for desc in self._descs:
            widget = self._make_widget(desc)
            self._widgets[desc.key] = widget
            setattr(self, _WIDGET_ATTRS[desc.key], widget)
            slot = (desc.tab, desc.section)
            form = forms.get(slot)
            if form is None:
                form = QFormLayout()
                forms[slot] = form
                section_order[desc.tab].append(desc.section)
            row = self._compose_row(desc, widget)
            if desc.kind == "bool":
                form.addRow("", row)     # the toggle carries its own label
            else:
                form.addRow(f"{desc.label}:", row)

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        # documentMode's tab-bar base line is drawn by Fusion from the
        # unthemed palette — a pure-white hairline on dark themes.
        tabs.tabBar().setDrawBase(False)
        for tab in settings_schema.TABS:
            items: list = list(self._tab_prelude(tab))
            for section in section_order[tab]:
                items.append(Heading(section, 34))
                items.extend(self._section_prelude(tab, section))
                items.append(forms[(tab, section)])
                items.extend(self._section_chrome(tab, section))
            items.extend(self._tab_chrome(tab))
            tabs.addTab(_page(*items), tab)
        # About is dialog chrome, not options — its own quiet tab.
        tabs.addTab(_page(*self._about_items()), "about")
        self._tabs = tabs

        self.save_btn = QPushButton("save")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._on_save)
        self.cancel_btn = QPushButton("cancel")
        self.cancel_btn.clicked.connect(self._on_cancel)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.save_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 14)
        root.setSpacing(10)
        root.addWidget(tabs, stretch=1)
        root.addLayout(btn_row)

    def _make_widget(self, desc: settings_schema.OptionDesc) -> QWidget:
        kind = desc.kind
        if kind == "bool":
            w: QWidget = QCheckBox(desc.label)
            w.toggled.connect(
                lambda _on=False, k=desc.key: self._on_option_changed(k))
        elif kind == "choice":
            w = QComboBox()
            for value, label in self._choice_rows(desc):
                w.addItem(label, value)
            w.currentIndexChanged.connect(
                lambda _i=0, k=desc.key: self._on_option_changed(k))
        elif kind == "int":
            spec = _INT_SPECS.get(desc.key, {})
            w = QSpinBox()
            w.setRange(int(spec.get("min", 0)), int(spec.get("max", 9999)))
            if spec.get("special"):
                w.setSpecialValueText(spec["special"])
            if spec.get("suffix"):
                w.setSuffix(spec["suffix"])
            w.valueChanged.connect(
                lambda _v=0, k=desc.key: self._on_option_changed(k))
        elif kind == "str":
            spec = _STR_SPECS.get(desc.key, {})
            w = QLineEdit()
            if spec.get("placeholder"):
                w.setPlaceholderText(spec["placeholder"])
            if spec.get("password"):
                w.setEchoMode(QLineEdit.Password)
            w.textChanged.connect(
                lambda _t="", k=desc.key: self._on_option_changed(k))
        elif kind == "font":
            w = self._build_font_picker(desc)
        elif kind == "custom":
            builder = CUSTOM_BUILDERS.get(desc.key)
            if builder is None:
                raise KeyError(
                    f"{desc.key}: custom kind with no registered builder")
            w = builder(self)
        else:
            raise ValueError(f"{desc.key}: unknown kind {kind!r}")
        if desc.tooltip:
            w.setToolTip(desc.tooltip)
        return w

    # ---------- personality-aware theme picking ----------

    def _choice_rows(self, desc: settings_schema.OptionDesc):
        """``settings_schema.resolve_choices`` for every descriptor but
        ``theme``, which is narrowed to the active personality (the
        schema keeps the whole catalog; the narrowing needs a Settings
        object it doesn't have)."""
        if desc.key == "theme":
            return self._theme_rows()
        return settings_schema.resolve_choices(desc)

    def _show_all_themes(self) -> bool:
        """The checkbox once it exists; the stored preference while the
        theme combo is being built (theme comes first in registry
        order)."""
        w = self._widgets.get("theme_picker_show_all")
        if w is not None:
            return bool(w.isChecked())
        return bool(getattr(self._settings, "theme_picker_show_all", False))

    def _theme_rows(self, selected: str = ""):
        """The theme combo's rows for the current personality +
        show-all state. Kept whatever their aesthetic: the opening
        theme and the pick in flight (see theme_choices_for for why).
        The pick is read from the pending diff, not the combo, so a
        mid-dialog personality flip — which clears the diff and
        re-bases the snapshot — doesn't drag the outgoing theme into
        the incoming list.
        """
        return settings_schema.theme_choices_for(
            str(getattr(self._settings, "preset", "") or ""),
            show_all=self._show_all_themes(),
            keep=(self._initial_theme,
                  str(self._pending.get("theme") or ""),
                  selected),
        )

    def refresh_theme_choices(self, select: str = "") -> None:
        """Rebuild the theme combo in place — the show-all hook, and
        how a freshly saved theme joins the list. ``select`` names a
        slug to land on; otherwise the current pick is kept. Runs under
        the populate guard so re-adding rows can never fire a theme
        preview."""
        combo = self._widgets.get("theme")
        if combo is None:
            return
        pending = str(self._pending.get("theme") or "")
        rows = self._theme_rows(selected=select)
        self._populating = True
        try:
            combo.clear()
            for value, label in rows:
                combo.addItem(label, value)
            # Requested slug, else the pick in flight, else the opening
            # theme — landing anywhere else would turn a list rebuild
            # into a theme change nobody asked for.
            idx = -1
            for candidate in (select, pending, self._initial_theme):
                idx = combo.findData(candidate) if candidate else -1
                if idx >= 0:
                    break
            if idx >= 0:
                combo.setCurrentIndex(idx)
        finally:
            self._populating = False

    def _compose_row(self, desc: settings_schema.OptionDesc,
                     widget: QWidget) -> QWidget:
        """The widget plus its row chrome: per-key extras (help buttons)
        and the ·per personality· marker for STASH_FIELDS descriptors."""
        extras: list[QWidget] = []
        if desc.key == "discord_app_id":
            self.discord_help = QPushButton("get an app id  →")
            self.discord_help.setFlat(True)
            self.discord_help.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl(DISCORD_HELP_URL)))
            extras.append(self.discord_help)
        elif desc.key == "listenbrainz_token":
            self.lb_help = QPushButton("get a token  →")
            self.lb_help.setFlat(True)
            self.lb_help.clicked.connect(
                lambda: QDesktopServices.openUrl(
                    QUrl("https://listenbrainz.org/profile/")))
            extras.append(self.lb_help)
        marker: QLabel | None = None
        if desc.per_preset:
            marker = QLabel("·per personality·")
            marker.setProperty("class", "dim")
            marker.setToolTip(
                "flips with the personality preset — each side remembers "
                "its own value")
            self._preset_markers[desc.key] = marker
        if not extras and marker is None:
            return widget
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(widget, stretch=1)
        for extra in extras:
            row.addWidget(extra)
        if marker is not None:
            row.addWidget(marker)
        return box

    def _build_font_picker(self, desc: settings_schema.OptionDesc) -> QComboBox:
        """The v1 full-family font picker: every system family in its
        own face, bundled fonts pinned up top, editable."""
        picker = QComboBox()
        picker.addItem("from theme", "")
        for f in ("IBM Plex Mono", "JetBrains Mono", "Inter"):
            picker.addItem(f"{f} · bundled", f)
        try:
            from PySide6.QtGui import QFont, QFontDatabase
            from PySide6.QtWidgets import QListView

            def _preview_font(family: str) -> QFont:
                # Pin the size so a display face can't blow up row heights.
                font = QFont(family)
                font.setPointSize(10)
                return font

            existing = {picker.itemData(i) for i in range(picker.count())}
            for i in range(1, picker.count()):
                picker.setItemData(
                    i, _preview_font(picker.itemData(i)), Qt.FontRole
                )
            latin = QFontDatabase.WritingSystem.Latin
            for f in sorted(QFontDatabase.families()):
                if f in existing or f.startswith("."):
                    continue    # dupes + private system faces
                systems = QFontDatabase.writingSystems(f)
                if systems and latin not in systems:
                    # Symbol/emoji/CJK-only faces are useless as a UI
                    # font, and rendering their names in themselves is a
                    # fallback stress test Qt has been seen to lose.
                    continue
                picker.addItem(f, f)
                picker.setItemData(
                    picker.count() - 1, _preview_font(f), Qt.FontRole
                )
            # One prototype row sizes the whole popup. Without this,
            # every app restyle re-measured EVERY row in its own font —
            # the layout storm a real session died inside.
            view = picker.view()
            if isinstance(view, QListView):
                view.setUniformItemSizes(True)
        except Exception:
            pass
        # Editable so any family can be pasted. Typed names apply on
        # save; picked rows apply live.
        picker.setEditable(True)
        picker.setInsertPolicy(QComboBox.NoInsert)
        picker.currentIndexChanged.connect(
            lambda _i=0, k=desc.key: self._on_option_changed(k))
        return picker

    # ---------- dialog chrome (hand-built, non-descriptor surfaces) ----------

    def _tab_prelude(self, tab: str) -> list:
        if tab == "appearance":
            # Personality leads — everything below is a detail of it.
            return self._personality_chrome()
        if tab == "sources":
            return [_dim(QLabel(
                "credentials, folders and per-source on/off switches live "
                "on the sources page (the nav rail's [source] tab). these "
                "are the power knobs behind them."
            ))]
        return []

    def _section_prelude(self, tab: str, section: str) -> list[QWidget]:
        if (tab, section) == ("windows", "mini player"):
            mini_key = self._binding_text("mini_mode", "ctrl+m")
            opener = ("clicking the now-playing art"
                      + (f" or {mini_key}" if mini_key else ""))
            self.mini_blurb = _dim(QLabel(
                f"the small frameless window. open it by {opener}. "
                "everything here is also on the mini's own right-click menu."
            ))
            return [self.mini_blurb]
        return []

    def _section_chrome(self, tab: str, section: str) -> list:
        if (tab, section) == ("integrations", "play reporting"):
            report_explainer = _dim(QLabel(
                "on: when a song starts, tide sends the same play event the "
                "yt music web player sends. it goes to your own account and "
                "nowhere else, and your history and recommendations pick up "
                "what you play in tide. off: tide reports nothing, and the "
                "only traffic is fetching the music."
            ))
            self.taste_btn = QPushButton("tune recommendations  →")
            self.taste_btn.setFlat(True)
            self.taste_btn.clicked.connect(self._on_open_taste)
            taste_blurb = _dim(QLabel(
                "pick the artists youtube music should treat as your taste. "
                "this steers home shelves and radio."
            ))
            col = QVBoxLayout()
            col.setSpacing(6)
            col.addWidget(report_explainer)
            col.addSpacing(8)
            col.addWidget(self.taste_btn, alignment=Qt.AlignLeft)
            col.addWidget(taste_blurb)
            return [col]
        return []

    def _tab_chrome(self, tab: str) -> list:
        if tab == "appearance":
            return self._power_tool_chrome()
        if tab == "playback":
            fx_heading = Heading("audio fx", 34)
            fx_key = self._binding_text("view_audio_fx", "ctrl+8")
            fx_open = (f"open the full panel with {fx_key} (or the [fx] nav "
                       "tab)" if fx_key
                       else "open the full panel from the [fx] nav tab")
            self.fx_blurb = _dim(QLabel(
                "10-band eq + reverb + loudness norm + the rest of the rack.\n"
                f"{fx_open}, or use the [fx] popover on the now-playing strip."
            ))
            self.audio_fx_open_btn = QPushButton("open audio fx panel  →")
            self.audio_fx_open_btn.clicked.connect(self._on_open_audio_fx)
            col = QVBoxLayout()
            col.setSpacing(6)
            col.addWidget(self.fx_blurb)
            col.addWidget(self.audio_fx_open_btn, alignment=Qt.AlignLeft)
            return [fx_heading, col]
        if tab == "sources":
            session_heading = Heading("youtube music session", 34)
            self.refresh_session_btn = QPushButton("refresh session")
            self.refresh_session_btn.clicked.connect(self._on_refresh_session)
            # Inline state row: at rest → "checking browsers…" → outcome.
            # The window also toasts, but this modal sits over the toast
            # host — the row is what the user actually sees.
            self.refresh_session_status = _dim(QLabel(self._session_state_text()))
            refresh_key = self._binding_text("refresh_session", "ctrl+shift+r")
            key_line = (f" {refresh_key} does the same thing without opening "
                        "settings." if refresh_key else "")
            self.session_blurb = _dim(QLabel(
                "pulls fresh cookies from whichever browser is still signed "
                "in to youtube music. use it when tide wakes up with "
                "anonymous results because the saved session expired."
                f"{key_line}"
            ))
            self.sign_out_btn = QPushButton("sign out + re-import session")
            self.sign_out_btn.clicked.connect(self._on_sign_out)
            session_row = QHBoxLayout()
            session_row.addWidget(self.refresh_session_btn)
            session_row.addWidget(self.refresh_session_status, stretch=1)
            col = QVBoxLayout()
            col.setSpacing(6)
            col.addLayout(session_row)
            col.addWidget(self.session_blurb)
            col.addSpacing(8)
            col.addWidget(self.sign_out_btn, alignment=Qt.AlignLeft)
            return [session_heading, col]
        return []

    def _power_tool_chrome(self) -> list:
        """Power-tool launchers + the shortcuts section. Every open is
        DEFERRED out of the click emission: the theme editor via our
        own singleShot; open_strip_builder / open_glyph_editor /
        open_keymap_editor defer internally, so wiring those straight
        to clicked is sanctioned."""
        tools_heading = Heading("power tools", 34)
        tools_blurb = _dim(QLabel(
            "the deep-cut editors. everything they change is previewable, "
            "revertable and saved per-field — no config files, ever."
        ))
        self.theme_editor_btn = QPushButton("theme editor  →")
        self.theme_editor_btn.setToolTip(
            "edit the active theme's colors, typography and radius live; "
            "save the result as your own theme")
        self.theme_editor_btn.clicked.connect(self._on_open_theme_editor)
        self.strip_builder_btn = QPushButton("strip builder  →")
        self.strip_builder_btn.setToolTip(
            "build your player bar: pick a variant per slot with a live "
            "miniature preview")
        self.strip_builder_btn.clicked.connect(self._on_open_strip_builder)
        self.glyphs_btn = QPushButton("glyph editor  →")
        self.glyphs_btn.setToolTip(
            "swap any transport glyph (▶ ▮▮ ♥ …) for 1-3 characters of "
            "your own — per personality")
        self.glyphs_btn.clicked.connect(self._on_open_glyphs)
        tools_row = QHBoxLayout()
        tools_row.setSpacing(8)
        tools_row.addWidget(self.theme_editor_btn)
        tools_row.addWidget(self.strip_builder_btn)
        tools_row.addWidget(self.glyphs_btn)
        tools_row.addStretch(1)
        tools_col = QVBoxLayout()
        tools_col.setSpacing(6)
        tools_col.addWidget(tools_blurb)
        tools_col.addLayout(tools_row)

        shortcuts_heading = Heading("shortcuts", 34)
        shortcuts_blurb = _dim(QLabel(
            "every keyboard shortcut is rebindable. the keymap is global "
            "— muscle memory doesn't flip with the personality."
        ))
        self.keymap_btn = QPushButton("keymap editor  →")
        self.keymap_btn.clicked.connect(self._on_open_keymap)
        sc_col = QVBoxLayout()
        sc_col.setSpacing(6)
        sc_col.addWidget(shortcuts_blurb)
        sc_col.addWidget(self.keymap_btn, alignment=Qt.AlignLeft)
        return [tools_heading, tools_col, shortcuts_heading, sc_col]

    # ---------- personality (v2.0 phase 4 — the settings re-pick route) ----

    def _personality_chrome(self) -> list:
        """The "personality" section: a quick flip and the door back
        to the chooser. Deliberately NOT a descriptor: ``preset`` may
        only move through presets.apply_preset (stash outgoing,
        restore incoming, managers in contract order) — the accept
        diff's generic setattr would leave ``settings.preset`` naming
        a personality nothing ever applied, holding the other one's
        stash. So: dialog chrome, committing through the same
        app.commit_personality_choice as the wizard and chooser."""
        heading = Heading("personality", 34)
        blurb = _dim(QLabel(
            "tide is two players sharing one library. each side remembers "
            "its own theme, layout, glyphs and tweaks — flipping back "
            "brings yours back exactly as you left them."
        ))
        self.personality_picker = QComboBox()
        self.personality_picker.setToolTip(
            "flips the whole look at once: theme, layout, motion, corners, "
            "nav icons, backdrops, sounds and glyphs.")
        self.personality_picker.activated.connect(self._on_personality_picked)
        self._sync_personality_picker()
        self.chooser_btn = QPushButton("choose your tide  →")
        self.chooser_btn.setToolTip(
            "reopen the full-screen chooser — a live preview of both, "
            "side by side.")
        self.chooser_btn.clicked.connect(self._on_open_chooser)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.personality_picker)
        row.addWidget(self.chooser_btn)
        row.addStretch(1)
        col = QVBoxLayout()
        col.setSpacing(6)
        col.addWidget(blurb)
        col.addLayout(row)
        return [heading, col]

    def _sync_personality_picker(self) -> None:
        """(Re)build the flip picker's rows, signals blocked — only a
        user activation may start a flip."""
        from .. import presets
        from .chooser import PANE_ORDER
        picker = self.personality_picker
        blocked = picker.blockSignals(True)
        try:
            picker.clear()
            for preset_id in PANE_ORDER:
                definition = presets.BUILTINS[preset_id]
                picker.addItem(f"{definition.label} · {definition.blurb}",
                               preset_id)
            active = str(getattr(self._settings, "preset", "") or "")
            idx = picker.findData(active)
            if idx < 0:
                # Pre-adoption / hand-edited config: don't claim a
                # personality the app never applied. The row goes away
                # on the first real pick.
                picker.insertItem(0, "— not picked yet —", "")
                idx = 0
            picker.setCurrentIndex(idx)
        finally:
            picker.blockSignals(blocked)

    def _on_personality_picked(self, index: int) -> None:
        preset_id = str(self.personality_picker.itemData(index) or "")
        if not preset_id or preset_id == str(
                getattr(self._settings, "preset", "") or ""):
            return      # re-picking what's already on is not a flip
        # A flip restyles and rebuilds slots — get off the combo's
        # activated emission first, like every restyle/modal path here.
        QTimer.singleShot(0, lambda: self._flip_personality(preset_id))

    def _flip_personality(self, preset_id: str) -> None:
        """Commit a flip from inside the open dialog, through
        app.commit_personality_choice — the ONE commit path the wizard
        and chooser take too, so stamp / slot seeding / manager order /
        field-scoped save are identical whichever door the user came
        through. With the MainWindow in reach it lands live via
        switch_preset."""
        from .. import app as app_module
        win = self._find_main_window()
        if not app_module.commit_personality_choice(
                self._settings, preset_id, window=win):
            self._sync_personality_picker()     # unknown id: put it back
            return
        self._tool_sound("toggle_on")
        self._rebase_after_personality_flip()

    def _on_open_chooser(self) -> None:
        # Deferred out of the click emission — a modal opened
        # synchronously from a click inside an already-modal dialog is
        # the PySide6 + py3.14 segfault pattern ([[feedback-pyside-modal]]).
        self._tool_sound("modal_open")
        QTimer.singleShot(0, self._do_open_chooser)

    def _do_open_chooser(self) -> None:
        """Reopen "choose your tide" over the settings dialog.
        app.run_chooser owns the modal and the commit; a dismissal
        resolves to nothing and leaves the personality untouched."""
        from .. import app as app_module
        win = self._find_main_window()
        choice = app_module.run_chooser(self._settings, parent=self,
                                        window=win)
        self._tool_sound("modal_close")
        if choice:
            # Even a re-pick of the current personality re-applies the
            # preset (dropping any open theme preview) — re-base so the
            # dialog and the app agree.
            self._rebase_after_personality_flip()

    def _rebase_after_personality_flip(self) -> None:
        """Re-open this dialog onto the personality that just landed.
        A flip is a COMMIT — every snapshot here was taken against the
        OUTGOING personality, so without a re-base accept would write
        the outgoing look back over the flip and cancel would "revert"
        a commit. Per-personality pending edits are dropped (the flip
        replaced them); shared ones are re-staged so a half-typed
        token survives. The WINDOW's snapshot gets the same re-base
        (rebase_settings_snapshot) — skipping it handed the outgoing
        personality's theme to the incoming one."""
        keep = {
            key: value for key, value in self._pending.items()
            if key in self._by_key and not self._by_key[key].per_preset
        }
        self._pending.clear()
        # A parked strip pick describes the OUTGOING personality's layout.
        self._pending_strip_overrides = None
        self._pending_strip_base = ""
        s = self._settings
        self._initial_theme = s.theme
        self._initial_thumbnails = s.show_thumbnails or "theme"
        self._initial_font = s.font_family_override or ""
        self._initial_font_size = int(s.font_size_override_pt or 0)
        self._initial_case = s.text_case_override or ""
        self._initial_layout = s.layout or "classic"
        self._initial_overrides = dict(s.layout_overrides or {})
        win = self._find_main_window()
        if win is not None and hasattr(win, "rebase_settings_snapshot"):
            win.rebase_settings_snapshot(s)
        # Rebuild the theme rows BEFORE _populate re-selects: the combo
        # still holds the outgoing personality's catalog, and _populate
        # would fall back to a theme the user never picked. The cleared
        # _pending / re-based _initial_theme above keep the outgoing
        # theme out of the keeper rows.
        self.refresh_theme_choices()
        self._populate()
        self._sync_personality_picker()
        for key, value in keep.items():
            # Through the widgets, so the normal change handler rebuilds
            # the diff. Nothing previews: every preview descriptor is
            # per-personality and was dropped.
            self._set_widget_value(self._by_key[key], value)

    def _set_widget_value(self, desc: settings_schema.OptionDesc,
                          value) -> None:
        """Push one value into its widget — _populate_widget's twin for
        the re-stage half of a personality re-base."""
        w = self._widgets.get(desc.key)
        if w is None:
            return
        if desc.kind == "bool":
            w.setChecked(bool(value))
        elif desc.kind == "choice":
            idx = w.findData(value)
            if idx >= 0:
                w.setCurrentIndex(idx)
        elif desc.kind == "int":
            w.setValue(int(value or 0))
        elif desc.kind == "str":
            w.setText(str(value or ""))
        elif desc.kind == "font":
            idx = w.findData(str(value or ""))
            if idx >= 0:
                w.setCurrentIndex(idx)
            else:
                w.setCurrentText(str(value or ""))

    def _about_items(self) -> list:
        from .. import __version__
        title = QLabel(f"tide  v{__version__}")
        title.setStyleSheet("font-weight: 600;")
        tagline = QLabel("one music player, two personalities.")
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
            lambda: QDesktopServices.openUrl(
                QUrl("https://github.com/captiencelovesarch/tide"))
        )
        self.issues_btn = QPushButton("report a bug  →")
        self.issues_btn.setFlat(True)
        self.issues_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl("https://github.com/captiencelovesarch/tide/issues"))
        )
        about_links = QHBoxLayout()
        about_links.addWidget(self.repo_btn)
        about_links.addWidget(self.issues_btn)
        about_links.addStretch(1)
        col = QVBoxLayout()
        col.setSpacing(4)
        col.addWidget(title)
        col.addWidget(tagline)
        col.addSpacing(6)
        col.addWidget(credits)
        col.addLayout(about_links)
        heading = Heading("about", 34)
        return [heading, col]

    # ---------- populate / read / diff ----------

    def _populate(self) -> None:
        """Load the live settings into the widgets, change handling
        suppressed — loading saved values must not fire previews."""
        self._populating = True
        try:
            for desc in self._descs:
                self._originals[desc.key] = self._normalize(
                    desc, getattr(self._settings, desc.key))
                self._populate_widget(desc)
            for controller in _ENABLE_RULES:
                self._apply_enable_rule(controller)
        finally:
            self._populating = False

    def _populate_widget(self, desc: settings_schema.OptionDesc) -> None:
        w = self._widgets[desc.key]
        raw = getattr(self._settings, desc.key)
        if desc.kind == "bool":
            w.setChecked(bool(raw))
        elif desc.kind == "choice":
            idx = w.findData(raw)
            if idx < 0:
                # Stored value isn't pickable (stale slug) — fall back to
                # the dataclass default, which the meta-test guarantees
                # is a row.
                idx = w.findData(getattr(self._defaults, desc.key))
            if idx >= 0:
                w.setCurrentIndex(idx)
        elif desc.kind == "int":
            w.setValue(int(raw or 0))
        elif desc.kind == "str":
            w.setText(str(raw or ""))
        elif desc.kind == "font":
            value = str(raw or "")
            idx = w.findData(value)
            if idx >= 0:
                w.setCurrentIndex(idx)
            else:
                # Custom family not in the list — show it as free text.
                w.setCurrentText(value)
        # custom widgets own their own populate

    def _normalize(self, desc: settings_schema.OptionDesc, raw) -> object:
        """The opening value through the same lens _read_value uses, so
        an untouched widget can never register as a phantom diff."""
        if desc.kind == "bool":
            return bool(raw)
        if desc.kind == "int":
            return int(raw or 0)
        if desc.kind in ("str", "font"):
            return str(raw or "").strip()
        return raw

    def _read_value(self, desc: settings_schema.OptionDesc):
        w = self._widgets[desc.key]
        if desc.kind == "bool":
            return bool(w.isChecked())
        if desc.kind == "choice":
            data = w.currentData()
            return self._originals.get(desc.key) if data is None else data
        if desc.kind == "int":
            return int(w.value())
        if desc.kind == "str":
            return w.text().strip()
        if desc.kind == "font":
            # A picked row's data wins — INCLUDING "from theme" (data
            # ""). Free text counts only when it differs from the row's
            # label, i.e. the user actually typed a family name (the
            # "from theme"-as-a-font regression).
            data = w.currentData()
            text = w.currentText().strip()
            row_label = w.itemText(w.currentIndex()).strip()
            value = data if text == row_label else text
            return value or ""
        return self._originals.get(desc.key)   # custom: widget owns state

    def widget_for(self, key: str) -> QWidget | None:
        return self._widgets.get(key)

    # ---------- change handling / previews ----------

    def _on_option_changed(self, key: str) -> None:
        if self._populating:
            return
        desc = self._by_key[key]
        value = self._read_value(desc)
        self._pending[key] = value
        if key in _ENABLE_RULES:
            self._apply_enable_rule(key)
        hook = _CHANGE_HOOKS.get(key)
        if hook is not None:
            getattr(self, hook)()
        if desc.preview:
            self._apply_preview(key, value)

    def _apply_enable_rule(self, controller: str) -> None:
        on = bool(self._read_value(self._by_key[controller]))
        for dep in _ENABLE_RULES[controller]:
            w = self._widgets.get(dep)
            if w is not None:
                w.setEnabled(on)

    def _apply_preview(self, key: str, value) -> None:
        """Live-preview one option through the managers. Never persists
        — cancel reverts via the opening snapshot, and the managers
        no-op on same-value pushes."""
        if key == "theme":
            if value:
                theming.manager().apply(str(value))
        elif key == "font_family_override":
            theming.manager().set_user_font(str(value or ""))
        elif key == "font_size_override_pt":
            theming.manager().set_user_font_size(int(value or 0))
        elif key == "text_case_override":
            theming.set_case_override(str(value or ""))
        elif key == "show_thumbnails":
            self._push_thumbnails(str(value or "theme"))
        elif key == "layout":
            self._preview_layout(str(value or ""))

    def _push_thumbnails(self, value: str) -> None:
        from .track_row import set_thumbnail_override
        set_thumbnail_override(value)
        # Force the live theme to re-emit so attached delegates repaint.
        current = theming.manager().current()
        if current is not None:
            theming.manager().theme_changed.emit(current)

    def _preview_layout(self, slug: str) -> None:
        """Live-apply a layout pick through the parent MainWindow;
        per-slot overrides ride along unchanged (the strip builder owns
        editing those)."""
        if not slug:
            return
        parent = self.parent()
        if parent is None or not hasattr(parent, "apply_layout"):
            return
        from .. import layout as layout_module
        effective = layout_module.manager().apply(
            slug, dict(self._settings.layout_overrides or {}))
        if effective is not None:
            parent.apply_layout(effective)

    # ---------- accept / cancel ----------

    def _on_save(self) -> None:
        """Write ONLY the fields whose widget value differs from the
        opening snapshot, save exactly those via save_fields, and
        remember the changed keys for the live-apply chain."""
        s = self._settings
        changed: list[str] = []
        for desc in self._descs:
            value = self._read_value(desc)
            if value is None:
                continue
            if value != self._originals[desc.key]:
                setattr(s, desc.key, value)
                changed.append(desc.key)
        # A parked strip pick commits only if the accepted layout is
        # still the one it was diffed against; otherwise it's dropped —
        # its diff describes another layout's slots.
        parked = self._pending_strip_overrides
        if parked is not None:
            self._pending_strip_overrides = None
            if (s.layout or "classic") == self._pending_strip_base:
                self._commit_strip_overrides(parked)
        # Flipping the play-reporting toggle (or having it on) answers
        # the one-time question and stops the upgrade pointer.
        # Untouched-and-off doesn't — we can't know the user looked.
        if ((bool(s.report_plays) or "report_plays" in changed)
                and not s.report_plays_answered):
            s.report_plays_answered = True
            changed.append("report_plays_answered")
        self._accepted_changes = tuple(changed)
        self._accepted = True
        if changed:
            settings_module.save_fields(s, *changed)
        self.accept()

    def changed_keys(self) -> tuple[str, ...]:
        """The accepted diff the live-apply chain runs on; empty until
        an accept happens."""
        return self._accepted_changes

    def _on_cancel(self) -> None:
        # Everything happens in reject() so button-cancel, Esc and the
        # window-manager close share ONE revert path.
        self.reject()

    def reject(self) -> None:   # cancel button, Esc, AND window close
        """QDialog routes Esc and the titlebar close straight here, NOT
        through the cancel button — so the preview revert lives here,
        guarded to run once and never after an accept."""
        if not self._accepted and not self._reverted:
            self._reverted = True
            self._revert_previews()
        super().reject()

    def _revert_previews(self) -> None:
        # Put back the opening state (no-ops when untouched).
        theming.manager().set_user_font(self._initial_font)
        theming.manager().set_user_font_size(self._initial_font_size)
        theming.set_case_override(self._initial_case)
        theme_now = self._widgets["theme"].currentData()
        if self._initial_theme and self._initial_theme != theme_now:
            theming.manager().apply(self._initial_theme)
        thumbs_now = self._widgets["show_thumbnails"].currentData() or "theme"
        if self._initial_thumbnails != thumbs_now:
            self._push_thumbnails(self._initial_thumbnails)
        layout_now = self._widgets["layout"].currentData() or "classic"
        if (self._initial_layout != layout_now
                or self._pending_strip_overrides is not None):
            # A parked strip pick is pixels-only — re-applying the
            # opening layout+overrides erases it.
            self._pending_strip_overrides = None
            self._revert_layout_preview()

    def _revert_layout_preview(self) -> None:
        parent = self.parent()
        if parent is None or not hasattr(parent, "apply_layout"):
            return
        from .. import layout as layout_module
        effective = layout_module.manager().apply(
            self._initial_layout, dict(self._initial_overrides))
        if effective is not None:
            parent.apply_layout(effective)

    # ---------- session / advanced chrome handlers ----------

    def _on_sign_out(self) -> None:
        # Defer the message boxes past the click emission
        # ([[feedback-pyside-modal]] segfault pattern).
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

    def _find_main_window(self):
        """Walk up to the MainWindow — it owns the refresh worker, the
        dedup flags and the completion signal."""
        win = self.parent()
        while win is not None and not hasattr(win, "refresh_session_manual"):
            win = win.parent()
        return win

    def _binding_text(self, action_id: str, fallback: str) -> str:
        """A live key binding for dialog blurbs (binding_display), so a
        rebind can't orphan the copy. Parentless dialogs (tests) fall
        back to the shipped default; a deliberately unbound action
        resolves to "" so callers drop the mention."""
        win = self._find_main_window()
        if win is not None and hasattr(win, "binding_display"):
            return win.binding_display(action_id)
        return fallback

    def _tool_sound(self, key: str) -> None:
        """Click feedback via the main window's UiSoundPlayer; silently
        absent when the dialog is parentless."""
        win = self._find_main_window()
        if win is not None and hasattr(win, "_ui_sound"):
            win._ui_sound(key)

    def _session_state_text(self) -> str:
        """The yt session at rest. Expiry is best effort — imports from
        before tide recorded it show plain "signed in", which must not
        read as a problem."""
        if not auth.have_auth():
            return "not signed in"
        remaining = auth.seconds_until_expiry()
        if remaining is None:
            return "signed in"
        if remaining <= 0:
            return "signed in · session expired"
        days = int(remaining // 86400)
        hours = int(remaining // 3600)
        if days >= 1:
            when = f"{days}d"
        elif hours >= 1:
            when = f"{hours}h"
        else:
            # Under an hour "0h" reads like a bug — count minutes, and
            # round anything under one up so it never says "0m".
            when = f"{max(1, int(remaining // 60))}m"
        return f"signed in · expires in {when}"

    def _on_refresh_session(self) -> None:
        win = self._find_main_window()
        if win is None:
            # No MainWindow parent (tests) — nothing to delegate to,
            # nothing will emit completion.
            self.refresh_session_status.setText("couldn't reach the main window")
            return
        self.refresh_session_btn.setEnabled(False)
        self.refresh_session_status.setText("checking browsers…")
        win.refresh_session_manual()

    def _on_refresh_session_finished(self, ok: bool, message: str) -> None:
        self.refresh_session_btn.setEnabled(True)
        self.refresh_session_status.setText(message)

    def _on_open_taste(self) -> None:
        # Deferred past the click emission ([[feedback-pyside-modal]]).
        QTimer.singleShot(0, self._do_open_taste)

    def _do_open_taste(self) -> None:
        """Taste-profile editor. Talks to the live YT source from the
        registry; reports when there isn't one."""
        from ..sources import registry
        src = registry().get("ytmusic")
        if src is None or not src.supports("taste"):
            QMessageBox.information(
                self, "tune recommendations",
                "youtube music isn't signed in, so there's nothing to tune.")
            return
        dlg = _TasteProfileDialog(src, self)
        dlg.exec()

    def _on_open_audio_fx(self) -> None:
        """Save + close, then jump to the full audio FX panel (which
        mutates state without this dialog)."""
        self._on_save()
        win = self.parent()
        while win is not None and not hasattr(win, "_switch_view"):
            win = win.parent()
        if win is not None:
            try:
                win._switch_view("audio_fx")
            except Exception:
                pass

    # ---------- power tools ----------

    def _on_open_theme_editor(self) -> None:
        # Deferred past the click emission ([[feedback-pyside-modal]]).
        self._tool_sound("modal_open")
        QTimer.singleShot(0, self._do_open_theme_editor)

    def _do_open_theme_editor(self) -> None:
        from .theme_editor import ThemeEditorDialog
        dlg = ThemeEditorDialog(parent=self)
        dlg.theme_saved.connect(self._on_theme_saved)
        dlg.exec()
        dlg.deleteLater()
        self._tool_sound("modal_close")

    def _refit_for_scale(self, _theme) -> None:
        scale.fit_dialog(self, 680, 720, min_width=620, grow_only=True)

    def _on_theme_saved(self, slug: str) -> None:
        """A theme was just written AND applied by the editor (registry
        already refreshed). Persisting ``settings.theme`` is OUR job:
        rebuild the combo (via refresh_theme_choices, so the slug is
        listed even across the aesthetic filter) and select it, so the
        pick rides the normal pending-diff + accept path. Cancel still
        reverts the applied theme — previews never commit; the saved
        theme dir just stays on disk."""
        combo = self._widgets.get("theme")
        if combo is None:
            return
        self.refresh_theme_choices(select=slug)
        if combo.currentData() != slug:
            return      # not in the registry after all — nothing staged
        # The rebuild ran under the populate guard (no signal fired) —
        # file the pending change and its preview by hand.
        self._on_option_changed("theme")

    def _on_open_strip_builder(self) -> None:
        """open_strip_builder defers construction internally. A parked
        pick re-opens as the builder's starting state, not the stale
        saved dict."""
        self._tool_sound("modal_open")
        from .strip_builder import open_strip_builder
        overrides = (dict(self._pending_strip_overrides)
                     if self._pending_strip_overrides is not None
                     else dict(self._settings.layout_overrides or {}))
        open_strip_builder(
            parent=self,
            overrides=overrides,
            on_chosen=self._on_strip_overrides,
        )

    def _layout_preview_pending(self) -> bool:
        """True while the layout combo shows an unaccepted preview — a
        strip diff made now describes a layout settings doesn't hold
        yet."""
        layout_now = self._widgets["layout"].currentData() or "classic"
        return layout_now != self._initial_layout

    def _on_strip_overrides(self, payload: dict) -> None:
        """The strip builder's accepted diff: committed immediately
        when no layout preview is pending, otherwise PARKED — previewed
        on the window, committed only when accept commits the layout it
        was diffed against, dropped on cancel. An empty dict means
        "clear to layout defaults" and MUST still apply."""
        if self._layout_preview_pending():
            self._pending_strip_overrides = dict(payload)
            self._pending_strip_base = (
                self._widgets["layout"].currentData() or "classic")
            self._preview_parked_strip()
            return
        self._commit_strip_overrides(payload)

    def _commit_strip_overrides(self, payload: dict) -> None:
        """update_overrides → apply_layout's keep-list path + a
        field-scoped save — the ONE sanctioned persist route."""
        win = self._find_main_window()
        if win is not None and hasattr(win, "apply_strip_overrides"):
            win.apply_strip_overrides(dict(payload))
        else:
            # Parentless (tests): keep settings + layout manager
            # truthful with no window strip to rebuild.
            from .. import layout as layout_module
            self._settings.layout_overrides = dict(payload)
            layout_module.manager().update_overrides(dict(payload))
            settings_module.save_fields(self._settings, "layout_overrides")
        # The committed bar is a COMMIT — a later cancel must revert
        # onto these overrides, not the opening snapshot.
        self._initial_overrides = dict(self._settings.layout_overrides or {})

    def _preview_parked_strip(self) -> None:
        """Show the parked bar on the previewed layout — pixels only;
        nothing persists, cancel re-applies the opening snapshot."""
        parent = self.parent()
        if parent is None or not hasattr(parent, "apply_layout"):
            return
        from .. import layout as layout_module
        effective = layout_module.manager().apply(
            self._pending_strip_base,
            dict(self._pending_strip_overrides or {}))
        if effective is not None:
            parent.apply_layout(effective)

    def _on_open_glyphs(self) -> None:
        """open_glyph_editor defers internally. Parented to this dialog,
        so its refresh_glyphs walk reaches the MainWindow behind it."""
        self._tool_sound("modal_open")
        from .glyph_editor import open_glyph_editor
        open_glyph_editor(self._settings, self)

    def _on_open_keymap(self) -> None:
        """open_keymap_editor defers internally."""
        self._tool_sound("modal_open")
        from .keymap_editor import open_keymap_editor
        open_keymap_editor(self, self._settings)

    # ---------- result ----------

    def updated_settings(self) -> settings_module.Settings:
        return self._settings


class _TasteProfileDialog(QDialog):
    """Pick the artists the source's recommender should treat as
    taste. Write-only by API design: YT returns the tunable list but
    not the current selection, so this is "select and apply", not an
    editor."""

    def __init__(self, source, parent=None) -> None:
        super().__init__(parent)
        from PySide6.QtCore import QObject, QThread, Signal as _Signal
        from PySide6.QtWidgets import QListWidget, QListWidgetItem
        from .. import qthreads
        self.setWindowTitle("tune recommendations")
        self.setModal(True)
        scale.fit_dialog(self, 420, 520)
        self._source = source

        blurb = QLabel(
            "check the artists you actually listen to and hit apply. "
            "youtube music rebuilds its recommendations from the "
            "selection. the api doesn't report what's currently selected, "
            "so this always applies fresh."
        )
        blurb.setWordWrap(True)
        blurb.setProperty("class", "dim")

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("filter artists…")
        self._filter.textChanged.connect(self._apply_filter)

        self._list = QListWidget()
        self._list.setUniformItemSizes(True)

        self._status = QLabel("loading artists…")
        self._status.setProperty("class", "dim")

        self._apply_btn = QPushButton("apply")
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._on_apply)
        close_btn = QPushButton("close")
        close_btn.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addWidget(self._status, stretch=1)
        btns.addWidget(self._apply_btn)
        btns.addWidget(close_btn)

        col = QVBoxLayout(self)
        col.addWidget(blurb)
        col.addWidget(self._filter)
        col.addWidget(self._list, stretch=1)
        col.addLayout(btns)

        class _Loader(QObject):
            done = _Signal(list)

            def run(self_inner) -> None:
                try:
                    self_inner.done.emit(source.get_taste_profile())
                except Exception:
                    self_inner.done.emit([])

        thread = QThread()
        worker = _Loader()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_loaded)
        worker.done.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self._load_thread = thread
        self._load_worker = worker
        qthreads.retain(thread, worker)
        thread.start()

    def _on_loaded(self, artists: list) -> None:
        from PySide6.QtWidgets import QListWidgetItem
        if not artists:
            self._status.setText("couldn't load the artist list")
            return
        for name in artists:
            item = QListWidgetItem(str(name))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self._list.addItem(item)
        self._status.setText(f"{len(artists)} artists")
        self._apply_btn.setEnabled(True)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def _on_apply(self) -> None:
        from PySide6.QtCore import QObject, QThread, Signal as _Signal
        from .. import qthreads
        picked = [self._list.item(i).text()
                  for i in range(self._list.count())
                  if self._list.item(i).checkState() == Qt.Checked]
        if not picked:
            self._status.setText("nothing checked, nothing sent")
            return
        self._apply_btn.setEnabled(False)
        self._status.setText("applying…")
        source = self._source

        class _Applier(QObject):
            done = _Signal(bool)

            def run(self_inner) -> None:
                try:
                    self_inner.done.emit(bool(source.set_taste_profile(picked)))
                except Exception:
                    self_inner.done.emit(False)

        thread = QThread()
        worker = _Applier()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_applied)
        worker.done.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self._apply_thread = thread
        self._apply_worker = worker
        qthreads.retain(thread, worker)
        thread.start()

    def _on_applied(self, ok: bool) -> None:
        self._apply_btn.setEnabled(True)
        self._status.setText(
            "applied. recommendations will update" if ok
            else "youtube refused, try again later")
