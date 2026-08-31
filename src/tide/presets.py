"""Personality presets — the v2.0 "two tides" core.

tide presents as two different players: "brutalist" (mono, still,
text-bracket chrome) and "modern" (adaptive color, motion, soft
corners). Secretly it's one app — a preset is the bundle of look/feel
settings that flips between the two atomically.

Each personality keeps its own tweaks: switching stashes the outgoing
preset's STASH_FIELDS into ``Settings.preset_state`` and restores the
incoming one's stash (or its builtin defaults on first visit), so
brutalist → modern → brutalist brings back your gruvbox, your slots,
everything.

Import-time Qt-free on purpose: ``apply_preset`` runs at startup before
any window exists, and the unit tests drive it with fake managers.
Anything Qt-adjacent is imported lazily inside the functions.
"""
from __future__ import annotations

import copy
from dataclasses import MISSING, dataclass, fields

from .settings import Settings


@dataclass(frozen=True)
class PresetDef:
    """A builtin personality: id + the look/feel values a fresh visit
    (no stash yet) starts from. Fields with a Settings twin use the same
    name so restore() can copy them across by string."""
    id: str
    label: str
    blurb: str
    theme: str
    layout: str
    motion: str
    corner_style: str
    nav_icon_set: str
    text_case_override: str
    adaptive_accent: bool
    adaptive_background: bool
    adaptive_background_style: str
    adaptive_pulse: bool
    mini_backdrop_style: str
    fullscreen_backdrop_style: str
    mini_pulse: bool
    fullscreen_pulse: bool
    ui_sounds_enabled: bool
    show_thumbnails: str
    sound_pack: str = "default"
    glyph_pack: str = "default"


BUILTINS: dict[str, PresetDef] = {
    "brutalist": PresetDef(
        id="brutalist",
        label="brutalist",
        blurb="a music player. nothing else.",
        theme="brutalist-mono",
        layout="classic",
        motion="off",
        corner_style="sharp",
        nav_icon_set="off",
        text_case_override="",
        adaptive_accent=False,
        adaptive_background=False,
        adaptive_background_style="field",
        adaptive_pulse=False,
        mini_backdrop_style="off",
        fullscreen_backdrop_style="off",
        mini_pulse=False,
        fullscreen_pulse=False,
        ui_sounds_enabled=False,
        # mono + art: art is content, not chrome — thumbnails stay on.
        show_thumbnails="on",
    ),
    "modern": PresetDef(
        id="modern",
        label="modern",
        blurb="the same songs, alive.",
        theme="adaptive",
        layout="classic",
        motion="full",
        corner_style="soft",
        nav_icon_set="classic",
        text_case_override="",
        adaptive_accent=True,
        adaptive_background=True,
        adaptive_background_style="liquid",
        adaptive_pulse=True,
        mini_backdrop_style="follow",
        fullscreen_backdrop_style="follow",
        mini_pulse=True,
        fullscreen_pulse=True,
        ui_sounds_enabled=False,
        show_thumbnails="theme",
    ),
}


# The Settings fields a personality owns — everything stash/restore
# round-trips on a flip. Anything not listed here (volume, sources,
# playback speed, ...) is shared between personalities and untouched.
STASH_FIELDS: tuple[str, ...] = (
    "theme",
    "layout",
    "layout_overrides",
    "motion",
    "corner_style",
    "nav_icon_set",
    "text_case_override",
    "font_family_override",
    "font_size_override_pt",
    "adaptive_accent",
    "adaptive_background",
    "adaptive_background_style",
    "adaptive_pulse",
    "mini_backdrop_style",
    "fullscreen_backdrop_style",
    "mini_pulse",
    "fullscreen_pulse",
    "ui_sounds_enabled",
    "show_thumbnails",
)


def builtin(preset_id: str) -> PresetDef:
    """The builtin definition for ``preset_id``. KeyError on unknown ids."""
    return BUILTINS[preset_id]


def _settings_default(name: str):
    """The Settings dataclass default for one field — the reset value for
    stash fields a PresetDef doesn't carry (layout_overrides, font
    overrides)."""
    for f in fields(Settings):
        if f.name != name:
            continue
        if f.default is not MISSING:
            return f.default
        if f.default_factory is not MISSING:  # type: ignore[misc]
            return f.default_factory()  # type: ignore[misc]
    return None


def _fresh_value(preset_id: str, name: str):
    """What ``name`` starts as on a preset never visited before (or a
    stash from an older build that predates the field): the builtin def's
    value when it has one, else the Settings default."""
    d = BUILTINS.get(preset_id)
    if d is not None and hasattr(d, name):
        return getattr(d, name)
    return _settings_default(name)


def stash(settings: Settings) -> None:
    """Snapshot the active personality's STASH_FIELDS into
    ``settings.preset_state``. No-op when no preset is active ("")."""
    if not settings.preset:
        return
    settings.preset_state[settings.preset] = {
        name: copy.deepcopy(getattr(settings, name)) for name in STASH_FIELDS
    }


def restore(settings: Settings, preset_id: str) -> None:
    """Write ``preset_id``'s stash (or its builtin defaults when it has
    never been visited) onto the settings fields and make it active.

    Stash entries win field-by-field, so a stash written by an older
    build simply gets defaults for fields it doesn't know about.
    KeyError when ``preset_id`` is unknown and has no stash to go on.
    """
    stashed = settings.preset_state.get(preset_id)
    if stashed is None and preset_id not in BUILTINS:
        raise KeyError(preset_id)
    for name in STASH_FIELDS:
        if stashed is not None and name in stashed:
            value = copy.deepcopy(stashed[name])
        else:
            value = copy.deepcopy(_fresh_value(preset_id, name))
        setattr(settings, name, value)
    settings.preset = preset_id


def apply_preset(settings: Settings, preset_id: str, window=None,
                 persist: bool = True) -> None:
    """Flip the active personality: stash the outgoing preset, restore the
    incoming one, and push the result to the live managers in the one
    order that honors every contract (theme bundle first, then layout,
    then motion, then the sticky corner override).

    Safe pre-window (``window=None``) — startup calls this before any
    widget exists; the window-side visuals hook only runs when a window
    is passed. Persistence is field-scoped (save_fields) so a flip can't
    clobber what another saver wrote in between.

    Re-applying the ACTIVE preset (startup bootstrap, wizard handoff)
    never restores: the live fields are the truth — the settings dialog
    edits them without touching the stash, so restoring here would revert
    every dialog customization on the next launch. The stash is refreshed
    from the live values instead; only a real flip round-trips through it.
    """
    if settings.preset != preset_id:
        if settings.preset:
            stash(settings)
        restore(settings, preset_id)
    else:
        stash(settings)

    # One batched theme apply — slug + font + size + case in a single
    # restyle instead of the four the individual setters would cost.
    from . import theming
    theming.manager().apply_bundle(
        slug=settings.theme,
        font_family=settings.font_family_override,
        font_size=settings.font_size_override_pt,
        case=settings.text_case_override,
    )
    from . import layout as layout_module
    layout_module.manager().apply(
        settings.layout or "classic", dict(settings.layout_overrides or {})
    )
    from .ui import motion as motion_module
    motion_module.set_intensity(settings.motion)
    # Corner style rides the same sticky @radius override the settings
    # dialog pushes (window._do_open_settings) so every QSS widget that
    # reads @radius matches the personality's softness.
    from .ui.central_bg import corner_radius as _corner_radius
    radius_px = _corner_radius(settings.corner_style)
    theming.manager().set_user_override(
        "radius", f"{radius_px}px" if radius_px > 0 else None
    )
    if window is not None:
        window.apply_preset_visuals()

    if persist:
        from . import settings as settings_module
        try:
            settings_module.save_fields(
                settings, "preset", "preset_chosen", "preset_state",
                *STASH_FIELDS,
            )
        except Exception:
            # Persistence is best-effort — the flip already happened on
            # screen; a full save retries the next time anything saves.
            pass


def adopt_current(settings: Settings) -> str:
    """Silent 1.x → 2.0 migration: file the user's current look under the
    personality their theme belongs to, changing NOTHING they can see.

    Every field keeps its value verbatim; the stash snapshots them so the
    first real flip round-trips back to exactly this state. preset_chosen
    stays False — adoption is not a choice, so the chooser can still
    offer itself later. Returns the adopted preset id.
    """
    aesthetic = ""
    try:
        from . import theming
        for t in theming.manager().list_themes():
            if t.slug == settings.theme:
                aesthetic = str(getattr(t, "aesthetic", "") or "")
                break
    except Exception:
        aesthetic = ""
    preset_id = aesthetic if aesthetic in BUILTINS else "modern"
    settings.preset = preset_id
    stash(settings)
    return preset_id
