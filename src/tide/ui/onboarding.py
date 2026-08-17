"""Premium first-launch wizard.

Multi-step onboarding that pre-tide users walk through once. Each step is a
QWidget added to a QStackedWidget; navigation crossfades between steps via
the motion module. The dialog returns an ``OnboardingResult`` with every
choice the user made — app.py applies it before constructing MainWindow so
the first frame already reflects the user's preferences.

Steps:
  1. Welcome — logo + tagline + "get started"
  2. Aesthetic — pick brutalist or modern (two big preview cards)
  3. Theme — grid of themes filtered to the chosen aesthetic
  4. Sources — toggle the 5 ready sources; YT cookie import + Local folder
     pick run inline as sub-flows
  5. Feel — adaptive accent toggle + motion intensity + ui scale
  6. Integrations — discord rich presence + listenbrainz, both optional
  7. All set — summary + launch button

The wizard is also reachable from Settings → "rerun onboarding" so the user
can fly through it again at any time without nuking their config.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import theming
from .widgets import BracketButton


# ---------- result + state ----------


@dataclass
class OnboardingResult:
    """What the wizard returns. Empty defaults so app.py can safely apply
    a partial result if the user cancels mid-flight."""

    completed: bool = False
    aesthetic: str = "brutalist"
    theme_slug: str = "brutalist-mono"
    # Nothing pre-selected: a checked box the user never touched isn't a
    # choice, and the wizard advances fine with zero sources (app.py binds
    # the UI to a fallback source until they enable one).
    sources_enabled: dict[str, bool] = field(default_factory=lambda: {
        "ytmusic": False, "soundcloud": False, "bandcamp": False,
        "mixcloud": False, "local": False, "spotify": False, "subsonic": False,
    })
    active_source: str = "soundcloud"
    yt_authed: bool = False
    spotify_authed: bool = False
    subsonic_authed: bool = False
    local_dir: str = ""
    # Subsonic creds the user entered during setup. Empty strings mean
    # the user toggled the source off or never opened the [set up] flow.
    subsonic_url: str = ""
    subsonic_user: str = ""
    subsonic_pass: str = ""
    subsonic_auth_style: str = "salt"
    adaptive_accent: bool = True
    adaptive_background: bool = True
    motion: str = "lite"
    ui_scale: str = "normal"
    # Optional integrations step. Enabled only when the matching credential
    # was actually provided — a checked box with an empty field is not a
    # working integration.
    discord_enabled: bool = False
    discord_app_id: str = ""
    listenbrainz_enabled: bool = False
    listenbrainz_token: str = ""


# ---------- progress dots ----------


class _ProgressDots(QWidget):
    """Row of N dots; current step filled with accent, prior steps with fg,
    upcoming steps with dim. Tiny but tells the user where they are."""

    DOT_R = 4
    GAP = 14

    def __init__(self, total: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._total = total
        self._current = 0
        self.setFixedHeight(self.DOT_R * 2 + 6)
        # Width auto-grows with number of dots.
        self.setMinimumWidth((self.DOT_R * 2 + self.GAP) * total)
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)

    def _on_theme(self, theme) -> None:
        self._theme = theme
        self.update()

    def set_step(self, idx: int) -> None:
        if idx == self._current:
            return
        self._current = max(0, min(self._total - 1, idx))
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        fg = QColor(self._theme.token("fg", "#fff") if self._theme else "#fff")
        dim = QColor(self._theme.token("dim", "#888") if self._theme else "#888")
        accent = QColor(self._theme.token("accent", "#d4b95e") if self._theme else "#d4b95e")
        r = self.DOT_R
        spacing = r * 2 + self.GAP
        total_w = spacing * self._total - self.GAP
        x = (self.width() - total_w) // 2 + r
        y = self.height() // 2
        for i in range(self._total):
            if i == self._current:
                p.setBrush(accent)
                p.setPen(Qt.NoPen)
                p.drawEllipse(x - r - 1, y - r - 1, (r + 1) * 2, (r + 1) * 2)
            elif i < self._current:
                p.setBrush(fg)
                p.setPen(Qt.NoPen)
                p.drawEllipse(x - r, y - r, r * 2, r * 2)
            else:
                p.setBrush(Qt.NoBrush)
                p.setPen(dim)
                p.drawEllipse(x - r, y - r, r * 2, r * 2)
            x += spacing
        p.end()


# ---------- big card primitive used by aesthetic + theme steps ----------


class _PickCard(QFrame):
    """Clickable card with title + subtitle + a small colored stripe. Used
    for picking aesthetic + picking a theme. Single-select within a group."""

    clicked_signal = Signal(str)

    def __init__(self, key: str, title: str, subtitle: str, swatch: list[str],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._key = key
        self._selected = False
        self._swatch = swatch
        self.setObjectName("PickCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        self._title = QLabel(title)
        f = QFont(self._title.font())
        f.setBold(True)
        f.setPointSize(f.pointSize() + 2)
        self._title.setFont(f)

        self._subtitle = QLabel(subtitle)
        self._subtitle.setProperty("class", "dim")
        self._subtitle.setWordWrap(True)

        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        text_col.addWidget(self._title)
        text_col.addWidget(self._subtitle)
        text_col.addStretch(1)

        # Swatch strip — small color tiles showing the palette.
        self._swatch_row = QHBoxLayout()
        self._swatch_row.setSpacing(4)
        for color in swatch:
            tile = QLabel()
            tile.setFixedSize(18, 18)
            tile.setStyleSheet(
                f"background: {color}; border-radius: 4px; border: 1px solid rgba(255,255,255,0.1);"
            )
            self._swatch_row.addWidget(tile)
        self._swatch_row.addStretch(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(10)
        outer.addLayout(text_col, stretch=1)
        outer.addLayout(self._swatch_row)

        self._apply_style()

    def setSelected(self, on: bool) -> None:
        if on == self._selected:
            return
        self._selected = on
        self._apply_style()

    def _apply_style(self) -> None:
        theme = theming.manager().current()
        fg = theme.token("fg", "#fff") if theme else "#fff"
        bg = theme.token("bg", "#000") if theme else "#000"
        bg_alt = theme.token("bg_alt", "#111") if theme else "#111"
        accent = theme.token("accent", "#d4b95e") if theme else "#d4b95e"
        border_col = theme.token("border_col", fg) if theme else fg
        border = accent if self._selected else border_col
        width = 2 if self._selected else 1
        radius = int(theme.t("layout", "radius_px", 0) if theme else 0)
        self.setStyleSheet(
            f"QFrame#PickCard {{ background: {bg_alt}; "
            f"border: {width}px solid {border}; border-radius: {radius}px; }}"
            f"QFrame#PickCard:hover {{ border-color: {accent}; }}"
        )

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            self.clicked_signal.emit(self._key)
        super().mousePressEvent(ev)


# ---------- steps ----------


class _Step(QWidget):
    """Base for all wizard steps. Each step owns its layout + exposes
    ``can_advance()`` so the parent dialog can enable/disable Next, and
    ``apply_to(result)`` to persist the step's choices into the shared
    state dict when leaving the step.
    """

    state_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def can_advance(self) -> bool:
        return True

    def apply_to(self, result: OnboardingResult) -> None:
        pass

    def on_enter(self, result: OnboardingResult) -> None:
        """Called when the step becomes visible. Lets the step hydrate from
        the shared state (e.g. show the previously-picked theme as already
        selected if the user goes back)."""
        pass


class _WelcomeStep(_Step):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        logo = QLabel()
        # Find tide's bundled icon.
        icon_path = Path(__file__).resolve().parent.parent.parent.parent / "assets" / "icon-256.png"
        if not icon_path.is_file():
            icon_path = Path(__file__).resolve().parent.parent.parent / "tide" / "assets" / "icon-256.png"
        if icon_path.is_file():
            pm = QPixmap(str(icon_path)).scaled(160, 160,
                Qt.KeepAspectRatio, Qt.SmoothTransformation)
            logo.setPixmap(pm)
        logo.setAlignment(Qt.AlignCenter)

        title = QLabel("welcome to tide")
        f = QFont(title.font())
        f.setBold(True)
        f.setPointSize(f.pointSize() + 14)
        title.setFont(f)
        title.setAlignment(Qt.AlignCenter)

        tagline = QLabel("a brutalist multi-source music client")
        f2 = QFont(tagline.font())
        f2.setPointSize(f2.pointSize() + 2)
        tagline.setFont(f2)
        tagline.setProperty("class", "dim")
        tagline.setAlignment(Qt.AlignCenter)

        body = QLabel(
            "let's get you set up. takes about a minute.\n"
            "every choice is reversible from Settings."
        )
        body.setAlignment(Qt.AlignCenter)
        body.setWordWrap(True)
        body.setProperty("class", "dim")

        col = QVBoxLayout(self)
        col.setSpacing(18)
        col.addStretch(1)
        col.addWidget(logo)
        col.addSpacing(8)
        col.addWidget(title)
        col.addWidget(tagline)
        col.addSpacing(12)
        col.addWidget(body)
        col.addStretch(2)


class _AestheticStep(_Step):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._choice: str = "brutalist"

        prompt = QLabel("first — pick your vibe.")
        f = QFont(prompt.font())
        f.setBold(True)
        f.setPointSize(f.pointSize() + 6)
        prompt.setFont(f)
        prompt.setAlignment(Qt.AlignCenter)

        sub = QLabel("you can change this anytime. each comes in several themes.")
        sub.setProperty("class", "dim")
        sub.setAlignment(Qt.AlignCenter)

        brutalist_card = _PickCard(
            "brutalist",
            "brutalist",
            "sharp corners · monospace font · block characters · [bracket buttons] · inverted hover",
            ["#0b0b0b", "#e6e6e6", "#d4b95e"],
        )
        modern_card = _PickCard(
            "modern",
            "modern",
            "soft corners · sans font · smooth bars · glyph icons · airy padding · subtle hover",
            ["#15151a", "#c79bff", "#f0eef0"],
        )
        self._cards = {"brutalist": brutalist_card, "modern": modern_card}
        for c in self._cards.values():
            c.clicked_signal.connect(self._on_picked)

        # Default the brutalist card on entry.
        brutalist_card.setSelected(True)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(14)
        cards_row.addWidget(brutalist_card, stretch=1)
        cards_row.addWidget(modern_card, stretch=1)

        col = QVBoxLayout(self)
        col.setSpacing(18)
        col.addStretch(1)
        col.addWidget(prompt)
        col.addWidget(sub)
        col.addSpacing(8)
        col.addLayout(cards_row)
        col.addStretch(2)

    def _on_picked(self, key: str) -> None:
        self._choice = key
        for k, card in self._cards.items():
            card.setSelected(k == key)
        self.state_changed.emit()

    def apply_to(self, result: OnboardingResult) -> None:
        result.aesthetic = self._choice

    def on_enter(self, result: OnboardingResult) -> None:
        self._choice = result.aesthetic
        for k, card in self._cards.items():
            card.setSelected(k == self._choice)


class _ThemeStep(_Step):
    """Theme grid filtered to chosen aesthetic. Clicking a card live-applies
    the theme so the user sees the wizard itself restyle — premium touch."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._choice: str = "brutalist-mono"
        self._cards: dict[str, _PickCard] = {}

        prompt = QLabel("fine-tune the look.")
        f = QFont(prompt.font())
        f.setBold(True)
        f.setPointSize(f.pointSize() + 6)
        prompt.setFont(f)
        prompt.setAlignment(Qt.AlignCenter)

        self._sub = QLabel("click a theme to preview it.")
        self._sub.setProperty("class", "dim")
        self._sub.setAlignment(Qt.AlignCenter)

        self._grid = QGridLayout()
        self._grid.setSpacing(10)

        scroll = QScrollArea()
        inner = QWidget()
        inner.setLayout(self._grid)
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        col = QVBoxLayout(self)
        col.setSpacing(12)
        col.addWidget(prompt)
        col.addWidget(self._sub)
        col.addWidget(scroll, stretch=1)

    def _rebuild_grid(self, aesthetic: str) -> None:
        # Clear existing widgets.
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._cards.clear()

        themes = theming.discover_themes()
        # Filter to chosen aesthetic.
        matches = [
            (slug, t) for slug, t in themes.items()
            if getattr(t, "aesthetic", "modern") == aesthetic
        ]
        matches.sort(key=lambda kv: kv[1].name)
        for i, (slug, t) in enumerate(matches):
            swatch = [
                t.token("bg", "#000"),
                t.token("fg", "#fff"),
                t.token("accent", "#d4b95e"),
                t.token("bg_alt", "#111"),
            ]
            subtitle_parts = []
            family = str(t.t("typography", "family", ""))
            if family:
                subtitle_parts.append(family.split(",")[0].strip())
            case = str(t.t("typography", "case", "")).strip()
            if case and case != "normal":
                subtitle_parts.append(case)
            radius = int(t.t("layout", "radius_px", 0))
            subtitle_parts.append(f"{radius}px radius")
            sub = " · ".join(subtitle_parts)
            card = _PickCard(slug, t.name, sub, swatch)
            card.clicked_signal.connect(self._on_picked)
            self._cards[slug] = card
            row, col = divmod(i, 2)
            self._grid.addWidget(card, row, col)
        # Stretch the last row so cards don't grow vertically when there's
        # an odd count.
        self._grid.setRowStretch(self._grid.rowCount(), 1)
        # Apply current selection visual.
        for slug, card in self._cards.items():
            card.setSelected(slug == self._choice)

    def _on_picked(self, slug: str) -> None:
        self._choice = slug
        for k, card in self._cards.items():
            card.setSelected(k == slug)
        # Live-apply for instant preview. The wizard re-styles in place.
        try:
            theming.manager().apply(slug)
        except Exception:
            pass
        self.state_changed.emit()

    def apply_to(self, result: OnboardingResult) -> None:
        result.theme_slug = self._choice

    def on_enter(self, result: OnboardingResult) -> None:
        # Pick a sensible default from the aesthetic if the saved choice
        # doesn't match the aesthetic family.
        themes = theming.discover_themes()
        cur = themes.get(self._choice)
        if cur is None or getattr(cur, "aesthetic", "modern") != result.aesthetic:
            # First matching theme alphabetically.
            for slug, t in sorted(themes.items(), key=lambda kv: kv[1].name):
                if getattr(t, "aesthetic", "modern") == result.aesthetic:
                    self._choice = slug
                    break
        result.theme_slug = self._choice
        self._rebuild_grid(result.aesthetic)
        # Live-apply the entry theme too.
        try:
            theming.manager().apply(self._choice)
        except Exception:
            pass


class _SourceCard(QFrame):
    """A single source row with checkbox + name + status."""

    toggled_signal = Signal(str, bool)
    setup_clicked = Signal(str)

    def __init__(self, key: str, title: str, tags: str, needs_setup: bool,
                 coming_soon: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SourceCard")
        self._key = key
        self._needs_setup = needs_setup
        self._coming_soon = coming_soon
        self._setup_done = False

        self._check = QCheckBox(title)
        f = QFont(self._check.font())
        f.setBold(True)
        f.setPointSize(f.pointSize() + 1)
        self._check.setFont(f)
        self._check.toggled.connect(self._on_toggled)
        self._check.setEnabled(not coming_soon)

        sub = QLabel(tags)
        sub.setProperty("class", "dim")

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.addWidget(self._check)
        text_col.addWidget(sub)

        self._setup_btn = BracketButton("set up")
        self._setup_btn.clicked.connect(lambda: self.setup_clicked.emit(self._key))
        self._setup_btn.setVisible(needs_setup and not coming_soon)

        self._status = QLabel("")
        self._status.setProperty("class", "dim")

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(12)
        row.addLayout(text_col, stretch=1)
        row.addWidget(self._setup_btn)
        row.addWidget(self._status)

        self._apply_style()
        # Bound method, not a lambda — lambda connections to the global
        # theming manager outlive the widget and fire into freed C++ objects.
        theming.manager().theme_changed.connect(self._on_theme_restyle)

    def _on_theme_restyle(self, _theme) -> None:
        self._apply_style()

    def _apply_style(self) -> None:
        theme = theming.manager().current()
        bg_alt = theme.token("bg_alt", "#111") if theme else "#111"
        border_col = theme.token("border_col", "#333") if theme else "#333"
        radius = int(theme.t("layout", "radius_px", 0) if theme else 0)
        dim = theme.token("dim", "#888") if theme else "#888"
        opacity = "0.5" if self._coming_soon else "1"
        self.setStyleSheet(
            f"QFrame#SourceCard {{ background: {bg_alt}; "
            f"border: 1px solid {border_col}; border-radius: {radius}px; }}"
        )

    def isChecked(self) -> bool:
        return self._check.isChecked()

    def setChecked(self, on: bool) -> None:
        self._check.setChecked(on)

    def setSetupDone(self, done: bool, label: str = "") -> None:
        self._setup_done = done
        self._status.setText(label or ("✓ ready" if done else ""))

    def needsSetup(self) -> bool:
        return self._needs_setup and not self._setup_done

    def _on_toggled(self, on: bool) -> None:
        self.toggled_signal.emit(self._key, on)


class _SourcesStep(_Step):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._enabled: dict[str, bool] = {
            "ytmusic": False, "soundcloud": False, "bandcamp": False,
            "mixcloud": False, "local": False, "spotify": False, "subsonic": False,
        }
        self._yt_authed = False
        self._spotify_authed = False
        self._subsonic_authed = False
        self._local_dir = ""
        self._subsonic_cfg = None  # SubsonicConfig once the user [save]s setup

        prompt = QLabel("where will your music come from?")
        f = QFont(prompt.font())
        f.setBold(True)
        f.setPointSize(f.pointSize() + 4)
        prompt.setFont(f)
        prompt.setAlignment(Qt.AlignCenter)

        sub = QLabel("toggle any source — sources that need setup will ask. pick as many or as few as you like.")
        sub.setProperty("class", "dim")
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)

        self._cards: dict[str, _SourceCard] = {
            "ytmusic": _SourceCard("ytmusic", "YouTube Music",
                "the full catalog · needs browser cookie import",
                needs_setup=True),
            "soundcloud": _SourceCard("soundcloud", "SoundCloud",
                "free · no setup needed", needs_setup=False),
            "bandcamp": _SourceCard("bandcamp", "Bandcamp",
                "independent artists · no setup needed", needs_setup=False),
            "mixcloud": _SourceCard("mixcloud", "Mixcloud",
                "DJ mixes & radio shows · no setup needed", needs_setup=False),
            "local": _SourceCard("local", "Local Files",
                "your own music · needs a music directory",
                needs_setup=True),
            "spotify": _SourceCard("spotify", "Spotify",
                "shelved — search works, playback blocked by spotify",
                needs_setup=True),
            "subsonic": _SourceCard("subsonic", "Subsonic / Navidrome",
                "self-hosted music server · needs server url + login",
                needs_setup=True),
            "apple": _SourceCard("apple", "Apple Music",
                "Apple ID · shelved", needs_setup=False, coming_soon=True),
        }
        # Nothing arrives pre-checked (see OnboardingResult.sources_enabled).
        for k, on in self._enabled.items():
            self._cards[k].setChecked(on)
        for card in self._cards.values():
            card.toggled_signal.connect(self._on_toggled)
            card.setup_clicked.connect(self._on_setup_clicked)

        cards_col = QVBoxLayout()
        cards_col.setSpacing(8)
        for k in ("ytmusic", "soundcloud", "bandcamp", "mixcloud", "local",
                  "subsonic", "spotify", "apple"):
            cards_col.addWidget(self._cards[k])
        cards_col.addStretch(1)

        scroll_inner = QWidget()
        scroll_inner.setLayout(cards_col)
        scroll = QScrollArea()
        scroll.setWidget(scroll_inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        col = QVBoxLayout(self)
        col.setSpacing(12)
        col.addWidget(prompt)
        col.addWidget(sub)
        col.addWidget(scroll, stretch=1)

    def _on_toggled(self, key: str, on: bool) -> None:
        self._enabled[key] = on
        # If the user toggles on a source that needs setup but hasn't done
        # it yet, prompt the setup flow — DEFERRED past this emission.
        if on and self._cards[key].needsSetup():
            QTimer.singleShot(0, lambda k=key: self._do_setup(k))
        self.state_changed.emit()

    def _on_setup_clicked(self, key: str) -> None:
        # [set up] button — defer the modal so we return from the click
        # emission first. See _do_setup for the why.
        QTimer.singleShot(0, lambda k=key: self._do_setup(k))

    def _do_setup(self, key: str) -> None:
        # Opening a modal QDialog directly inside the click / toggled
        # emission is unsafe on PySide6 + Python 3.14: when the modal
        # closes and the Python local goes out of scope here, Shiboken
        # tears down the dialog's QWidget tree while the SignalManager
        # frames are still live, and the deleteChildren walk corrupts
        # the heap. Two mitigations together: (1) defer via
        # QTimer.singleShot so the click handler returns first; (2)
        # explicit deleteLater() so destruction is queued for the next
        # event-loop tick instead of synchronous on Python ref-drop.
        if key == "ytmusic":
            from .wizard import SignInDialog
            dlg = SignInDialog(self)
            try:
                accepted = dlg.exec() == QDialog.DialogCode.Accepted
            finally:
                dlg.deleteLater()
            if accepted:
                self._yt_authed = True
                self._cards["ytmusic"].setSetupDone(True, "✓ signed in")
            else:
                self._uncheck_cancelled("ytmusic")
        elif key == "spotify":
            from .spotify_signin import SpotifySignInDialog, confirm_spotify_enable
            # Surface the shelved-state warning before any OAuth — the
            # user needs to consent to enabling a known-broken source
            # before we send them through a sign-in flow.
            if not confirm_spotify_enable(self):
                self._uncheck_cancelled("spotify")
                return
            dlg = SpotifySignInDialog(self)
            try:
                accepted = dlg.exec() == QDialog.DialogCode.Accepted
            finally:
                dlg.deleteLater()
            if accepted:
                self._spotify_authed = True
                self._cards["spotify"].setSetupDone(True, "✓ connected (no audio)")
            else:
                self._uncheck_cancelled("spotify")
        elif key == "subsonic":
            from .subsonic_signin import SubsonicSignInDialog
            dlg = SubsonicSignInDialog(self, initial=self._subsonic_cfg)
            try:
                accepted = dlg.exec() == QDialog.DialogCode.Accepted
                cfg = dlg.result_config() if accepted else None
            finally:
                dlg.deleteLater()
            if accepted and cfg is not None and cfg.is_complete():
                self._subsonic_cfg = cfg
                self._subsonic_authed = True
                # Short hostname label for the card status line.
                from urllib.parse import urlparse
                host = urlparse(cfg.url).hostname or cfg.url
                self._cards["subsonic"].setSetupDone(True, f"✓ {host}")
            else:
                self._uncheck_cancelled("subsonic")
        elif key == "local":
            home = str(Path.home())
            chosen = QFileDialog.getExistingDirectory(
                self, "pick your music directory",
                str(Path.home() / "Music") if (Path.home() / "Music").exists() else home,
            )
            if chosen:
                self._local_dir = chosen
                short = chosen.replace(home, "~", 1)
                if len(short) > 36:
                    short = "…" + short[-36:]
                self._cards["local"].setSetupDone(True, f"✓ {short}")
            else:
                self._uncheck_cancelled("local")
        # Setup outcomes flip can_advance() (e.g. _yt_authed) but reach here
        # via a deferred call — no toggle emission in flight — so [next]
        # stays stale until we re-emit. Without this, a successful sign-in
        # left the button disabled until the user toggled the card off/on.
        self.state_changed.emit()

    def _uncheck_cancelled(self, key: str) -> None:
        # The setup dialog (SignInDialog / QFileDialog) was reached from
        # inside _on_toggled. Re-firing toggled via setChecked() would
        # re-enter the slot mid-emission; on PySide6 + Wayland the cross-
        # thread Shiboken object-destroy races the main thread's GIL
        # hand-off and segfaults on first-launch users. Block the card's
        # signals so the inner QCheckBox flips back visually without
        # bubbling, then sync state by hand.
        card = self._cards[key]
        was_blocked = card.blockSignals(True)
        try:
            card.setChecked(False)
        finally:
            card.blockSignals(was_blocked)
        self._enabled[key] = False
        self.state_changed.emit()

    def can_advance(self) -> bool:
        # Allow advancing even with zero sources — user can configure later.
        # Block only if a sign-in/config-required source is enabled but
        # its setup hasn't completed.
        if self._enabled.get("ytmusic") and not self._yt_authed:
            return False
        if self._enabled.get("spotify") and not self._spotify_authed:
            return False
        if self._enabled.get("subsonic") and not self._subsonic_authed:
            return False
        if self._enabled.get("local") and not self._local_dir:
            return False
        return True

    def apply_to(self, result: OnboardingResult) -> None:
        result.sources_enabled = dict(self._enabled)
        result.yt_authed = self._yt_authed
        result.spotify_authed = self._spotify_authed
        result.subsonic_authed = self._subsonic_authed
        result.local_dir = self._local_dir
        if self._subsonic_cfg is not None:
            result.subsonic_url = self._subsonic_cfg.url
            result.subsonic_user = self._subsonic_cfg.user
            result.subsonic_pass = self._subsonic_cfg.password
            result.subsonic_auth_style = self._subsonic_cfg.auth_style
        # Pick a default active source — first enabled non-coming-soon.
        # Spotify ranked last: even if the user enabled it through the
        # shelved-state warning, picking it as the *default active* source
        # would land them in a search-only experience first thing.
        order = ["ytmusic", "subsonic", "soundcloud", "bandcamp", "mixcloud", "local", "spotify"]
        for k in order:
            if result.sources_enabled.get(k):
                result.active_source = k
                break


class _FeelStep(_Step):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._adaptive = True
        self._living_bg = True
        self._motion = "lite"
        self._scale = "normal"

        prompt = QLabel("a few last touches.")
        f = QFont(prompt.font())
        f.setBold(True)
        f.setPointSize(f.pointSize() + 4)
        prompt.setFont(f)
        prompt.setAlignment(Qt.AlignCenter)

        sub = QLabel("all of these are toggles in Settings later — this is just to bootstrap your vibe.")
        sub.setProperty("class", "dim")
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)

        # Adaptive accent toggle.
        self._adaptive_check = QCheckBox("shift the accent to the album cover (adaptive)")
        self._adaptive_check.setChecked(True)
        self._adaptive_check.toggled.connect(self._on_adaptive)

        # Living background — the album-lit gradient that breathes with the
        # music (adaptive_background + adaptive_pulse; style stays "field",
        # refinable later in Settings → appearance).
        self._living_bg_check = QCheckBox(
            "living background — album-colored glow that breathes with the music"
        )
        self._living_bg_check.setChecked(True)
        self._living_bg_check.toggled.connect(self._on_living_bg)

        # Motion picker — radio row.
        motion_label = QLabel("animations:")
        motion_label.setProperty("class", "dim")
        self._motion_group = QButtonGroup(self)
        motion_row = QHBoxLayout()
        motion_row.setSpacing(14)
        for value, label in (("off", "off"), ("lite", "lite"), ("full", "full")):
            rb = QRadioButton(label)
            if value == "lite":
                rb.setChecked(True)
            rb.toggled.connect(lambda on, v=value: on and self._on_motion(v))
            self._motion_group.addButton(rb)
            motion_row.addWidget(rb)
        motion_row.addStretch(1)

        # UI scale.
        scale_label = QLabel("ui scale:")
        scale_label.setProperty("class", "dim")
        self._scale_group = QButtonGroup(self)
        scale_row = QHBoxLayout()
        scale_row.setSpacing(14)
        for value, label in (("compact", "compact"), ("normal", "normal"),
                             ("large", "large"), ("huge", "huge")):
            rb = QRadioButton(label)
            if value == "normal":
                rb.setChecked(True)
            rb.toggled.connect(lambda on, v=value: on and self._on_scale(v))
            self._scale_group.addButton(rb)
            scale_row.addWidget(rb)
        scale_row.addStretch(1)

        col = QVBoxLayout(self)
        col.setSpacing(16)
        col.addWidget(prompt)
        col.addWidget(sub)
        col.addSpacing(8)
        col.addWidget(self._adaptive_check)
        col.addWidget(self._living_bg_check)
        col.addSpacing(8)
        col.addWidget(motion_label)
        col.addLayout(motion_row)
        col.addSpacing(8)
        col.addWidget(scale_label)
        col.addLayout(scale_row)
        col.addStretch(1)

    def _on_adaptive(self, on: bool) -> None:
        self._adaptive = on
        self.state_changed.emit()

    def _on_living_bg(self, on: bool) -> None:
        self._living_bg = on
        self.state_changed.emit()

    def _on_motion(self, value: str) -> None:
        self._motion = value
        self.state_changed.emit()

    def _on_scale(self, value: str) -> None:
        self._scale = value
        self.state_changed.emit()

    def apply_to(self, result: OnboardingResult) -> None:
        result.adaptive_accent = self._adaptive
        result.adaptive_background = self._living_bg
        result.motion = self._motion
        result.ui_scale = self._scale


class _IntegrationsStep(_Step):
    """Optional integrations — both need a credential the user has to go
    fetch in a browser, so both default off and the step is skippable.
    Discord gets no bundled app ID on purpose (see discord_rpc.py: presences
    show the app OWNER's name + assets, so a shared ID is a trust problem);
    the flow here mirrors Settings → discord."""

    DISCORD_HELP_URL = "https://discord.com/developers/applications"
    LB_HELP_URL = "https://listenbrainz.org/profile/"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        prompt = QLabel("plug tide into your profile?")
        f = QFont(prompt.font())
        f.setBold(True)
        f.setPointSize(f.pointSize() + 4)
        prompt.setFont(f)
        prompt.setAlignment(Qt.AlignCenter)

        sub = QLabel("both are optional and live in Settings later — "
                     "skip straight past if you just want music.")
        sub.setProperty("class", "dim")
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)

        # -- discord --
        self._discord_check = QCheckBox("discord rich presence")
        fb = QFont(self._discord_check.font())
        fb.setBold(True)
        self._discord_check.setFont(fb)
        self._discord_check.toggled.connect(self._on_changed)

        self._discord_id = QLineEdit()
        self._discord_id.setPlaceholderText("paste discord application id")
        self._discord_id.setEnabled(False)
        self._discord_id.textChanged.connect(self._on_changed)
        self._discord_check.toggled.connect(self._discord_id.setEnabled)

        discord_help = BracketButton("get an app id")
        discord_help.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(self.DISCORD_HELP_URL))
        )
        discord_row = QHBoxLayout()
        discord_row.addWidget(self._discord_id, stretch=1)
        discord_row.addWidget(discord_help)

        discord_blurb = QLabel(
            "shows what you're listening to on your discord profile. takes "
            "~30 seconds: create an application at the developer portal and "
            "paste its application id — discord shows whatever name/image "
            "you give that app."
        )
        discord_blurb.setWordWrap(True)
        discord_blurb.setProperty("class", "dim")

        # -- listenbrainz --
        self._lb_check = QCheckBox("listenbrainz scrobbling")
        self._lb_check.setFont(fb)
        self._lb_check.toggled.connect(self._on_changed)

        self._lb_token = QLineEdit()
        self._lb_token.setPlaceholderText("paste your listenbrainz user token")
        self._lb_token.setEchoMode(QLineEdit.Password)
        self._lb_token.setEnabled(False)
        self._lb_token.textChanged.connect(self._on_changed)
        self._lb_check.toggled.connect(self._lb_token.setEnabled)

        lb_help = BracketButton("get a token")
        lb_help.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(self.LB_HELP_URL))
        )
        lb_row = QHBoxLayout()
        lb_row.addWidget(self._lb_token, stretch=1)
        lb_row.addWidget(lb_help)

        lb_blurb = QLabel(
            "keeps an open-source record of your listening history at "
            "listenbrainz.org. free account → settings → copy the token."
        )
        lb_blurb.setWordWrap(True)
        lb_blurb.setProperty("class", "dim")

        col = QVBoxLayout(self)
        col.setSpacing(10)
        col.addWidget(prompt)
        col.addWidget(sub)
        col.addSpacing(10)
        col.addWidget(self._discord_check)
        col.addLayout(discord_row)
        col.addWidget(discord_blurb)
        col.addSpacing(12)
        col.addWidget(self._lb_check)
        col.addLayout(lb_row)
        col.addWidget(lb_blurb)
        col.addStretch(1)

    def _on_changed(self, *_a) -> None:
        self.state_changed.emit()

    def can_advance(self) -> bool:
        # A checked integration with an empty credential can't work — hold
        # [next] until the field is filled or the box is unchecked again.
        if self._discord_check.isChecked() and not self._discord_id.text().strip():
            return False
        if self._lb_check.isChecked() and not self._lb_token.text().strip():
            return False
        return True

    def apply_to(self, result: OnboardingResult) -> None:
        result.discord_app_id = self._discord_id.text().strip()
        result.discord_enabled = (
            self._discord_check.isChecked() and bool(result.discord_app_id)
        )
        result.listenbrainz_token = self._lb_token.text().strip()
        result.listenbrainz_enabled = (
            self._lb_check.isChecked() and bool(result.listenbrainz_token)
        )


class _AllSetStep(_Step):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._summary_label = QLabel("")
        self._summary_label.setAlignment(Qt.AlignCenter)
        self._summary_label.setWordWrap(True)

        title = QLabel("you're tide-ready.")
        f = QFont(title.font())
        f.setBold(True)
        f.setPointSize(f.pointSize() + 10)
        title.setFont(f)
        title.setAlignment(Qt.AlignCenter)

        sub = QLabel("hit launch and start listening. everything you just picked is reversible from Settings.")
        sub.setProperty("class", "dim")
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)

        col = QVBoxLayout(self)
        col.setSpacing(14)
        col.addStretch(1)
        col.addWidget(title)
        col.addWidget(sub)
        col.addSpacing(12)
        col.addWidget(self._summary_label)
        col.addStretch(2)

    def on_enter(self, result: OnboardingResult) -> None:
        enabled = [k for k, v in result.sources_enabled.items() if v]
        srcs = " · ".join(enabled) if enabled else "(no sources — add some in Settings)"
        themes = theming.discover_themes()
        theme_name = themes[result.theme_slug].name if result.theme_slug in themes else result.theme_slug
        bits = [
            f"theme — {theme_name} ({result.aesthetic})",
            f"sources — {srcs}",
            f"motion — {result.motion} · ui scale — {result.ui_scale}"
            + (" · adaptive accent on" if result.adaptive_accent else ""),
        ]
        integrations = [name for name, on in (
            ("discord", result.discord_enabled),
            ("listenbrainz", result.listenbrainz_enabled),
        ) if on]
        if integrations:
            bits.append("integrations — " + " · ".join(integrations))
        self._summary_label.setText("\n".join(bits))


# ---------- the dialog ----------


class OnboardingDialog(QDialog):
    """Premium first-launch wizard. ``exec()``-modal; returns
    QDialog.DialogCode.Accepted when the user reaches the final step + hits
    launch, Rejected if they close the window mid-flight."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("tide — welcome")
        self.setModal(True)
        self.resize(720, 600)

        self._result = OnboardingResult()
        self._steps: list[_Step] = [
            _WelcomeStep(self),
            _AestheticStep(self),
            _ThemeStep(self),
            _SourcesStep(self),
            _FeelStep(self),
            _IntegrationsStep(self),
            _AllSetStep(self),
        ]
        # Wire state_changed to refresh the Next button enablement.
        for s in self._steps:
            s.state_changed.connect(self._update_buttons)

        self._stack = QStackedWidget()
        for s in self._steps:
            self._stack.addWidget(s)

        self._progress = _ProgressDots(total=len(self._steps))

        self._back_btn = BracketButton("back")
        self._back_btn.clicked.connect(self._back)
        self._next_btn = BracketButton("next")
        self._next_btn.clicked.connect(self._next)
        self._launch_btn = BracketButton("launch tide")
        self._launch_btn.clicked.connect(self._finish)
        self._launch_btn.hide()

        btn_row = QHBoxLayout()
        btn_row.addWidget(self._back_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self._next_btn)
        btn_row.addWidget(self._launch_btn)

        top_row = QHBoxLayout()
        top_row.addStretch(1)
        top_row.addWidget(self._progress)
        top_row.addStretch(1)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 18, 28, 18)
        root.setSpacing(18)
        root.addLayout(top_row)
        root.addWidget(self._stack, stretch=1)
        root.addLayout(btn_row)

        self._on_step_entered(0)

    # ---------- navigation ----------

    def _back(self) -> None:
        idx = self._stack.currentIndex()
        if idx <= 0:
            return
        self._transition_to(idx - 1)

    def _next(self) -> None:
        idx = self._stack.currentIndex()
        step = self._steps[idx]
        if not step.can_advance():
            return
        step.apply_to(self._result)
        self._transition_to(idx + 1)

    def _transition_to(self, idx: int) -> None:
        # Apply prior step's choices to the result before leaving so the
        # next step's on_enter sees fresh state.
        from . import motion as motion_module
        try:
            motion_module.crossfade_stack(
                self._stack, idx, dur=motion_module.DUR_SHORT,
                on_done=lambda: self._on_step_entered(idx),
            )
        except Exception:
            self._stack.setCurrentIndex(idx)
            self._on_step_entered(idx)

    def _on_step_entered(self, idx: int) -> None:
        self._progress.set_step(idx)
        self._steps[idx].on_enter(self._result)
        self._update_buttons()

    def _update_buttons(self) -> None:
        idx = self._stack.currentIndex()
        step = self._steps[idx]
        self._back_btn.setVisible(idx > 0)
        last = idx == len(self._steps) - 1
        self._next_btn.setVisible(not last)
        self._next_btn.setEnabled(step.can_advance())
        self._launch_btn.setVisible(last)

    def _finish(self) -> None:
        # Persist final step.
        self._steps[self._stack.currentIndex()].apply_to(self._result)
        self._result.completed = True
        self.accept()

    # ---------- public API ----------

    def result_data(self) -> OnboardingResult:
        return self._result
