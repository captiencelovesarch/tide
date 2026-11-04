"""Lyric cards: a few lines of a song as an image to share (the lyrics
view's right-click, "make a card").

Square (1080 × 1080) or tall (1080 × 1920, for stories and short video),
in the cover's own colours: the cover small at the top with the title and
the artist, the lines big in the middle, a small tide mark at the foot.
Drawn with QPainter at full size; the window shows it scaled down and
copies or saves the full one. Nothing leaves the machine.
"""
from __future__ import annotations

import colorsys
import re
from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QRectF, QStandardPaths, Qt, QTimer
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from .. import contrast, theming
from . import scale as _scale

SHAPES: dict[str, tuple[int, int]] = {"square": (1080, 1080), "tall": (1080, 1920)}
MAX_LINES = 4


def _bg_from(c: QColor, value: float) -> QColor:
    h, s, _v = colorsys.rgb_to_hsv(c.redF(), c.greenF(), c.blueF())
    r, g, b = colorsys.hsv_to_rgb(h, min(s, 0.7), value)
    return QColor.fromRgbF(r, g, b)


def card_colors(cover: QImage | None, theme) -> tuple[QColor, QColor]:
    """(top, bottom) of the card's gradient: the cover's two biggest
    colours, taken down to a deep shade so big light text sits on them.
    No cover: the theme's accent."""
    picks: list[QColor] = []
    if cover is not None and not cover.isNull():
        from .adaptive import extract_palette
        for c, _n in extract_palette(cover):
            if all(abs(c.hueF() - p.hueF()) > 0.06 or abs(c.valueF() - p.valueF()) > 0.25
                   for p in picks):
                picks.append(c)
            if len(picks) == 2:
                break
    if not picks:
        accent = theme.token("accent", "#5a6f8a") if theme else "#5a6f8a"
        picks = [QColor(accent)]
    first = picks[0]
    second = picks[1] if len(picks) > 1 else first
    return _bg_from(first, 0.34), _bg_from(second, 0.16)


def _ink_on(bg: QColor) -> QColor:
    y = contrast.apca_y(bg)
    white = abs(contrast.apca_lc(contrast.apca_y(QColor("#ffffff")), y))
    black = abs(contrast.apca_lc(contrast.apca_y(QColor("#111111")), y))
    return QColor("#f7f7f5") if white >= black else QColor("#141414")


def _family(theme) -> str:
    fam = theme.t("typography", "family", "") if theme else ""
    return str(fam or QApplication.font().family())


def _wrapped_height(fm: QFontMetrics, text: str, width: int) -> int:
    return fm.boundingRect(QRect(0, 0, width, 100000), int(Qt.TextWordWrap), text).height()


def render(lines: list[str], *, title: str, artist: str, cover: QImage | None,
           shape: str = "square", theme=None) -> QImage:
    """The card at full size."""
    W, H = SHAPES.get(shape, SHAPES["square"])
    img = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
    top, bottom = card_colors(cover, theme)
    ink = _ink_on(_mix_mid(top, bottom))
    soft = QColor(ink)
    soft.setAlphaF(0.72)
    family = _family(theme)
    pad = round(W * 0.083)

    p = QPainter(img)
    p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing
                     | QPainter.SmoothPixmapTransform)
    grad = QLinearGradient(QPointF(0, 0), QPointF(W * 0.35, H))
    grad.setColorAt(0.0, top)
    grad.setColorAt(1.0, bottom)
    p.fillRect(img.rect(), grad)

    # the cover, the title, the artist
    thumb = round(W * 0.12)
    x0 = pad
    if cover is not None and not cover.isNull():
        art = cover.scaled(thumb, thumb, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(pad, pad, thumb, thumb), thumb * 0.1, thumb * 0.1)
        p.save()
        p.setClipPath(clip)
        p.drawImage(QRectF(pad, pad, thumb, thumb), art,
                    QRectF((art.width() - thumb) / 2, (art.height() - thumb) / 2, thumb, thumb))
        p.restore()
        x0 = pad + thumb + round(W * 0.026)
    head_w = W - pad - x0
    f_title = QFont(family)
    f_title.setPixelSize(round(W * 0.036))
    f_title.setBold(True)
    f_artist = QFont(family)
    f_artist.setPixelSize(round(W * 0.029))
    fm_t, fm_a = QFontMetrics(f_title), QFontMetrics(f_artist)
    block = fm_t.height() + fm_a.height() + 4
    ty = pad + max(0, (thumb - block) // 2)
    p.setFont(f_title)
    p.setPen(ink)
    p.drawText(QRect(x0, ty, head_w, fm_t.height()), int(Qt.AlignLeft | Qt.AlignVCenter),
               fm_t.elidedText(title, Qt.ElideRight, head_w))
    p.setFont(f_artist)
    p.setPen(soft)
    p.drawText(QRect(x0, ty + fm_t.height() + 4, head_w, fm_a.height()),
               int(Qt.AlignLeft | Qt.AlignVCenter), fm_a.elidedText(artist, Qt.ElideRight, head_w))

    # the mark at the foot
    from .app_icon import tray_image
    mark = round(W * 0.042)
    f_mark = QFont(family)
    f_mark.setPixelSize(round(W * 0.03))
    f_mark.setBold(True)
    fm_m = QFontMetrics(f_mark)
    foot_h = max(mark, fm_m.height())
    fy = H - pad - foot_h
    p.setOpacity(0.8)
    p.drawImage(QRectF(pad, fy + (foot_h - mark) / 2, mark, mark),
                tray_image(mark * 2, ink.name()))
    p.setFont(f_mark)
    p.setPen(ink)
    p.drawText(QRect(pad + mark + round(W * 0.012), fy, W, foot_h),
               int(Qt.AlignLeft | Qt.AlignVCenter), "tide")
    p.setOpacity(1.0)

    # the lines, as big as they fit
    box_top = pad + thumb + round(pad * 0.7)
    box = QRect(pad, box_top, W - 2 * pad, fy - round(pad * 0.5) - box_top)
    lines = [ln for ln in lines if ln.strip()][:MAX_LINES] or [" "]
    size = round(W * 0.078)
    while True:
        f_line = QFont(family)
        f_line.setPixelSize(size)
        f_line.setBold(True)
        fm = QFontMetrics(f_line)
        gap = round(size * 0.28)
        heights = [_wrapped_height(fm, ln, box.width()) for ln in lines]
        total = sum(heights) + gap * (len(heights) - 1)
        if total <= box.height() or size <= round(W * 0.03):
            break
        size = round(size * 0.94)
    p.setFont(f_line)
    p.setPen(ink)
    y = box.top() + max(0, (box.height() - total) // 2)
    for ln, h in zip(lines, heights):
        p.drawText(QRect(box.left(), y, box.width(), h), int(Qt.TextWordWrap | Qt.AlignLeft), ln)
        y += h + gap
    p.end()
    return img


def _mix_mid(a: QColor, b: QColor) -> QColor:
    return QColor((a.red() + b.red()) // 2, (a.green() + b.green()) // 2,
                  (a.blue() + b.blue()) // 2)


def _file_name(title: str) -> str:
    stem = re.sub(r"[^\w\- ]+", "", title or "").strip() or "lyrics"
    return f"{stem} lyrics.png"


class CardDialog(QDialog):
    """Pick the lines (up to four, starting at the one you clicked), the
    shape, then copy the card or save it as a png."""

    PREVIEW = 360

    def __init__(self, lines: list[str], start: int, *, title: str, artist: str,
                 cover: QImage | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .widgets import BracketButton
        self.setAttribute(Qt.WA_DeleteOnClose)
        case = theming.styled_case
        self.setWindowTitle(case("lyric card"))
        self._lines = list(lines)
        self._start = max(0, min(int(start), len(self._lines) - 1))
        self._count = min(2, len(self._lines) - self._start) or 1
        self._title, self._artist = title or "", artist or ""
        self._cover = cover
        self._shape = "square"
        self._image: QImage | None = None

        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview.setMinimumSize(_scale.px(self.PREVIEW), _scale.px(self.PREVIEW))
        self._status = QLabel("")
        self._status.setProperty("class", "dim")

        def btn(label: str, fn) -> BracketButton:
            b = BracketButton(label)
            b.setRole("pill")
            b.clicked.connect(fn)
            return b
        self._earlier = btn("earlier", lambda: self._move(-1))
        self._later = btn("later", lambda: self._move(1))
        self._fewer = btn("fewer lines", lambda: self._resize(-1))
        self._more = btn("more lines", lambda: self._resize(1))
        self._square = btn("square", lambda: self._set_shape("square"))
        self._tall = btn("tall", lambda: self._set_shape("tall"))
        copy_btn = btn("copy", self._copy)
        save_btn = btn("save", lambda: QTimer.singleShot(0, self, self._save))

        lines_row = QHBoxLayout()
        for b in (self._earlier, self._later, self._fewer, self._more):
            lines_row.addWidget(b)
        lines_row.addStretch(1)
        shape_row = QHBoxLayout()
        shape_row.addWidget(self._square)
        shape_row.addWidget(self._tall)
        shape_row.addStretch(1)
        shape_row.addWidget(self._status)
        shape_row.addWidget(copy_btn)
        shape_row.addWidget(save_btn)
        col = QVBoxLayout(self)
        col.setSpacing(_scale.px(10))
        col.addWidget(self._preview, stretch=1)
        col.addLayout(lines_row)
        col.addLayout(shape_row)
        self._refresh()

    # ---------- state ----------

    def chosen(self) -> list[str]:
        return self._lines[self._start:self._start + self._count]

    def set_cover(self, image: QImage | None) -> None:
        self._cover = image
        self._refresh()

    def _move(self, step: int) -> None:
        self._start = max(0, min(self._start + step, len(self._lines) - self._count))
        self._refresh()

    def _resize(self, step: int) -> None:
        room = len(self._lines) - self._start
        self._count = max(1, min(self._count + step, MAX_LINES, room))
        self._refresh()

    def _set_shape(self, shape: str) -> None:
        self._shape = shape
        self._refresh()

    def _refresh(self) -> None:
        self._image = render(self.chosen(), title=self._title, artist=self._artist,
                             cover=self._cover, shape=self._shape,
                             theme=theming.manager().current_effective())
        side = _scale.px(self.PREVIEW)
        pix = QPixmap.fromImage(self._image).scaled(
            side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._preview.setPixmap(pix)
        self._earlier.setEnabled(self._start > 0)
        self._later.setEnabled(self._start + self._count < len(self._lines))
        self._fewer.setEnabled(self._count > 1)
        self._more.setEnabled(self._count < MAX_LINES
                              and self._start + self._count < len(self._lines))
        self._square.setEnabled(self._shape != "square")
        self._tall.setEnabled(self._shape != "tall")
        self._status.setText("")

    # ---------- out ----------

    def _copy(self) -> None:
        if self._image is not None:
            QApplication.clipboard().setImage(self._image)
            self._status.setText(theming.styled_case("copied"))

    def _save(self) -> None:
        if self._image is None:
            return
        folder = QStandardPaths.writableLocation(QStandardPaths.PicturesLocation) or str(Path.home())
        path, _f = QFileDialog.getSaveFileName(
            self, theming.styled_case("save the card"),
            str(Path(folder) / _file_name(self._title)), "png (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        ok = self._image.save(path, "PNG")
        self._status.setText(theming.styled_case("saved" if ok else "couldn't save it"))
