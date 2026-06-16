"""Discord Rich Presence integration.

Off by default — to enable, the user provides a Discord Application Client ID
in Settings → Discord (or via the DISCORD_APP_ID env var). They get one in
~30 seconds at https://discord.com/developers/applications: New Application →
copy "Application ID" from General Information.

Why not bundle a default ID? Discord apps are namespaced by their owner. The
app's name + uploaded image assets are what Discord shows. Anyone using a
"tide-default" ID would see whatever assets that account uploaded, which
isn't a great trust story. Owning your own ID is also why the icon you see
in Discord can be your own album-art artwork instead of a stock glyph.

Failures are non-fatal. If Discord isn't running, we wait and try again. If
pypresence raises, we log and move on — the app keeps working.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Qt, Signal

try:
    from pypresence import ActivityType, Presence  # type: ignore
    from pypresence.exceptions import (            # type: ignore
        DiscordError,
        DiscordNotFound,
        InvalidID,
        InvalidPipe,
        PipeClosed,
    )
    PYPRESENCE_AVAILABLE = True
except Exception:
    ActivityType = None  # type: ignore
    PYPRESENCE_AVAILABLE = False

if TYPE_CHECKING:
    from .api import Track
    from .player import Player
    from .queue import Queue


RECONNECT_INTERVAL_MS = 30_000


@dataclass
class _Activity:
    title: str
    artists: str
    album: str
    duration_seconds: int
    # None while the track is still resolving/loading. Set the moment the
    # player transitions to PLAYING (or resumes from pause) to ``time.time()
    # - current_position`` so Discord's elapsed clock matches actual audio.
    started_at: float | None
    paused: bool
    art_url: str = ""
    source: str = ""


class DiscordPresence(QObject):
    """Presence client. Holds a pypresence connection if up; auto-reconnects."""

    connection_changed = Signal(bool)   # True when connected

    def __init__(self, player: "Player", queue: "Queue", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._player = player
        self._queue = queue

        self._client: "Presence | None" = None
        self._app_id: str = ""
        self._enabled: bool = False
        self._connected: bool = False
        self._last_activity: _Activity | None = None
        # Last known playback position (seconds). Tracked from
        # ``position_changed`` so ``_on_state_changed(PLAYING)`` can anchor
        # ``started_at`` to actual audio progress on first-play or resume.
        self._last_position: float = 0.0

        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setInterval(RECONNECT_INTERVAL_MS)
        self._reconnect_timer.timeout.connect(self._try_connect)

    # ---------- lifecycle ----------

    def configure(self, app_id: str | None, enabled: bool) -> None:
        """Set credentials + on/off. Reconnect/disconnect accordingly."""
        app_id = (app_id or "").strip()
        env_override = os.environ.get("DISCORD_APP_ID", "").strip()
        if env_override:
            app_id = env_override

        wants_on = enabled and bool(app_id) and PYPRESENCE_AVAILABLE
        creds_changed = app_id != self._app_id
        was_enabled = self._enabled

        self._app_id = app_id
        self._enabled = wants_on

        if was_enabled and (not wants_on or creds_changed):
            self._disconnect()
        if wants_on:
            self._try_connect()

    def start_wire(self) -> None:
        """Subscribe to track + state changes so presence stays current."""
        self._queue.current_changed.connect(self._on_current_changed)
        self._player.state_changed.connect(self._on_state_changed)
        self._player.duration_changed.connect(self._on_duration_changed)
        self._player.position_changed.connect(self._on_position_changed)

    def shutdown(self) -> None:
        self._reconnect_timer.stop()
        self._disconnect()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ---------- connection ----------

    def _try_connect(self) -> None:
        if not self._enabled or self._connected or not PYPRESENCE_AVAILABLE:
            return
        if not self._app_id:
            return
        try:
            client = Presence(self._app_id)
            client.connect()
        except (DiscordNotFound, InvalidPipe, ConnectionRefusedError, FileNotFoundError):
            # Discord isn't running — try again later.
            self._reconnect_timer.start()
            return
        except (InvalidID, DiscordError) as exc:
            # The app ID is wrong or banned — stop trying.
            print(f"tide: discord rpc disabled — {exc}")
            self._enabled = False
            return
        except Exception as exc:
            print(f"tide: discord rpc connect failed — {exc!r}")
            self._reconnect_timer.start()
            return

        self._client = client
        self._connected = True
        self._reconnect_timer.stop()
        self.connection_changed.emit(True)
        if self._last_activity is not None:
            self._push_current()

    def _disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.clear()
            except Exception:
                pass
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        if self._connected:
            self._connected = False
            self.connection_changed.emit(False)

    # ---------- track signal handlers ----------

    def _on_current_changed(self, track) -> None:
        if track is None:
            self._last_activity = None
            self._last_position = 0.0
            self._clear()
            return
        # Reset cached position — the new track hasn't started, so position 0
        # is the correct anchor if the PLAYING signal beats the first
        # position_changed tick (which it usually does).
        self._last_position = 0.0
        self._last_activity = _Activity(
            title=track.title or "",
            artists=track.artists or "",
            album=track.album or "",
            duration_seconds=int(track.duration_seconds or 0),
            # No timestamps yet — the song is still resolving/buffering.
            # _on_state_changed(PLAYING) will set this when audio starts.
            started_at=None,
            paused=False,
            art_url=track.thumbnail or "",
            source=getattr(track, "source", "") or "",
        )
        # Push title/artist/album only so Discord shows what's coming up.
        # The progress bar appears once audio actually starts.
        self._push_current()

    def _on_state_changed(self, state) -> None:
        if self._last_activity is None:
            return
        from .player import PlayState
        if state == PlayState.PLAYING:
            # Anchor Discord's elapsed clock to actual playback position so
            # the resolve+buffer gap doesn't get counted as "elapsed". The
            # formula ``time.time() - position`` is self-correcting and works
            # for first-play (position≈0), resume-from-pause (position=where
            # the user left off), and reconnect-mid-song.
            self._last_activity.started_at = time.time() - max(0.0, self._last_position)
            was_paused = self._last_activity.paused
            self._last_activity.paused = False
            # Always push on PLAYING — either we're setting the start time
            # for the first time, or we just unpaused. Either way the activity
            # shape changed.
            if was_paused or self._last_activity.started_at is not None:
                self._push_current()
        elif state == PlayState.PAUSED:
            if not self._last_activity.paused:
                self._last_activity.paused = True
                self._push_current()
        # IDLE / STOPPED / LOADING: don't push. Keep the prior activity up
        # until either a new track arrives or audio actually starts.

    def _on_duration_changed(self, secs: float) -> None:
        if self._last_activity is None:
            return
        new_dur = int(secs)
        if new_dur == self._last_activity.duration_seconds:
            return
        self._last_activity.duration_seconds = new_dur
        # Re-push when audio is already running so Discord picks up the
        # progress-bar end timestamp (mpv sometimes reports duration a frame
        # or two after PLAYING fires; without this, Discord shows just
        # elapsed-since-start without the "/ total" until the next event).
        if self._last_activity.started_at is not None and not self._last_activity.paused:
            self._push_current()

    def _on_position_changed(self, secs: float) -> None:
        # Discord rate-limits presence updates to ~5/min, so we don't push on
        # every tick — the client renders smoothly from the `start` field once
        # set. We DO cache the latest position so _on_state_changed(PLAYING)
        # can anchor started_at to it on resume / reconnect.
        self._last_position = float(secs)

    # ---------- presence push ----------

    def _push_current(self) -> None:
        if not self._connected or self._client is None or self._last_activity is None:
            return
        a = self._last_activity

        # When paused, hide the presence entirely — most users don't want
        # "paused tide" sitting on their profile while they walked away.
        if a.paused:
            self._clear()
            return

        # Honor the active theme's typography.case so brutalist users get
        # lowercase presence, synthwave gets l33t, etc.
        from . import theming
        details = theming.styled_case((a.title or "tide").strip())
        state_parts: list[str] = []
        if a.artists:
            state_parts.append(theming.styled_case(a.artists))
        if a.album:
            state_parts.append(theming.styled_case(a.album))
        state_text = " · ".join(state_parts) or "—"

        # When started_at is set (audio actually started), include the unix-
        # second start + end timestamps so Discord renders the "0:34 / 3:42"
        # progress bar. When it's None (track is still resolving/loading), we
        # show the title+artist+album without timestamps — Discord renders
        # just the song info, no clock — and the bar appears the instant
        # audio starts via _on_state_changed.
        duration_secs = max(0, a.duration_seconds)
        if a.started_at is None:
            start_s = 0
            end_s = 0
        else:
            start_s = int(a.started_at)
            if duration_secs > 0:
                # Clamp so Discord doesn't display "playing past the end" if
                # the player position somehow exceeds reported duration.
                end_candidate = int(a.started_at + duration_secs)
                end_s = end_candidate
            else:
                end_s = 0

        # Per-source label for large_text / small_text. Some Discord apps
        # have per-source asset keys uploaded (ytmusic, soundcloud, etc.);
        # if so we use them, otherwise fall back to "tide". Bare slug as
        # asset key — Discord ignores unknown keys silently.
        # Stored in their canonical proper casing; styled_case below
        # rewrites them to match the active theme's typography.case so a
        # brutalist user sees "youtube music", upper-case sees "YOUTUBE
        # MUSIC", synthwave sees the l33t variant, etc.
        source_label_raw = {
            "ytmusic": "YouTube Music",
            "soundcloud": "SoundCloud",
            "bandcamp": "Bandcamp",
            "mixcloud": "Mixcloud",
            "local": "Local Files",
            "spotify": "Spotify",
            "apple": "Apple Music",
        }.get(a.source, "tide")
        source_label = theming.styled_case(source_label_raw)

        kwargs: dict = {
            "activity_type": ActivityType.LISTENING,
            "details": details[:128],
            "state": state_text[:128],
            "large_text": source_label,
            "small_text": theming.styled_case("tide"),
        }
        if start_s > 0:
            kwargs["start"] = start_s
            if end_s > 0:
                kwargs["end"] = end_s
        if a.art_url:
            kwargs["large_image"] = a.art_url
        else:
            # No per-track art (local files; thumbnails not surfaced). Try
            # the source-named asset; Discord will silently fall back if
            # the user's app hasn't uploaded that key.
            kwargs["large_image"] = a.source or "tide"
        # Tag the small-image badge with the source slug so Discord apps
        # that have per-source icons uploaded get them; falls back silently
        # if absent.
        if a.source:
            kwargs["small_image"] = a.source

        try:
            self._client.update(**kwargs)
        except (PipeClosed, BrokenPipeError):
            self._disconnect()
            self._reconnect_timer.start()
        except Exception as exc:
            print(f"tide: discord rpc update failed — {exc!r}")

    def _clear(self) -> None:
        if not self._connected or self._client is None:
            return
        try:
            self._client.clear()
        except Exception:
            pass
