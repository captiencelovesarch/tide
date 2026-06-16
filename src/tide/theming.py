"""Theme system.

A theme is a directory of files. `theme.toml` declares tokens, typography,
and layout flags. `theme.qss` is a Qt stylesheet using @token placeholders
that the loader substitutes at apply-time. Optional `fonts/*.ttf` files are
auto-registered into the Qt font database.

Themes are discovered from three sources (later wins):
  1. bundled       — src/tide/themes/
  2. system        — /usr/share/tide/themes/
  3. user override — ~/.config/tide/themes/

`ThemeManager.apply(slug)` rebuilds the stylesheet, sets the application
font, and emits `theme_changed(Theme)` so custom-painted widgets can
re-read tokens without a restart.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from . import config


BUNDLED_THEMES_DIR = Path(__file__).parent / "themes"
SYSTEM_THEMES_DIR = Path("/usr/share/tide/themes")


@dataclass(frozen=True)
class Theme:
    slug: str
    name: str
    path: Path
    tokens: dict[str, str] = field(default_factory=dict)
    typography: dict[str, object] = field(default_factory=dict)
    layout: dict[str, object] = field(default_factory=dict)
    qss: str = ""
    dark: bool = True

    def token(self, name: str, default: str = "") -> str:
        return self.tokens.get(name, default)

    def t(self, kind: str, key: str, default=None):
        bag = {"layout": self.layout, "typography": self.typography}[kind]
        return bag.get(key, default)


def _theme_dirs() -> list[Path]:
    return [BUNDLED_THEMES_DIR, SYSTEM_THEMES_DIR, config.USER_THEMES_DIR]


def _read_theme(path: Path) -> Theme | None:
    toml_path = path / "theme.toml"
    qss_path = path / "theme.qss"
    if not toml_path.is_file():
        return None
    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return None
    meta = data.get("meta", {})
    slug = meta.get("slug") or path.name
    qss_text = qss_path.read_text(encoding="utf-8") if qss_path.is_file() else ""
    return Theme(
        slug=slug,
        name=meta.get("name", slug),
        path=path,
        tokens=dict(data.get("tokens", {})),
        typography=dict(data.get("typography", {})),
        layout=dict(data.get("layout", {})),
        qss=qss_text,
        dark=bool(meta.get("dark", True)),
    )


def discover_themes() -> dict[str, Theme]:
    """Return {slug: Theme}, with later sources overriding earlier ones."""
    found: dict[str, Theme] = {}
    for base in _theme_dirs():
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            theme = _read_theme(child)
            if theme is not None:
                found[theme.slug] = theme
    return found


# ---------- token substitution ----------

_TOKEN_RE = re.compile(r"@([a-z_][a-z0-9_]*)", re.IGNORECASE)


def _substitute(qss: str, theme: Theme) -> str:
    # tokens come from the [tokens] table plus a couple synthetic ones from
    # [layout] (border, radius, spacing) so QSS can reference them uniformly.
    lookups: dict[str, str] = dict(theme.tokens)
    lookups.setdefault("border", f"{int(theme.t('layout', 'border_px', 1))}px")
    lookups.setdefault("radius", f"{int(theme.t('layout', 'radius_px', 0))}px")
    lookups.setdefault("spacing", f"{int(theme.t('layout', 'spacing_px', 8))}px")
    lookups.setdefault("font_family", str(theme.t("typography", "family", "monospace")))
    lookups.setdefault("font_size", f"{int(theme.t('typography', 'size_pt', 10))}pt")

    def repl(match: re.Match) -> str:
        name = match.group(1)
        return lookups.get(name, match.group(0))

    return _TOKEN_RE.sub(repl, qss)


# ---------- font registration ----------


def _register_fonts(theme: Theme) -> None:
    font_dir = theme.path / "fonts"
    if not font_dir.is_dir():
        return
    for f in font_dir.iterdir():
        if f.suffix.lower() in (".ttf", ".otf"):
            QFontDatabase.addApplicationFont(str(f))


# ---------- manager ----------


class ThemeManager(QObject):
    theme_changed = Signal(object)   # Theme

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._themes: dict[str, Theme] = {}
        self._current: Theme | None = None
        self._token_overrides: dict[str, str] = {}

    def refresh(self) -> None:
        self._themes = discover_themes()

    def list_themes(self) -> list[Theme]:
        if not self._themes:
            self.refresh()
        return list(self._themes.values())

    def current(self) -> Theme | None:
        return self._current

    def apply(self, slug: str) -> Theme | None:
        if not self._themes:
            self.refresh()
        theme = self._themes.get(slug)
        if theme is None:
            return None
        _register_fonts(theme)

        # Explicit theme switch — drop any runtime token overrides from the
        # previous theme (adaptive accent etc.). The adaptive driver will
        # re-establish them for the new base palette if it's still enabled.
        prev_slug = self._current.slug if self._current is not None else None
        if slug != prev_slug:
            self._token_overrides.clear()

        # Compose the stylesheet with substituted tokens (overrides win).
        effective_theme = self._with_overrides(theme)
        qss = _substitute(theme.qss, effective_theme)

        app = QApplication.instance()
        if app is not None:
            family = str(theme.t("typography", "family", ""))
            size_pt = int(theme.t("typography", "size_pt", 10))
            weight = int(theme.t("typography", "weight", 400))
            if family:
                font = QFont(family)
                font.setPointSize(size_pt)
                font.setWeight(QFont.Weight(weight))
                app.setFont(font)
            app.setStyleSheet(qss)

        self._current = theme
        self.theme_changed.emit(effective_theme)
        return effective_theme

    def _with_overrides(self, theme: Theme) -> Theme:
        """Return a Theme whose tokens have the runtime overrides applied."""
        if not self._token_overrides:
            return theme
        merged = dict(theme.tokens)
        merged.update(self._token_overrides)
        return Theme(
            slug=theme.slug, name=theme.name, path=theme.path,
            tokens=merged, typography=theme.typography, layout=theme.layout,
            qss=theme.qss, dark=theme.dark,
        )

    def override_tokens(self, overrides: dict[str, str]) -> None:
        """Patch one or more token values at runtime (used by the adaptive
        driver). Pushes new QSS to the QApplication every call (cheap), but
        throttles the cascading ``theme_changed`` signal that drives custom-
        painted widgets to repaint — they only need the latest value, and
        emitting 60×/sec triggers a stampede of viewport updates across all
        list views.
        """
        if not overrides:
            return
        self._token_overrides.update(overrides)
        if self._current is None:
            return
        effective = self._with_overrides(self._current)
        qss = _substitute(self._current.qss, effective)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(qss)
        # Throttle: emit at most ~10Hz during a burst of overrides.
        import time as _t
        now = _t.monotonic()
        last = getattr(self, "_last_override_emit", 0.0)
        if now - last >= 0.10:
            self._last_override_emit = now
            self.theme_changed.emit(effective)
        else:
            # Schedule a trailing emit so the final state is delivered.
            from PySide6.QtCore import QTimer as _QT
            if not getattr(self, "_pending_override_emit", False):
                self._pending_override_emit = True
                def _flush():
                    self._pending_override_emit = False
                    if self._current is not None:
                        self._last_override_emit = _t.monotonic()
                        self.theme_changed.emit(self._with_overrides(self._current))
                _QT.singleShot(110, _flush)

    def clear_accent_override(self) -> None:
        """Remove the accent override (and bg_alt) so the base theme returns."""
        had = bool(self._token_overrides)
        self._token_overrides.clear()
        if had and self._current is not None:
            qss = _substitute(self._current.qss, self._current)
            app = QApplication.instance()
            if app is not None:
                app.setStyleSheet(qss)
            self.theme_changed.emit(self._current)


# Process-global theme manager. UI code uses `manager()` to subscribe to
# theme_changed without juggling its own instance.
_manager: ThemeManager | None = None


def manager() -> ThemeManager:
    global _manager
    if _manager is None:
        _manager = ThemeManager()
    return _manager


# ---------- text-case transforms ----------

_LEET_MAP = str.maketrans({
    "a": "4", "A": "4",
    "e": "3", "E": "3",
    "i": "1", "I": "1",
    "o": "0", "O": "0",
    "s": "5", "S": "5",
    "t": "7", "T": "7",
    "l": "1", "L": "1",
    "z": "2", "Z": "2",
})

_ZALGO_MARKS = (
    "̀", "́", "̂", "̃", "̄", "̆", "̇",
    "̈", "̊", "̋", "̌", "̐", "̒", "̓",
    "̔", "̚", "̼", "͏", "͛",
)


def _to_zalgo(s: str, intensity: int = 2) -> str:
    import random
    rng = random.Random(hash(s) & 0xFFFFFFFF)
    out: list[str] = []
    for ch in s:
        out.append(ch)
        if ch.isalpha():
            for _ in range(rng.randint(0, intensity)):
                out.append(rng.choice(_ZALGO_MARKS))
    return "".join(out)


def styled_case(text: str, theme: "Theme | None" = None) -> str:
    """Apply the active theme's typography.case to ``text``.

    Modes:
      - "lower"  → all lowercase
      - "upper"  → ALL UPPERCASE
      - "normal" → keep input casing
      - "leet"   → L1K3 TH1Z!1
      - "zalgo"  → text with combining diacritics
    """
    if not text:
        return text
    t = theme if theme is not None else manager().current()
    case = "lower"
    if t is not None:
        case = str(t.t("typography", "case", "lower"))
    if case == "upper":
        return text.upper()
    if case == "normal":
        return text
    if case == "leet":
        return text.translate(_LEET_MAP)
    if case == "zalgo":
        return _to_zalgo(text)
    return text.lower()
