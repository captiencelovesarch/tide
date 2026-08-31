import colorsys
import unittest

from PySide6.QtGui import QColor, QImage

from tide.ui.adaptive import extract_palette, pick_accent, pick_accent_alt, pick_bg_tint


def _hue(c: QColor) -> float:
    return colorsys.rgb_to_hls(c.redF(), c.greenF(), c.blueF())[0] * 360.0


def _hue_distance(a: QColor, b: QColor) -> float:
    d = abs(_hue(a) - _hue(b))
    return min(d, 360.0 - d)


def _cover(fill) -> QImage:
    """Synthetic 64×64 album cover; ``fill(x, y) -> (r, g, b)``."""
    img = QImage(64, 64, QImage.Format_RGB888)
    for y in range(64):
        for x in range(64):
            r, g, b = fill(x, y)
            img.setPixelColor(x, y, QColor(r, g, b))
    return img


def _gray(x: int, y: int) -> int:
    # Full-range gradient so every lightness band is represented.
    return x * 4 + y // 16


class AdaptivePaletteTests(unittest.TestCase):
    def test_muted_green_mass_beats_tiny_warm_outliers(self) -> None:
        palette = [
            (QColor("#667568"), 2600),
            (QColor("#727871"), 900),
            (QColor("#d9bf35"), 80),
            (QColor("#e35b9a"), 60),
        ]
        accent = pick_accent(palette, QColor("#0b0b0b"))
        tint = pick_bg_tint(palette)
        self.assertIsNotNone(accent)
        self.assertIsNotNone(tint)
        self.assertGreaterEqual(_hue(accent), 85.0)
        self.assertLessEqual(_hue(accent), 165.0)
        self.assertGreaterEqual(_hue(tint), 85.0)
        self.assertLessEqual(_hue(tint), 165.0)
        self.assertLess(_hue_distance(pick_accent_alt(palette, accent, QColor("#0b0b0b")), accent), 8.0)

    def test_secondary_hue_needs_real_palette_mass(self) -> None:
        palette = [
            (QColor("#6f1919"), 1700),
            (QColor("#451313"), 850),
            (QColor("#2d58d9"), 90),
        ]
        accent = pick_accent(palette, QColor("#0b0b0b"))
        alt = pick_accent_alt(palette, accent, QColor("#0b0b0b"))
        self.assertIsNotNone(accent)
        self.assertLess(_hue_distance(alt, accent), 8.0)

    def test_real_second_hue_can_be_used(self) -> None:
        palette = [
            (QColor("#366b45"), 1800),
            (QColor("#713b85"), 1400),
            (QColor("#202020"), 600),
        ]
        accent = pick_accent(palette, QColor("#0b0b0b"))
        alt = pick_accent_alt(palette, accent, QColor("#0b0b0b"))
        self.assertIsNotNone(accent)
        self.assertGreater(_hue_distance(alt, accent), 60.0)

    def test_grayscale_palette_has_no_fake_album_hue(self) -> None:
        palette = [
            (QColor("#777777"), 1800),
            (QColor("#4c4c4c"), 1200),
            (QColor("#a0a0a0"), 400),
            (QColor("#de4a5f"), 35),
        ]
        self.assertIsNone(pick_accent(palette, QColor("#0b0b0b")))
        self.assertIsNone(pick_bg_tint(palette))

    def test_extract_palette_reports_true_bucket_means(self) -> None:
        # Neutral pixels straddling a quantization-bucket edge used to come
        # back as bucket centers with chroma 17/255 — fake color.
        def fill(x, y):
            return (127, 127, 133) if x % 2 else (127, 127, 127)

        for color, _count in extract_palette(_cover(fill)):
            channels = (color.red(), color.green(), color.blue())
            self.assertLessEqual(max(channels) - min(channels), 6)

    def test_grayscale_cover_yields_no_override(self) -> None:
        palette = extract_palette(_cover(lambda x, y: (_gray(x, y),) * 3))
        self.assertIsNone(pick_accent(palette, QColor("#0b0b0b")))
        self.assertIsNone(pick_bg_tint(palette))

    def test_faint_color_cast_on_grayscale_yields_no_override(self) -> None:
        # The reported bug: a b/w cover with uniform scanner/JPEG toning
        # (a few counts of extra blue everywhere) chose a blue accent.
        for cast in (3, 5, 8):
            with self.subTest(cast=cast):
                def fill(x, y, cast=cast):
                    v = _gray(x, y)
                    return (v, v, min(255, v + cast))

                palette = extract_palette(_cover(fill))
                self.assertIsNone(pick_accent(palette, QColor("#0b0b0b")))
                self.assertIsNone(pick_bg_tint(palette))

    def test_solid_logo_on_bw_cover_wins(self) -> None:
        # A real color element at ~9% area (label logo, sticker) must still
        # override — grayscale detection must not eat actual color.
        def fill(x, y):
            if x < 24 and y < 16:
                return (40, 80, 220)
            v = _gray(x, y)
            return (v, v, v)

        palette = extract_palette(_cover(fill))
        accent = pick_accent(palette, QColor("#0b0b0b"))
        self.assertIsNotNone(accent)
        self.assertGreaterEqual(_hue(accent), 200.0)
        self.assertLessEqual(_hue(accent), 250.0)
        self.assertIsNotNone(pick_bg_tint(palette))

    def test_muted_cover_keeps_its_hue(self) -> None:
        # Faded/pastel sleeves are everywhere; desaturated-but-real color
        # (saturation ~0.12) must survive the grayscale gates.
        def fill(x, y):
            lightness = 0.30 + 0.30 * (x / 63.0)
            r, g, b = colorsys.hls_to_rgb(0.5, lightness, 0.12)
            return (int(r * 255), int(g * 255), int(b * 255))

        palette = extract_palette(_cover(fill))
        accent = pick_accent(palette, QColor("#0b0b0b"))
        self.assertIsNotNone(accent)
        self.assertGreaterEqual(_hue(accent), 160.0)
        self.assertLessEqual(_hue(accent), 200.0)
        tint = pick_bg_tint(palette)
        self.assertIsNotNone(tint)
        self.assertGreaterEqual(_hue(tint), 160.0)
        self.assertLessEqual(_hue(tint), 200.0)

    def test_small_vivid_glyph_keeps_accent(self) -> None:
        # The vivid path: a saturated glyph at ~2% area on a grey sleeve
        # carried the accent in practice for years (fake bucket chroma used
        # to inflate its family past the area gate; the true-mean rework
        # removed that crutch, so this needs its own gate now).
        def fill(x, y):
            if x < 9 and y < 9:
                return (210, 30, 25)
            v = 120 + (x + y) % 20
            return (v, v, v)

        palette = extract_palette(_cover(fill))
        accent = pick_accent(palette, QColor("#0b0b0b"))
        self.assertIsNotNone(accent)
        self.assertLessEqual(_hue_distance(accent, QColor(210, 30, 25)), 24.0)
        self.assertIsNotNone(pick_bg_tint(palette))

    def test_dark_wash_boundary_is_monotonic(self) -> None:
        # Very dark washes fade to "no accent" as they darken — fine. What
        # must not happen is the boundary flickering: a hard bucket-edge
        # cut once kept the tint at L 0.10 and 0.12 but dropped it at 0.11.
        # Once color appears at some lightness, every lighter wash of the
        # same hue must keep it.
        seen_hue = False
        for lightness in (0.08, 0.09, 0.10, 0.11, 0.12, 0.13, 0.14):
            def fill(x, y, lightness=lightness):
                r, g, b = colorsys.hls_to_rgb(190 / 360.0, lightness, 0.3)
                return (int(r * 255), int(g * 255), int(b * 255))

            accent = pick_accent(
                extract_palette(_cover(fill)), QColor("#0b0b0b"))
            with self.subTest(lightness=lightness):
                if seen_hue:
                    self.assertIsNotNone(accent)
            if accent is not None:
                seen_hue = True
        self.assertTrue(seen_hue)

    def test_near_black_navy_cover_keeps_navy_accent(self) -> None:
        # The dark path: at HSV value ~15 a navy cover's huefulness tops
        # out around 0.028 — under every body gate — but its saturation
        # stays high. It must keep a navy accent instead of reading as
        # toned grey.
        def fill(x, y):
            v = 13 + (x + y) % 5          # HSV value 13..17 of 255
            return (round(v * 0.40), round(v * 0.55), v)

        palette = extract_palette(_cover(fill))
        accent = pick_accent(palette, QColor("#0b0b0b"))
        self.assertIsNotNone(accent)
        self.assertGreaterEqual(_hue(accent), 205.0)
        self.assertLessEqual(_hue(accent), 260.0)
        tint = pick_bg_tint(palette)
        self.assertIsNotNone(tint)
        self.assertGreaterEqual(_hue(tint), 205.0)
        self.assertLessEqual(_hue(tint), 260.0)

    def test_toned_grey_gradient_gains_no_dark_accent(self) -> None:
        # The dark door must stay shut for uniform toning: a +12 blue cast
        # on a grey gradient reads as saturation ~0.10-0.17 — inside the
        # cast/grain band — while the navy above sits at 0.6.
        def fill(x, y):
            v = 60 + x
            return (v, v, min(255, v + 12))

        palette = extract_palette(_cover(fill))
        self.assertIsNone(pick_accent(palette, QColor("#0b0b0b")))
        self.assertIsNone(pick_bg_tint(palette))

    def test_near_black_noise_gains_no_accent(self) -> None:
        # The dark door's chroma floor: per-pixel sensor noise on a
        # near-black cover shows loud saturation pixel by pixel, but the
        # true bucket means stay near neutral — under _DARK_CHROMA_RAMP.
        def fill(x, y):
            base = 10 + (x % 3)
            return (
                base + (x * 7 + y * 13) % 5,
                base + (x * 11 + y * 3) % 5,
                base + (x * 5 + y * 17) % 5,
            )

        palette = extract_palette(_cover(fill))
        self.assertIsNone(pick_accent(palette, QColor("#0b0b0b")))
        self.assertIsNone(pick_bg_tint(palette))


if __name__ == "__main__":
    unittest.main()
