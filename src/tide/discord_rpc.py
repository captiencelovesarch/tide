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
from bisect import bisect_right
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Qt, Signal

try:
    from pypresence import ActivityType, Presence  # type: ignore
    try:
        from pypresence import StatusDisplayType  # type: ignore
    except Exception:                       # pypresence < 4.4
        StatusDisplayType = None  # type: ignore
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

# Discord silently drops presence writes past 5 per 20 s. Every push goes
# through a trailing-edge timer that sends the *latest* desired state when
# the budget allows, so a burst of skips or play/pause collapses into one
# send of the final state and a pause/clear is never the write that drops.
PUSH_BUDGET = 5
PUSH_WINDOW_S = 20.0
# Track changes, pause and resume: at least this far apart, inside the budget.
MIN_PUSH_INTERVAL_S = 2.0

# Lyrics are sustained traffic (a line every few seconds for the whole
# song), so they may use at most this many writes of any 20 s window. The
# last one is held back for pause/skip.
LYRIC_BUDGET = 4
# Lyric writes never closer than this.
MIN_LYRIC_INTERVAL_S = 2.5
# ...and paced like a token bucket: one write's worth comes back every
# LYRIC_REFILL_S, at most LYRIC_BURST saved up. Spending the window's
# budget in a burst left the rest of it dark, so the lines in between all
# had to ride along on one write, ten seconds early. Only writes that put
# new lines up spend from it; every write counts against the hard window.
LYRIC_REFILL_S = 5.0
LYRIC_BURST = 1.0
# A line is pushed this far ahead of its timestamp (song time): Discord
# takes about a second to reach the people looking at the profile.
LYRIC_LEAD_S = 0.8
# A write never carries lines further ahead than this (song seconds). When
# the budget is tight, a line too far out waits for a later write instead
# of showing up absurdly early.
LYRIC_REACH_S = 12.0
# A new track's write waits this long so it usually goes out together with
# the "audio started" one (two writes per song start became one).
TRACK_SETTLE_S = 1.0
# Discord's state field limit.
STATE_MAX = 128
_LYRIC_JOIN = " / "
# Activity fields newer Discord clients understand; dropped if refused.
_NEWER_FIELDS = ("status_display_type", "details_url", "state_url",
                 "large_url", "buttons")


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
    # The lyric text on the profile right now (one line, or the lines that
    # fall inside one push window joined with " / "). Empty = no line under
    # the playhead; the state field falls back to artist · album.
    lyric: str = ""
    # Links (https only; "" = none): the track, its first artist, its album.
    track_url: str = ""
    artist_url: str = ""
    album_url: str = ""


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
        # What the member list says after the verb: "song" (line 1) or
        # "app" (the Discord application's name, the old behaviour).
        self._status_display: str = "song"
        # Title / artist / cover link out, plus a "listen on" button.
        self._links: bool = True
        # Privacy: hide the presence entirely, or just for local files.
        self._hidden: bool = False
        self._hide_local: bool = False
        # Whether Discord currently shows something of ours (so a hidden
        # presence clears once instead of on every event).
        self._showing: bool = False
        # Set when Discord refused the newer activity fields (see _flush).
        self._plain_only: bool = False
        # The synced lyrics of the current track (LyricTracker), and which
        # of its lines are on the profile now as (first, last).
        self._lyric_times: list[float] = []
        self._lyric_lines: list[str] = []
        self._lyric_shown: tuple[int, int] | None = None
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
        # time, so intermediate states collapse into the final one. The
        # deque holds when recent writes went out (monotonic), for the budget.
        self._push_times: deque[float] = deque()
        self._lyric_tokens: float = LYRIC_BURST
        self._lyric_tokens_at: float = 0.0
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
        status_display: str = "song",
        links: bool = True,
        hidden: bool = False,
        hide_local: bool = False,
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
            status_display if status_display in ("song", "app") else "song",
            bool(links),
            bool(hidden),
            bool(hide_local),
        )
        old = (self._details_template, self._state_template, self._show_paused,
               self._show_progress, self._activity_type, self._status_display,
               self._links, self._hidden, self._hide_local)
        if new == old:
            return
        (self._details_template, self._state_template, self._show_paused,
         self._show_progress, self._activity_type, self._status_display,
         self._links, self._hidden, self._hide_local) = new
        if self._connected and (self._last_activity is not None or self._showing):
            self._push_current()

    def set_hidden(self, hidden: bool) -> None:
        """The quick privacy switch (tray). Same as set_options(hidden=)."""
        hidden = bool(hidden)
        if hidden == self._hidden:
            return
        self._hidden = hidden
        if self._connected:
            self._push_current()

    @property
    def hidden(self) -> bool:
        return self._hidden

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
        self._showing = False
        if self._connected:
            self._connected = False
            self.connection_changed.emit(False)

    # ---------- track signal handlers ----------

    def _on_current_changed(self, track) -> None:
        self._lyric_shown = None
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
            **_links_for(track),
        )
        # Push title/artist/album only so Discord shows what's coming up.
        # The progress bar appears once audio actually starts; a fast start
        # lands inside the settle and both go out as one write.
        self._push_current(settle=TRACK_SETTLE_S)

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
        # Discord renders the clock smoothly from the `start` field, so ticks
        # never push by themselves. The position anchors started_at on
        # resume / reconnect, and drives the lyric schedule.
        self._last_position = float(secs)
        self._lyric_tick()

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

    def set_lyric_timeline(self, timeline: object) -> None:
        """The current track's synced lyrics as ``(times, lines)``, or None
        (no timed lyrics, the feature is off, or the track changed). Fed by
        LyricTracker, which is gated by the settings toggle upstream."""
        if isinstance(timeline, tuple) and len(timeline) == 2:
            times, lines = timeline
            self._lyric_times = [float(t) for t in times]
            self._lyric_lines = [str(line) for line in lines]
        else:
            self._lyric_times = []
            self._lyric_lines = []
        self._lyric_shown = None
        a = self._last_activity
        if a is not None and a.lyric and not self._lyric_times:
            self._push_current()          # take the old lyric off the profile
        else:
            self._lyric_tick()

    def _lyric_tick(self) -> None:
        """Ask for a lyric write when the line about to be sung isn't on the
        profile yet, or a gap starts while a lyric still is. "About to" is
        LYRIC_LEAD_S ahead of the playhead, to cover Discord's delay."""
        a = self._last_activity
        if (a is None or a.paused or a.started_at is None or not self._connected
                or not self._lyric_times or self._suppressed(a)):
            return
        i = self._lyric_index(self._last_position + LYRIC_LEAD_S * a.speed)
        if i is None:
            if a.lyric:
                self._push_current(lyric=True)
            return
        shown = self._lyric_shown
        if shown is not None and shown[0] <= i <= shown[1]:
            return
        self._push_current(lyric=True)

    def _lyric_index(self, position: float) -> int | None:
        """The line sung at ``position`` (song time), None in a gap."""
        i = bisect_right(self._lyric_times, position) - 1
        if i < 0 or not self._lyric_lines[i].strip():
            return None
        return i

    def _compose_lyric(self, a: _Activity) -> tuple[str, tuple[int, int] | None]:
        """The lyric text for a write going out now: the line about to be
        sung, plus every following line that starts before the next lyric
        write could. Discord allows a write every few seconds at best, so a
        fast verse used to lose the lines that came and went in between;
        now they ride along ("line one / line two"), a little early rather
        than never. Stops at a gap and at the state field's length."""
        if not self._lyric_times or a.paused or a.started_at is None:
            return "", None
        ahead = self._last_position + LYRIC_LEAD_S * a.speed
        i = self._lyric_index(ahead)
        if i is None:
            return "", None
        now = time.monotonic()
        nxt = self._lyric_slot(after_write=True)
        # A line rides along only if the next write would show it late:
        # starting before that write's own lead point, with half the lead
        # as slack (a line it would show 0.4 s ahead is fine to wait for).
        limit = (ahead + min(LYRIC_REACH_S, max(0.0, nxt - now) * a.speed)
                 - LYRIC_LEAD_S / 2)
        parts = [self._lyric_lines[i].strip()]
        j = i
        room = STATE_MAX - 2                       # "♪ "
        while j + 1 < len(self._lyric_times) and self._lyric_times[j + 1] < limit:
            line = self._lyric_lines[j + 1].strip()
            if not line:
                break
            if len(_LYRIC_JOIN.join(parts + [line])) > room:
                break
            parts.append(line)
            j += 1
        return _LYRIC_JOIN.join(parts), (i, j)

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

    def _push_current(self, *, lyric: bool = False, settle: float = 0.0) -> None:
        """Request that Discord reflect the current ``_last_activity``.

        Coalesced through a trailing-edge timer: if the budget doesn't allow
        a write now, the timer fires when it does and sends the state as it
        is *then*, so a burst of state changes (skip, duration frame, quick
        play/pause) collapses into one send of the final state. That's what
        makes pause/skip reliably stop the presence: Discord drops writes
        past 5 per 20 s, so without the guard a rapid clear could be the one
        that gets swallowed, leaving a stale "playing" on the profile.

        ``lyric`` writes get a smaller share of the budget (LYRIC_BUDGET)
        and a longer spacing, so pause/skip always has a write in reserve.
        The pending timer keeps the *earliest* requested deadline: an urgent
        push shortens a pending lyric wait, never the reverse.
        """
        if not self._connected:
            return
        if lyric:
            target = self._lyric_slot()
        else:
            target = self._slot(PUSH_BUDGET, MIN_PUSH_INTERVAL_S)
        target = max(target, time.monotonic() + settle)
        delay_ms = int((target - time.monotonic()) * 1000)
        if delay_ms <= 0:
            self._flush()
            return
        if self._flush_timer.isActive() and self._flush_timer.remainingTime() <= delay_ms:
            return
        self._flush_timer.start(delay_ms)

    def _slot(self, limit: int, gap: float, also: float | None = None) -> float:
        """Earliest monotonic time a write may go out such that the last
        PUSH_WINDOW_S holds fewer than ``limit`` writes and the previous
        one is ``gap`` behind. ``also`` counts a hypothetical write at that
        time (the one being composed)."""
        now = time.monotonic()
        while self._push_times and self._push_times[0] <= now - PUSH_WINDOW_S:
            self._push_times.popleft()
        recent = list(self._push_times)
        if also is not None:
            recent.append(also)
        t = now
        if recent:
            t = max(t, recent[-1] + gap)
        if len(recent) >= limit:
            t = max(t, recent[-limit] + PUSH_WINDOW_S + 0.05)
        return t

    def _tokens(self, now: float) -> float:
        return min(LYRIC_BURST, self._lyric_tokens
                   + max(0.0, now - self._lyric_tokens_at) / LYRIC_REFILL_S)

    def _spend(self, now: float, *, lyric: bool = False) -> None:
        """Book a write that is going out now."""
        self._push_times.append(now)
        if lyric:
            self._lyric_tokens = max(-1.0, self._tokens(now) - 1.0)
            self._lyric_tokens_at = now

    def _lyric_slot(self, *, after_write: bool = False) -> float:
        """When the next lyric write may go out. ``after_write``: as if one
        were going out right now (to see how far ahead it should reach)."""
        now = time.monotonic()
        tokens = self._tokens(now) - (1.0 if after_write else 0.0)
        paced = now + max(0.0, 1.0 - tokens) * LYRIC_REFILL_S
        window = self._slot(LYRIC_BUDGET, MIN_LYRIC_INTERVAL_S,
                            also=now if after_write else None)
        return max(paced, window)

    def _suppressed(self, a: "_Activity | None") -> bool:
        """Whether nothing should be on the profile right now."""
        if a is None or self._hidden:
            return True
        if self._hide_local and a.source == "local":
            return True
        return a.paused and not self._show_paused

    def _flush(self) -> None:
        # Cancel any pending trailing fire — whether we were called by the
        # timer or directly, this send covers the latest state.
        self._flush_timer.stop()
        if not self._connected or self._client is None:
            return
        a = self._last_activity

        # No track: nothing to show. Paused: hide the presence entirely by
        # default — most users don't want "paused tide" sitting on their
        # profile while they walked away — unless they opted into the
        # "show paused" presence. Hidden (the privacy switch, or a local
        # file with local sharing off): nothing either.
        if self._suppressed(a):
            if a is not None:
                a.lyric = ""
            self._lyric_shown = None
            self._clear()
            return
        before = a.lyric
        a.lyric, self._lyric_shown = self._compose_lyric(a)
        self._spend(time.monotonic(), lyric=bool(a.lyric) and a.lyric != before)

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

        artist_link = ""
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
            # Only this line is about the artist, so only it links there.
            artist_link = a.artist_url

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

        # The member list reads "listening to <song>" instead of "listening
        # to <app name>". Older pypresence without the field keeps the app.
        if self._status_display == "song" and StatusDisplayType is not None:
            kwargs["status_display_type"] = StatusDisplayType.DETAILS

        if self._links and a.track_url:
            kwargs["details_url"] = a.track_url
            kwargs["large_url"] = a.album_url or a.track_url
            if artist_link:
                kwargs["state_url"] = artist_link
            # Discord shows buttons to everyone but you, max 32 characters.
            label = theming.styled_case(f"listen on {source_label_raw}")
            kwargs["buttons"] = [{"label": label[:32], "url": a.track_url}]

        if self._plain_only:
            for key in _NEWER_FIELDS:
                kwargs.pop(key, None)
        try:
            self._send(kwargs)
        except (PipeClosed, BrokenPipeError):
            self._disconnect()
            self._reconnect_timer.start()
        except Exception as exc:
            if not self._plain_only and any(k in kwargs for k in _NEWER_FIELDS):
                # An older Discord (or arRPC) refusing the newer fields
                # must not take the whole presence down: drop them for the
                # rest of the session and send the plain activity.
                self._plain_only = True
                for key in _NEWER_FIELDS:
                    kwargs.pop(key, None)
                try:
                    self._send(kwargs)
                    return
                except Exception as exc2:
                    exc = exc2
            print(f"tide: discord rpc update failed — {exc!r}")

    def _send(self, kwargs: dict) -> None:
        self._client.update(**kwargs)
        self._showing = True

    def _clear(self) -> None:
        if not self._connected or self._client is None or not self._showing:
            return
        self._spend(time.monotonic())
        try:
            self._client.clear()
        except Exception:
            pass
        self._showing = False


def presence_options(s) -> dict:
    """``set_options`` keyword arguments from a Settings object."""
    return dict(
        details_template=s.discord_details_template,
        state_template=s.discord_state_template,
        show_paused=s.discord_show_paused,
        show_progress=s.discord_show_progress,
        activity_type=s.discord_activity_type,
        status_display=s.discord_status_display,
        links=s.discord_links,
        hidden=s.discord_hidden,
        hide_local=s.discord_hide_local,
    )


def _https(url: str) -> str:
    return url if url.startswith("https://") and len(url) <= 512 else ""


def _first_id(value) -> str:
    """The ``id`` of an artists list's first entry, or of an album dict."""
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return ""


def _links_for(track) -> dict[str, str]:
    """Public pages for a track, its first artist and its album, as far as
    its source has them. Local files and self-hosted servers have none."""
    from urllib.parse import quote
    source = getattr(track, "source", "") or ""
    vid = str(getattr(track, "video_id", "") or "")
    extras = getattr(track, "extras", None) or {}
    artist_id = _first_id(extras.get("artists"))
    album_id = _first_id(extras.get("album"))
    out = {"track_url": "", "artist_url": "", "album_url": ""}
    if source == "ytmusic" and vid:
        out["track_url"] = f"https://music.youtube.com/watch?v={quote(vid)}"
        if artist_id:
            out["artist_url"] = f"https://music.youtube.com/channel/{quote(artist_id)}"
        if album_id:
            out["album_url"] = f"https://music.youtube.com/browse/{quote(album_id)}"
    elif source == "spotify" and vid:
        out["track_url"] = f"https://open.spotify.com/track/{quote(vid)}"
        if artist_id:
            out["artist_url"] = f"https://open.spotify.com/artist/{quote(artist_id)}"
        if album_id:
            out["album_url"] = f"https://open.spotify.com/album/{quote(album_id)}"
    elif source in ("soundcloud", "bandcamp", "mixcloud"):
        out["track_url"] = vid             # the permalink is the id
    return {k: _https(v) for k, v in out.items()}
