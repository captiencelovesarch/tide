"""stream overlay: what's playing as a page for obs (a browser source).

The page (page.html) is served on 127.0.0.1 only (server.py) and kept
live with server-sent events: the song, the cover, tide's colours and
fonts, the lyric line, and the beat. The beat goes through tide's flash
cap before it leaves, for the people watching the stream.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication

from tide.plugin_api import FlashLimiter, cover_colors

from .server import Server

PULSE_MS = 33              # beat events: ~30 a second, the peak in between
POSITION_EVERY_S = 5.0     # the page runs its own clock; this keeps it honest
DRIFT_S = 1.5              # a jump this far from where the page thinks we are: a seek


class Overlay:
    def start(self, ctx) -> None:
        self.ctx = ctx
        self.server: Server | None = None
        self._art_url = ""
        self._art_bytes = b""
        self._art_version = 0
        self._glow: list | None = None
        self._lyric = {"prev": "", "now": "", "next": ""}
        self._flash = FlashLimiter()
        self._peak = 0.0
        self._sent_pulse = -1.0
        self._pos_sent_at = 0.0
        self._pos_sent = 0.0
        self._pulse_timer = ctx.adopt(QTimer())
        self._pulse_timer.setInterval(PULSE_MS)
        self._pulse_timer.timeout.connect(self._send_pulse)

        pb = ctx.playback
        pb.track_changed.connect(self._track)
        pb.state_changed.connect(lambda _s: self._publish())
        pb.duration_changed.connect(lambda _d: self._publish())
        pb.speed_changed.connect(lambda _r: self._publish())
        pb.position_changed.connect(self._position)
        ctx.lyrics.line_changed.connect(lambda _l: self._lyrics())
        ctx.lyrics.changed.connect(lambda _s: self._lyrics())
        ctx.beats.pulse.connect(self._pulse)
        ctx.theme.changed.connect(self._publish)
        ctx.settings_changed.connect(self._settings_changed)
        self._serve()
        self._apply_settings()
        self._track(pb.current())

    def stop(self) -> None:
        self._pulse_timer.stop()
        if self.server is not None:
            self.server.stop()
            self.server = None

    # ---- serving ----

    def _port(self) -> int:
        try:
            port = int(self.ctx.settings["port"])
        except (TypeError, ValueError):
            port = 7311
        return port if 1024 <= port <= 65535 else 7311

    def link(self) -> str:
        port = self.server.port if self.server is not None else self._port()
        return f"http://127.0.0.1:{port}/"

    def _serve(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.server = None
        port = self._port()
        try:
            self.server = Server(port)
        except OSError as exc:
            self.ctx.set_status(f"port {port} is taken ({exc.strerror or exc}). pick another")
            return
        self.server.start()
        self.ctx.set_status(f"for obs: {self.link()}")
        self._publish()
        self._send_art_bytes()

    def _settings_changed(self, keys) -> None:
        if "port" in keys:
            self._serve()
        self._apply_settings()
        self._publish()

    def _apply_settings(self) -> None:
        s = self.ctx.settings
        if s["lyrics"]:
            self.ctx.lyrics.subscribe()
        else:
            self.ctx.lyrics.unsubscribe()
        if s["pulse"]:
            self.ctx.beats.subscribe()
            self._pulse_timer.start()
        else:
            self.ctx.beats.unsubscribe()
            self._pulse_timer.stop()

    def on_action(self, key: str) -> None:
        if key == "copy_link":
            QGuiApplication.clipboard().setText(self.link())
            self.ctx.ui.toast(f"copied {self.link()}. paste it into an obs browser source")

    # ---- what the page shows ----

    def _theme(self) -> dict:
        t = self.ctx.theme
        fonts = t.fonts() if hasattr(t, "fonts") else {}
        return {
            "bg": t.token("bg", "#0b0b0b"), "fg": t.token("fg", "#e6e6e6"),
            "dim": t.token("dim", "#8a8a8a"), "accent": t.token("accent", "#ffd400"),
            "border": t.token("border_col", t.token("fg", "#e6e6e6")),
            "modern": t.aesthetic() == "modern",
            "fonts": {k: (v or {}).get("family", "") for k, v in fonts.items()},
        }

    def _glow_colors(self) -> list:
        if self._glow is not None:
            return self._glow
        c = QColor(self.ctx.theme.token("accent", "#ffd400"))
        h, s, v, _a = c.getHsvF()
        d = QColor.fromHsvF(max(0.0, (h + 0.06) % 1.0), s, v * 0.75)
        return [[c.red(), c.green(), c.blue()], [d.red(), d.green(), d.blue()]]

    def _state(self) -> dict:
        pb, t, s = self.ctx.playback, self.ctx.theme, self.ctx.settings
        track = pb.current()
        info = None
        if track is not None:
            info = {"title": t.case(track.title), "artists": t.case(track.artists),
                    "album": t.case(track.album)}
        self._pos_sent = pb.position()
        self._pos_sent_at = time.monotonic()
        return {
            "track": info,
            "playing": pb.state() == "playing",
            "position": self._pos_sent,
            "duration": pb.duration(),
            "speed": pb.speed(),
            "art_version": self._art_version if self._art_url else 0,
            "lyric": self._lyric if s["lyrics"] else {},
            "lyrics": bool(s["lyrics"]),
            "pulse": bool(s["pulse"]),
            "layout": s["layout"],
            "glow": self._glow_colors(),
            "theme": self._theme(),
        }

    def _publish(self) -> None:
        if self.server is not None:
            self.server.publish("state", self._state())

    # ---- from tide ----

    def _track(self, track) -> None:
        url = getattr(track, "art_url", "") if track is not None else ""
        self._lyric = {"prev": "", "now": "", "next": ""}
        if url != self._art_url:
            self._art_url = url
            self._glow = None
            if url:
                self.ctx.track.art(url, lambda img, u=url: self._got_art(u, img))
        self._publish()

    def _got_art(self, url: str, img) -> None:
        if url != self._art_url or img is None or img.isNull():
            return
        buf = QByteArray()
        dev = QBuffer(buf)
        dev.open(QIODevice.WriteOnly)
        img.scaled(512, 512, Qt.KeepAspectRatio, Qt.SmoothTransformation).save(dev, "JPG", 90)
        dev.close()
        self._art_bytes = bytes(buf.data())
        colors = cover_colors(img)
        self._glow = ([[round(c * 255) for c in rgb] for rgb in colors] if colors else None)
        self._art_version += 1
        self._send_art_bytes()
        self._publish()

    def _send_art_bytes(self) -> None:
        if self.server is not None and self._art_bytes:
            self.server.set_art(self._art_bytes, "image/jpeg")

    def _lyrics(self) -> None:
        if not self.ctx.settings["lyrics"]:
            return
        prev, now, nxt = self.ctx.lyrics.lines_around(self.ctx.playback.position())
        t = self.ctx.theme
        lyric = {"prev": t.case(prev or ""), "now": t.case(now or ""), "next": t.case(nxt or "")}
        if lyric != self._lyric:
            self._lyric = lyric
            if self.server is not None:
                self.server.publish("lyric", lyric)

    def _position(self, seconds: float) -> None:
        pb = self.ctx.playback
        if self.ctx.settings["lyrics"]:
            self._lyrics()
        now = time.monotonic()
        playing = pb.state() == "playing"
        expected = self._pos_sent + ((now - self._pos_sent_at) * pb.speed() if playing else 0.0)
        if abs(seconds - expected) > DRIFT_S or now - self._pos_sent_at > POSITION_EVERY_S:
            self._pos_sent, self._pos_sent_at = seconds, now
            if self.server is not None:
                self.server.publish("position", {"position": seconds, "playing": playing,
                                                 "speed": pb.speed()})

    def _pulse(self, level: float) -> None:
        self._peak = max(self._peak, float(level))

    def _send_pulse(self) -> None:
        if self.server is None or not self.server.clients():
            self._peak = 0.0
            return
        playing = self.ctx.playback.state() == "playing"
        level = self._flash.update(time.monotonic(), self._peak if playing else 0.0)
        self._peak = 0.0
        if abs(level - self._sent_pulse) > 0.004:
            self._sent_pulse = level
            self.server.publish("pulse", {"l": round(level, 3)})
