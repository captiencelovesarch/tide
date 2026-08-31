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


def line_heading(label: str, total: int = 60) -> str:
    """Render ``label`` as a section rule: ``── label ────────``.

    ``total`` is the target width in characters; the trailing rule shrinks
    to fit long labels but never below four dashes, so a heading always
    reads as a rule even when the label overflows."""
    dash = glyphs.glyph("heading_dash")
    styled = theming.styled_case(label)
    line = dash * max(4, total - len(styled) - 6)
    return f"{dash}{dash} {styled} {line}"
