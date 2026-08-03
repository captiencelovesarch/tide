"""Session persistence — resume the queue + position across launches.

Saved on:
  - track changes (queue current_changed)
  - queue mutations (add/remove/clear)
  - playback state transitions
  - throttled by `SAVE_THROTTLE_SECONDS` during position updates

Restored on:
  - app startup after sign-in succeeds, before the main window is shown
  - tracks are pushed back into the queue, current index restored, stream
    URL resolved, and `mpv` paused at the saved position
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config
from .api import Track


SAVE_THROTTLE_SECONDS = 2.0


@dataclass
class Snapshot:
    tracks: list[dict] = field(default_factory=list)
    current_index: int = -1
    position_seconds: float = 0.0
    paused: bool = True
    radio_enabled: bool = False
    radio_seed: str | None = None
    # Playback modes (v1.4+). Stored as plain strings/bools so old tide
    # versions reading a new snapshot just ignore them.
    shuffle: bool = False
    repeat: str = "off"
    saved_at: float = 0.0


def _track_to_dict(t: Track) -> dict:
    return {
        "video_id": t.video_id,
        "title": t.title,
        "artists": t.artists,
        "album": t.album,
        "duration": t.duration,
        "duration_seconds": t.duration_seconds,
        "thumbnail": t.thumbnail,
        "source": getattr(t, "source", "") or "ytmusic",
    }


def _track_from_dict(d: dict) -> Track:
    return Track(
        video_id=d.get("video_id", ""),
        title=d.get("title", ""),
        artists=d.get("artists", ""),
        album=d.get("album", ""),
        duration=d.get("duration", ""),
        duration_seconds=int(d.get("duration_seconds") or 0),
        thumbnail=d.get("thumbnail", ""),
        # v1.1 sessions lack `source`; default to ytmusic so they still play.
        source=d.get("source") or "ytmusic",
    )


def save(snapshot: Snapshot) -> None:
    """Write atomically. Failures swallowed — losing session is not fatal.

    Unique temp file + fsync + rename so a crash mid-save can't truncate
    the previous snapshot, and 0o600 throughout — subsonic thumbnail URLs
    in the snapshot can embed credentials.
    """
    path = config.SESSION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot.saved_at = time.time()
    tmp: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)  # mkstemp default, but be explicit
            json.dump(asdict(snapshot), f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def load() -> Snapshot | None:
    path = config.SESSION_FILE
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return Snapshot(
        tracks=data.get("tracks", []) or [],
        current_index=int(data.get("current_index", -1)),
        position_seconds=float(data.get("position_seconds") or 0.0),
        paused=bool(data.get("paused", True)),
        radio_enabled=bool(data.get("radio_enabled", False)),
        radio_seed=data.get("radio_seed"),
        shuffle=bool(data.get("shuffle", False)),
        repeat=str(data.get("repeat") or "off"),
        saved_at=float(data.get("saved_at") or 0.0),
    )


def clear() -> None:
    try:
        config.SESSION_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def snapshot_from(queue, player_state, position_seconds: float) -> Snapshot:
    """Build a Snapshot from the runtime objects."""
    from .player import PlayState  # local import to avoid circular
    return Snapshot(
        tracks=[_track_to_dict(t) for t in queue.tracks],
        current_index=queue.current_index,
        position_seconds=position_seconds,
        paused=player_state in (PlayState.PAUSED, PlayState.IDLE),
        radio_enabled=queue.radio_enabled,
        radio_seed=None,
        shuffle=queue.shuffle_enabled,
        repeat=queue.repeat_mode.value,
    )


def tracks_from_snapshot(snap: Snapshot) -> list[Track]:
    return [_track_from_dict(d) for d in snap.tracks if d.get("video_id")]
