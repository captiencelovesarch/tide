"""Section-line headings.

The ``── heading ────────`` rule that tops every list view was the same
four lines copy-pasted eight times (window, home, library, song page,
lyrics, history, album, artist) — eight chances for the total width or
the dash to drift. This is the one builder. The dash comes from the
glyph registry so a future glyph pack can restyle the rules along with
the transport, and the case styling rides theming.styled_case like every
other piece of chrome text.
"""
from __future__ import annotations

from .. import glyphs, theming


def line_heading(label: str, total: int = 60, *, theme=None,
                 dash: str = "") -> str:
    """Render ``label`` as a section rule: ``── label ────────``.

    ``total`` is the target width in characters; the trailing rule shrinks
    to fit long labels but never below four dashes, so a heading always
    reads as a rule even when the label overflows.

    ``theme`` and ``dash`` are the PREVIEW escape, for a surface drawing
    a heading for a theme other than the live one (the chooser's
    personality panes). Both live layers this builder normally reads are
    per-personality user state — the sticky text-case override and the
    glyph pack's ``heading_dash`` override — so a preview that used them
    would dress one personality's pitch in the other's customizations.
    Passing ``theme`` takes the casing from that theme's own typography
    and never from the override; passing ``dash`` names the rule
    character outright. Both default to the live layers, so every
    ordinary caller is unchanged.
    """
    dash = dash or glyphs.glyph("heading_dash")
    styled = (theming.styled_case(label, theme, allow_override=False)
              if theme is not None else theming.styled_case(label))
    line = dash * max(4, total - len(styled) - 6)
    return f"{dash}{dash} {styled} {line}"
