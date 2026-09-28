"""Backdrop style registry — the single copy central_bg's renderer and
the settings pickers share: ordered slugs, picker labels, and the
"follow" / "off" sentinels companion windows wrap around the real styles
("follow" = mirror the main window, "off" = flat card; neither is a
renderer style). A test pins this against central_bg's accepted set.

Choice helpers return ``(slug, label)`` rows in display order; note
QComboBox.addItem takes them flipped: ``addItem(label, slug)``.
"""
from __future__ import annotations


# picker display order — must match central_bg set_style()'s whitelist
SLUGS: tuple[str, ...] = (
    "field",
    "band",
    "vbeam",
    "horizon",
    "lightning",
    "ripples",
    "stage",
    "rimlight",
    "liquid",
    "contours",
    "vinyl",
)

# Picker labels, verbatim from the shipped settings dialog.
LABELS: dict[str, str] = {
    "field": "living fields · layered ambience",
    "band": "diagonal band · classic sweep",
    "vbeam": "bass arch · hazy hill swells on bass",
    "horizon": "sunset horizon · sun low over water",
    "lightning": "lightning · strikes on the beat",
    "ripples": "ripples · every kick drops a ring on the water",
    "stage": "stage lights · beams sweep through the haze",
    "rimlight": "rim light · edges hold the light",
    "liquid": "liquid cover · the album art, melted",
    "contours": "contours · topo lines that shift with the bass",
    "vinyl": "vinyl · the grooves catch the light",
}

# Styles that shipped once and were cut, mapped to the nearest one still
# here. settings.load rewrites a stored pick through this, so nobody
# silently lands on "field" with a picker showing nothing selected.
RETIRED: dict[str, str] = {
    "depths": "ripples",
    "caustics": "ripples",
    "aurora": "stage",
    "smoke": "stage",
}

# Sentinels the companion-window pickers wrap around the real styles.
FOLLOW = "follow"
FOLLOW_LABEL = "follow main backdrop style"
OFF = "off"
OFF_LABEL = "off · flat card"

# surfaces resolve() knows — not the main window, which has no "follow"
# to resolve (it IS the thing being followed)
SURFACES: tuple[str, ...] = ("mini", "fullscreen")


def resolve(style: str, *, adaptive_on: bool, surface: str,
            main_style: str = "") -> str:
    """Companion "follow" semantics, in one place: the stored pick +
    the main window's backdrop master toggle → the slug for the
    companion's ``CentralBg`` ("off" = flat card).

      style        surface      adaptive_on   →  result
      ─────────────────────────────────────────────────────
      explicit s   either       either        →  s
      "off"        either       either        →  "off"
      follow / ""  fullscreen   True          →  main_style or "field"
      follow / ""  fullscreen   False         →  "off"
      follow / ""  mini         either        →  main_style or "field"

    The mini/fullscreen split on the last rows is deliberate: fullscreen
    mirrors whether the main backdrop is on at all (forcing a gradient
    painted nord's plainly wrong bg_alt blue over a main window showing
    none); the mini keeps its gradient because the mini IS its backdrop —
    a frameless card with nothing painted is a hole in the compositor.
    Unknown non-sentinel styles pass through verbatim — validating real
    slugs stays ``central_bg.set_style``'s job (falls back to "field").
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
    """Renderer styles as (slug, label) rows — the main window's picker."""
    return [(slug, LABELS[slug]) for slug in SLUGS]


def choices_with_follow() -> list[tuple[str, str]]:
    """"follow" first, then the styles — for companion pickers."""
    return [(FOLLOW, FOLLOW_LABEL), *choices()]


def choices_with_off() -> list[tuple[str, str]]:
    """The styles with "off" last — for surfaces that can decline to
    paint. (The mini's picker composes both sentinels itself.)"""
    return [*choices(), (OFF, OFF_LABEL)]
