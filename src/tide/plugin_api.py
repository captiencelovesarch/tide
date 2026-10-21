"""What a tide plugin may import.

This module is the only stable surface: anything else under ``tide.*`` is
internal and can change between releases. A plugin declares the major
version it was written against (``api = 1`` in its plugin.toml); tide
refuses to load a plugin built for a different major.

A plugin is a folder with a ``plugin.toml`` and some python. Its entry
class gets one object, ``ctx``, in ``start(ctx)`` and talks to tide only
through it. Everything registered through ``ctx`` is undone when the
plugin is switched off, even if ``stop()`` forgets something. See
design/plugins.md for the whole picture.

    class MyPlugin:
        def start(self, ctx):
            ctx.playback.track_changed.connect(self.on_track)

        def stop(self):            # optional
            pass

Rules: call ``ctx`` from the GUI thread only, and its signals arrive on
the GUI thread. Work that blocks goes through ``ctx.jobs`` or a thread of
the plugin's own that it stops in ``stop()``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Protocol

API_VERSION = 1

# what a [[setting]] in plugin.toml may be: same meanings as
# ui/settings_schema.KINDS, plus "action" (a button that runs a callback)
SETTING_KINDS: tuple[str, ...] = ("bool", "choice", "int", "str", "action")

# the fixed list a plugin.toml `uses` line picks from. shown in the plugin
# list as what the author says the plugin touches; tide doesn't enforce it
USES: tuple[str, ...] = ("playback", "queue", "lyrics", "audio", "network", "files", "ui", "vr")


@dataclass(frozen=True)
class TrackInfo:
    """A read-only snapshot of a track for plugins."""
    id: str
    title: str
    artists: str
    album: str = ""
    duration: int = 0          # seconds, 0 when unknown
    source: str = ""
    link: str = ""             # a web page for the track, "" when there isn't one
    art_url: str = ""
    # tide's own record, so handing this back to ctx (play it, queue it)
    # plays exactly what was found. opaque to plugins.
    ref: object = field(default=None, compare=False, repr=False)

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "ref"}


@dataclass(frozen=True)
class ItemInfo:
    """an album, playlist or artist from search or the library."""
    kind: str                  # album / playlist / artist
    id: str
    title: str
    subtitle: str = ""         # artist and year, a description, follower count...
    art_url: str = ""
    ref: object = field(default=None, compare=False, repr=False)


class Plugin(Protocol):
    def start(self, ctx) -> None: ...


def track_link(video_id: str, source: str) -> str:
    """The public page for a track, or "" (local files have none)."""
    if not video_id:
        return ""
    if video_id.startswith(("https://", "http://")):
        return video_id
    if source == "ytmusic":
        return f"https://music.youtube.com/watch?v={video_id}"
    return ""


def track_info(track) -> TrackInfo | None:
    """A ``sources.base.Track`` as a ``TrackInfo``."""
    if track is None:
        return None
    source = str(getattr(track, "source", "") or "")
    vid = str(getattr(track, "video_id", "") or "")
    return TrackInfo(
        id=vid,
        title=str(getattr(track, "title", "") or ""),
        artists=str(getattr(track, "artists", "") or ""),
        album=str(getattr(track, "album", "") or ""),
        duration=int(getattr(track, "duration_seconds", 0) or 0),
        source=source,
        link=track_link(vid, source),
        art_url=str(getattr(track, "thumbnail", "") or ""),
        ref=track,
    )


def item_info(kind: str, entry) -> ItemInfo | None:
    """an AlbumEntry / PlaylistEntry / ArtistEntry (or detail) as an ItemInfo."""
    if entry is None:
        return None
    g = lambda name: str(getattr(entry, name, "") or "")  # noqa: E731
    if kind == "album":
        sub = " · ".join(x for x in (g("artists"), g("year")) if x)
        return ItemInfo("album", g("browse_id"), g("title"), sub, g("thumbnail"), entry)
    if kind == "playlist":
        return ItemInfo("playlist", g("playlist_id"), g("title"), g("description"),
                        g("thumbnail"), entry)
    if kind == "artist":
        return ItemInfo("artist", g("channel_id"), g("name"), g("subscribers"), g("thumbnail"),
                        entry)
    return None


# ---------------------------------------------------------------------------
# the beat, shaped for lights and glows
#
# ctx.beats.pulse gives tide's bass envelope, 0..1, about 60 times a second.
# these turn it into what tide's own backdrop shows, with a hard cap on
# flashing. steamvr, lights and the stream overlay all use them, so a beat
# looks the same everywhere.
# ---------------------------------------------------------------------------

import math as _math

PULSE_MODES: tuple[str, ...] = ("off", "kick", "breathing")
STRENGTH: dict[str, float] = {"subtle": 0.45, "normal": 0.7, "loud": 1.0}


def glow_level(mode: str, pulse: float, playing: bool) -> float:
    """how bright a glow is for a pulse (0..1), before strength. kick:
    barely there between hits. breathing: always softly there."""
    if mode == "off" or not playing:
        return 0.0
    pulse = max(0.0, min(1.0, pulse))
    if mode == "kick":
        return 0.12 + 0.35 * pulse
    return 0.3 + 0.7 * pulse


class FlashLimiter:
    """no more than ``per_second`` flashes a second, whatever the music
    does. a flash is the level jumping by ``jump`` or more. past the limit
    a hit still shows, as a step just under a flash, so no beat goes
    missing. photosensitive seizure guidance draws the line at three a
    second. use it on anything that lights up a room or fills your view."""

    SOFT = 0.8                 # a hit past the limit: this share of a flash

    def __init__(self, per_second: int = 3, jump: float = 0.3,
                 slow_rise_per_s: float = 1.5) -> None:
        self.per_second, self.jump, self.slow = per_second, jump, slow_rise_per_s
        self.level = 0.0
        self._flashes: list[float] = []
        self._last: float | None = None

    def update(self, now: float, target: float) -> float:
        dt = 0.0 if self._last is None else max(0.0, now - self._last)
        self._last = now
        self._flashes = [t for t in self._flashes if now - t < 1.0]
        target = max(0.0, min(1.0, float(target)))
        if target - self.level >= self.jump:
            if len(self._flashes) < self.per_second:
                self._flashes.append(now)
                self.level = target
            else:
                self.level = min(target, self.level + max(self.jump * self.SOFT, self.slow * dt))
        else:
            self.level = target          # small rises and every fall pass straight
        return self.level


class Envelope:
    """the pulse at your own frame rate, shaped like tide's backdrop:
    straight up on a hit, a 30 ms glide down. feed it every frame (the
    peak since the last one), not just when a pulse arrives."""

    RELEASE_S = 0.030

    def __init__(self, release_s: float | None = None) -> None:
        self.release_s = self.RELEASE_S if release_s is None else release_s
        self.level = 0.0
        self._last: float | None = None

    def update(self, now: float, target: float) -> float:
        dt = 0.0 if self._last is None else min(0.1, max(0.0, now - self._last))
        self._last = now
        target = float(target)
        if target >= self.level:
            self.level = target
        else:
            self.level += (target - self.level) * (1.0 - _math.exp(-dt / self.release_s))
        return self.level


class Kicks:
    """which pulses are kicks. a kick is the pulse rising ``RISE`` above
    its recent low (a trough that follows the pulse straight down and only
    slowly up, so a rise spread over a few frames still counts); one kick
    per attack however long it takes to peak. ``per_second`` of them are
    full, the rest soft (for flashes)."""

    RISE = 0.12
    MIN = 0.22                # quieter than this isn't a kick
    GAP_S = 0.07
    LIFT_S = 0.12             # how slowly the trough follows the pulse up
    per_second = 3

    def __init__(self) -> None:
        self.last = -1e9
        self._trough = 0.0
        self._rising = False
        self._last_t: float | None = None
        self._starts: list[float] = []

    def update(self, now: float, pulse: float) -> str | None:
        """"full", "soft" or None for this frame's pulse."""
        dt = 0.0 if self._last_t is None else min(0.1, max(0.0, now - self._last_t))
        self._last_t = now
        pulse = max(0.0, min(1.0, float(pulse)))
        self._starts = [t for t in self._starts if now - t < 1.0]
        hit = None
        if self._rising and pulse >= self._trough:
            self._trough = pulse
        elif now - self.last < self.GAP_S:
            self._rising = False
            self._trough = max(self._trough, pulse)
        elif pulse - self._trough >= self.RISE and pulse >= self.MIN:
            hit = "full" if len(self._starts) < self.per_second else "soft"
            if hit == "full":
                self._starts.append(now)
            self.last = now
            self._trough = pulse
            self._rising = True
        elif pulse <= self._trough:
            self._rising = False
            self._trough = pulse
        else:
            self._trough += (pulse - self._trough) * (1.0 - _math.exp(-dt / self.LIFT_S))
        return hit


def cover_palette(img) -> list[tuple[tuple[float, float, float], float]]:
    """a cover's colours by how much of it they cover: up to three
    (hue, saturation, value) 0..1 with their share of the colourful area,
    biggest first. greys, blacks and whites don't count; a colour has to
    cover a real part of the cover, so a small detail (a blue painting on
    a red cover) doesn't take over (2026-10-21).

    a dark, moody cover (a night scene in navy) has nothing vivid enough
    to count; then its quieter tint is used, so it still reads as night
    blue (it used to give nothing, and the desktop kept the last song's
    colour). [] only for a cover with no colour at all."""
    return (_palette(img, min_s=0.18, min_v=0.22, by_vividness=True)
            or _palette(img, min_s=0.07, min_v=0.08, by_vividness=False))


def _palette(img, min_s: float, min_v: float, by_vividness: bool):
    import colorsys
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage
    if img is None or img.isNull():
        return []
    small = img.convertToFormat(QImage.Format_RGB32).scaled(
        32, 32, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    bins = 24
    weight = [0.0] * bins
    sums = [[0.0, 0.0, 0.0, 0.0] for _ in range(bins)]     # sin, cos, s, v (weighted)
    for y in range(small.height()):
        for x in range(small.width()):
            px = small.pixel(x, y)
            r, g, b = ((px >> 16) & 255) / 255, ((px >> 8) & 255) / 255, (px & 255) / 255
            h, sat, v = colorsys.rgb_to_hsv(r, g, b)
            if sat < min_s or v < min_v:
                continue
            w = sat * v if by_vividness else sat
            k = int(h * bins) % bins
            weight[k] += w
            sums[k][0] += w * _math.sin(2 * _math.pi * h)
            sums[k][1] += w * _math.cos(2 * _math.pi * h)
            sums[k][2] += w * sat
            sums[k][3] += w * v
    total = sum(weight)
    if total < (0.5 if by_vividness else 8.0):    # hardly any of the cover is colour
        return []
    # each bin with its neighbours, so a hue on a bin edge isn't split in two
    smooth = [weight[(k - 1) % bins] * 0.5 + weight[k] + weight[(k + 1) % bins] * 0.5
              for k in range(bins)]
    picked: list[int] = []
    for k in sorted(range(bins), key=lambda k: -smooth[k]):
        if smooth[k] <= 0 or any(min(abs(k - j), bins - abs(k - j)) < 3 for j in picked):
            continue
        picked.append(k)
        if len(picked) == 3:
            break
    out = []
    for k in picked:
        ks = [(k - 1) % bins, k, (k + 1) % bins]
        w = sum(weight[j] for j in ks)
        if w <= 0:
            continue
        hue = (_math.atan2(sum(sums[j][0] for j in ks), sum(sums[j][1] for j in ks))
               / (2 * _math.pi)) % 1.0
        out.append(((hue, sum(sums[j][2] for j in ks) / w, sum(sums[j][3] for j in ks) / w),
                    w / total))
    return out


SECOND_SHARE = 0.18        # a second colour has to cover this much of the colourful area


WHITE = (1.0, 1.0, 1.0)


def cover_colors(img) -> tuple | None:
    """two colours from a cover (a QImage) as rgb 0..1 tuples: the colour
    most of it is, and a second one if it covers a real part of it too
    (else the first twice), lifted for light. a black-and-white cover is
    white light (2026-10-21: it used to fall back to the theme's accent,
    "it can clearly just be white"). None only when there's no image."""
    import colorsys
    if img is None or img.isNull():
        return None
    palette = cover_palette(img)
    if not palette:
        return WHITE, WHITE
    first = palette[0][0]
    second = next((hsv for hsv, share in palette[1:] if share >= SECOND_SHARE), first)

    def lift(hsv: tuple) -> tuple:
        h, s, v = hsv
        return colorsys.hsv_to_rgb(h, min(1.0, s * 1.1), max(0.85, v))
    return lift(first), lift(second)
