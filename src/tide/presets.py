"""Personality presets — the v2.0 "two tides" core: "brutalist" and
"modern" are look/feel bundles that flip atomically. A flip stashes the
outgoing preset's STASH_FIELDS into ``Settings.preset_state`` and
restores the incoming one's stash (or builtin defaults on first visit),
so each personality keeps its own tweaks. Qt imports stay lazy:
``apply_preset`` runs before any window exists, and the unit tests
drive it with fake managers.
"""
from __future__ import annotations

import copy
from dataclasses import MISSING, dataclass, fields

from .settings import Settings


@dataclass(frozen=True)
class PresetDef:
    """A builtin personality's fresh-visit values. Fields with a Settings
    twin share its name so restore() can copy by string."""
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
        nav_icon_set="svg",
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
        sound_pack="modern",
    ),
}


# The Settings fields a personality owns — stash/restore round-trips all
# of them; anything not listed (volume, sources, speed, ...) is shared.
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
    "adaptive_text_contrast",
    "mini_backdrop_style",
    "fullscreen_backdrop_style",
    "mini_pulse",
    "fullscreen_pulse",
    "ui_sounds_enabled",
    "show_thumbnails",
    # glyphs flip (chrome is personality); keymap stays global (muscle memory)
    "glyph_overrides",
)


# What a FIRST answer to "choose your tide" keeps; the rest resets to
# the chosen builtin (see claim_builtin). These are the look a 1.x
# upgrader chose on purpose — the migration invariant forbids wiping them.
FIRST_ANSWER_KEEPS: tuple[str, ...] = (
    "theme",
    "layout",
    "layout_overrides",
    "text_case_override",
    "font_family_override",
    "font_size_override_pt",
    "glyph_overrides",
)


def builtin(preset_id: str) -> PresetDef:
    """The builtin definition for ``preset_id``. KeyError on unknown ids."""
    return BUILTINS[preset_id]


def _settings_default(name: str):
    """Settings dataclass default for ``name`` — the reset value for stash
    fields a PresetDef doesn't carry (layout_overrides, font overrides)."""
    for f in fields(Settings):
        if f.name != name:
            continue
        if f.default is not MISSING:
            return f.default
        if f.default_factory is not MISSING:  # type: ignore[misc]
            return f.default_factory()  # type: ignore[misc]
    return None


def _fresh_value(preset_id: str, name: str):
    """Value for a never-visited preset (or a stash predating the field):
    the builtin's value if it has one, else the Settings default."""
    d = BUILTINS.get(preset_id)
    if d is not None and hasattr(d, name):
        return getattr(d, name)
    return _settings_default(name)


def claim_builtin(settings: Settings, preset_id: str) -> None:
    """Make an ALREADY-ACTIVE personality wear its builtin — the first
    answer to "choose your tide" when it matches the silently-adopted
    id. Without this the pre-selected pane is a dead button, since
    apply_preset's same-id path only refreshes the stash.
    ``FIRST_ANSWER_KEEPS`` stays. Callers gate on ``preset_chosen``, not
    the id — a re-pick must never reset tweaks. KeyError on unknown ids.
    """
    if preset_id not in BUILTINS:
        raise KeyError(preset_id)
    for name in STASH_FIELDS:
        if name in FIRST_ANSWER_KEEPS:
            continue
        setattr(settings, name, copy.deepcopy(_fresh_value(preset_id, name)))


def stash(settings: Settings) -> None:
    """Snapshot the active personality's STASH_FIELDS into
    ``settings.preset_state``. No-op when no preset is active ("")."""
    if not settings.preset:
        return
    settings.preset_state[settings.preset] = {
        name: copy.deepcopy(getattr(settings, name)) for name in STASH_FIELDS
    }


def restore(settings: Settings, preset_id: str) -> None:
    """Write ``preset_id``'s stash (or builtin defaults) onto the
    settings fields and make it active. Stash entries win field-by-field
    — an older build's stash gets defaults for fields it lacks. KeyError
    for an unknown id with no stash.
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
    """Flip the active personality: stash the outgoing, restore the
    incoming, push to the live managers in contract order (theme bundle,
    layout, motion, sticky corner override). Safe with ``window=None``.
    Persistence is field-scoped (save_fields) so a flip can't clobber a
    concurrent saver. Re-applying the ACTIVE preset only refreshes the
    stash: the settings dialog edits live fields without touching the
    stash, and restoring here would revert those edits on next launch.
    """
    if settings.preset != preset_id:
        if settings.preset:
            stash(settings)
        restore(settings, preset_id)
    else:
        stash(settings)

    # one restyle for slug+font+size+case instead of four
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
    # dialect follows the personality, not the intensity — this is what
    # keeps OutBack overshoot out of brutalist at motion=full
    motion_module.bind_preset(preset_id)
    # same sticky @radius override the settings dialog pushes
    # (window._do_open_settings), so every @radius QSS widget matches
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
            # best-effort — the flip happened; the next full save retries
            pass


def adopt_current(settings: Settings) -> str:
    """Silent 1.x → 2.0 migration: file the current look under the
    personality its theme belongs to, changing NOTHING visible; the stash
    snapshots every field so the first flip round-trips back.
    preset_chosen stays False — adoption is not a choice, the chooser
    may still offer itself. Returns the preset id."""
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
