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

# Discord silently drops presence writes past ~5 per 20s. We coalesce all
# pushes through a trailing-edge timer spaced at least this far apart so the
# *latest* desired state (crucially, a pause/clear) always lands even when the
# user mashes play/pause or skips rapidly.
MIN_PUSH_INTERVAL_S = 2.0

# Lyric pushes are *sustained* traffic — a line every few seconds for the
# whole song — unlike the bursty track-change/pause pushes above. Discord's
# ~5/20s drop threshold is a budget, so sustained spacing must stay under
# one write per 4s or writes silently stop landing (worst case swallowing a
# later pause/clear). At 4.5s, fast verses skip lines — the trailing-edge
# flush sends whichever line is current at fire time — which is the right
# behavior for a status display: always the *now* line, never a backlog.
MIN_LYRIC_PUSH_INTERVAL_S = 4.5


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
    # Playback rate (1.0 = normal). Discord fills its progress bar in real
    # wall-clock time, so a slowed/sped track needs start/end scaled by this
    # or the bar drifts out of sync with the audio.
    speed: float = 1.0
    # Current synced-lyric line (LyricTracker). Empty = no line under the
    # playhead; the state field falls back to artist · album.
    lyric: str = ""


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
        # Presence customization (see Settings.discord_* / set_options).
        self._details_template: str = ""
        self._state_template: str = ""
        self._show_paused: bool = False
        self._show_progress: bool = True
        self._activity_type: str = "listening"
        # Last known playback position (seconds). Tracked from
        # ``position_changed`` so ``_on_state_changed(PLAYING)`` can anchor
        # ``started_at`` to actual audio progress on first-play or resume.
        self._last_position: float = 0.0

        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setInterval(RECONNECT_INTERVAL_MS)
        self._reconnect_timer.timeout.connect(self._try_connect)

        # Outbound rate-limit guard. Every push/clear request updates
        # ``_last_activity`` then asks this trailing-edge timer to flush the
        # current desired state; the timer reads the latest activity at fire
        # time, so intermediate states collapse into the final one.
        self._last_push_monotonic: float = 0.0
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.timeout.connect(self._flush)

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

    def set_options(
        self,
        *,
        details_template: str = "",
        state_template: str = "",
        show_paused: bool = False,
        show_progress: bool = True,
        activity_type: str = "listening",
    ) -> None:
        """Presence customization knobs (Settings → discord). Applying them
        re-pushes the current activity so the profile updates without
        waiting for the next track/state event."""
        new = (
            (details_template or "").strip(),
            (state_template or "").strip(),
            bool(show_paused),
            bool(show_progress),
            activity_type if activity_type in ("listening", "playing", "watching")
            else "listening",
        )
        old = (self._details_template, self._state_template, self._show_paused,
               self._show_progress, self._activity_type)
        if new == old:
            return
        (self._details_template, self._state_template, self._show_paused,
         self._show_progress, self._activity_type) = new
        if self._connected and self._last_activity is not None:
            self._push_current()

    def start_wire(self) -> None:
        """Subscribe to track + state changes so presence stays current."""
        self._queue.current_changed.connect(self._on_current_changed)
        self._player.state_changed.connect(self._on_state_changed)
        self._player.duration_changed.connect(self._on_duration_changed)
        self._player.position_changed.connect(self._on_position_changed)
        self._player.speed_changed.connect(self._on_speed_changed)

    def shutdown(self) -> None:
        self._reconnect_timer.stop()
        self._flush_timer.stop()
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
            # Route through the throttle so the clear can't be the write Discord
            # drops — _flush() clears when _last_activity is None.
            self._push_current()
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
            # Carry the current playback rate onto the new track — speed is a
            # global player setting that persists across skips.
            speed=self._current_speed(),
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
            # formula is self-correcting and works for first-play (position≈0),
            # resume-from-pause (position=where the user left off), and
            # reconnect-mid-song. Position is divided by speed because Discord
            # measures elapsed in wall-clock seconds, and it takes
            # ``position/speed`` real seconds to reach that audio position.
            self._last_activity.started_at = self._anchor(self._last_position, self._last_activity.speed)
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

    def _on_speed_changed(self, rate: float) -> None:
        # A speed change reshapes Discord's progress bar (its end timestamp is
        # derived from duration/speed), so re-anchor to the current position at
        # the new rate and re-push. Only meaningful once audio is actually
        # playing — otherwise started_at stays None and the next PLAYING event
        # will anchor with the right speed anyway.
        if self._last_activity is None:
            return
        self._last_activity.speed = max(0.01, float(rate))
        if self._last_activity.started_at is not None and not self._last_activity.paused:
            self._last_activity.started_at = self._anchor(self._last_position, self._last_activity.speed)
            self._push_current()

    def set_lyric(self, text: object) -> None:
        """Live synced-lyric line for the state field; None/"" falls back
        to artist · album. Fed by LyricTracker (which is gated by the
        settings toggle upstream, so a disabled feature never gets here
        with real lines)."""
        lyric = text.strip() if isinstance(text, str) else ""
        if self._last_activity is None:
            return
        if lyric == self._last_activity.lyric:
            return
        self._last_activity.lyric = lyric
        self._push_current(min_interval=MIN_LYRIC_PUSH_INTERVAL_S)

    def _current_speed(self) -> float:
        try:
            return max(0.01, float(self._player.speed))
        except Exception:
            return 1.0

    @staticmethod
    def _anchor(position: float, speed: float) -> float:
        """Virtual unix start time such that Discord's wall-clock elapsed
        (now - start) equals the real time taken to reach ``position`` at
        ``speed``. Pairs with ``end = start + duration/speed`` in the push."""
        speed = speed if speed > 0 else 1.0
        return time.time() - max(0.0, position) / speed

    # ---------- presence push ----------

    def _push_current(self, min_interval: float = MIN_PUSH_INTERVAL_S) -> None:
        """Request that Discord reflect the current ``_last_activity``.

        Coalesced through a trailing-edge timer: if we pushed recently we arm
        the timer to fire later and return, so a burst of state changes (skip,
        duration frame, quick play/pause) collapses into a single send of the
        *final* state. This is what makes pause/skip reliably stop the
        presence — Discord drops writes past ~5/20s, so without the guard a
        rapid clear could be the one that gets swallowed, leaving a stale
        "playing" on the profile.

        ``min_interval`` widens the spacing for lyric-driven pushes (see
        MIN_LYRIC_PUSH_INTERVAL_S). The pending timer always keeps the
        *earliest* requested deadline — an urgent pause/skip push shortens a
        pending lyric wait, never the reverse — and the flush sends the
        latest full state either way.
        """
        if not self._connected:
            return
        elapsed = time.monotonic() - self._last_push_monotonic
        delay_ms = int((min_interval - elapsed) * 1000)
        if delay_ms <= 0:
            self._flush()
            return
        if self._flush_timer.isActive() and self._flush_timer.remainingTime() <= delay_ms:
            return
        self._flush_timer.start(delay_ms)

    def _flush(self) -> None:
        # Cancel any pending trailing fire — whether we were called by the
        # timer or directly, this send covers the latest state.
        self._flush_timer.stop()
        if not self._connected or self._client is None:
            return
        self._last_push_monotonic = time.monotonic()
        a = self._last_activity

        # No track: nothing to show. Paused: hide the presence entirely by
        # default — most users don't want "paused tide" sitting on their
        # profile while they walked away — unless they opted into the
        # "show paused" presence.
        if a is None or (a.paused and not self._show_paused):
            self._clear()
            return

        # Per-source label for large_text / small_text and the {source}
        # template placeholder. Some Discord apps have per-source asset keys
        # uploaded (ytmusic, soundcloud, etc.); if so we use them, otherwise
        # fall back to "tide". Bare slug as asset key — Discord ignores
        # unknown keys silently. Stored in canonical casing; styled_case
        # rewrites them to the active typography.case (so brutalist sees
        # "youtube music", upper-case "YOUTUBE MUSIC", synthwave the l33t
        # variant, etc.).
        source_label_raw = {
            "ytmusic": "YouTube Music",
            "soundcloud": "SoundCloud",
            "bandcamp": "Bandcamp",
            "mixcloud": "Mixcloud",
            "local": "Local Files",
            "spotify": "Spotify",
            "apple": "Apple Music",
        }.get(a.source, "tide")

        # Honor the active theme's typography.case so brutalist users get
        # lowercase presence, synthwave gets l33t, etc.
        from . import theming
        source_label = theming.styled_case(source_label_raw)

        def fill(template: str) -> str:
            """User template → text. A template that errors (unknown
            {placeholder}, stray brace) or collapses to nothing yields ""
            so the caller falls back to the default line."""
            try:
                return template.format(
                    title=a.title, artists=a.artists, album=a.album,
                    source=source_label_raw,
                ).strip()
            except Exception:
                return ""

        details = (
            fill(self._details_template) if self._details_template else ""
        ) or (a.title or "tide").strip()
        details = theming.styled_case(details)

        # State-line precedence: a live lyric takes over while one is under
        # the playhead (the ♪ prefix makes it read as a lyric rather than a
        # weird second title, and keeps one-word lines above Discord's 2-char
        # field minimum); then the user's template; then artist · album.
        if a.paused:
            state_text = theming.styled_case("⏸ paused")
        elif a.lyric:
            state_text = theming.styled_case(f"♪ {a.lyric}")
        elif self._state_template and fill(self._state_template):
            state_text = theming.styled_case(fill(self._state_template))
        else:
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
        # audio starts via _on_state_changed. The user can switch the bar
        # off wholesale; a paused presence never shows one (the timestamps
        # would keep counting wall-clock while the audio stands still).
        duration_secs = max(0, a.duration_seconds)
        if a.started_at is None or a.paused or not self._show_progress:
            start_s = 0
            end_s = 0
        else:
            start_s = int(a.started_at)
            if duration_secs > 0:
                # End is scaled by playback speed: at 0.5x the song really
                # ends 2x later in wall-clock time, and Discord's bar advances
                # in wall-clock, so the end timestamp must stretch to match or
                # the bar races ahead of the audio.
                speed = a.speed if a.speed > 0 else 1.0
                end_s = int(a.started_at + duration_secs / speed)
            else:
                end_s = 0

        activity_type = {
            "playing": getattr(ActivityType, "PLAYING", ActivityType.LISTENING),
            "watching": getattr(ActivityType, "WATCHING", ActivityType.LISTENING),
        }.get(self._activity_type, ActivityType.LISTENING)

        kwargs: dict = {
            "activity_type": activity_type,
            "details": details[:128],
            "state": (state_text or "—")[:128],
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
