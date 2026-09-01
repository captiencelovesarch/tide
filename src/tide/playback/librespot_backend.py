"""LibrespotBackend — Spotify Premium playback via the librespot daemon.

The architecture is "librespot is the audio plumbing, Spotify Web API is
the control plane". librespot runs as a subprocess advertising itself as
a Spotify Connect device named "tide"; tide uses ``PUT /v1/me/player/*``
calls to transfer activity to that device and to issue play / pause /
seek / next. Position is interpolated locally between Web API polls so
the progress bar feels smooth without hammering rate limits.

Auth split — relevant because Spotify's Feb 2026 platform-security
update closed librespot's old ``--access-token`` audio-key path. Now:

  - Web API (search, library, control): tide's OAuth-PKCE refresh token,
    managed by ``auth_spotify``.
  - librespot audio session: separate, paired ONCE via Spotify Connect
    zeroconf and cached at ``~/.cache/tide/librespot/credentials.json``.
    On first run, the user opens Spotify on a phone or another desktop,
    taps "tide" in the device-picker, and librespot captures + caches
    long-term credentials. Subsequent runs load cache and skip pairing.

This mirrors the model spotifyd / ncspot use, which is what works
against the current Spotify backend. Premium is still required (audio
keys aren't granted to free accounts) — surfaced as a toast on first
play attempt against a non-Premium account.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from .. import audio_capture, config
from ..player import PlayState
from .base import PlaybackBackend


WEB_API_BASE = "https://api.spotify.com/v1"


class _ApiError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"spotify api {status}: {body[:200]}")
        self.status = status
        self.body = body


def _api(
    method: str,
    path: str,
    token: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    timeout: float = 10.0,
) -> dict | None:
    url = f"{WEB_API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 204 or not resp.length:
                return None
            try:
                return json.loads(resp.read())
            except json.JSONDecodeError:
                return None
    except urllib.error.HTTPError as exc:
        raise _ApiError(exc.code, exc.read().decode("utf-8", errors="replace")) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"spotify network: {exc}") from exc


class LibrespotBackend(PlaybackBackend):
    """Plays Spotify tracks by driving the librespot daemon via Web API."""

    slug = "librespot"

    # librespot renders Spotify's audio itself — set_speed stays the base
    # no-op, and the UI greys the speed control
    supports_speed = False

    # Name we register the librespot device under. Suffix avoids colliding
    # with tide's own org.mpris.MediaPlayer2.tide bus name.
    DEVICE_NAME = "tide"

    # Poll cadence for syncing local position with Spotify's view of the
    # world. Tight enough that external-device interrupts (user pauses
    # from phone) show up within ~2 seconds; loose enough to leave room
    # in the Web API rate limits for search + library work.
    SYNC_INTERVAL_MS = 2000
    # Position-tick cadence for the UI progress bar. Local interpolation
    # so this is a cheap timer with no network cost.
    POSITION_TICK_MS = 250
    # If librespot dies, give it this many ms before declaring playback
    # broken vs. just hiccuping (mostly PipeWire restart cases).
    RESPAWN_GRACE_MS = 1500

    # Surfaces unique to this backend that the SpotifySource / UI wire.
    premium_required = Signal()
    device_registered = Signal(str)            # device_id
    librespot_died = Signal()
    pairing_required = Signal()                # first run, no creds cached
    pairing_complete = Signal()                # creds appeared mid-session

    def __init__(
        self,
        token_provider: Callable[[], str],
        *,
        bitrate: int = 320,
        audio_device: str = "",
        connect_enabled: bool = True,
        librespot_binary: str = "",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._token_provider = token_provider
        self._bitrate = bitrate if bitrate in (96, 160, 320) else 320
        self._audio_device = audio_device
        self._connect_enabled = connect_enabled
        self._librespot_binary = librespot_binary or shutil.which("librespot") or ""
        self._cache_dir: Path = config.CACHE_DIR / "librespot"
        self._creds_file: Path = self._cache_dir / "credentials.json"
        self._pair_announced: bool = False

        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._device_id: str = ""
        self._loaded_uri: str = ""
        self._loaded_track_id: str = ""

        self._state: PlayState = PlayState.IDLE
        self._duration_s: float = 0.0
        self._position_ms: float = 0.0
        self._external_advance_at: float = float("-inf")
        # Reference points for position interpolation between syncs.
        self._anchor_ms: float = 0.0           # last server position
        self._anchor_at: float = 0.0           # monotonic time of anchor
        self._anchor_was_playing: bool = False
        self._volume_percent: int = 80

        # Polling timers — owned by the QObject parent (us) so they tear
        # down cleanly with the backend.
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(self.SYNC_INTERVAL_MS)
        self._sync_timer.timeout.connect(self._sync_state)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(self.POSITION_TICK_MS)
        self._tick_timer.timeout.connect(self._tick_position)

        # Lazily constructed in _start_pairing_watch() because it only
        # matters for the first-run zeroconf flow.
        self._pair_timer: QTimer | None = None

        # Premium check is one-shot per session — we don't want to nag.
        self._premium_warned = False

    # ---------- public surface (PlaybackBackend) ----------

    def load(self, payload: str) -> None:
        if not payload:
            self.error.emit("empty payload")
            return
        if not self._token_provider():
            self.error.emit("spotify: not signed in")
            return
        if not self._librespot_binary:
            self.error.emit("librespot binary not found — pacman -S librespot")
            return
        # Make sure the daemon is alive and registered.
        if not self._ensure_librespot_running():
            return
        # First-run pairing gate. Until librespot's credentials cache is
        # populated by a Spotify Connect pair from another device, audio
        # can't play — surface this clearly instead of silently failing.
        if not self.is_paired():
            self.pairing_required.emit()
            self.error.emit(
                "spotify: open spotify on your phone or laptop, tap "
                "'tide' under devices to pair (first time only)"
            )
            self._start_pairing_watch()
            return
        # Discover the device id of our librespot, if we haven't.
        if not self._device_id and not self._discover_device():
            self.error.emit("spotify: tide device not registered — try again")
            return
        # Transfer activity to our device so the play call lands there,
        # then start the URI. Transfer-then-play is the only combination
        # that works reliably across "no current playback" and
        # "playback on another device".
        token = self._token_provider()
        try:
            _api(
                "PUT", "/me/player", token,
                body={"device_ids": [self._device_id], "play": False},
            )
        except _ApiError as exc:
            if exc.status not in (202, 204):
                self.error.emit(f"spotify transfer failed: {exc.status}")
                return
        try:
            _api(
                "PUT", "/me/player/play", token,
                params={"device_id": self._device_id},
                body={"uris": [payload]},
            )
        except _ApiError as exc:
            if exc.status == 403 and not self._premium_warned:
                self._premium_warned = True
                self.premium_required.emit()
                self.error.emit("spotify premium is required for playback in tide")
                return
            if exc.status == 404:
                # Device disappeared — librespot died between transfer
                # and play. Respawn once and retry.
                self._device_id = ""
                self._stop_librespot()
                if self._ensure_librespot_running() and self._discover_device():
                    try:
                        _api("PUT", "/me/player/play", token,
                             params={"device_id": self._device_id},
                             body={"uris": [payload]})
                    except _ApiError as retry_exc:
                        self.error.emit(f"spotify play failed: {retry_exc.status}")
                        return
                else:
                    self.error.emit("spotify: tide device not ready")
                    return
            else:
                self.error.emit(f"spotify play failed: {exc.status}")
                return

        self._loaded_uri = payload
        self._loaded_track_id = payload.split(":")[-1]
        self._set_state(PlayState.LOADING)
        self._anchor_ms = 0.0
        self._anchor_at = time.monotonic()
        self._anchor_was_playing = True
        self._sync_timer.start()
        self._tick_timer.start()

    def play(self) -> None:
        if not self._device_id:
            return
        try:
            _api("PUT", "/me/player/play", self._token_provider(),
                 params={"device_id": self._device_id})
        except _ApiError as exc:
            if exc.status not in (202, 204):
                self.error.emit(f"spotify play failed: {exc.status}")
                return
        self._anchor_at = time.monotonic()
        self._anchor_was_playing = True
        self._set_state(PlayState.PLAYING)
        self._tick_timer.start()
        self._sync_timer.start()

    def pause(self) -> None:
        if not self._device_id:
            return
        try:
            _api("PUT", "/me/player/pause", self._token_provider(),
                 params={"device_id": self._device_id})
        except _ApiError as exc:
            if exc.status not in (202, 204):
                # 403 here usually means "nothing was playing", which is
                # fine — don't surface as an error.
                return
        # Lock in current interpolated position so a subsequent play()
        # picks up cleanly.
        self._position_ms = self._current_position_ms()
        self._anchor_ms = self._position_ms
        self._anchor_was_playing = False
        self._set_state(PlayState.PAUSED)

    def stop(self) -> None:
        # Spotify's Web API has no "stop" — pause is the closest. We also
        # forget our loaded uri so the next load() does a fresh transfer.
        self.pause()
        self._loaded_uri = ""
        self._loaded_track_id = ""
        self._sync_timer.stop()
        self._tick_timer.stop()
        self._set_state(PlayState.IDLE)

    def seek(self, seconds: float) -> None:
        if not self._device_id:
            return
        position_ms = max(0, int(seconds * 1000))
        try:
            _api("PUT", "/me/player/seek", self._token_provider(),
                 params={"position_ms": position_ms, "device_id": self._device_id})
        except _ApiError as exc:
            if exc.status not in (202, 204):
                self.error.emit(f"spotify seek failed: {exc.status}")
                return
        self._anchor_ms = float(position_ms)
        self._anchor_at = time.monotonic()
        self._position_ms = float(position_ms)
        self.position_changed.emit(position_ms / 1000.0)

    def set_volume(self, percent: int) -> None:
        v = max(0, min(100, int(percent)))
        self._volume_percent = v
        if not self._device_id:
            return
        try:
            _api("PUT", "/me/player/volume", self._token_provider(),
                 params={"volume_percent": v, "device_id": self._device_id})
        except _ApiError:
            # Web API volume isn't available for all device classes;
            # silently ignore so the global tide volume slider still feels
            # responsive for the other backends.
            return

    # set_speed / set_pitch_correction left as no-ops from the base
    # class — librespot doesn't support variable speed.

    def capture_suspect_window(self) -> float:
        # autoplay/remote skips surface at the sync poll, up to
        # SYNC_INTERVAL_MS after the audio actually changed. the ended →
        # stop dispatch that consumes this runs in the same event-loop
        # turn; 1s of slack covers a queued hop without leaving the flag
        # armed long enough to trim an unrelated later stop.
        if time.monotonic() - self._external_advance_at <= 1.0:
            return self.SYNC_INTERVAL_MS / 1000.0
        return 0.0

    def shutdown(self) -> None:
        self._sync_timer.stop()
        self._tick_timer.stop()
        self._stop_librespot()

    @property
    def state(self) -> PlayState:
        return self._state

    @property
    def duration(self) -> float:
        return self._duration_s

    @property
    def device_id(self) -> str:
        return self._device_id

    # ---------- librespot subprocess management ----------

    def _ensure_librespot_running(self) -> bool:
        with self._proc_lock:
            if self._proc is not None and self._proc.poll() is None:
                return True
            return self._spawn_librespot_locked()

    def _spawn_librespot_locked(self) -> bool:
        if not self._librespot_binary:
            self.error.emit("librespot binary missing — install the `librespot` package")
            return False
        # Backend choice: rodio is librespot's default and auto-routes
        # through the system's mixer (pipewire-pulse on Wayland desktops,
        # direct PulseAudio on X11, ALSA otherwise). librespot's Arch
        # 0.8.0-5 build doesn't include the pulseaudio backend, so even
        # though it works "on pulseaudio" through rodio we mustn't pass
        # --backend pulseaudio explicitly.
        #
        # Auth: --credentials-cache lets librespot reuse a previously-
        # paired session. First run, the cache is empty and librespot
        # falls back to zeroconf (Spotify Connect) advertising so the
        # user can pair from another Spotify Connect device (phone /
        # desktop). After pairing, credentials.json appears in the
        # cache dir and subsequent runs are immediate.
        #
        # NOT used: --access-token. Spotify's Feb 2026 platform-security
        # update started refusing audio-key grants to sessions
        # authenticated via OAuth access-token. The Web API endpoints
        # tide actually drives (search, library, /me/player) still
        # accept the OAuth token; librespot just gets its own
        # cache-based session.
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.error.emit(f"librespot cache dir create failed: {exc}")
            return False
        argv = [
            self._librespot_binary,
            "--name", self.DEVICE_NAME,
            "--bitrate", str(self._bitrate),
            "--system-cache", str(self._cache_dir),
            "--initial-volume", str(self._volume_percent),
            "--enable-volume-normalisation",
        ]
        if self._audio_device:
            argv += ["--device", self._audio_device]
        if not self._connect_enabled:
            argv += ["--disable-discovery"]
        try:
            # Leave stderr inheriting tide's so librespot's connection
            # logs (Connect handshake, audio backend init, network
            # hiccups) land in the same log stream tide writes to. The
            # subprocess is otherwise a black box — silent-failure was
            # how the --backend pulseaudio bug took an hour to spot.
            self._proc = subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=None,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, FileNotFoundError) as exc:
            self.error.emit(f"librespot launch failed: {exc}")
            return False

        # Claim librespot's audio streams for the visualizer/pulse feed's
        # auto capture — its sink inputs carry the child's pid, not tide's.
        audio_capture.register_stream_pid(self._proc.pid)

        # Give librespot a beat to register with Spotify before we ask
        # for our device id. 800ms is typically enough on a wired link.
        time.sleep(0.8)
        if self._proc.poll() is not None:
            self.error.emit("librespot exited immediately — check that no other librespot is running")
            audio_capture.unregister_stream_pid(self._proc.pid)
            self._proc = None
            return False
        # If credentials aren't cached yet, surface a one-time toast so
        # the user knows to pair from another Spotify Connect device.
        if not self.is_paired() and not self._pair_announced:
            self._pair_announced = True
            self.pairing_required.emit()
        return True

    def is_paired(self) -> bool:
        """True when librespot's credentials cache exists. Used by the
        UI to decide whether to show the "open spotify on your phone to
        pair" toast and by load() to gate the Web API play call."""
        return self._creds_file.is_file()

    def _stop_librespot(self) -> None:
        with self._proc_lock:
            if self._proc is None:
                return
            audio_capture.unregister_stream_pid(self._proc.pid)
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            except OSError:
                pass
            self._proc = None
            self._device_id = ""

    def _discover_device(self) -> bool:
        try:
            res = _api("GET", "/me/player/devices", self._token_provider()) or {}
        except _ApiError:
            return False
        for dev in res.get("devices", []) or []:
            if (dev.get("name") or "").lower() == self.DEVICE_NAME.lower():
                self._device_id = dev.get("id") or ""
                if self._device_id:
                    self.device_registered.emit(self._device_id)
                    return True
        return False

    # ---------- state sync ----------

    def _sync_state(self) -> None:
        try:
            res = _api("GET", "/me/player", self._token_provider()) or {}
        except _ApiError as exc:
            # 204 = no active device (after long idle). Don't spam errors.
            if exc.status not in (204, 429):
                self.error.emit(f"spotify sync: {exc.status}")
            return
        except RuntimeError:
            return
        if not res:
            return
        item = res.get("item") or {}
        current_id = item.get("id") or ""
        is_playing = bool(res.get("is_playing"))
        progress_ms = float(res.get("progress_ms") or 0)
        duration_ms = float((item.get("duration_ms") or 0)) if item else 0.0

        # Track changed under us (autoplay / external skip). Treat as end
        # of the previous track so the queue can advance.
        if self._loaded_track_id and current_id and current_id != self._loaded_track_id:
            prev_loaded = self._loaded_track_id
            self._loaded_track_id = current_id
            self._loaded_uri = item.get("uri") or f"spotify:track:{current_id}"
            # The new track has been audible for up to a full poll interval
            # while we still reported the old one — anything captured in
            # that window is suspect (see capture_suspect_window).
            self._external_advance_at = time.monotonic()
            # Queue's auto-advance handler picks up from here.
            self.ended.emit()
            # Don't return — also re-anchor with the new track's progress.
            _ = prev_loaded

        if duration_ms and duration_ms / 1000.0 != self._duration_s:
            self._duration_s = duration_ms / 1000.0
            self.duration_changed.emit(self._duration_s)

        # Re-anchor position interpolation against the server's view.
        self._anchor_ms = progress_ms
        self._anchor_at = time.monotonic()
        self._anchor_was_playing = is_playing

        if is_playing:
            if self._state != PlayState.PLAYING:
                self._set_state(PlayState.PLAYING)
        else:
            # If the server thinks we're paused but we just loaded, hold
            # LOADING until it confirms — librespot's pre-roll delay can
            # otherwise flash a PAUSED state right after load().
            if self._state == PlayState.LOADING and progress_ms == 0:
                return
            if self._state != PlayState.PAUSED:
                # Did we reach the end of the loaded track?
                if (self._duration_s > 0
                        and progress_ms >= duration_ms - 1500
                        and current_id == self._loaded_track_id):
                    self._set_state(PlayState.IDLE)
                    self.ended.emit()
                else:
                    self._set_state(PlayState.PAUSED)

        self._position_ms = progress_ms
        self.position_changed.emit(progress_ms / 1000.0)

    def _tick_position(self) -> None:
        if self._state != PlayState.PLAYING:
            return
        pos = self._current_position_ms()
        self._position_ms = pos
        self.position_changed.emit(pos / 1000.0)
        # Hard end-of-track detection in case the sync poll missed it.
        if self._duration_s > 0 and pos >= self._duration_s * 1000 + 250:
            self._set_state(PlayState.IDLE)
            self.ended.emit()
            self._tick_timer.stop()

    def _current_position_ms(self) -> float:
        if not self._anchor_was_playing:
            return self._anchor_ms
        return self._anchor_ms + (time.monotonic() - self._anchor_at) * 1000.0

    def _set_state(self, st: PlayState) -> None:
        if st == self._state:
            return
        self._state = st
        self.state_changed.emit(st)

    # ---------- pairing watch ----------

    def _start_pairing_watch(self) -> None:
        """Poll for the credentials.json appearing. librespot writes it
        right after a successful Connect-device handshake from the user's
        phone / desktop. Once it shows up, emit `pairing_complete` so
        the UI can drop its banner and the user can retry play."""
        if self._pair_timer is None:
            self._pair_timer = QTimer(self)
            self._pair_timer.setInterval(1500)
            self._pair_timer.timeout.connect(self._check_pairing)
        if not self._pair_timer.isActive():
            self._pair_timer.start()

    def _check_pairing(self) -> None:
        if self.is_paired():
            self._pair_timer.stop()
            self.pairing_complete.emit()
            # Now that librespot has creds it'll attach to the spirc
            # session on its own — re-discover so a subsequent load()
            # can hit the right device id.
            self._discover_device()

    # ---------- token refresh hook ----------

    def on_token_refreshed(self, _new_access_token: str) -> None:
        """Pre-zeroconf migration this rebounced the subprocess so the
        new --access-token took effect. With v1.2.1's credentials-cache
        flow librespot doesn't consume the OAuth token at all, so this
        is now a no-op kept for caller-API stability — the Web API
        helpers in this backend call ``token_provider()`` per-request
        and always see the freshly-refreshed token."""
        return None
