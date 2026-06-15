"""Queue: ordered list of tracks plus a current index.

This is the playback timeline. The list is total — past, current, and
upcoming all sit in it. `current_index` points at what's playing (or what
last played). `advance()` moves forward; `back()` moves backward.

Radio: when `radio_enabled` is true, once playback enters the last 3 slots
we ask the API to fetch a radio playlist seeded from the most recent track
and append non-duplicate tracks. Refill is one-shot per dip below the
threshold so we don't hammer the API.

The model is exposed as a QAbstractListModel so QListView/QListWidget can
bind directly. Custom data roles are exposed for the title, artist,
duration string, and whether a row is the current one.
"""
from __future__ import annotations

from enum import IntEnum
from typing import Callable, Iterable

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal

from .api import Track


class Role(IntEnum):
    Track = Qt.UserRole + 1
    IsCurrent = Qt.UserRole + 2
    DisplayLine = Qt.UserRole + 3


class Queue(QAbstractListModel):
    current_changed = Signal(object)        # Track or None
    refill_requested = Signal(str, list)    # seed_video_id, exclude_ids — UI runs the network
    radio_state_changed = Signal(bool)

    REFILL_TAIL = 3

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tracks: list[Track] = []
        self._current: int = -1
        self._radio_enabled: bool = False
        self._radio_seed: str | None = None
        self._refill_in_flight: bool = False

    # ---------- QAbstractListModel ----------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._tracks)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._tracks)):
            return None
        tr = self._tracks[index.row()]
        if role == Qt.DisplayRole or role == Role.DisplayLine:
            artist = (tr.artists or "").lower()
            title = (tr.title or "").lower()
            dur = tr.duration or ""
            marker = "* " if index.row() == self._current else "  "
            return f"{marker}{artist} — {title}    {dur}"
        if role == Role.Track:
            return tr
        if role == Role.IsCurrent:
            return index.row() == self._current
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsSelectable | Qt.ItemIsEnabled

    # ---------- introspection ----------

    @property
    def tracks(self) -> list[Track]:
        return list(self._tracks)

    @property
    def current_index(self) -> int:
        return self._current

    @property
    def current(self) -> Track | None:
        if 0 <= self._current < len(self._tracks):
            return self._tracks[self._current]
        return None

    @property
    def upcoming_count(self) -> int:
        return max(0, len(self._tracks) - 1 - self._current)

    @property
    def radio_enabled(self) -> bool:
        return self._radio_enabled

    def video_ids(self) -> set[str]:
        return {t.video_id for t in self._tracks}

    # ---------- mutators ----------

    def _row_changed(self, row: int) -> None:
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [Qt.DisplayRole, int(Role.IsCurrent), int(Role.DisplayLine)])

    def clear(self) -> None:
        if not self._tracks and self._current == -1:
            return
        self.beginResetModel()
        self._tracks.clear()
        self._current = -1
        self.endResetModel()
        self.current_changed.emit(None)

    def _append_one(self, track: Track) -> None:
        row = len(self._tracks)
        self.beginInsertRows(QModelIndex(), row, row)
        self._tracks.append(track)
        self.endInsertRows()

    def add(self, track: Track) -> None:
        self._append_one(track)

    def add_many(self, tracks: Iterable[Track]) -> int:
        new = [t for t in tracks if t and t.video_id not in self.video_ids()]
        if not new:
            return 0
        row = len(self._tracks)
        self.beginInsertRows(QModelIndex(), row, row + len(new) - 1)
        self._tracks.extend(new)
        self.endInsertRows()
        return len(new)

    def add_next(self, track: Track) -> None:
        """Insert immediately after the current track (or at the front)."""
        target = self._current + 1 if self._current >= 0 else 0
        self.beginInsertRows(QModelIndex(), target, target)
        self._tracks.insert(target, track)
        self.endInsertRows()

    def remove(self, row: int) -> None:
        if not (0 <= row < len(self._tracks)):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._tracks[row]
        self.endRemoveRows()
        if row < self._current:
            self._current -= 1
        elif row == self._current:
            # The currently-playing row was removed; current pointer now
            # implicitly refers to the same slot, which is whoever shifted
            # up (or out-of-bounds if removed from the end).
            if self._current >= len(self._tracks):
                self._current = len(self._tracks) - 1
            if self._current >= 0:
                self._row_changed(self._current)
            self.current_changed.emit(self.current)

    def move(self, src: int, dst: int) -> None:
        if not (0 <= src < len(self._tracks)) or not (0 <= dst < len(self._tracks)):
            return
        if src == dst:
            return
        # Track which row holds "current" before/after so its index follows.
        prev_current_track = self.current
        self.beginResetModel()
        t = self._tracks.pop(src)
        self._tracks.insert(dst, t)
        if prev_current_track is not None:
            for i, tr in enumerate(self._tracks):
                if tr.video_id == prev_current_track.video_id:
                    self._current = i
                    break
        self.endResetModel()

    # ---------- playback pointer ----------

    def set_current(self, row: int) -> Track | None:
        if not (0 <= row < len(self._tracks)):
            return None
        old = self._current
        self._current = row
        if old >= 0:
            self._row_changed(old)
        self._row_changed(row)
        self.current_changed.emit(self._tracks[row])
        self._maybe_refill()
        return self._tracks[row]

    def advance(self) -> Track | None:
        nxt = self._current + 1
        if nxt >= len(self._tracks):
            return None
        return self.set_current(nxt)

    def back(self) -> Track | None:
        if self._current <= 0:
            return None
        return self.set_current(self._current - 1)

    def can_advance(self) -> bool:
        return 0 <= self._current < len(self._tracks) - 1

    def can_go_back(self) -> bool:
        return self._current > 0

    # ---------- radio ----------

    def enable_radio(self, seed_video_id: str | None) -> None:
        was = self._radio_enabled
        self._radio_enabled = True
        self._radio_seed = seed_video_id
        if not was:
            self.radio_state_changed.emit(True)
        self._maybe_refill()

    def disable_radio(self) -> None:
        if not self._radio_enabled:
            return
        self._radio_enabled = False
        self._radio_seed = None
        self.radio_state_changed.emit(False)

    def absorb_radio(self, tracks: list[Track]) -> int:
        self._refill_in_flight = False
        return self.add_many(tracks)

    def _maybe_refill(self) -> None:
        if not self._radio_enabled or self._refill_in_flight:
            return
        if self.upcoming_count > self.REFILL_TAIL:
            return
        seed = self._latest_video_id_for_seed()
        if not seed:
            return
        self._refill_in_flight = True
        self.refill_requested.emit(seed, list(self.video_ids()))

    def _latest_video_id_for_seed(self) -> str | None:
        # Use the current track as the seed if available, else the most recent
        # track in the queue, else the originally enabled seed.
        if self.current is not None:
            return self.current.video_id
        if self._tracks:
            return self._tracks[-1].video_id
        return self._radio_seed
