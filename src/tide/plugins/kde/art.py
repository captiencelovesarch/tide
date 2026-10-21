"""The wallpaper and the accent, from a cover.

The wallpaper is the cover filling the screen, blurred far enough that
it reads as colour and light rather than a picture, a touch darker so
desktop icons stay readable, with a hair of noise so the gradients don't
band. Blurred at a small size with numpy and scaled up smoothly: cheap,
and a blur has no detail to lose.
"""
from __future__ import annotations

import colorsys

import numpy as np

SMALL = 96                 # the blur's working width
BLUR_PASSES = 4
DIM = 0.78


def _box_blur(a: np.ndarray, r: int) -> np.ndarray:
    """a separable box blur with edge padding, ``r`` pixels each way."""
    k = 2 * r + 1
    for axis in (0, 1):
        pad = [(0, 0)] * a.ndim
        pad[axis] = (r, r)
        p = np.pad(a, pad, mode="edge")
        c = np.cumsum(p, axis=axis, dtype=np.float64)
        c = np.concatenate([np.zeros_like(np.take(c, [0], axis=axis)), c], axis=axis)
        a = (np.take(c, range(k, c.shape[axis]), axis=axis)
             - np.take(c, range(0, c.shape[axis] - k), axis=axis)) / k
    return a


def wallpaper(cover, width: int, height: int):
    """a ``width`` × ``height`` QImage from ``cover`` (a QImage)."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage
    sw = SMALL
    sh = max(1, round(SMALL * height / max(1, width)))
    small = cover.convertToFormat(QImage.Format_RGB888).scaled(
        sw, sh, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    x0, y0 = (small.width() - sw) // 2, (small.height() - sh) // 2
    small = small.copy(x0, y0, sw, sh)
    a = np.frombuffer(small.constBits(), np.uint8).reshape(sh, small.bytesPerLine())[:, :sw * 3]
    a = a.reshape(sh, sw, 3).astype(np.float64)
    for _ in range(BLUR_PASSES):
        a = _box_blur(a, max(1, sw // 16))
    a = a * DIM
    out = np.ascontiguousarray(np.clip(a, 0, 255).astype(np.uint8))
    img = QImage(out.data, sw, sh, sw * 3, QImage.Format_RGB888).copy()
    big = img.scaled(width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    # a hair of noise: 8-bit gradients this smooth band otherwise
    b = np.frombuffer(big.constBits(), np.uint8).reshape(height, big.bytesPerLine())[:, :width * 3]
    rng = np.random.default_rng(3)
    noisy = np.clip(b.astype(np.int16) + rng.integers(-2, 3, b.shape, dtype=np.int16), 0, 255)
    noisy = np.ascontiguousarray(noisy.astype(np.uint8))
    return QImage(noisy.data, width, height, width * 3, QImage.Format_RGB888).copy()


def _luminance(rgb: tuple[float, float, float]) -> float:
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def accent(color: tuple[float, float, float]) -> str:
    """a cover colour (rgb 0..1) made into a usable accent: vivid enough
    to read as a colour, dark enough that white text on it (selections,
    the default button) stays readable at 3:1 or better."""
    h, s, v = colorsys.rgb_to_hsv(*color)
    s = max(s, 0.55)
    v = min(max(v, 0.5), 0.92)
    rgb = colorsys.hsv_to_rgb(h, s, v)
    while (1.05 / (_luminance(rgb) + 0.05)) < 3.0 and v > 0.3:
        v -= 0.03
        rgb = colorsys.hsv_to_rgb(h, s, v)
    return "#" + "".join(f"{round(c * 255):02x}" for c in rgb)
