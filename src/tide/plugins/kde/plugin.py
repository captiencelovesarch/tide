"""kde desktop: your plasma desktop follows the music.

The accent colour takes a colour from the cover; with the wallpaper level
the wallpaper becomes a soft blur of it on every screen. What you had is
saved before the first change (to restore.json, so a crash doesn't lose
it either) and put back when the plugin is switched off or tide quits.

Every change to the desktop runs in order on one worker thread: Plasma's
tools take a moment, and a restore must never race a change.
"""
from __future__ import annotations

import json
import queue
import threading

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from tide.plugin_api import cover_colors

from . import art, desk

SETTLE_MS = 500            # a song has to stay this long before the desktop changes
PAUSE_MS = 3000            # paused (or the music ran out) this long: your look comes back
KEEP_WALLPAPERS = 2


class _Said(QObject):
    said = Signal(str)


class KdeDesktop:
    def start(self, ctx) -> None:
        self.ctx = ctx
        self.desk = desk.Desk()
        self.dir = ctx.paths.data_dir
        self.restore_file = self.dir / "restore.json"
        self._track = None
        self._look: tuple | None = None       # (art url, accent, cover image)
        self._applied = False
        self._shown: str | None = None        # the art url whose look is on the desktop
        self._serial = 0
        self.status = ctx.adopt(_Said())
        self.status.said.connect(ctx.set_status)
        self._settle = ctx.adopt(QTimer())
        self._settle.setSingleShot(True)
        self._settle.setInterval(SETTLE_MS)
        self._settle.timeout.connect(self._song_settled)
        self._pause = ctx.adopt(QTimer())
        self._pause.setSingleShot(True)
        self._pause.setInterval(PAUSE_MS)
        self._pause.timeout.connect(self._back_to_yours)
        self._jobs: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._work, name="tide-kde", daemon=True)
        self._worker.start()

        why = desk.missing()
        if why:
            ctx.set_status(why)
            self._off = True
            return
        self._off = False
        # a crash last time left your look changed: put it back first
        self._queue(self._restore)
        pb = ctx.playback
        pb.track_changed.connect(self._track_changed)
        pb.state_changed.connect(self._state)
        ctx.settings_changed.connect(self._settings_changed)
        ctx.set_status("your desktop follows the music")
        self._track_changed(pb.current())

    def stop(self) -> None:
        self._settle.stop()
        self._pause.stop()
        if not self._off:
            self._queue(self._restore)
        self._jobs.put(None)
        # quitting waits for your look to come back (plasma's tools are quick)
        self._worker.join(timeout=15.0)

    # ---- the worker ----

    def _queue(self, fn, *args) -> None:
        self._jobs.put((fn, args))

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            fn, args = job
            try:
                fn(*args)
            except Exception as exc:  # noqa: BLE001 - one failed step shouldn't end the plugin
                self._say(f"couldn't change the desktop: {exc}"[:120])

    def _say(self, text: str) -> None:
        try:
            self.status.said.emit(text)
        except RuntimeError:
            pass                      # switched off already

    # ---- your look, saved and restored (worker thread) ----

    def _saved(self) -> dict:
        try:
            return json.loads(self.restore_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.restore_file.write_text(json.dumps(data))

    def _restore(self) -> None:
        saved = self._saved()
        if saved.get("wallpapers"):
            self.desk.restore_wallpapers(saved["wallpapers"])
        if saved.get("accent"):
            self.desk.restore_accent(saved["accent"])
        self.restore_file.unlink(missing_ok=True)
        self._applied = False
        for old in self.dir.glob("wallpaper-*.jpg"):
            old.unlink(missing_ok=True)

    def _apply(self, serial: int, accent: str | None, cover, size, level: str) -> None:
        if serial != self._serial:
            return                    # a newer song got here first
        saved = self._saved()
        if "accent" not in saved:
            saved["accent"] = self.desk.read_accent()
            self._save(saved)
        if level == "wallpaper" and "wallpapers" not in saved:
            saved["wallpapers"] = self.desk.read_wallpapers()
            self._save(saved)
        if accent:
            self.desk.set_accent(accent)
        else:
            # a cover with no colour at all: yours, not the last song's
            # (2026-10-21: a night cover kept the song before's orange)
            self.desk.restore_accent(saved["accent"])
        if level == "wallpaper" and cover is not None and not cover.isNull():
            path = self.dir / f"wallpaper-{serial}.jpg"
            art.wallpaper(cover, *size).save(str(path), "JPG", 92)
            self.desk.set_wallpaper(path)
            # plasma has crossfaded to the new one: drop the older files
            olds = sorted(self.dir.glob("wallpaper-*.jpg"), key=lambda p: p.stat().st_mtime)
            for old in olds[:-KEEP_WALLPAPERS]:
                old.unlink(missing_ok=True)
        elif level != "wallpaper" and saved.get("wallpapers"):
            # went back to the accent only: your wallpaper comes back
            self.desk.restore_wallpapers(saved.pop("wallpapers"))
            self._save(saved)
        self._applied = True

    # ---- from tide (gui thread) ----
    #
    # the rules (2026-10-21, after it went back to your colour mid-song):
    # · on play, the song's colour goes on
    # · it stays while the song plays. loading between songs counts as
    #   playing: tide goes idle, then loading, then playing, and that gap
    #   used to look like a pause
    # · paused, or the music ran out: the colour stays 3 s, then yours
    # · a new song (next, skip, radio): its colour, half a second in

    def _playing(self) -> bool:
        return self.ctx.playback.state() in ("playing", "loading")

    def _track_changed(self, track) -> None:
        self._track = track
        if track is not None:
            self._settle.start()

    def _song_settled(self) -> None:
        track = self._track
        if track is None:
            return
        url = track.art_url
        if self._look is not None and self._look[0] == url:
            self._present()
        elif not url:
            # no cover at all: your own accent while it plays
            self._look = ("", None, None)
            self._present()
        else:
            self.ctx.track.art(url, lambda img, u=url: self._got_art(u, img))

    def _got_art(self, url: str, img) -> None:
        track = self._track
        if track is None or track.art_url != url or img is None or img.isNull():
            return
        colors = cover_colors(img)
        accent = art.accent(colors[0]) if colors else None
        self._look = (url, accent, img.copy())
        self._present()

    def _present(self, force: bool = False) -> None:
        """the current song's look on the desktop, if music is playing.
        paused, it waits for play."""
        look = self._look
        if look is None or not self._playing():
            return
        self._pause.stop()
        if look[0] == self._shown and not force:
            return
        self._shown = look[0]
        self._show(look[1], look[2])

    def _show(self, accent, img) -> None:
        self._serial += 1
        screen = QGuiApplication.primaryScreen()
        size = (1920, 1080)
        if screen is not None:
            s = screen.size() * screen.devicePixelRatio()
            w = min(3840, max(640, s.width()))
            size = (w, max(360, round(w * s.height() / max(1, s.width()))))
        self._queue(self._apply, self._serial, accent, img, size, self.ctx.settings["level"])
        title = self._track.title if self._track is not None else ""
        self.ctx.set_status(f"following {self.ctx.theme.case(title)}"[:120])

    def _state(self, state: str) -> None:
        if state in ("playing", "loading"):
            self._pause.stop()
            self._present()
        elif self._shown is not None:
            self._pause.start()             # 3 s with the song's colour, then yours

    def _back_to_yours(self) -> None:
        if self._playing():
            return
        self._shown = None
        self._serial += 1                   # anything still queued for a song is stale
        self._queue(self._restore)
        self.ctx.set_status("your desktop follows the music")

    def _settings_changed(self, _keys) -> None:
        if self._shown is not None:
            self._present(force=True)
