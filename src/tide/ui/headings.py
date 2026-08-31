"""Section-line headings — one builder for the ``── heading ────────``
rule that was copy-pasted across eight views. The dash comes from the
glyph registry so a glyph pack can restyle it; casing rides
``theming.styled_case``.
"""
from __future__ import annotations

from .. import glyphs, theming


def line_heading(label: str, total: int = 60, *, theme=None,
                 dash: str = "") -> str:
    """Render ``label`` as a section rule: ``── label ────────``.

    ``total`` is the target width in chars; the rule never drops below
    four dashes. ``theme`` / ``dash`` are the PREVIEW escape (the
    chooser's personality panes): the live case override and glyph-pack
    dash are per-personality user state, so a preview reading them would
    dress one personality's pitch in the other's customizations —
    ``theme`` takes casing from that theme's typography (never the
    override); ``dash`` names the rule character. Defaults = live layers.
    """
    dash = dash or glyphs.glyph("heading_dash")
    styled = (theming.styled_case(label, theme, allow_override=False)
              if theme is not None else theming.styled_case(label))
    line = dash * max(4, total - len(styled) - 6)
    return f"{dash}{dash} {styled} {line}"
