"""Section headings.

``line_heading`` is the one builder for the ``── heading ────────`` rule
that was copy-pasted across eight views. The dash comes from the glyph
registry so a glyph pack can restyle it; casing rides
``theming.styled_case``.

``Heading`` is the label that wears it. brutalist: the rule, dim, body
size, exactly the string ``line_heading`` builds. modern: the bare label
at the title size (``QLabel.title`` in the base sheet), in the foreground
colour — a heading, not a divider. It re-renders itself on theme change,
so a view that used to rebuild its heading text on every theme signal can
just call ``set_label`` when the words change.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QWidget

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


class Heading(QLabel):
    """A section heading that renders per aesthetic. See module doc."""

    def __init__(self, label: str = "", total: int = 60,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._label = str(label)
        self._total = int(total)
        self._class = ""
        self.setTextFormat(Qt.PlainText)
        self._apply(theming.manager().current_effective())
        theming.manager().theme_changed.connect(self._apply)

    def set_label(self, label: str) -> None:
        self._label = str(label)
        self._render()

    def label(self) -> str:
        return self._label

    def _modern(self) -> bool:
        return getattr(self._theme, "aesthetic", "") == "modern"

    def _apply(self, theme) -> None:
        self._theme = theme
        klass = "title" if self._modern() else "dim"
        if klass != self._class:
            self._class = klass
            self.setProperty("class", klass)
            st = self.style()
            st.unpolish(self)
            st.polish(self)
        if self._modern():
            from . import scale as _scale
            self.setContentsMargins(0, _scale.px(6), 0, 0)
        else:
            self.setContentsMargins(0, 0, 0, 0)
        self._render()

    def _render(self) -> None:
        if self._modern():
            self.setText(theming.styled_case(self._label))
        else:
            self.setText(line_heading(self._label, self._total))
