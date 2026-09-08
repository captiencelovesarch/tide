"""Option descriptors — the settings dialog as data.

Each :class:`OptionDesc` names a Settings field and declares its
tab/section row, widget kind, choice source, per-preset flag, and live
applier. :func:`coverage_report` + its meta-test force every field into
:data:`REGISTRY` or :data:`INTERNAL_FIELDS` (with a written reason).

No Qt at import time — choice providers import lazily so the meta-test
runs without a QApplication.
"""
from __future__ import annotations

from dataclasses import dataclass, fields as _dataclass_fields

from .. import presets
from ..settings import Settings


# bool → toggle, choice → combo fed by ``choices``, int → spin box,
# str → line edit, font → the v1 font picker, custom → a registered
# builder on the dialog side.
KINDS: tuple[str, ...] = ("bool", "choice", "int", "str", "font", "custom")

# Dialog tabs. About / session tools stay hand-built chrome.
TABS: tuple[str, ...] = (
    "appearance",
    "playback",
    "sources",
    "integrations",
    "windows",
)


@dataclass(frozen=True)
class OptionDesc:
    """One user-facing option.

    ``key`` is the Settings field name. ``choices`` is an inline tuple
    of ``(value, label)`` rows or the NAME of a zero-arg provider in
    this module, called at dialog-open so pickers read live registries.
    ``live`` names the MainWindow applier run by the accept-time chain
    (window.LIVE_APPLY_ORDER); None = read fresh where consumed.
    ``preview`` options apply while the dialog is open, with
    snapshot-revert on cancel — previews never commit."""
    key: str
    label: str
    kind: str
    tab: str
    section: str
    choices: tuple[tuple[object, str], ...] | str | None = None
    per_preset: bool = False
    live: str | None = None
    preview: bool = False
    tooltip: str = ""
    advanced: bool = False


# ---------------------------------------------------------------------------
# choice providers — the live registries, resolved at call time
# ---------------------------------------------------------------------------

def _theme_rows() -> tuple[tuple[str, str, str], ...]:
    """(slug, display name, aesthetic) per discovered theme, sorted by
    display name. Aesthetic is "" for a theme that never DECLARED one:
    tide's mono guess picks a QSS dialect, but the picker must not hide
    a theme on a guess (see :func:`theme_choices_for`)."""
    from .. import theming
    themes = theming.discover_themes()
    rows: list[tuple[str, str, str]] = []
    for slug, theme in sorted(themes.items(), key=lambda kv: kv[1].name):
        declared = bool(getattr(theme, "aesthetic_declared", True))
        aesthetic = str(getattr(theme, "aesthetic", "") or "")
        rows.append((slug, theme.name, aesthetic if declared else ""))
    return tuple(rows)


def theme_choices() -> tuple[tuple[str, str], ...]:
    """The full catalog, unfiltered — what the meta-tests and the theme
    editor read. The appearance tab narrows it via
    :func:`theme_choices_for` (a provider takes no arguments)."""
    return tuple((slug, name) for slug, name, _aesthetic in _theme_rows())


def theme_choices_for(preset_id: str, show_all: bool = False,
                      keep: tuple[str, ...] | list[str] | set[str] = (),
                      ) -> tuple[tuple[str, str], ...]:
    """The catalog narrowed to one personality's [meta] aesthetic.

    ``show_all`` or an unknown/absent ``preset_id`` returns the catalog
    verbatim. ``keep`` slugs stay listed whatever their aesthetic: the
    picker passes the theme it opened on and the theme it's showing —
    a picker that can't display the value it holds silently re-writes
    it on the next accept. A theme with no declared aesthetic stays
    listed for BOTH sides: hand-installed themes predate the key, and
    filing them by tide's mono guess would make a brutalist user's own
    themes look deleted (a v1.x regression).
    """
    # Personality ids and theme aesthetics share one vocabulary, so the
    # match is ==.
    rows = _theme_rows()
    if show_all or preset_id not in presets.BUILTINS:
        return tuple((slug, name) for slug, name, _a in rows)
    keep_set = {str(slug) for slug in keep if slug}
    return tuple(
        (slug, name) for slug, name, aesthetic in rows
        if not aesthetic or aesthetic == preset_id or slug in keep_set
    )


def layout_choices() -> tuple[tuple[str, str], ...]:
    """Every discovered layout preset, sorted by display name."""
    from .. import layout as layout_module
    layouts = layout_module.discover_layouts()
    return tuple(
        (lay.slug, lay.name)
        for lay in sorted(layouts.values(), key=lambda l: l.name)
    )


def backdrop_choices() -> tuple[tuple[str, str], ...]:
    """Backdrop styles for the main window, which always paints one."""
    from .. import backdrops
    return tuple(backdrops.choices())


def companion_backdrop_choices() -> tuple[tuple[str, str], ...]:
    """Mini/fullscreen backdrops: "follow" leads, "off" trails."""
    from .. import backdrops
    return (
        *backdrops.choices_with_follow(),
        (backdrops.OFF, backdrops.OFF_LABEL),
    )


def audio_device_choices() -> tuple[tuple[str, str], ...]:
    """Monitor sources after the "auto" default. Best-effort — a
    machine with no audio server still gets a working picker."""
    rows: list[tuple[str, str]] = [("", "auto (tide's audio only)")]
    try:
        from .. import audio_capture
        rows.extend(
            (name, label) for name, label in audio_capture.list_monitor_sources()
        )
    except Exception:
        pass
    return tuple(rows)


def nav_icon_choices() -> tuple[tuple[str, str], ...]:
    """Nav-rail icon sets; glyph-set labels embed the actual glyphs."""
    from . import nav_icons
    rows: list[tuple[str, str]] = []
    for set_name in nav_icons.VALID_SETS:
        if set_name == "off":
            rows.append(("off", "off · text only"))
        elif set_name == "svg":
            rows.append(("svg", "svg · line-art icons"))
        else:
            bag = nav_icons.NAV_ICON_SETS[set_name]
            rows.append((set_name, f"{set_name} · {' '.join(bag.values())}"))
    return tuple(rows)


def corner_choices() -> tuple[tuple[str, str], ...]:
    """Corner styles labelled with their radii, read straight from
    central_bg.CORNER_RADII."""
    from .central_bg import CORNER_RADII
    return tuple(
        (style, f"{style} · {px}px") for style, px in CORNER_RADII.items()
    )


# Indexed (not .get) on purpose: a new registry value without a blurb
# fails the schema meta-test instead of KeyError-ing at picker-open.
_MOTION_BLURBS: dict[str, str] = {
    "off": "off · instant transitions",
    "lite": "lite · signature + everyday",
    "full": "full · everything including ambient",
}

_LOADING_BLURBS: dict[str, str] = {
    "off": "off",
    "numbers": "numbers · 42%",
    "blocks": "blocks · █████░░░░░",
    "dots": "dots · ●●●●●○○○○○",
    "ascii": "ascii · [#####-----]",
}

_CASE_BLURBS: dict[str, str] = {
    "lower": "lowercase",
    "upper": "UPPERCASE",
    "normal": "As Written",
    "leet": "l33t · L1K3 TH1Z",
    "zalgo": "zalgo · c̛u̅rsed",
}


def motion_choices() -> tuple[tuple[str, str], ...]:
    """Motion tiers from the Intensity enum, in order."""
    from . import motion as motion_module
    return tuple(
        (tier.value, _MOTION_BLURBS[tier.value])
        for tier in motion_module.Intensity
    )


def text_transition_choices() -> tuple[tuple[str, str], ...]:
    """Track-change text styles, in text_fx's order."""
    from . import text_fx
    return text_fx.choices()


def scale_choices() -> tuple[tuple[str, str], ...]:
    """Scale presets; the multiplier labels read off the factor table."""
    from . import scale as scale_module
    factors = getattr(scale_module, "_FACTORS")
    return tuple(
        (s.value, f"{s.value} · {factors[s]:.2f}×") for s in scale_module.Scale
    )


def loading_choices() -> tuple[tuple[str, str], ...]:
    """Loading-indicator styles, each labelled with a sample."""
    from .loading_indicator import VALID_STYLES
    return tuple((style, _LOADING_BLURBS[style]) for style in VALID_STYLES)


def case_choices() -> tuple[tuple[str, str], ...]:
    """theming.CASE_MODES after the follow-the-theme empty default."""
    from .. import theming
    return (
        ("", "from theme"),
        *((mode, _CASE_BLURBS[mode]) for mode in theming.CASE_MODES),
    )


# ---------------------------------------------------------------------------
# live-apply vocabulary
# ---------------------------------------------------------------------------

# Applier names the meta-test accepts before they exist on MainWindow —
# lets a schema entry land ahead of its window.py work. Empty now.
PENDING_LIVE_APPLIERS: dict[str, str] = {}


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------

REGISTRY: tuple[OptionDesc, ...] = (
    # ---- appearance · theme ----
    OptionDesc(
        key="theme", label="theme", kind="choice",
        tab="appearance", section="theme",
        choices="theme_choices", per_preset=True,
        live="apply_theme_bundle_setting", preview=True,
        tooltip="themes hot-swap while you browse. cancel puts the old "
                "one back.",
    ),
    OptionDesc(
        key="theme_picker_show_all",
        label="show all themes · including the other personality's",
        kind="bool", tab="appearance", section="theme",
        # No live applier: this reshapes the PICKER above it, not the
        # app — the dialog rebuilds the theme rows on the spot via
        # _CHANGE_HOOKS → refresh_theme_choices.
        tooltip="the list above shows the themes built for the "
                "personality you're wearing. tick this to see all of "
                "them — picking one from the other side takes you to "
                "that tide, with everything it remembers.",
    ),
    # ---- appearance · typography ----
    OptionDesc(
        key="font_family_override", label="font", kind="font",
        tab="appearance", section="typography",
        per_preset=True, live="apply_theme_bundle_setting", preview=True,
        tooltip="overrides the active theme's font. empty = use whatever "
                "the theme says.",
    ),
    OptionDesc(
        key="font_size_override_pt", label="size", kind="int",
        tab="appearance", section="typography",
        per_preset=True, live="apply_theme_bundle_setting", preview=True,
        tooltip="0 pt = follow the theme. ui scale still multiplies "
                "whichever base wins.",
    ),
    OptionDesc(
        key="text_case_override", label="text case", kind="choice",
        tab="appearance", section="typography",
        choices="case_choices", per_preset=True,
        live="apply_theme_bundle_setting", preview=True,
        tooltip="beats the theme's typography.case everywhere chrome text "
                "routes through styled_case (nav, buttons, toasts, …).",
    ),
    OptionDesc(
        key="ui_scale", label="ui scale", kind="choice",
        tab="appearance", section="typography",
        choices="scale_choices", live="apply_ui_scale_setting",
        tooltip="multiplies the theme's type size, which cascades to "
                "every widget. applied before the theme restyle.",
    ),
    # ---- appearance · layout ----
    OptionDesc(
        key="layout", label="layout", kind="choice",
        tab="appearance", section="layout",
        choices="layout_choices", per_preset=True,
        live="apply_layout_setting", preview=True,
        tooltip="the arrangement of the player strip and panes. per-slot "
                "variants live in the strip builder.",
    ),
    # ---- appearance · chrome ----
    OptionDesc(
        key="corner_style", label="corners", kind="choice",
        tab="appearance", section="chrome",
        choices="corner_choices", per_preset=True,
        live="apply_corner_setting",
        tooltip="a sticky @radius override on the theming manager — every "
                "widget that reads @radius matches the chosen softness.",
    ),
    OptionDesc(
        key="nav_icon_set", label="nav icons", kind="choice",
        tab="appearance", section="chrome",
        choices="nav_icon_choices", per_preset=True,
        live="apply_nav_icons_setting",
        tooltip="a small glyph rendered before each nav label.",
    ),
    OptionDesc(
        key="csd_titlebar",
        label="tide titlebar (themed window chrome — matches the active "
              "theme)",
        kind="bool", tab="appearance", section="chrome",
        live="apply_csd_setting",
        tooltip="off = the compositor's native decoration.",
    ),
    OptionDesc(
        key="show_thumbnails", label="thumbnails", kind="choice",
        tab="appearance", section="chrome",
        choices=(
            ("theme", "from theme"),
            ("on", "always show"),
            ("off", "never show"),
        ),
        per_preset=True, live="apply_thumbnails_setting", preview=True,
        tooltip="whether track rows show cover thumbnails.",
    ),
    OptionDesc(
        key="loading_indicator_style", label="loading bar", kind="choice",
        tab="appearance", section="chrome",
        choices="loading_choices", live="apply_loading_setting",
        tooltip="the status-bar loading indicator's rendering.",
    ),
    OptionDesc(
        key="home_layout", label="home layout", kind="choice",
        tab="appearance", section="chrome",
        choices=(
            ("patterns", "patterns · hero, grids, mosaics, charts, moods"),
            ("shelves", "plain shelves · the classic rows"),
        ),
        tooltip="picked up the next time the home page rebuilds.",
    ),
    # ---- appearance · backdrop ----
    OptionDesc(
        key="adaptive_accent", label="shift accent to album art",
        kind="bool", tab="appearance", section="backdrop",
        per_preset=True, live="apply_adaptive_setting",
        tooltip="the accent color follows the playing album's palette.",
    ),
    OptionDesc(
        key="adaptive_background",
        label="tint central area with album-derived gradient",
        kind="bool", tab="appearance", section="backdrop",
        per_preset=True, live="apply_adaptive_setting",
        tooltip="independent of the accent shift — either can be on "
                "without the other.",
    ),
    OptionDesc(
        key="adaptive_background_style", label="style", kind="choice",
        tab="appearance", section="backdrop",
        choices="backdrop_choices", per_preset=True,
        live="apply_adaptive_setting",
    ),
    OptionDesc(
        key="adaptive_pulse", label="pulse the gradient on heavy bass",
        kind="bool", tab="appearance", section="backdrop",
        per_preset=True, live="apply_adaptive_setting",
        tooltip="needs the monitor capture running, so it costs a little "
                "constant cpu during playback.",
    ),
    OptionDesc(
        key="adaptive_text_contrast", label="keep text readable over the backdrop",
        kind="bool", tab="appearance", section="backdrop",
        per_preset=True, live="apply_text_contrast_setting",
        tooltip="nudges text and dim text until they clear a contrast "
                "floor against whatever sits behind them. a colour you "
                "picked yourself in the theme editor is left alone.",
    ),
    # ---- appearance · motion & sound ----
    OptionDesc(
        key="motion", label="motion", kind="choice",
        tab="appearance", section="motion & sound",
        choices="motion_choices", per_preset=True,
        live="apply_motion_setting",
        tooltip="gates every animation in the app.",
    ),
    OptionDesc(
        key="text_transition", label="text transition", kind="choice",
        tab="appearance", section="motion & sound",
        choices="text_transition_choices",
        live="apply_text_transition_setting",
        tooltip="how the title changes when the track does. motion off makes it instant.",
    ),
    OptionDesc(
        key="ui_sounds_enabled",
        label="ui sounds (nav · modals · toggles · auto-mutes during "
              "playback)",
        kind="bool", tab="appearance", section="motion & sound",
        per_preset=True, live="apply_ui_sounds_setting",
    ),
    # ---- appearance · visualizer ----
    OptionDesc(
        key="audio_device", label="visualizer audio", kind="choice",
        tab="appearance", section="visualizer",
        choices="audio_device_choices", live="apply_audio_device_setting",
        tooltip="which monitor source the visualizer captures. backup for "
                "the visualizer's own cog menu; auto = tide's audio only.",
    ),

    # ---- playback · instant play ----
    OptionDesc(
        key="prefetch_hover", label="pre-resolve stream on hover",
        kind="bool", tab="playback", section="instant play",
        live="apply_prefetch_setting",
        tooltip="resolves stream urls before you click so playback starts "
                "faster.",
    ),
    OptionDesc(
        key="prefetch_warm_results", label="warm results", kind="choice",
        tab="playback", section="instant play",
        choices=((0, "off"), (2, "top 2"), (3, "top 3"), (5, "top 5")),
        tooltip="how many top search hits to resolve in the background. "
                "read per-search — no restart needed.",
    ),
    # ---- playback · speed ----
    OptionDesc(
        key="preserve_pitch", label="preserve pitch when changing speed",
        kind="bool", tab="playback", section="speed",
        live="apply_pitch_setting",
        tooltip="mpv's scaletempo keeps pitch steady as speed changes. "
                "default off — the tide aesthetic is the slowed / "
                "sped-with-pitch one.",
    ),

    # ---- sources · local files ----
    OptionDesc(
        key="local_auto_index", label="re-index music folder at launch",
        kind="bool", tab="sources", section="local files",
        advanced=True,
        tooltip="walks the local music folder in the background at "
                "startup. the folder itself is picked in the source "
                "panel's local gear.",
    ),
    # ---- sources · spotify ----
    OptionDesc(
        key="spotify_bitrate", label="bitrate", kind="choice",
        tab="sources", section="spotify",
        choices=(
            (96, "96 kbps"),
            (160, "160 kbps"),
            (320, "320 kbps · premium"),
        ),
        advanced=True,
        tooltip="takes effect the next time the spotify backend starts.",
    ),
    OptionDesc(
        key="spotify_audio_device", label="librespot sink", kind="str",
        tab="sources", section="spotify",
        advanced=True,
        tooltip="pulse/pipewire sink name passed to librespot. empty = "
                "default sink.",
    ),
    OptionDesc(
        key="spotify_connect_enabled",
        label="show tide as a spotify connect device",
        kind="bool", tab="sources", section="spotify",
        advanced=True,
        tooltip="off = librespot runs with --disable-discovery, "
                "local-only.",
    ),

    # ---- integrations · discord rich presence ----
    OptionDesc(
        key="discord_enabled", label="enable discord rich presence",
        kind="bool", tab="integrations", section="discord rich presence",
        live="apply_discord_setting",
    ),
    OptionDesc(
        key="discord_app_id", label="app id", kind="str",
        tab="integrations", section="discord rich presence",
        live="apply_discord_setting",
        tooltip="create an application at the discord developer portal, "
                "copy its application id, paste it here. tide shows "
                "whatever name / image you gave that app.",
    ),
    OptionDesc(
        key="discord_lyrics_enabled", label="show live lyric in presence",
        kind="bool", tab="integrations", section="discord rich presence",
        live="apply_discord_setting",
        tooltip="the current synced line replaces artist · album on your "
                "profile. everyone can see it, and lyrics can be "
                "explicit.",
    ),
    OptionDesc(
        key="discord_show_paused",
        label="keep a '⏸ paused' presence instead of clearing it",
        kind="bool", tab="integrations", section="discord rich presence",
        live="apply_discord_setting",
    ),
    OptionDesc(
        key="discord_show_progress",
        label="show the elapsed / total progress bar",
        kind="bool", tab="integrations", section="discord rich presence",
        live="apply_discord_setting",
    ),
    OptionDesc(
        key="discord_activity_type", label="activity verb", kind="choice",
        tab="integrations", section="discord rich presence",
        choices=(
            ("listening", "listening to …"),
            ("playing", "playing …"),
            ("watching", "watching …"),
        ),
        live="apply_discord_setting",
    ),
    OptionDesc(
        key="discord_details_template", label="line 1", kind="str",
        tab="integrations", section="discord rich presence",
        live="apply_discord_setting",
        tooltip="takes {title} {artists} {album} {source} placeholders. "
                "empty = the classic default, {title}.",
    ),
    OptionDesc(
        key="discord_state_template", label="line 2", kind="str",
        tab="integrations", section="discord rich presence",
        live="apply_discord_setting",
        tooltip="takes {title} {artists} {album} {source} placeholders. "
                "empty = {artists} · {album}. a live lyric still takes "
                "over while one is playing.",
    ),
    # ---- integrations · listenbrainz ----
    OptionDesc(
        key="listenbrainz_enabled", label="enable listenbrainz scrobbling",
        kind="bool", tab="integrations", section="listenbrainz scrobbling",
        live="apply_listenbrainz_setting",
        tooltip="submits 'playing now' on track start and a 'listen' once "
                "you've heard 30s (or 50% / 4 minutes).",
    ),
    OptionDesc(
        key="listenbrainz_token", label="user token", kind="str",
        tab="integrations", section="listenbrainz scrobbling",
        live="apply_listenbrainz_setting",
        tooltip="listenbrainz.org → settings → token.",
    ),
    # ---- integrations · play reporting ----
    OptionDesc(
        key="report_plays", label="report plays to youtube music",
        kind="bool", tab="integrations", section="play reporting",
        tooltip="on: tide sends the same play event the yt music web "
                "player sends, to your own account and nowhere else, so "
                "your recommendations learn from tide plays. read at "
                "play time — no push needed. (the accept path must still "
                "stamp report_plays_answered when this is touched.)",
    ),

    # ---- windows · mini player ----
    OptionDesc(
        key="mini_mode_default", label="start in mini player",
        kind="bool", tab="windows", section="mini player",
        tooltip="startup-only — the next launch opens straight into the "
                "mini.",
    ),
    OptionDesc(
        key="mini_backdrop_style", label="backdrop", kind="choice",
        tab="windows", section="mini player",
        choices="companion_backdrop_choices", per_preset=True,
        live="apply_mini_setting",
    ),
    OptionDesc(
        key="mini_progress_style", label="progress", kind="choice",
        tab="windows", section="mini player",
        choices=(
            ("ring", "border ring · the window edge fills"),
            ("thin", "thin bar"),
        ),
        live="apply_mini_setting",
    ),
    OptionDesc(
        key="mini_ticker", label="live synced-lyric ticker line",
        kind="bool", tab="windows", section="mini player",
        live="apply_mini_setting",
    ),
    OptionDesc(
        key="mini_zen", label="auto-hide controls when idle",
        kind="bool", tab="windows", section="mini player",
        live="apply_mini_setting",
    ),
    OptionDesc(
        key="mini_pulse", label="backdrop breathes with the bass",
        kind="bool", tab="windows", section="mini player",
        per_preset=True, live="apply_mini_setting",
    ),
    OptionDesc(
        key="mini_pulse_resize",
        label="window breathes too · resizes on bass",
        kind="bool", tab="windows", section="mini player",
        live="apply_mini_setting",
        tooltip="the card physically swells from its center on bass — "
                "rides the pulse envelope, so it needs the pulse on.",
    ),
    OptionDesc(
        key="mini_show_visualizer", label="visualizer instead of album art",
        kind="bool", tab="windows", section="mini player",
        live="apply_mini_setting",
        tooltip="the capture only runs while the vis page is on screen "
                "and something is playing.",
    ),
    # ---- windows · fullscreen ----
    OptionDesc(
        key="fullscreen_backdrop_style", label="backdrop", kind="choice",
        tab="windows", section="fullscreen",
        choices="companion_backdrop_choices", per_preset=True,
        live="apply_fullscreen_setting",
        # No hotkey named on purpose: this layer can't read the live
        # keymap, and a hardcoded "f11" goes stale on rebind. The [⤢]
        # button's tooltip advertises the live binding.
        tooltip="the lean-back now-playing window (the [⤢] button on "
                "the strip).",
    ),
    OptionDesc(
        key="fullscreen_pulse", label="backdrop swells with the bass",
        kind="bool", tab="windows", section="fullscreen",
        per_preset=True, live="apply_fullscreen_setting",
        tooltip="shares the mini's capture consumer; only runs while the "
                "window is up.",
    ),
)


# ---------------------------------------------------------------------------
# fields with no descriptor — each with its reason
# ---------------------------------------------------------------------------

# Every Settings field NOT in the registry, with why: another GUI
# surface owns it, or it's bookkeeping no user sets. (spotify_client_id
# isn't here — nothing read it, so 2.0 deleted it from Settings.)
INTERNAL_FIELDS: frozenset[str] = frozenset({
    # bookkeeping — stamped by the app, never chosen by the user
    "first_launch_complete",    # onboarding-completion stamp
    "preset",                   # active personality id — chooser/switcher owns it
    "preset_chosen",            # explicit-pick stamp for the chooser
    "preset_state",             # per-personality stash — presets.py owns it
    "window_sizes",             # per-layout size memory — resize/close record it
    "report_plays_answered",    # one-time upgrade-pointer stamp
    # remembered widget state — the surface that shows it owns it
    "mini_lyrics_open",         # the mini's lyrics panel remembers itself
    "nav_rail_collapsed",       # the rail's own chevron (and Ctrl+B) owns it
    "mini_pin",                 # the mini's own right-click menu owns pinning
    "fullscreen_pane",          # the fullscreen window's pane toggles own it
    "volume",                   # the player bar's volume control owns it
    "sleep_preset_minutes",     # the sleep popover remembers its last pick
    "playback_speed",           # the speed button owns it (debounced save)
    # owned by a dedicated tool / panel
    "audio_fx_state",           # the fx rack owns it (json blob, own panel)
    "layout_overrides",         # the strip builder owns per-slot variants
    "keymap",                   # the keymap editor owns bindings
    "glyph_overrides",          # the glyph editor owns per-glyph swaps
    # owned by the source panel (its gears do creds, dirs, toggles)
    "sources_enabled",          # per-source on/off lives on the source page
    "active_source",            # picked by using the source page
    "federated_search",         # the source page's federate checkbox
    "local_music_dir",          # local gear's folder picker (needs a file dialog)
    "subsonic_url",             # subsonic gear / onboarding sign-in form
    "subsonic_user",            # ”
    "subsonic_pass",            # ”
    "subsonic_auth_style",      # ” (part of the same sign-in form)
})


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def resolve_choices(desc: OptionDesc) -> tuple[tuple[object, str], ...]:
    """Concrete (value, label) rows — inline tuples pass through,
    provider names get called. Raises on missing choices or a dangling
    provider (the meta-test catches both)."""
    if desc.choices is None:
        raise ValueError(f"{desc.key} has no choices (kind={desc.kind})")
    if isinstance(desc.choices, str):
        provider = globals().get(desc.choices)
        if provider is None or not callable(provider):
            raise KeyError(
                f"{desc.key}: choices provider {desc.choices!r} is not a "
                f"function in settings_schema"
            )
        return tuple((value, label) for value, label in provider())
    return tuple(desc.choices)


def by_key() -> dict[str, OptionDesc]:
    """{field name: descriptor} for engine-side lookups."""
    return {desc.key: desc for desc in REGISTRY}


def coverage_report() -> tuple[list[str], list[str]]:
    """Returns ``(missing, orphaned)``: Settings fields covered by
    neither REGISTRY nor INTERNAL_FIELDS, and registry keys that aren't
    dataclass fields. The meta-test asserts both empty."""
    field_names = {f.name for f in _dataclass_fields(Settings)}
    registry_keys = [desc.key for desc in REGISTRY]
    covered = set(registry_keys) | INTERNAL_FIELDS
    missing = sorted(field_names - covered)
    orphaned = sorted(key for key in registry_keys if key not in field_names)
    return missing, orphaned


def stash_membership_consistent() -> list[str]:
    """Registry keys whose per_preset flag disagrees with
    presets.STASH_FIELDS — the dialog's per-preset row markers lie
    unless this comes back empty."""
    stashed = set(presets.STASH_FIELDS)
    return sorted(
        desc.key for desc in REGISTRY
        if desc.per_preset != (desc.key in stashed)
    )
