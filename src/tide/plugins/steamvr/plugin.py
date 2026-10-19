"""tide in vr: a watch on your wrist opens a now-playing panel in front of
you, look away and it goes; and the ring, tide's backdrop pulse on the
edge of your view. design/steamvr.md is the plan.

The gui half lives here: it keeps the qt panel up to date, renders it
when something changed and posts the frames to the vr thread (vr.py),
which does everything that talks to steamvr.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QOffscreenSurface, QSurfaceFormat

from . import ring as ringmod
from .panel import PANEL_PX, Forwarder, VrPanel, back_face, watch_face
from .vr import VrBridge, VrThread

FRAME_MS = 33          # at most ~30 redraws a second while open
TICK_MS = 500          # times and the watch ring move this often


class SteamVr:
    def start(self, ctx) -> None:
        self.ctx = ctx
        self.panel_open = False
        self.watch_up = False
        self._last_ring = "breathing"
        self._eyes = None                 # steamvr's lens outlines, once connected
        self._ring_colors = None          # from the current cover
        self._ring_art = ""
        self._ring_drawn = False
        self._glow_drawn = False
        # every qt object here is handed to ctx.adopt: tide deletes them
        # right after stop(), while qt is still whole
        self.panel = ctx.adopt(VrPanel(ctx))
        self.panel.show()          # WA_DontShowOnScreen keeps it off the desktop
        self.fwd = Forwarder(self.panel, on_change=self._dirty)

        self.bridge = ctx.adopt(VrBridge())
        self.bridge.status.connect(self._status)
        self.bridge.panel_mouse.connect(self._mouse)
        self.bridge.panel_opened.connect(self._opened)
        self.bridge.panel_closed.connect(self._closed)
        self.bridge.watch_visible.connect(self._watch_visible)
        self.bridge.lens.connect(self._lens)
        self.bridge.panel_scroll.connect(self.fwd.scroll)
        self.bridge.keyboard_text.connect(self.panel.search.typing)
        self.bridge.keyboard_done.connect(self.panel.search.submit)
        self.bridge.keyboard_closed.connect(lambda: self.panel.search.typing(self.panel.search.query))

        # qt wants offscreen surfaces made on the gui thread; the vr thread
        # makes its opengl context over this one
        self.surface = ctx.adopt(QOffscreenSurface())
        self.surface.setFormat(QSurfaceFormat.defaultFormat())
        self.surface.create()

        self._redraw = ctx.adopt(QTimer())
        self._redraw.setSingleShot(True)
        self._redraw.setInterval(FRAME_MS)
        self._redraw.timeout.connect(self._send_panel)
        self._downloading = False
        self._download_progress = (0, 1)
        self._download_tick = ctx.adopt(QTimer())
        self._download_tick.setInterval(300)
        self._download_tick.timeout.connect(self._show_download)
        self._listen_tick = ctx.adopt(QTimer())
        self._listen_tick.setInterval(250)
        self._listen_tick.timeout.connect(self._listening)
        self._tick = ctx.adopt(QTimer())
        self._tick.setInterval(TICK_MS)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

        pb, q = ctx.playback, ctx.queue
        pb.track_changed.connect(self._track_changed)
        pb.state_changed.connect(self._state)
        pb.liked_changed.connect(lambda _l: self._refresh())
        # the speed label and slider only caught up on the next reopen
        # (2026-10-19)
        pb.speed_changed.connect(lambda _r: self._refresh())
        pb.position_changed.connect(self._position)
        q.modes_changed.connect(self._refresh)
        q.changed.connect(self._refresh)
        ctx.lyrics.changed.connect(lambda _s: self._refresh())
        ctx.beats.pulse.connect(self._pulse)
        ctx.theme.changed.connect(self._restyle)
        ctx.settings_changed.connect(lambda _keys: self._apply_settings())
        self.panel.changed.connect(self._dirty)
        self.panel.close_requested.connect(lambda: self.thread.post("close", "x"))
        self.panel.setting_changed.connect(self._set)
        self.panel.now.ring.clicked.connect(self._toggle_ring)
        self.panel.search.wants_keyboard.connect(lambda text: self.thread.post("keyboard", text))
        self.panel.search.mic_down.connect(self._mic_down)
        self.panel.search.mic_up.connect(self._mic_up)

        self.thread = VrThread(self.bridge, self.surface, self._vr_settings())
        self.thread.start()
        self._apply_settings()
        self._send_back()
        self.thread.post("playing", pb.state() == "playing")
        ctx.set_status("waiting for steamvr")

    def stop(self) -> None:
        self._tick.stop()
        self._redraw.stop()
        self.thread.stop()
        # the vr thread checks for stop every tick, but a dying compositor
        # can hold one call for about a second
        if not self.thread.wait(5000):
            self.ctx.log.warning("vr thread didn't stop in time")
            _STRAGGLERS.append(self.thread)   # destroying a running QThread aborts
        else:
            self.thread.dispose()
            self.ctx.adopt(self.thread)

    # ---- settings ----

    def _springy(self) -> bool:
        """modern's motion: the theme is modern and motion isn't off."""
        m = self.ctx.motion
        return (not m.off() and self.ctx.theme.aesthetic() == "modern"
                and m.profile() == "springy")

    def _durations(self) -> dict:
        m = self.ctx.motion
        return {k: m.dur(k) / 1000.0 for k in ("micro", "short", "med")}

    def _vr_settings(self) -> dict:
        s = self.ctx.settings
        return {
            "springy": self._springy(),
            "log_path": str(self.ctx.paths.data_dir / "sessions.log"),
            "watch_hand": s["watch_hand"],
            "open_by_looking": s["open_by_looking"],
            "size": s["size"],
            "linger": float(s["linger"]),
            "haptics": s["haptics"],
            "ring": s["ring"],
            "ring_strength": s["ring_strength"],
            "motion_off": self.ctx.motion.off(),
            "cursor": self.ctx.theme.token("accent", "#ffd400"),
            "touch": s["touch"],
            "panel_glow": s["panel_glow"],
        }

    def _apply_settings(self) -> None:
        s = self.ctx.settings
        self.thread.post("settings", self._vr_settings())
        if s["ring"] != "off":
            self._last_ring = s["ring"]
            if not self._ring_drawn:
                self._ring_drawn = True
                self._draw_ring()
        if s["panel_glow"] != "off" and not self._glow_drawn:
            self._glow_drawn = True
            self._draw_glow()
        if s["ring"] != "off" or s["panel_glow"] != "off":
            self.ctx.beats.subscribe()
        else:
            self.ctx.beats.unsubscribe()
        if s["lyrics"]:
            self.ctx.lyrics.subscribe()
        else:
            self.ctx.lyrics.unsubscribe()
        self.panel.ring_on = s["ring"] != "off"
        self.panel.lyrics_on = s["lyrics"]
        self.panel.set_animate(self._springy(), self._durations())
        self.panel.refresh()

    def _set(self, key: str, value) -> None:
        """a change made on the panel's own settings page."""
        if self.ctx.settings.desc(key).kind == "action":
            self.on_action(key)
            return
        try:
            self.ctx.settings.set(key, value)
        except (KeyError, ValueError):
            return
        self._apply_settings()

    # ---- the voice model ----

    def on_action(self, key: str) -> None:
        """an action setting's button (desktop plugins tab or the vr
        settings page)."""
        if key != "voice_model" or self._downloading:
            return
        ok, _why = self.ctx.speech.available()
        if ok:
            self.ctx.set_status("the voice model is already here")
            return
        self._downloading = True
        self._download_progress = (0, 1)

        def progress(done: int, total: int) -> None:
            self._download_progress = (done, total)    # read by the timer below

        def finished(_result) -> None:
            self._downloading = False
            self._download_tick.stop()
            self.ctx.set_status("voice model ready")
            self.panel.search.say("voice is ready: hold the mic and talk")

        def failed(exc) -> None:
            self._downloading = False
            self._download_tick.stop()
            self.ctx.set_status(f"voice model didn't download: {exc}"[:120])

        self._download_tick.start()
        self.ctx.speech.download(progress, finished, failed)

    def _show_download(self) -> None:
        done, total = self._download_progress
        pct = int(100 * done / total) if total else 0
        self.ctx.set_status(f"getting the voice model · {pct}%")
        self.panel.search.say(f"getting the voice model · {pct}%")

    # ---- voice search: hold the mic, talk, let go ----

    def _mic_down(self) -> None:
        ok, why = self.ctx.speech.available()
        if not ok:
            self.panel.search.say(why + ": settings → get the voice model "
                                  f"({self.ctx.speech.model_size_mb()} mb)")
            return
        self.ctx.playback.duck(0.3)
        self.ctx.speech.listen()
        self.panel.search.say("listening")
        self._listen_tick.start()

    def _mic_up(self) -> None:
        self._listen_tick.stop()
        if not self.ctx.speech.listening():
            return
        self.ctx.playback.unduck()
        self.panel.search.say("…")

        def heard(text: str) -> None:
            text = _strip_command(text)
            if text:
                self.panel.search.submit(text)
            else:
                self.panel.search.say("didn't catch that. hold the mic while you talk")

        self.ctx.speech.finish(heard, lambda exc: self.panel.search.say(f"voice failed: {exc}"[:110]))

    def _listening(self) -> None:
        self.panel.search.say(f"listening · {self.ctx.speech.seconds():.1f} s")

    def _toggle_ring(self) -> None:
        self._set("ring", "off" if self.ctx.settings["ring"] != "off" else self._last_ring)

    # ---- from the vr thread ----

    def _status(self, text: str) -> None:
        self.ctx.set_status(text)

    def _mouse(self, kind: str, x: float, y: float) -> None:
        self.fwd.handle(kind, x, y)
        self._dirty()

    def _opened(self, how: str) -> None:
        self.panel_open = True
        self.panel.show_tab("now")
        self.panel.refresh()
        self._send_panel()

    def _closed(self, why: str) -> None:
        self.panel_open = False

    def _watch_visible(self, on: bool) -> None:
        self.watch_up = on
        if on:
            self._send_watch()

    def _lens(self, eyes: dict) -> None:
        """steamvr told us each eye's outline: the ring can be drawn."""
        self._eyes = eyes
        self._draw_ring()

    def _draw_ring(self) -> None:
        """draw the ring off the gui thread (half a second of numpy) and
        hand it over. per song: the colours come from the cover."""
        if self._eyes is None or self.ctx.settings["ring"] == "off":
            return
        eyes = self._eyes
        a, b = self._ring_colors or (self._accent(), self._accent(0.75))
        style = self.ctx.theme.aesthetic()
        self.ctx.jobs.run(lambda: ringmod.draw(eyes, (a, b), style),
                          on_done=lambda result: self.thread.post("ring_frames", *result))

    def _draw_glow(self) -> None:
        """the panel's glow, per song like the ring."""
        if self.ctx.settings["panel_glow"] == "off":
            return
        a, b = self._ring_colors or (self._accent(), self._accent(0.75))
        style = self.ctx.theme.aesthetic()
        radius = 26 if style == "modern" else 0
        self.ctx.jobs.run(lambda: ringmod.panel_glow(PANEL_PX, radius, (a, b), style),
                          on_done=lambda result: self.thread.post("glow_frame", *result))

    def _send_back(self) -> None:
        self.thread.post("back_frame", back_face(self.ctx))

    def _track_changed(self, track) -> None:
        self._refresh()
        url = getattr(track, "art_url", "") if track is not None else ""
        if url == self._ring_art:
            return
        self._ring_art = url
        if not url:
            self._ring_colors = None
            self._draw_ring()
            self._draw_glow()
            return
        self.ctx.track.art(url, lambda img, u=url: self._ring_cover(u, img))

    def _ring_cover(self, url: str, img) -> None:
        if url != self._ring_art:
            return
        colors = ringmod.cover_colors(img)
        if colors != self._ring_colors:
            self._ring_colors = colors
            self._draw_ring()
            self._draw_glow()

    # ---- from tide ----

    def _state(self, state: str) -> None:
        self.thread.post("playing", state == "playing")
        self._refresh()

    def _pulse(self, level: float) -> None:
        self.thread.post("pulse", level)

    def _position(self, _seconds: float) -> None:
        # lyric lines turn over on position ticks, not just on the timer
        if self.panel_open and self.panel.progress():
            self._dirty()

    def _accent(self, shade: float = 1.0) -> tuple[float, float, float]:
        c = QColor(self.ctx.theme.token("accent", "#ffd400"))
        if shade != 1.0:
            h, s, v, _a = c.getHsvF()
            c = QColor.fromHsvF(max(0.0, (h + 0.06) % 1.0), s, v * shade)
        return (c.redF(), c.greenF(), c.blueF())

    # ---- frames ----

    def _refresh(self) -> None:
        self.panel.refresh()

    def _restyle(self) -> None:
        self.panel.restyle()
        self.panel.set_animate(self._springy(), self._durations())
        self.thread.post("settings", self._vr_settings())
        self._draw_ring()
        self._draw_glow()
        self._send_back()
        if self.watch_up:
            self._send_watch()

    def _dirty(self) -> None:
        if self.panel_open and not self._redraw.isActive():
            self._redraw.start(FRAME_MS)

    def _send_panel(self) -> None:
        if self.panel_open:
            self.thread.post("panel_frame", self.panel.frame())
            # something still moving (a page sliding, a ripple, a fade):
            # keep frames coming at ~60 a second until it settles
            if self.panel.animating() and not self._redraw.isActive():
                self._redraw.start(16)

    def _send_watch(self) -> None:
        dur = self.ctx.playback.duration()
        progress = self.ctx.playback.position() / dur if dur > 0 else 0.0
        self.thread.post("watch_frame", watch_face(self.ctx, progress))

    def _on_tick(self) -> None:
        if self.panel_open:
            self.panel.progress()
            self._dirty()
        if self.watch_up:
            self._send_watch()


def _strip_command(text: str) -> str:
    """"play daft punk" and "search for daft punk" both search daft punk."""
    text = " ".join(text.split())
    for lead in ("search for ", "look up ", "find ", "play ", "queue "):
        if text.startswith(lead):
            return text[len(lead):]
    return text


# vr threads that wouldn't stop in time: kept referenced so python never
# destroys a running QThread (a fatal abort); they end with the process
_STRAGGLERS: list = []
