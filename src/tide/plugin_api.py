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

from dataclasses import asdict, dataclass
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

    def as_dict(self) -> dict:
        return asdict(self)


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
    )
