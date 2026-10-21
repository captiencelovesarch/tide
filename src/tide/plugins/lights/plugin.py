"""lights: your rgb lights pulse with tide's bass, in the cover's colours.

OpenRGB (the sdk server, for pc parts, keyboards, strips on a motherboard
header) and WLED (wifi led strips). The show runs on a thread of its own
(show.py); this side only hands it the pulse, the colours and the
settings.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor

from tide.plugin_api import cover_colors

from . import launch, openrgb, wled
from .show import Show


class _Status(QObject):
    """the show's status lines, from its thread to the gui thread."""
    said = Signal(str)


class Lights:
    def start(self, ctx) -> None:
        self.ctx = ctx
        self.show: Show | None = None
        self._art = ""
        self._cover: tuple | None = None
        self.status = ctx.adopt(_Status())
        self.status.said.connect(ctx.set_status)
        pb = ctx.playback
        pb.track_changed.connect(self._track)
        pb.state_changed.connect(self._state)
        ctx.beats.pulse.connect(self._pulse)
        ctx.theme.changed.connect(self._colours)
        ctx.settings_changed.connect(self._settings_changed)
        ctx.beats.subscribe()
        self._restart()
        self._track(pb.current())
        self._state(pb.state())

    def stop(self) -> None:
        self._end_show()
        self._stop_openrgb()

    def _stop_openrgb(self) -> None:
        """only an openrgb tide started itself, after the show has given
        every device its colours back."""
        launch.stop_server(getattr(self, "_openrgb", None))
        self._openrgb = None

    # ---- the show ----

    def _outputs(self) -> list:
        s = self.ctx.settings
        outs: list = []
        if s["openrgb"]:
            outs.append(openrgb.Server(s["openrgb_address"]))
        for address in (s["wled"] or "").replace(";", ",").split(","):
            if address.strip():
                outs.append(wled.Strip(address))
        return outs

    def _restart(self) -> None:
        self._end_show()
        outs = self._outputs()
        if not outs:
            self.ctx.set_status("no lights set up: switch on openrgb or add a wled address")
            return
        self.ctx.set_status("looking for your lights")
        s = self.ctx.settings
        if s["openrgb"] and s["start_openrgb"] and getattr(self, "_openrgb", None) is None:
            host, port = openrgb.parse_address(s["openrgb_address"])
            self._openrgb, said = launch.start_server(host, port)
            if said:
                self.ctx.set_status(said)
        for out in outs:
            if isinstance(out, openrgb.Server):
                out.size_headers = bool(s["size_headers"])
        self.show = Show(outs, self.status.said.emit)
        self._configure()
        self._colours()
        self.show.configure(playing=self.ctx.playback.state() == "playing")
        self.show.start()

    def _end_show(self) -> None:
        if self.show is not None:
            self.show.stop()
            # a frame and the devices' goodbyes; a socket to a dead device
            # times out on its own thread
            self.show.join(timeout=3.0)
            self.show = None

    def _configure(self) -> None:
        if self.show is None:
            return
        s = self.ctx.settings
        self.show.configure(mode=s["mode"], strength=s["strength"], paused=s["paused"])

    def _settings_changed(self, keys) -> None:
        if {"openrgb", "openrgb_address", "start_openrgb"} & set(keys):
            self._end_show()
            self._stop_openrgb()
        if {"openrgb", "openrgb_address", "wled", "start_openrgb", "size_headers"} & set(keys):
            self._restart()
        else:
            self._configure()
            self._colours()

    def on_action(self, key: str) -> None:
        if key == "test":
            if self.show is None:
                self.ctx.ui.toast("no lights set up yet")
                return
            self.show.sweep()

    # ---- from tide ----

    def _pulse(self, level: float) -> None:
        if self.show is not None:
            self.show.set_pulse(level)

    def _state(self, state) -> None:
        if self.show is not None:
            self.show.configure(playing=state == "playing")

    def _track(self, track) -> None:
        url = getattr(track, "art_url", "") if track is not None else ""
        if url == self._art:
            return
        self._art = url
        if not url:
            self._cover = None
            self._colours()
            return
        self.ctx.track.art(url, lambda img, u=url: self._got_art(u, img))

    def _got_art(self, url: str, img) -> None:
        if url != self._art:
            return
        self._cover = cover_colors(img)
        self._colours()

    def _accent(self) -> tuple:
        c = QColor(self.ctx.theme.token("accent", "#ffd400"))
        h, s, v, _a = c.getHsvF()
        other = QColor.fromHsvF(max(0.0, (h + 0.06) % 1.0), s, v * 0.75)
        return ((c.redF(), c.greenF(), c.blueF()), (other.redF(), other.greenF(), other.blueF()))

    def _colours(self) -> None:
        if self.show is None:
            return
        use_cover = self.ctx.settings["colours"] == "cover" and self._cover is not None
        self.show.configure(colors=self._cover if use_cover else self._accent())
