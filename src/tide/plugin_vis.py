"""What a plugin's visualizer tile builds on: the tile base classes and
the drawing helpers tide's own tiles use. Register a tile with
``ctx.ui.add_visualizer_tile``; its knobs are the plugin's settings and
``self.knob(key, default)`` reads them.

A tile is a QWidget that tide's VisEngine drives: subclass ``PlayTile``
(a ``CanvasTile`` you can click into) and draw in ``paint_graphics(p, w, h, f)`` (plus ``paint_overlay`` for text)
with the ``Frame`` it hands you (spectrum, beat clock, palette, look).
Like ``tide.plugin_api``, this is the supported surface; anything else
under ``tide.ui`` can change between releases.
"""
from __future__ import annotations

from . import theming
from .ui.vis_deck import (
    CanvasTile, Frame, TileMixin, VisEngine, _alpha as alpha, _mix as mix, bloom,
    paint_outline,
)
from .ui.vis_play import PlayTile


def styled_case(text: str) -> str:
    """text in the theme's letter case, like tide's own labels."""
    return theming.styled_case(text)


__all__ = ["CanvasTile", "Frame", "PlayTile", "TileMixin", "VisEngine", "alpha", "bloom",
           "mix", "paint_outline", "styled_case"]
