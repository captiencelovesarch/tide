"""Backdrop style registry.

central_bg.set_style() whitelists the slugs its renderer knows, and the
settings pickers (main appearance, mini, and eventually fullscreen) each
hand-list the same slugs with labels — three copies of one list. This
registry is the single copy: ordered slugs, the picker labels, and the
"follow" / "off" sentinels the companion windows add around the real
styles ("follow" = mirror the main window's style, "off" = flat card,
neither is a renderer style).

Call sites don't import this yet — that migration is a later integration
pass. The test pins this registry against central_bg's accepted set so
the two can't drift.

Choice helpers return ``(slug, label)`` rows in display order. Note
QComboBox.addItem takes them flipped: ``addItem(label, slug)``.
"""
from __future__ import annotations


# Renderer styles, in picker display order. Must match central_bg
# set_style()'s whitelist exactly.
SLUGS: tuple[str, ...] = (
    "field",
    "band",
    "vbeam",
    "horizon",
    "lightning",
    "depths",
    "rimlight",
    "liquid",
    "aurora",
    "smoke",
    "caustics",
)

# Picker labels, verbatim from the shipped settings dialog.
LABELS: dict[str, str] = {
    "field": "living fields · layered ambience",
    "band": "diagonal band · classic sweep",
    "vbeam": "bass arch · hazy hill swells on bass",
    "horizon": "sunset horizon · sun low over water",
    "lightning": "lightning · strikes on the beat",
    "depths": "deep water · glow wells up from below",
    "rimlight": "rim light · edges hold the light",
    "liquid": "liquid cover · the album art, melted",
    "aurora": "aurora · slow curtains of light",
    "smoke": "smoke · drifts, glows from within",
    "caustics": "caustics · underwater light web",
}

# Sentinels the companion-window pickers wrap around the real styles.
FOLLOW = "follow"
FOLLOW_LABEL = "follow main backdrop style"
OFF = "off"
OFF_LABEL = "off · flat card"

# Surfaces resolve() knows. The main window isn't one — it has no
# "follow" to resolve (it IS the thing being followed).
SURFACES: tuple[str, ...] = ("mini", "fullscreen")


def resolve(style: str, *, adaptive_on: bool, surface: str,
            main_style: str = "") -> str:
    """The ONE place the companion "follow" semantics live.

    ``style`` is the companion's stored pick (``mini_backdrop_style`` /
    ``fullscreen_backdrop_style``), ``adaptive_on`` the main window's
    backdrop master toggle, ``main_style`` the main window's style pick.
    Returns the slug the companion should hand to its ``CentralBg``
    (``"off"`` meaning: disable it, flat card).

    Truth table:

      style        surface      adaptive_on   →  result
      ─────────────────────────────────────────────────────────────
      explicit s   either       either        →  s   (a picked style
                                                  ignores the main
                                                  toggle — both today)
      "off"        either       either        →  "off"
      follow / ""  fullscreen   True          →  main_style or "field"
      follow / ""  fullscreen   False         →  "off"
      follow / ""  mini         either        →  main_style or "field"

    The mini/fullscreen divergence on the last two rows is deliberate,
    not drift: fullscreen mirrors the main surface's WHOLE look,
    including whether the backdrop is on at all — forcing a gradient
    there painted the theme's bg_alt hue (nord's plainly wrong blue)
    whenever the user's main window showed none. The mini keeps its
    gradient even with the main backdrop off because the mini IS its
    backdrop — a frameless card with nothing painted on it isn't a
    player, it's a hole in the compositor.

    Unknown non-sentinel styles pass through verbatim; validating real
    slugs stays ``central_bg.set_style``'s job (it falls back to
    "field"), same as before this helper existed.
    """
    if surface not in SURFACES:
        raise ValueError(f"unknown companion surface: {surface!r}")
    style = style or FOLLOW
    if style != FOLLOW:
        return style
    if surface == "fullscreen" and not adaptive_on:
        return OFF
    return main_style or "field"


def choices() -> list[tuple[str, str]]:
    """The renderer styles as (slug, label) rows — the main window's
    picker, where a backdrop always paints something."""
    return [(slug, LABELS[slug]) for slug in SLUGS]


def choices_with_follow() -> list[tuple[str, str]]:
    """"follow" first, then the styles — for companion pickers whose
    default is to mirror the main window."""
    return [(FOLLOW, FOLLOW_LABEL), *choices()]


def choices_with_off() -> list[tuple[str, str]]:
    """The styles with "off" last — for surfaces that can decline to
    paint a backdrop at all. (The mini's picker composes both sentinels:
    ``[(FOLLOW, FOLLOW_LABEL), *choices(), (OFF, OFF_LABEL)]``.)"""
    return [*choices(), (OFF, OFF_LABEL)]
