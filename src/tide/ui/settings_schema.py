"""The option-descriptor table — settings as data.

Through v1.x every user-facing option cost four hand-written touch
points: a widget in the dialog's _build_ui, a load in _populate, a store
in _on_save, and a live-apply block in window._do_open_settings. Four
places to forget, and the hidden-field bugs to prove it (local_auto_index
and the spotify tuning knobs shipped with no GUI at all).

This registry is the one place. Each :class:`OptionDesc` names a
Settings field and declares everything the generated dialog needs: which
tab/section row it lives on, what widget kind renders it, where its
choices come from, whether it flips with the personality preset, and
which MainWindow applier pushes it live on accept.

The guarantee lives in :func:`coverage_report` and its meta-test: every
``Settings`` dataclass field is either in :data:`REGISTRY` (it has a GUI
surface) or in :data:`INTERNAL_FIELDS` (a written reason why it doesn't
need one). Add a field without deciding, and the suite goes red.

Import-time Qt-free on purpose — choice providers import their
registries lazily so the meta-test (and anything else) can read the
schema without a QApplication.
"""
from __future__ import annotations

from dataclasses import dataclass, fields as _dataclass_fields

from .. import presets
from ..settings import Settings


# The widget vocabulary the generated dialog knows how to build.
#   bool   → bracket toggle / checkbox
#   choice → combo fed by ``choices``
#   int    → spin box
#   str    → line edit
#   font   → the shipped font picker (preview faces, editable, bundled
#            fonts pinned up top) — preserved as-is, not regenerated
#   custom → a registered builder on the dialog side
KINDS: tuple[str, ...] = ("bool", "choice", "int", "str", "font", "custom")

# The dialog's tab taxonomy. v1.x shipped appearance / mini player /
# playback / integrations / advanced; 2.0 keeps the shape but the mini
# tab becomes "windows" (the fullscreen options join it) and the
# hidden source tuning gets a real "sources" tab. About / session tools
# stay hand-built dialog chrome, not descriptors.
TABS: tuple[str, ...] = (
    "appearance",
    "playback",
    "sources",
    "integrations",
    "windows",
)


@dataclass(frozen=True)
class OptionDesc:
    """One user-facing option, declaratively.

    ``key`` is the Settings dataclass field name. ``choices`` is either
    an inline tuple of ``(value, label)`` rows or the NAME of a zero-arg
    provider function in this module (so pickers read the live
    registries — themes on disk, monitor sources, layout dirs — at
    dialog-open time instead of freezing a list that can drift).
    ``live`` names a MainWindow applier method run by the accept-time
    live-apply chain (window.LIVE_APPLY_ORDER); None = the value is read
    fresh wherever it's consumed, nothing to push. ``preview`` marks
    options that apply while the dialog is still open (with
    snapshot-revert on cancel — previews never commit)."""
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

def theme_choices() -> tuple[tuple[str, str], ...]:
    """Every discovered theme (bundled + system + user), sorted by
    display name — the same order the v1 picker shipped."""
    from .. import theming
    themes = theming.discover_themes()
    return tuple(
        (slug, theme.name)
        for slug, theme in sorted(themes.items(), key=lambda kv: kv[1].name)
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
    """The renderer's backdrop styles — main-window picker, where a
    backdrop always paints something."""
    from .. import backdrops
    return tuple(backdrops.choices())


def companion_backdrop_choices() -> tuple[tuple[str, str], ...]:
    """The mini/fullscreen composition: "follow" leads, "off" trails —
    a companion window can mirror the main backdrop or decline to paint."""
    from .. import backdrops
    return (
        *backdrops.choices_with_follow(),
        (backdrops.OFF, backdrops.OFF_LABEL),
    )


def audio_device_choices() -> tuple[tuple[str, str], ...]:
    """Monitor sources for the visualizer capture, after the "auto"
    default. Best-effort — enumerating sources shells out to the audio
    server, and a machine without one still gets a working picker."""
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
    """Nav-rail icon sets from the nav_icons registry. Glyph-set labels
    embed the actual glyphs so the picker previews the vibe itself; a new
    set registered in NAV_ICON_SETS appears here with zero edits."""
    from . import nav_icons
    rows: list[tuple[str, str]] = []
    for set_name in nav_icons.VALID_SETS:
        if set_name == "off":
            rows.append(("off", "off · text only"))
        elif set_name == "svg":
            rows.append(("svg", "svg · brutalist line-art icons"))
        else:
            bag = nav_icons.NAV_ICON_SETS[set_name]
            rows.append((set_name, f"{set_name} · {' '.join(bag.values())}"))
    return tuple(rows)


def corner_choices() -> tuple[tuple[str, str], ...]:
    """Corner styles with their pixel radii, straight from the one
    radius map (central_bg.CORNER_RADII) so the labels can't lie."""
    from .central_bg import CORNER_RADII
    return tuple(
        (style, f"{style} · {px}px") for style, px in CORNER_RADII.items()
    )


# Blurbs keyed by registry value. Indexing (not .get) is deliberate: a
# new tier/style landing in its registry without a blurb here raises,
# and the schema meta-test goes red instead of a picker silently
# shipping a KeyError at open time.
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
    """Motion tiers from the Intensity enum, in enum order."""
    from . import motion as motion_module
    return tuple(
        (tier.value, _MOTION_BLURBS[tier.value])
        for tier in motion_module.Intensity
    )


def scale_choices() -> tuple[tuple[str, str], ...]:
    """UI scale presets from the Scale enum; the multiplier in each label
    reads straight off the factor table so the numbers can't drift."""
    from . import scale as scale_module
    factors = getattr(scale_module, "_FACTORS")
    return tuple(
        (s.value, f"{s.value} · {factors[s]:.2f}×") for s in scale_module.Scale
    )


def loading_choices() -> tuple[tuple[str, str], ...]:
    """Loading-indicator styles from the indicator's own whitelist, each
    labelled with a tiny sample rendering."""
    from .loading_indicator import VALID_STYLES
    return tuple((style, _LOADING_BLURBS[style]) for style in VALID_STYLES)


def case_choices() -> tuple[tuple[str, str], ...]:
    """Text-case modes from theming.CASE_MODES, after the follow-the-theme
    empty default."""
    from .. import theming
    return (
        ("", "from theme"),
        *((mode, _CASE_BLURBS[mode]) for mode in theming.CASE_MODES),
    )


# ---------------------------------------------------------------------------
# live-apply vocabulary
# ---------------------------------------------------------------------------

# Applier methods the settings engine was still due to add to MainWindow
# when this schema landed — the allowlist the meta-test accepted
# alongside real hasattr(MainWindow, name) hits, so this file could land
# before the engine's window.py work. All 18 appliers now exist on
# MainWindow (each carries its contract in its own docstring;
# LIVE_APPLY_ORDER encodes scale→theme, corner→csd→translucency and
# pitch-last as data), so the allowlist is pruned to its documented end
# state: empty, with the existence test running purely on hasattr.
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
    # ---- appearance · motion & sound ----
    OptionDesc(
        key="motion", label="motion", kind="choice",
        tab="appearance", section="motion & sound",
        choices="motion_choices", per_preset=True,
        live="apply_motion_setting",
        tooltip="gates every animation in the app.",
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
        # No key name here on purpose: this layer is Qt/window-free, so
        # it can't resolve the live keymap, and a hardcoded "f11" goes
        # stale the moment the fullscreen action is rebound. The [⤢]
        # button's own tooltip advertises the live binding.
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

# Every Settings field NOT in the registry, with why it needs no settings
# row. The rule: a field lands here only when some OTHER GUI surface owns
# it (named below) or it's pure bookkeeping no user should ever set —
# never because someone forgot. (spotify_client_id isn't listed because
# it's gone: grep showed nothing ever read it — the sign-in dialog keeps
# its own field and auth_spotify resolves the effective id itself — so
# 2.0 deleted it from Settings outright.)
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
    """The concrete (value, label) rows for a choice descriptor —
    inline tuples pass through, provider names get called. Raises on a
    descriptor with no choices or a dangling provider name; both are
    programming errors the meta-test catches."""
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
    """The GUI-exposure guarantee, as data.

    Returns ``(missing, orphaned)``: Settings dataclass fields covered by
    neither REGISTRY nor INTERNAL_FIELDS, and registry keys that aren't
    dataclass fields. The meta-test asserts both empty — so no future
    Settings field can ship without either a GUI surface or a written
    reason it doesn't need one."""
    field_names = {f.name for f in _dataclass_fields(Settings)}
    registry_keys = [desc.key for desc in REGISTRY]
    covered = set(registry_keys) | INTERNAL_FIELDS
    missing = sorted(field_names - covered)
    orphaned = sorted(key for key in registry_keys if key not in field_names)
    return missing, orphaned


def stash_membership_consistent() -> list[str]:
    """Registry keys whose per_preset flag disagrees with
    presets.STASH_FIELDS membership. Empty = consistent; the per-preset
    row markers in the dialog are only honest if this holds."""
    stashed = set(presets.STASH_FIELDS)
    return sorted(
        desc.key for desc in REGISTRY
        if desc.per_preset != (desc.key in stashed)
    )
