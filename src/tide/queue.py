"""Queue: ordered list of tracks plus a current index.

This is the playback timeline. The list is total — past, current, and
upcoming all sit in it. `current_index` points at what's playing (or what
last played). `advance()` moves forward; `back()` moves backward.

Shuffle / repeat: `advance()` honors the two playback modes. With shuffle
on, the next track is drawn randomly from the rows not yet played this
cycle (played-ness tracked by video_id, so queue edits don't corrupt it);
the pick is pre-committed so `peek_next()` — and everything hanging off it,
like the up-next label and the stream prefetcher — agrees with what
`advance()` will actually do. Repeat "all" wraps (or, shuffled, starts a
fresh cycle) instead of ending; repeat "one" is intentionally NOT handled
here — replaying the same track is an audio decision, so the window's
track-ended handler consults `repeat_mode` itself. A manual [next] always
moves on.

Radio: when `radio_enabled` is true, once playback enters the last 3 slots
we ask the API to fetch a radio playlist seeded from the most recent track
and append non-duplicate tracks. Refill is one-shot per dip below the
threshold so we don't hammer the API.

The model is exposed as a QAbstractListModel so QListView/QListWidget can
bind directly. Custom data roles are exposed for the title, artist,
duration string, and whether a row is the current one.
"""
from __future__ import annotations

import random

from enum import Enum, IntEnum
from typing import Callable, Iterable

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal

from .api import Track


class Role(IntEnum):
    Track = Qt.UserRole + 1
    IsCurrent = Qt.UserRole + 2
    DisplayLine = Qt.UserRole + 3


class RepeatMode(str, Enum):
    """Values are the strings persisted in the session snapshot."""

    OFF = "off"
    ALL = "all"
    ONE = "one"

    @classmethod
    def parse(cls, value) -> "RepeatMode":
        if isinstance(value, cls):
            return value
        v = str(value or "").strip().lower()
        for m in cls:
            if m.value == v:
                return m
        return cls.OFF

    def cycled(self) -> "RepeatMode":
        order = (RepeatMode.OFF, RepeatMode.ALL, RepeatMode.ONE)
        return order[(order.index(self) + 1) % len(order)]


class Queue(QAbstractListModel):
    current_changed = Signal(object)        # Track or None
    refill_requested = Signal(str, list)    # seed_video_id, exclude_ids — UI runs the network
    radio_state_changed = Signal(bool)
    # The currently-playing row was removed. Payload is the track that took
    # its slot and should start playing now (skip-to-next), or None when
    # there's nothing to advance to and playback should stop. The window owns
    # playback, so it — not the model — decides what to do with the audio.
    current_removed = Signal(object)        # Track or None
    # Shuffle flag or repeat mode changed. UI + MPRIS re-read both.
    modes_changed = Signal()

    REFILL_TAIL = 3

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tracks: list[Track] = []
        self._current: int = -1
        self._radio_enabled: bool = False
        self._radio_seed: str | None = None
        self._refill_in_flight: bool = False
        self._shuffle: bool = False
        self._repeat: RepeatMode = RepeatMode.OFF
        # Shuffle-cycle bookkeeping, all by video_id so row edits can't
        # desync it: what already played this cycle, the breadcrumb trail
        # [prev] walks, and the pre-committed next pick peek_next() promises.
        self._played_vids: set[str] = set()
        self._shuffle_trail: list[str] = []
        self._shuffle_next: str | None = None

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
        if role == Role.Track or role == Qt.UserRole:
            return tr
        if role == Role.IsCurrent:
            return index.row() == self._current
        if role == Qt.UserRole + 100:   # TrackRowDelegate.IsCurrentRole
            return index.row() == self._current
        return None

    def flags(self, index: QModelIndex):
        base = Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsDragEnabled
        if not index.isValid():
            return Qt.ItemIsDropEnabled
        return base

    def supportedDropActions(self) -> Qt.DropActions:
        return Qt.MoveAction

    def supportedDragActions(self) -> Qt.DropActions:
        return Qt.MoveAction

    def mimeTypes(self) -> list[str]:
        return ["application/x-tide-queue-row"]

    def mimeData(self, indexes):
        from PySide6.QtCore import QByteArray, QMimeData
        rows = sorted({i.row() for i in indexes if i.isValid()})
        if not rows:
            return None
        md = QMimeData()
        payload = ",".join(str(r) for r in rows).encode("ascii")
        md.setData("application/x-tide-queue-row", QByteArray(payload))
        return md

    def dropMimeData(self, data, action, row: int, column: int, parent: QModelIndex) -> bool:
        if action == Qt.IgnoreAction:
            return True
        if not data.hasFormat("application/x-tide-queue-row"):
            return False
        raw = bytes(data.data("application/x-tide-queue-row")).decode("ascii")
        try:
            src_rows = sorted({int(s) for s in raw.split(",") if s})
        except ValueError:
            return False
        if not src_rows:
            return False
        target = row if row >= 0 else self.rowCount()
        if parent.isValid():
            target = parent.row()
        # Single-row move via existing helper.
        if len(src_rows) == 1:
            src = src_rows[0]
            dst = target - 1 if target > src else target
            dst = max(0, min(self.rowCount() - 1, dst))
            self.move(src, dst)
            return True
        # Multi-row move.
        moved = [self._tracks[r] for r in src_rows]
        prev_current_track = self.current
        self.beginResetModel()
        for r in reversed(src_rows):
            del self._tracks[r]
            if r < target:
                target -= 1
        for offset, tr in enumerate(moved):
            self._tracks.insert(target + offset, tr)
        if prev_current_track is not None:
            for i, t in enumerate(self._tracks):
                if t.video_id == prev_current_track.video_id:
                    self._current = i
                    break
        self.endResetModel()
        return True

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

    def peek_next(self) -> Track | None:
        """Return the track that ``advance()`` would next select, without
        moving the pointer. Used by the prefetch system to pre-resolve the
        upcoming stream URL while the current track is still playing, and by
        the up-next label. Shuffle pre-commits its random pick here so the
        promise holds; repeat-all wraps to row 0 at the end."""
        if not self._tracks:
            return None
        if self._shuffle:
            row = self._commit_shuffle_pick()
            return self._tracks[row] if row >= 0 else None
        nxt = self._current + 1
        if 0 <= nxt < len(self._tracks):
            return self._tracks[nxt]
        if self._repeat == RepeatMode.ALL and self._current >= 0:
            return self._tracks[0]
        return None

    @property
    def radio_enabled(self) -> bool:
        return self._radio_enabled

    def video_ids(self) -> set[str]:
        return {t.video_id for t in self._tracks}

    # ---------- shuffle / repeat ----------

    @property
    def shuffle_enabled(self) -> bool:
        return self._shuffle

    @property
    def repeat_mode(self) -> RepeatMode:
        return self._repeat

    def set_shuffle(self, on: bool) -> None:
        on = bool(on)
        if on == self._shuffle:
            return
        self._shuffle = on
        # A fresh toggle starts a fresh cycle: only the playing track counts
        # as "already played", and the trail starts from it.
        self._played_vids.clear()
        self._shuffle_trail.clear()
        self._shuffle_next = None
        cur = self.current
        if on and cur is not None:
            self._played_vids.add(cur.video_id)
            self._shuffle_trail.append(cur.video_id)
        self.modes_changed.emit()

    def toggle_shuffle(self) -> bool:
        self.set_shuffle(not self._shuffle)
        return self._shuffle

    def set_repeat(self, mode) -> None:
        mode = RepeatMode.parse(mode)
        if mode == self._repeat:
            return
        self._repeat = mode
        self.modes_changed.emit()

    def cycle_repeat(self) -> RepeatMode:
        self.set_repeat(self._repeat.cycled())
        return self._repeat

    def _row_for_vid(self, vid: str) -> int:
        for i, t in enumerate(self._tracks):
            if t.video_id == vid:
                return i
        return -1

    def _commit_shuffle_pick(self) -> int:
        """Row of the pre-committed random pick, choosing one now if needed.
        Returns -1 when the cycle is exhausted (and can't restart)."""
        if self._shuffle_next is not None:
            row = self._row_for_vid(self._shuffle_next)
            if row >= 0:
                return row
            self._shuffle_next = None   # pick was removed from the queue
        candidates = [
            i for i, t in enumerate(self._tracks)
            if i != self._current and t.video_id not in self._played_vids
        ]
        if not candidates:
            if self._repeat != RepeatMode.ALL or len(self._tracks) < 2:
                return -1
            # Repeat-all: everything played once → start a new cycle. The
            # playing track stays "played" so it can't draw itself twice
            # in a row across the cycle seam.
            self._played_vids.clear()
            cur = self.current
            if cur is not None:
                self._played_vids.add(cur.video_id)
            candidates = [i for i in range(len(self._tracks)) if i != self._current]
        row = random.choice(candidates)
        self._shuffle_next = self._tracks[row].video_id
        return row

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
        # New timeline, new shuffle cycle. The modes themselves persist —
        # shuffle/repeat are how the user listens, not queue contents.
        self._played_vids.clear()
        self._shuffle_trail.clear()
        self._shuffle_next = None
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

    def add_prev(self, track: Track) -> None:
        """Insert immediately BEFORE the current track.

        Used by the window's cross-queue [prev] history: a track we're
        stepping back to belongs behind what's playing. Inserting it *after*
        (add_next) put it one row ahead of the track we just left, so the
        next back() walked forward into that track instead of continuing
        backwards through history.
        """
        target = self._current if self._current >= 0 else 0
        self.beginInsertRows(QModelIndex(), target, target)
        self._tracks.insert(target, track)
        if self._current >= target:
            self._current += 1
        self.endInsertRows()

    def remove(self, row: int) -> None:
        if not (0 <= row < len(self._tracks)):
            return
        was_current = row == self._current
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._tracks[row]
        self.endRemoveRows()
        if not was_current:
            # Removing something above the current row shifts current down by
            # one; removing below it leaves the index untouched.
            if row < self._current:
                self._current -= 1
            return
        # The currently-playing row was removed. Emitting current_changed
        # alone used to leave the audio and the pointer disagreeing: the
        # removed track kept playing while the highlight jumped to the next
        # one, which advance() would then skip. Treat it as a skip instead —
        # if a track shifted into the slot, that becomes current and the
        # window plays it; if we removed the last track, stop.
        if row < len(self._tracks):
            # A later track shifted up into this slot — make it current.
            self._current = row
            self._row_changed(self._current)
            self.current_changed.emit(self.current)
            self._maybe_refill()
            self.current_removed.emit(self.current)   # → window plays it
        elif self._tracks:
            # Removed the last (and current) track; earlier tracks remain but
            # there's nothing to advance to. Clamp the pointer and stop.
            self._current = len(self._tracks) - 1
            self._row_changed(self._current)
            self.current_changed.emit(self.current)
            self.current_removed.emit(None)           # → window stops
        else:
            # Queue is now empty.
            self._current = -1
            self.current_changed.emit(None)
            self.current_removed.emit(None)           # → window stops

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
        track = self._tracks[row]
        if self._shuffle:
            # Any track that becomes current — via advance(), a queue-row
            # double-click, whatever — counts toward the shuffle cycle and
            # extends the [prev] breadcrumb trail.
            self._played_vids.add(track.video_id)
            if not self._shuffle_trail or self._shuffle_trail[-1] != track.video_id:
                self._shuffle_trail.append(track.video_id)
            if self._shuffle_next == track.video_id:
                self._shuffle_next = None
        self.current_changed.emit(track)
        self._maybe_refill()
        return track

    def advance(self) -> Track | None:
        """Move to the next track per the active modes. Repeat "one" is
        deliberately ignored here (see module docstring) — advance() is
        also what a manual [next] press calls, and [next] always moves on."""
        if self._shuffle:
            row = self._commit_shuffle_pick()
            if row < 0:
                return None
            return self.set_current(row)
        nxt = self._current + 1
        if nxt >= len(self._tracks):
            if (self._repeat == RepeatMode.ALL
                    and self._tracks and self._current >= 0):
                return self.set_current(0)
            return None
        return self.set_current(nxt)

    def back(self) -> Track | None:
        if self._shuffle:
            # Walk the breadcrumb trail: drop where we are, go where we were.
            while len(self._shuffle_trail) >= 2:
                self._shuffle_trail.pop()
                row = self._row_for_vid(self._shuffle_trail[-1])
                if row >= 0:
                    return self.set_current(row)
            return None
        if self._current <= 0:
            return None
        return self.set_current(self._current - 1)

    def can_advance(self) -> bool:
        if not self._tracks or self._current < 0:
            return False
        if self._shuffle:
            if any(
                i != self._current and t.video_id not in self._played_vids
                for i, t in enumerate(self._tracks)
            ):
                return True
            return self._repeat == RepeatMode.ALL and len(self._tracks) >= 2
        if self._current < len(self._tracks) - 1:
            return True
        return self._repeat == RepeatMode.ALL and len(self._tracks) >= 1

    def can_go_back(self) -> bool:
        if self._shuffle:
            return len(self._shuffle_trail) >= 2
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
        # Always clear the in-flight guard, even if radio was already off.
        # A refill can be requested and then answered by disable_radio()
        # instead of absorb_radio() — the window does exactly this when the
        # active source has no "radio" capability (e.g. double-clicking a
        # Spotify track with auto-radio on). Without clearing here the guard
        # stuck True forever and _maybe_refill() never fired again for the
        # rest of the session, even after switching to a radio-capable source.
        self._refill_in_flight = False
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
