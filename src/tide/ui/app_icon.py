"""The app icon: a crescent moon over the sea, its reflection laid on the
water as bars (the tide is the moon's doing; the bars are level meters,
and brutalist's blocks).

One drawing, two cuts. modern gets the lit version (gradients, a glow
round the moon, a soft tile); brutalist gets it flat and square, framed
in the theme's fg like every brutalist tile, the wave stepped. Colours
come from the theme, so every theme (a hand-made one too) has an icon
that matches it. ``CLASSIC`` is the hand-tuned palette the launcher icon
(assets/icon*.png, assets/icon.svg) is rendered from; after changing the
drawing, re-render those with tools/render_icons.py.

The window icon follows the active theme when the ``app_icon`` setting
says so. The launcher's icon is the installed one and stays classic: it's
read by the desktop before tide runs. The tray has a cut of its own (see
``tray_icon``): one flat colour, like every other icon in a panel tray.

Qt's SVG renderer (the one KDE draws icons with too) ignores clipPath
and masks, so every shape here stays inside the tile on its own.
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import (
    QColor, QGuiApplication, QIcon, QImage, QPainter, QPainterPath, QPixmap,
)

ICON_SIZES = (16, 22, 24, 32, 48, 64, 128, 256)

# The crescent: a circle at (256, 190) r 74 with a circle at (292, 162)
# r 64 taken out of it, as one path.
_CRESCENT = ("M246.93 116.57 A74 74 0 1 0 324.95 216.87 "
             "A64 64 0 0 1 246.93 116.57 Z")
_WAVE = ("M32 302 C 96 284 160 284 224 300 S 352 318 416 300 "
         "S 468 288 480 290")

_MODERN = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="sky" x1="0" y1="32" x2="0" y2="480" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{sky_top}"/>
      <stop offset="0.6" stop-color="{sky_mid}"/>
      <stop offset="1" stop-color="{sky_low}"/>
    </linearGradient>
    <radialGradient id="glow" cx="262" cy="190" r="150" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{moon_low}" stop-opacity="{glow}"/>
      <stop offset="0.5" stop-color="{moon_low}" stop-opacity="{glow_mid}"/>
      <stop offset="1" stop-color="{moon_low}" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="sea" x1="0" y1="286" x2="0" y2="480" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{sea_top}"/>
      <stop offset="1" stop-color="{sea_low}"/>
    </linearGradient>
    <linearGradient id="moon" x1="0" y1="118" x2="0" y2="262" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{moon_top}"/>
      <stop offset="1" stop-color="{moon_low}"/>
    </linearGradient>
  </defs>
  <rect x="32" y="32" width="448" height="448" rx="100" fill="url(#sky)"/>
  <circle cx="262" cy="190" r="150" fill="url(#glow)"/>
  <path d="{crescent}" fill="url(#moon)"/>
  <path d="{wave} L480 380 A100 100 0 0 1 380 480 L132 480 A100 100 0 0 1 32 380 Z" fill="url(#sea)"/>
  <path d="M34 301.5 C 96 284 160 284 224 300 S 352 318 416 300 S 468 288 478 290" fill="none" stroke="{crest}" stroke-width="7" opacity="0.85"/>
  <g fill="{bars}">
    <rect x="148" y="334" width="216" height="22" rx="11" opacity="0.95"/>
    <rect x="180" y="372" width="152" height="22" rx="11" opacity="0.72"/>
    <rect x="210" y="410" width="92" height="22" rx="11" opacity="0.50"/>
    <rect x="236" y="448" width="40" height="18" rx="9" opacity="0.30"/>
  </g>
</svg>
"""

# Stepped wave: the same swell, quantised to 51px columns and 14px rows
# so the steps still read at 32px.
_STEPS = (300, 286, 286, 300, 314, 314, 300, 286)

_BRUTALIST = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect x="32" y="32" width="448" height="448" fill="{frame}"/>
  <rect x="52" y="52" width="408" height="408" fill="{sky}"/>
  <path d="{crescent}" fill="{moon}"/>
  <path d="{sea_path}" fill="{sea}"/>
  <path d="{crest_path}" fill="none" stroke="{crest}" stroke-width="12" stroke-linejoin="miter"/>
  <g fill="{bars}">
    <rect x="148" y="334" width="216" height="22"/>
    <rect x="180" y="372" width="152" height="22"/>
    <rect x="210" y="410" width="92" height="22"/>
    <rect x="236" y="436" width="40" height="18"/>
  </g>
</svg>
"""


def _stepped_paths() -> tuple[str, str]:
    x0, x1, col = 52, 460, 51
    pts: list[tuple[int, int]] = []
    x = x0
    for y in _STEPS:
        nx = min(x1, x + col)
        pts += [(x, y), (nx, y)]
        x = nx
        if x >= x1:
            break
    crest = "M" + " L".join(f"{px} {py}" for px, py in pts)
    sea = crest + f" L{x1} 460 L{x0} 460 Z"
    return sea, crest


# The launcher icon's palette: night sea, gold moon (the gold carries over
# from the v1 icon's square).
CLASSIC: dict[str, str] = {
    "sky_top": "#1d2b48", "sky_mid": "#111b30", "sky_low": "#080c16",
    "moon_top": "#fdeab5", "moon_low": "#ecbc58",
    "sea_top": "#1f4d72", "sea_low": "#091424",
    "crest": "#72b8d2", "bars": "#f1c865",
    "glow": "0.30", "glow_mid": "0.08",
}


def _c(value: str, fallback: str) -> QColor:
    c = QColor(str(value or ""))
    if not c.isValid():
        c = QColor(fallback)
    c.setAlpha(255)     # translucent themes: the icon is always opaque
    return c


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(round(a.red() + (b.red() - a.red()) * t),
                  round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


def _hex(c: QColor) -> str:
    return c.name(QColor.HexRgb)


def modern_palette(theme) -> dict[str, str]:
    """The lit drawing's colours from a theme: its bg is the sky, its
    accent the moon and the bars, its second accent (or the first)
    tints the sea. Light themes get a pale sky over a darker sea."""
    bg = _c(theme.token("bg"), "#0b0b0b")
    fg = _c(theme.token("fg"), "#e6e6e6")
    accent = _c(theme.token("accent"), "#d4b95e")
    alt = _c(theme.token("accent_alt", theme.token("accent")), accent.name())
    if bg.lightnessF() > 0.5:
        return {
            "sky_top": _hex(bg.lighter(104)),
            "sky_mid": _hex(_mix(bg, alt, 0.08)),
            "sky_low": _hex(_mix(bg, alt, 0.16)),
            "moon_top": _hex(_mix(accent, bg, 0.25)),
            "moon_low": _hex(accent),
            "sea_top": _hex(_mix(alt, fg, 0.25)),
            "sea_low": _hex(_mix(fg, bg, 0.15)),
            "crest": _hex(_mix(alt, bg, 0.45)),
            "bars": _hex(_mix(accent, bg, 0.15)),
            "glow": "0.22", "glow_mid": "0.06",
        }
    # Dark themes: lift the sky off the bg a little (a pure black tile
    # disappears on a dark panel) and pull the sea toward the second
    # accent.
    sky = _mix(bg, alt, 0.10)
    if sky.lightnessF() < 0.08:
        sky = _mix(sky, fg, 0.06)
    return {
        "sky_top": _hex(_mix(sky, alt, 0.12)),
        "sky_mid": _hex(sky),
        "sky_low": _hex(bg.darker(140)),
        "moon_top": _hex(_mix(accent, QColor("#ffffff"), 0.45)),
        "moon_low": _hex(accent),
        "sea_top": _hex(_mix(bg, alt, 0.38)),
        "sea_low": _hex(bg.darker(130)),
        "crest": _hex(_mix(alt, fg, 0.30)),
        "bars": _hex(_mix(accent, fg, 0.10)),
        "glow": "0.30", "glow_mid": "0.08",
    }


def brutalist_palette(theme) -> dict[str, str]:
    bg = _c(theme.token("bg"), "#0b0b0b")
    fg = _c(theme.token("fg"), "#e6e6e6")
    accent = _c(theme.token("accent"), "#d4b95e")
    alt = _c(theme.token("bg_alt"), bg.name())
    return {
        "frame": _hex(fg),
        "sky": _hex(bg),
        "moon": _hex(accent),
        "sea": _hex(_mix(alt, fg, 0.10) if alt != bg else _mix(bg, fg, 0.12)),
        "crest": _hex(fg),
        "bars": _hex(accent),
    }


def classic_svg() -> str:
    return _MODERN.format(crescent=_CRESCENT, wave=_WAVE, **CLASSIC)


def svg_for(theme) -> str:
    """The icon for ``theme`` (None: the classic one)."""
    if theme is None:
        return classic_svg()
    if getattr(theme, "aesthetic", "modern") == "brutalist":
        sea, crest = _stepped_paths()
        return _BRUTALIST.format(crescent=_CRESCENT, sea_path=sea,
                                 crest_path=crest, **brutalist_palette(theme))
    return _MODERN.format(crescent=_CRESCENT, wave=_WAVE,
                          **modern_palette(theme))


def render(svg: str, size: int) -> QImage:
    from PySide6.QtSvg import QSvgRenderer
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        renderer.render(p)
    finally:
        p.end()
    return img


def icon_for(theme) -> QIcon:
    svg = svg_for(theme)
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(QPixmap.fromImage(render(svg, size)))
    return icon


# ---------- the tray cut ----------
#
# Panel trays are one flat colour: white on a dark panel, near-black on a
# light one. The full-colour tile stood out in a row of them, so the tray
# gets the same moon over its reflection as a flat glyph. It's drawn per
# size straight onto the pixel grid instead of from SVG: at 16-22 px the
# reflection bars of a scaled drawing land between pixels and smear.

TRAY_SIZES = (16, 22, 24, 32, 44, 48, 64)

# Breeze's text colours, which is what the panel's own icons are drawn in.
TRAY_INK_ON_DARK = "#fcfcfc"
TRAY_INK_ON_LIGHT = "#232629"

# Reflection bar widths as a share of the icon, top bar first.
_TRAY_BARS = (0.86, 0.58, 0.30)


def tray_ink(scheme=None) -> str:
    """The glyph's colour for the system's light or dark setting. Unknown
    (no platform theme says) counts as dark: most panels are."""
    if scheme is None:
        scheme = QGuiApplication.styleHints().colorScheme()
    return TRAY_INK_ON_LIGHT if scheme == Qt.ColorScheme.Light else TRAY_INK_ON_DARK


def tray_image(size: int, ink: str) -> QImage:
    """The tray glyph at ``size`` px: a crescent over three bars, each
    bar a whole number of pixels tall and centred on the grid."""
    S = int(size)
    img = QImage(S, S, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    col = QColor(ink)
    p = QPainter(img)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        bar_h = max(1, round(S / 11))
        bar_gap = max(1, round(S / 16)) if S >= 20 else 1
        margin = max(0, round(S / 24))
        bars_top = S - margin - (3 * bar_h + 2 * bar_gap)
        y = bars_top
        for share in _TRAY_BARS:
            w = max(2, round(S * share))
            if (S - w) % 2:
                w -= 1                      # whole pixels either side
            r = bar_h / 2 if bar_h >= 2 else 0.0
            p.drawRoundedRect(QRectF((S - w) / 2, y, w, bar_h), r, r)
            y += bar_h + bar_gap
        # The moon fills what's left above, the same crescent as the tile:
        # a circle with one 0.865 its size cut out up and to the right.
        gap = max(1, round(S / 16))
        d = min(bars_top - gap - margin, S * 0.6)
        r = d / 2
        cx, cy = S / 2 - r * 0.05, bars_top - gap - r
        moon = QPainterPath()
        moon.addEllipse(QRectF(cx - r, cy - r, d, d))
        ri = r * 0.865
        bite = QPainterPath()
        bite.addEllipse(QRectF(cx + r * 0.486 - ri, cy - r * 0.378 - ri,
                               2 * ri, 2 * ri))
        p.drawPath(moon.subtracted(bite))
    finally:
        p.end()
    return img


def tray_icon(ink: str | None = None) -> QIcon:
    ink = ink or tray_ink()
    icon = QIcon()
    for size in TRAY_SIZES:
        icon.addPixmap(QPixmap.fromImage(tray_image(size, ink)))
    return icon
