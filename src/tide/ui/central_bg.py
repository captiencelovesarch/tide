"""Adaptive app backdrop.

A wrapper widget that sits behind Tide's main app surface. By itself it
paints whatever the theme says ``bg`` is; when
``adaptive_background`` is on, it paints layered album-tinted fields over
that base color. The adaptive driver supplies ``ambient_bg`` / ``accent_alt``
via the theming manager's runtime overrides, so the background shifts with
album art automatically without the wrapper needing to know anything about
palette extraction.

Corners obey ``corner_style`` (sharp / soft / rounded). The radius is
applied to both the gradient draw and the clipping mask, so the gradient
stops *inside* the rounded shape — the window's bg shows through the
corners cleanly.

Child widgets keep their own QSS-defined backgrounds. Structural containers
are made transparent by theming._CONTENT_BACKDROP_QSS so the app has one
coherent backdrop, while real controls keep their own surfaces.
"""
from __future__ import annotations

import colorsys
import math
import time

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QRadialGradient,
)
from PySide6.QtWidgets import QHBoxLayout, QWidget

from .. import theming


# Maps the corner_style setting to a pixel radius. Kept here so the dialog
# and the painter share one source of truth.
CORNER_RADII: dict[str, int] = {
    "sharp": 0,
    "soft": 6,
    "rounded": 12,
}


def corner_radius(style: str) -> int:
    return CORNER_RADII.get(style or "sharp", 0)


# Animation tuning. The drift oscillators use mutually-prime-ish periods so
# the composite motion never obviously loops.
_ANIM_INTERVAL_MS = 42          # ~24 fps — a slow drift + bass swell needs no more,
                                # and the content now composites over it each frame
_PERIOD_FLOW_S = 43.0
_PERIOD_FIELD_A_S = 29.0
_PERIOD_FIELD_B_S = 37.0
_PERIOD_FIELD_C_S = 53.0
_BASE_ANGLE = math.radians(56)  # diagonal, top-left → bottom-right
# Offscreen buffer cap (long side, px). The gradient is smooth so a small
# buffer upscaled bilinearly is visually identical to a full-res fill, but
# caps the fill cost regardless of window size / desktop scaling.
_BUF_CAP = 384


def _lerp(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red()   + (b.red()   - a.red())   * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue()  + (b.blue()  - a.blue())  * t),
    )


def _alpha(c: QColor, a: int) -> QColor:
    out = QColor(c)
    out.setAlpha(max(0, min(255, int(a))))
    return out


def _bg_tone(c: QColor, l: float, s: float) -> QColor:
    """Take the *hue* of ``c`` and place it at a fixed lightness/saturation.
    Used to turn a vivid album accent into a background tone that's dark
    enough to keep content legible but light enough to actually be seen
    against the theme bg (the previous 'deepen' approach was so dark it was
    invisible)."""
    h, _, base_s = colorsys.rgb_to_hls(c.redF(), c.greenF(), c.blueF())
    target_s = max(0.0, min(1.0, s if base_s >= 0.035 else 0.0))
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), target_s)
    return QColor(int(r * 255), int(g * 255), int(b * 255))


def _hls_saturation(c: QColor) -> float:
    return colorsys.rgb_to_hls(c.redF(), c.greenF(), c.blueF())[2]


class CentralBg(QWidget):
    """Wraps the main app surface. When enabled, paints a slowly morphing
    album-palette field that also swells on bass.

    The colors come from the theme tokens ``bg`` / ``ambient_bg`` /
    ``accent_alt`` — the adaptive driver overrides ambient_bg + accent_alt
    from album art and the theming manager re-emits ``theme_changed``, so
    this widget tracks album color with no extra wiring. The bass pulse is
    fed in via ``set_pulse`` from the ambient controller and is a *local*
    paint effect — it never touches the theme/QSS, so a per-frame pulse costs
    one small buffer fill + a scaled blit, not an app-wide restyle.
    """

    def __init__(self, child: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # WA_StyledBackground=False so QSS doesn't override our paintEvent
        # (the brutalist theme sets `QWidget { background: @bg }` globally).
        self.setAttribute(Qt.WA_StyledBackground, False)
        # We DO want a backing buffer so children compose against our paint
        # rather than the window's bg, which prevents flicker on resize.
        self.setAutoFillBackground(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(child)

        self._enabled: bool = False
        self._radius: int = 0
        self._style: str = "field"          # "field" | "band" | "vbeam"
        self._motion: str = "lite"          # "off" freezes the drift
        self._bg = QColor("#0b0b0b")
        self._tone_a = QColor("#141414")
        self._tone_b = QColor("#141414")
        self._tone_c = QColor("#141414")
        self._pulse: float = 0.0            # target from the audio feed
        self._pulse_shown: float = 0.0      # smoothed value actually painted
        self._t0 = time.monotonic()
        self._buf: QImage | None = None

        self._anim = QTimer(self)
        self._anim.setInterval(_ANIM_INTERVAL_MS)
        self._anim.timeout.connect(self._tick)

        theming.manager().theme_changed.connect(self._on_theme)
        self._on_theme(theming.manager().current())

    # ---------- public API ----------

    def set_enabled(self, on: bool) -> None:
        if on == self._enabled:
            return
        self._enabled = on
        self._sync_timer()
        self.update()

    def set_radius(self, radius: int) -> None:
        r = max(0, int(radius))
        if r == self._radius:
            return
        self._radius = r
        self.update()

    def set_style(self, style: str) -> None:
        new_style = style if style in {"field", "band", "vbeam"} else "field"
        if new_style == self._style:
            return
        self._style = new_style
        self.update()

    def set_motion(self, motion: str) -> None:
        new_motion = motion or "lite"
        if new_motion == self._motion:
            return
        self._motion = new_motion
        self._sync_timer()
        self.update()

    def set_pulse(self, level: float) -> None:
        """Feed the current bass-energy envelope (0..1). Stored only — the
        animation timer paints it, so this can be called at audio rate
        without exceeding the repaint cap."""
        self._pulse = max(0.0, min(1.0, float(level)))
        self._sync_timer()

    # ---------- lifecycle ----------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._anim.stop()

    def _sync_timer(self) -> None:
        # Run only when there's something to animate and we're on screen.
        active = self._enabled and self.isVisible() and (
            self._motion != "off" or self._pulse > 0.001 or self._pulse_shown > 0.001
        )
        if active and not self._anim.isActive():
            self._anim.start()
        elif not active and self._anim.isActive():
            self._anim.stop()

    def _tick(self) -> None:
        if not self._enabled or not self.isVisible():
            self._anim.stop()
            return
        changed = self._motion != "off"
        if abs(self._pulse - self._pulse_shown) > 0.003:
            self._pulse_shown += (self._pulse - self._pulse_shown) * 0.5
            changed = True
        else:
            self._pulse_shown = self._pulse
        if changed:
            self.update()
        else:
            # Nothing moving (motion off + steady/zero pulse) — idle the timer.
            self._sync_timer()

    # ---------- theme tracking ----------

    def _on_theme(self, theme) -> None:
        if theme is None:
            return
        # The adaptive driver pushes ambient_bg + accent_alt as dynamic
        # overrides;
        # the theming manager re-emits theme_changed when that happens, so we
        # re-derive the tones with no additional wiring.
        self._bg = QColor(theme.token("bg", "#0b0b0b"))
        surface = QColor(theme.token("bg_alt", self._bg.name()))
        if not surface.isValid():
            surface = QColor(self._bg)
        ambient_bg = QColor(
            theme.token("ambient_bg", theme.token("bg_alt", self._bg.name()))
        )
        if not ambient_bg.isValid():
            ambient_bg = QColor(self._bg)
        accent = QColor(theme.token("accent", "#d4b95e"))
        if not accent.isValid():
            accent = QColor(ambient_bg)
        accent_alt = QColor(theme.token("accent_alt", accent.name()))
        if not accent_alt.isValid():
            accent_alt = QColor(accent)

        body_has_hue = _hls_saturation(ambient_bg) >= 0.04
        body = ambient_bg if body_has_hue else surface
        if not body_has_hue or _hls_saturation(accent) < 0.04:
            accent = QColor(body)
        if not body_has_hue or _hls_saturation(accent_alt) < 0.04:
            accent_alt = QColor(accent)

        # Album-derived hues placed in a visible band around the theme bg:
        # for a dark theme, tones sit a clear step *lighter* than bg (so the
        # gradient reads against black); for a light theme, a step darker.
        # Content stays legible because these are still well away from fg.
        if self._bg.lightnessF() > 0.5:
            self._tone_a = _bg_tone(body, 0.78, 0.22)
            self._tone_b = _bg_tone(accent, 0.70, 0.28)
            self._tone_c = _bg_tone(accent_alt, 0.62, 0.32)
        else:
            self._tone_a = _bg_tone(body, 0.22, 0.34)
            self._tone_b = _bg_tone(accent, 0.29, 0.38)
            self._tone_c = _bg_tone(accent_alt, 0.34, 0.42)
        self.update()

    # ---------- paint ----------

    def _render_buffer(self, w: int, h: int) -> QImage:
        if w >= h:
            bw = min(w, _BUF_CAP)
            bh = max(1, round(bw * h / max(1, w)))
        else:
            bh = min(h, _BUF_CAP)
            bw = max(1, round(bh * w / max(1, h)))
        if self._buf is None or self._buf.width() != bw or self._buf.height() != bh:
            self._buf = QImage(bw, bh, QImage.Format_RGB32)
        img = self._buf

        t = time.monotonic() - self._t0
        motion = 0.0
        if self._motion != "off":
            motion = 1.0 if self._motion == "full" else 0.58
        pulse = math.pow(max(0.0, min(1.0, self._pulse_shown)), 1.12)
        dark = self._bg.lightnessF() <= 0.5

        def wave(period: float, phase: float = 0.0) -> float:
            return math.sin((2 * math.pi * t / period) + phase)

        def reactive(color: QColor, mix: float) -> QColor:
            out = _lerp(self._bg, color, min(1.0, mix + 0.08 * pulse))
            if pulse <= 0.0:
                return out
            factor = int(100 + 42 * pulse)
            return out.lighter(factor) if dark else out.darker(factor)

        tone_a = reactive(self._tone_a, 0.74)
        tone_b = reactive(self._tone_b, 0.70)
        tone_c = reactive(self._tone_c, 0.66)
        clear = _alpha(self._bg, 0)
        max_side = max(bw, bh)

        pp = QPainter(img)
        pp.setRenderHint(QPainter.Antialiasing, True)
        pp.fillRect(img.rect(), self._bg)

        if self._style == "band":
            angle = (
                _BASE_ANGLE
                + motion * 0.50 * wave(_PERIOD_FLOW_S, 0.2)
                + pulse * 0.05
            )
            extent = 0.70 + motion * 0.14 * wave(_PERIOD_FIELD_A_S, 1.0)
            extent *= 1.0 + 0.22 * pulse
            ox = motion * 0.09 * wave(_PERIOD_FIELD_B_S, 0.3)
            oy = motion * 0.07 * wave(_PERIOD_FIELD_C_S, 1.4)

            halo = QRadialGradient(
                bw * (0.50 + motion * 0.22 * wave(_PERIOD_FIELD_B_S, 2.0)),
                bh * (0.50 + motion * 0.18 * wave(_PERIOD_FIELD_C_S, 3.0)),
                0.62 * max_side * (1.0 + 0.18 * pulse),
            )
            halo.setColorAt(0.0, _alpha(tone_b, 34 + int(52 * pulse)))
            halo.setColorAt(0.48, _alpha(tone_a, 22 + int(32 * pulse)))
            halo.setColorAt(1.0, clear)
            pp.fillRect(img.rect(), QBrush(halo))

            cx, cy = bw * (0.5 + ox), bh * (0.5 + oy)
            dx, dy = math.cos(angle), math.sin(angle)
            half = 0.5 * extent * math.hypot(bw, bh)
            band = QLinearGradient(cx - dx * half, cy - dy * half,
                                   cx + dx * half, cy + dy * half)
            band_alpha = 112 + int(62 * pulse)
            band.setColorAt(0.00, clear)
            band.setColorAt(0.18, clear)
            band.setColorAt(0.40, _alpha(tone_a, band_alpha))
            band.setColorAt(0.58, _alpha(tone_b, min(220, band_alpha + 24)))
            band.setColorAt(0.82, clear)
            band.setColorAt(1.00, clear)
            pp.fillRect(img.rect(), QBrush(band))
            pp.end()
            return img

        if self._style == "vbeam":
            # Hill → arch of light. Built entirely from elliptical glows —
            # every brightness contour is a gradient ramp, so nothing here can
            # produce an outline (same construction as the band/field styles,
            # which paint only gradients that fade to clear).
            sway = motion * 0.020 * wave(_PERIOD_FIELD_A_S, 1.0)
            breathe = motion * 0.020 * wave(_PERIOD_FLOW_S, 1.8)
            center_x = bw * (0.50 + sway)
            base_y = bh * 1.03
            arch_h = bh * (0.17 + breathe + 0.62 * pulse)
            half_w = bw * (0.32 + 0.24 * pulse)
            apex_y = base_y - arch_h

            def glow(cx: float, cy: float, rx: float, ry: float,
                     stops: list[tuple[float, QColor]]) -> None:
                # Elliptical radial glow: a unit radial gradient painted under
                # a scale transform, filled only over its own extent.
                pp.save()
                pp.translate(cx, cy)
                pp.scale(max(1e-3, rx), max(1e-3, ry))
                g = QRadialGradient(0.0, 0.0, 1.0)
                for pos, color in stops:
                    g.setColorAt(pos, color)
                pp.fillRect(QRectF(-1.0, -1.0, 2.0, 2.0), QBrush(g))
                pp.restore()

            # Wide under-wash anchoring the shape to the bottom edge; grows
            # with the arch so the idle scene stays quiet.
            glow(center_x, base_y,
                 bw * (0.72 + 0.20 * pulse), bh * (0.30 + 0.30 * pulse),
                 [(0.00, _alpha(tone_a, 46 + int(46 * pulse))),
                  (0.55, _alpha(tone_a, 18 + int(20 * pulse))),
                  (1.00, clear)])

            # Arch body — the dome itself is just this glow's upper half:
            # shallow at rest (a soft mound), tall on a bass hit (an arch).
            glow(center_x, base_y, half_w * 1.35, arch_h * 1.30,
                 [(0.00, _alpha(tone_b, 128 + int(64 * pulse))),
                  (0.45, _alpha(tone_a, 56 + int(56 * pulse))),
                  (0.75, _alpha(tone_a, 18 + int(22 * pulse))),
                  (1.00, clear)])

            # Hot core low in the mound for depth.
            glow(center_x, base_y, half_w * 0.80, arch_h * 0.85,
                 [(0.00, _alpha(tone_b, 84 + int(72 * pulse))),
                  (0.55, _alpha(tone_a, 26 + int(34 * pulse))),
                  (1.00, clear)])

            if pulse > 0.01:
                # Crest bloom — light gathering at the apex on bass. Reads as
                # a halo, never a rim: it is another edgeless glow. Tucked a
                # little below the apex and kept in the crest color family so
                # it fuses with the arch body instead of floating above it.
                crest_tone = tone_c.lighter(126) if dark else tone_c.darker(116)
                crest_r = max_side * (0.14 + 0.20 * pulse)
                glow(center_x, apex_y + arch_h * 0.18, crest_r, crest_r * 0.90,
                     [(0.00, _alpha(crest_tone, int(96 * pulse))),
                      (0.50, _alpha(tone_c, int(42 * pulse))),
                      (1.00, clear)])
            pp.end()
            return img

        def draw_field(
            color: QColor,
            base_x: float,
            base_y: float,
            radius: float,
            alpha: int,
            period: float,
            phase: float,
        ) -> None:
            drift_x = motion * 0.10 * wave(period, phase)
            drift_y = motion * 0.08 * wave(period * 1.19, phase + 1.7)
            kick_x = pulse * 0.035 * math.cos(phase + 0.8)
            kick_y = pulse * 0.030 * math.sin(phase + 0.4)
            x = bw * (base_x + drift_x + kick_x)
            y = bh * (base_y + drift_y + kick_y)
            r = max_side * radius * (1.0 + 0.16 * pulse)
            grad = QRadialGradient(x, y, r)
            grad.setColorAt(0.00, _alpha(color, alpha + int(34 * pulse)))
            grad.setColorAt(0.42, _alpha(color, int(alpha * 0.48) + int(22 * pulse)))
            grad.setColorAt(1.00, clear)
            pp.fillRect(img.rect(), QBrush(grad))

        draw_field(tone_a, 0.22, 0.22, 0.74, 76, _PERIOD_FIELD_A_S, 0.0)
        draw_field(tone_b, 0.82, 0.30, 0.70, 68, _PERIOD_FIELD_B_S, 2.2)
        draw_field(tone_c, 0.48, 0.86, 0.82, 58, _PERIOD_FIELD_C_S, 4.1)

        flow_angle = (
            _BASE_ANGLE
            + motion * 0.28 * wave(_PERIOD_FLOW_S, 0.5)
            + pulse * 0.07
        )
        flow_offset = motion * 0.10 * wave(_PERIOD_FLOW_S * 0.73, 2.0)
        cx = bw * (0.50 + flow_offset)
        cy = bh * (0.50 - flow_offset * 0.55)
        dx, dy = math.cos(flow_angle), math.sin(flow_angle)
        half = 0.78 * math.hypot(bw, bh) * (1.0 + 0.10 * pulse)
        wash = QLinearGradient(cx - dx * half, cy - dy * half,
                               cx + dx * half, cy + dy * half)
        wash_alpha = 34 + int(28 * pulse)
        wash.setColorAt(0.00, clear)
        wash.setColorAt(0.24, _alpha(tone_a, int(wash_alpha * 0.45)))
        wash.setColorAt(0.48, _alpha(tone_b, wash_alpha))
        wash.setColorAt(0.72, _alpha(tone_c, int(wash_alpha * 0.60)))
        wash.setColorAt(1.00, clear)
        pp.fillRect(img.rect(), QBrush(wash))

        vignette = QRadialGradient(bw * 0.52, bh * 0.46, max_side * 0.86)
        vignette.setColorAt(0.00, clear)
        vignette.setColorAt(0.68, clear)
        vignette.setColorAt(1.00, _alpha(self._bg, 88 if dark else 70))
        pp.fillRect(img.rect(), QBrush(vignette))
        pp.end()
        return img

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        try:
            rect = self.rect()
            if self._radius > 0:
                # Rounded clip so the fill stops inside the corners, leaving
                # the window bg to show through them.
                path = QPainterPath()
                path.addRoundedRect(
                    float(rect.left()), float(rect.top()),
                    float(rect.width()), float(rect.height()),
                    float(self._radius), float(self._radius),
                )
                p.setRenderHint(QPainter.Antialiasing, True)
                p.setClipPath(path)

            if self._enabled:
                img = self._render_buffer(max(1, rect.width()), max(1, rect.height()))
                p.setRenderHint(QPainter.SmoothPixmapTransform, True)
                p.drawImage(rect, img)
            else:
                p.fillRect(rect, self._bg)
        finally:
            p.end()
