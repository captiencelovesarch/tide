"""Source registry — singleton holding all instantiated `MusicSource`s.

The registry tracks per-source enable state, the active source (drives
Search / Library / Explore by default), and provides the iteration target
for federated search.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from .base import (
    AlbumDetail,
    AlbumEntry,
    ArtistDetail,
    ArtistEntry,
    MusicSource,
    NotSupportedError,
    PlaylistDetail,
    PlaylistEntry,
    Shelf,
    ShelfItem,
    StreamRef,
    Track,
)


class SourceRegistry(QObject):
    """Holds sources, fires when active or enabled state changes."""

    active_changed = Signal(str)            # new active slug
    enabled_changed = Signal(str, bool)     # slug, enabled
    # A source's *saved* session stopped authenticating (e.g. the imported
    # YT Music cookies expired). Emitted via notify_auth_expired(), usually
    # from the worker thread that hit the failure — receivers living in the
    # GUI thread get a queued delivery, so it's safe to raise UI from a slot.
    auth_expired = Signal(str)              # slug
    # a plugin added or took away a source (ctx.sources); the source panel
    # rebuilds its rows
    listing_changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._sources: dict[str, MusicSource] = {}
        self._enabled: dict[str, bool] = {}
        self._active: str = "ytmusic"

    def register(self, source: MusicSource, *, enabled: bool = True) -> None:
        slug = source.slug
        if not slug:
            raise ValueError("MusicSource must have a slug")
        self._sources[slug] = source
        self._enabled.setdefault(slug, enabled)
        self.listing_changed.emit()

    def unregister(self, slug: str) -> None:
        """take ``slug`` away (its plugin stopped). if it was the active
        source, the first enabled one left takes over, ytmusic first."""
        if self._sources.pop(slug, None) is None:
            return
        self._enabled.pop(slug, None)
        if slug == self._active:
            order = ["ytmusic"] + [s for s in self._sources if s != "ytmusic"]
            nxt = next((s for s in order if self._enabled.get(s)), None) \
                or next(iter(self._sources), "ytmusic")
            self._active = nxt
            self.active_changed.emit(nxt)
        self.listing_changed.emit()

    def all(self) -> list[MusicSource]:
        return list(self._sources.values())

    def get(self, slug: str) -> MusicSource | None:
        return self._sources.get(slug)

    def enabled_sources(self) -> list[MusicSource]:
        return [s for slug, s in self._sources.items() if self._enabled.get(slug)]

    def is_enabled(self, slug: str) -> bool:
        return bool(self._enabled.get(slug))

    def set_enabled(self, slug: str, enabled: bool) -> None:
        if slug not in self._sources:
            return
        if self._enabled.get(slug) == enabled:
            return
        self._enabled[slug] = enabled
        self.enabled_changed.emit(slug, enabled)

    @property
    def active(self) -> MusicSource | None:
        return self._sources.get(self._active)

    @property
    def active_slug(self) -> str:
        return self._active

    def set_active(self, slug: str) -> None:
        if slug not in self._sources or slug == self._active:
            return
        self._active = slug
        self._enabled.setdefault(slug, True)
        self._enabled[slug] = True
        self.active_changed.emit(slug)

    def notify_auth_expired(self, slug: str) -> None:
        """Report that ``slug``'s stored credentials no longer authenticate.

        Sources call this from whatever thread the failing request ran on;
        cross-thread delivery is the signal's job, not the caller's.
        """
        if slug in self._sources:
            self.auth_expired.emit(slug)


_registry: SourceRegistry | None = None


def registry() -> SourceRegistry:
    global _registry
    if _registry is None:
        _registry = SourceRegistry()
    return _registry


__all__ = [
    "AlbumDetail",
    "AlbumEntry",
    "ArtistDetail",
    "ArtistEntry",
    "MusicSource",
    "NotSupportedError",
    "PlaylistDetail",
    "PlaylistEntry",
    "Shelf",
    "ShelfItem",
    "SourceRegistry",
    "StreamRef",
    "Track",
    "registry",
]
