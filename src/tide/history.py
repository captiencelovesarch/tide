"""Append-only play history.

One JSON object per line at `~/.cache/tide/history.jsonl`. Newest entries
are written at the bottom; the view reads the tail and reverses it.

We don't dedupe: replaying the same song five times in a row produces five
history lines. That's the honest signal — collapsing them would lie about
the listening pattern.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import config
from .api import Track

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


MAX_LINES_RETAINED = 5000   # rotate when the file grows past this
DEFAULT_TAIL = 200


@dataclass
class HistoryEntry:
    video_id: str
    title: str
    artists: str
    album: str = ""
    duration: str = ""
    duration_seconds: int = 0
    thumbnail: str = ""
    played_at: float = 0.0
    source: str = "ytmusic"

    def to_track(self) -> Track:
        return Track(
            video_id=self.video_id,
            title=self.title,
            artists=self.artists,
            album=self.album,
            duration=self.duration,
            duration_seconds=self.duration_seconds,
            thumbnail=self.thumbnail,
            source=self.source or "ytmusic",
        )


def _acquire_lock(path: Path) -> int | None:
    """flock a sidecar `<name>.lock` file (created 0o600) and return its fd.

    We lock a sidecar rather than the history file itself because rotation
    replaces the history inode: a second writer that opened the old inode
    and then won the flock would append into an orphaned file — exactly the
    silent drop this lock exists to prevent. The sidecar is never replaced,
    so every locker contends on the same inode. Returns None if locking is
    impossible (exotic FS); callers proceed unlocked rather than drop the
    play.
    """
    try:
        fd = os.open(
            path.with_name(path.name + ".lock"),
            os.O_CREAT | os.O_WRONLY,
            0o600,
        )
    except OSError:
        return None
    try:
        if sys.platform == "win32":
            # locks the first byte; LK_LOCK retries for ~10 s, then raises
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        os.close(fd)
        return None
    return fd


def append(track: Track) -> None:
    if not track or not track.video_id:
        return
    path = config.HISTORY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "video_id": track.video_id,
        "title": track.title,
        "artists": track.artists,
        "album": track.album,
        "duration": track.duration,
        "duration_seconds": int(track.duration_seconds or 0),
        "thumbnail": track.thumbnail,
        "source": getattr(track, "source", "") or "ytmusic",
        "played_at": time.time(),
    }
    # Append + rotate form one critical section so a rotation can never
    # rewrite the file out from under a concurrent append.
    lock_fd = _acquire_lock(path)
    try:
        try:
            # 0o600 at create — subsonic stream URLs can embed credentials.
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            return
        try:
            os.chmod(path, 0o600)  # tighten files created by older versions
        except OSError:
            pass
        _maybe_rotate(path)
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)  # closing drops the lock
            except OSError:
                pass


def _maybe_rotate(path: Path) -> None:
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= MAX_LINES_RETAINED:
        return
    keep = lines[-MAX_LINES_RETAINED:]
    try:
        # Unique temp (mkstemp => 0o600) + fsync so the replace is atomic
        # and durable — a fixed-name temp could be clobbered by a peer.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
    except OSError:
        return
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(keep)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def read_recent(limit: int = DEFAULT_TAIL) -> list[HistoryEntry]:
    """Return the most recent entries, newest first."""
    path = config.HISTORY_FILE
    if not path.is_file():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out: list[HistoryEntry] = []
    for line in reversed(lines[-limit:]):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        out.append(HistoryEntry(
            video_id=d.get("video_id", ""),
            title=d.get("title", ""),
            artists=d.get("artists", ""),
            album=d.get("album", ""),
            duration=d.get("duration", ""),
            duration_seconds=int(d.get("duration_seconds") or 0),
            thumbnail=d.get("thumbnail", ""),
            source=d.get("source") or "ytmusic",
            played_at=float(d.get("played_at") or 0.0),
        ))
    return out


def clear() -> None:
    try:
        config.HISTORY_FILE.unlink(missing_ok=True)
    except OSError:
        pass
