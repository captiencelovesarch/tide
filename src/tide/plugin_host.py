"""The plugin host: finds plugins, switches them on and off without a
restart, and hands each one its ``ctx``. design/plugins.md is the why.

Where plugins live (later roots override earlier ones, like themes):
what the store installed in ``<data>/plugins/installed/`` (store.py),
``/usr/share/tide/plugins/`` from packages, and ``~/.config/tide/plugins/``.
A store plugin is on once it's installed (uninstalling is how you switch
it off); anything else starts off until the user switches it on. tide 3
ships no plugins of its own.

A plugin's ``vendor/`` folder (python modules it carries in its zip) goes
first on ``sys.path`` while it runs and comes off when it stops, with
every module imported from it.

Undo by construction: each plugin connects to signals on its own relay
object, never to the player or queue directly. Switching it off deletes
the relay, so every connection goes with it even if the plugin's
``stop()`` forgot one.

Safe mode: before a plugin's ``start()`` the host writes its slug to a
stamp file and removes it afterwards. A stamp found at launch means that
start took the whole process down (a native crash), so that plugin is
switched off and the user told. ``tide --no-plugins`` skips them all.
"""
from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import logging
import os
import re
import sys
import threading
import time
import tomllib
import types
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QThread, Signal

from . import config, qthreads
from .plugin_api import (
    API_VERSION, SETTING_KINDS, USES, ItemInfo, TrackInfo, item_info, track_info,
)

log = logging.getLogger("tide.plugins")

SYSTEM_DIR = Path("/usr/share/tide/plugins")
MODULE_ROOT = "tide_plugins"
# this many uncaught errors from one plugin inside the window switches it off
ERROR_LIMIT = 5
ERROR_WINDOW_S = 10.0
ERROR_LOG_BYTES = 256 * 1024   # a plugin's errors.log starts over past this

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ENTRY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$")


def plugin_roots() -> list[tuple[Path, str]]:
    return [(config.PLUGIN_STORE_DIR, "store"), (SYSTEM_DIR, "system"),
            (config.USER_PLUGINS_DIR, "yours")]


# ---------------------------------------------------------------------------
# manifests
# ---------------------------------------------------------------------------

class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class SettingDesc:
    key: str
    label: str
    kind: str
    default: object = None
    tooltip: str = ""
    choices: tuple[tuple[object, str], ...] | str | None = None
    advanced: bool = False
    secret: bool = False
    help_url: str = ""
    tab: str = "plugins"
    section: str = ""


@dataclass(frozen=True)
class Manifest:
    slug: str
    name: str
    description: str
    version: str
    api: int
    entry: str
    path: Path
    origin: str
    requires: tuple[str, ...] = ()
    uses: tuple[str, ...] = ()
    settings: tuple[SettingDesc, ...] = ()

    @property
    def ident(self) -> str:
        """the slug as a python identifier (module name)."""
        return self.slug.replace("-", "_")

    @property
    def vendor(self) -> Path:
        """python modules the plugin carries with it."""
        return self.path / "vendor"


def _choices(raw, key: str):
    if raw is None or isinstance(raw, str):
        return raw
    if not isinstance(raw, list) or not raw:
        raise ManifestError(f"setting {key}: choices must be a list or a function name")
    rows = []
    for item in raw:
        if isinstance(item, list) and len(item) == 2:
            rows.append((item[0], str(item[1])))
        elif isinstance(item, (str, int, float, bool)):
            rows.append((item, str(item)))
        else:
            raise ManifestError(f"setting {key}: each choice is a value or [value, label]")
    return tuple(rows)


def _type_ok(kind: str, value) -> bool:
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "str":
        return isinstance(value, str)
    if kind == "action":
        return value is None
    return value is not None       # choice: any scalar, checked against rows


def _setting(raw: dict, plugin_name: str) -> SettingDesc:
    if not isinstance(raw, dict):
        raise ManifestError("each [[setting]] must be a table")
    key = raw.get("key")
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise ManifestError(f"setting key {key!r} must be lowercase letters, digits and _")
    kind = raw.get("kind")
    if kind not in SETTING_KINDS:
        raise ManifestError(f"setting {key}: kind must be one of {', '.join(SETTING_KINDS)}")
    label = raw.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ManifestError(f"setting {key}: needs a label")
    choices = _choices(raw.get("choices"), key)
    if kind == "choice" and choices is None:
        raise ManifestError(f"setting {key}: a choice needs choices")
    default = raw.get("default")
    if not _type_ok(kind, default):
        raise ManifestError(f"setting {key}: default {default!r} doesn't fit kind {kind}")
    if kind == "choice" and isinstance(choices, tuple) and default not in [v for v, _ in choices]:
        raise ManifestError(f"setting {key}: default {default!r} isn't one of its choices")
    return SettingDesc(
        key=key, label=label, kind=kind, default=default,
        tooltip=str(raw.get("tooltip", "")), choices=choices,
        advanced=bool(raw.get("advanced", False)), secret=bool(raw.get("secret", False)),
        help_url=str(raw.get("help_url", "")), tab=str(raw.get("tab", "plugins")),
        section=str(raw.get("section", plugin_name)),
    )


def parse_manifest(folder: Path, origin: str = "yours") -> Manifest:
    path = folder / "plugin.toml"
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ManifestError("no plugin.toml") from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"plugin.toml doesn't parse: {exc}") from None
    meta = data.get("plugin")
    if not isinstance(meta, dict):
        raise ManifestError("plugin.toml needs a [plugin] table")
    slug = meta.get("slug") or folder.name
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise ManifestError(f"slug {slug!r} must be lowercase letters, digits and -")
    for key in ("name", "description", "version", "entry"):
        if not isinstance(meta.get(key), str) or not meta[key].strip():
            raise ManifestError(f"[plugin] needs {key}")
    if not _ENTRY_RE.match(meta["entry"]):
        raise ManifestError(f"entry {meta['entry']!r} must look like module:Class")
    api = meta.get("api")
    if not isinstance(api, int) or isinstance(api, bool):
        raise ManifestError("[plugin] needs api = <number>")
    requires = meta.get("requires", [])
    uses = meta.get("uses", [])
    if not all(isinstance(r, str) for r in requires):
        raise ManifestError("requires is a list of module names")
    bad = [u for u in uses if u not in USES]
    if bad:
        raise ManifestError(f"uses: unknown {', '.join(map(str, bad))}")
    settings = tuple(_setting(raw, meta["name"]) for raw in data.get("setting", []))
    keys = [s.key for s in settings]
    if len(keys) != len(set(keys)):
        raise ManifestError("two settings share a key")
    return Manifest(
        slug=slug, name=meta["name"], description=meta["description"],
        version=meta["version"], api=api, entry=meta["entry"], path=folder.resolve(),
        origin=origin, requires=tuple(requires), uses=tuple(uses),
        settings=settings,
    )


@dataclass
class Discovered:
    manifests: dict[str, Manifest] = field(default_factory=dict)
    broken: list[tuple[Path, str]] = field(default_factory=list)
    # slug -> the origin a later root's plugin replaced
    overrides: dict[str, str] = field(default_factory=dict)


def discover(roots: list[tuple[Path, str]] | None = None) -> Discovered:
    out = Discovered()
    for base, origin in roots or plugin_roots():
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            try:
                m = parse_manifest(child, origin)
            except ManifestError as exc:
                log.warning("plugin folder %s skipped: %s", child, exc)
                out.broken.append((child, str(exc)))
                continue
            if m.slug in out.manifests:
                out.overrides[m.slug] = out.manifests[m.slug].origin
            out.manifests[m.slug] = m
    return out


# ---------------------------------------------------------------------------
# tiny toml writer (flat scalars and lists), for state and plugin settings
# ---------------------------------------------------------------------------

def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def _write_private(path: Path, text: str) -> None:
    from .settings import _atomic_write
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    _atomic_write(path, text)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _read_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


# ---------------------------------------------------------------------------
# plugin settings
# ---------------------------------------------------------------------------

class PluginSettings:
    """One plugin's settings: only what its plugin.toml declares, stored in
    ``plugin-settings/<slug>.toml`` (0600). An undeclared key raises, so a
    plugin can't keep a setting the settings dialog doesn't show."""

    def __init__(self, manifest: Manifest, path: Path | None = None) -> None:
        self.manifest = manifest
        self.path = path or config.PLUGIN_SETTINGS_DIR / f"{manifest.slug}.toml"
        self._descs = {d.key: d for d in manifest.settings}
        self._values = {d.key: d.default for d in manifest.settings if d.kind != "action"}
        for key, value in _read_toml(self.path).items():
            d = self._descs.get(key)
            if d is not None and d.kind != "action" and self._valid(d, value):
                self._values[key] = value

    def _desc(self, key: str) -> SettingDesc:
        d = self._descs.get(key)
        if d is None:
            raise KeyError(f"plugin {self.manifest.slug} has no setting {key!r}; "
                           "declare it in plugin.toml")
        return d

    @staticmethod
    def _valid(d: SettingDesc, value) -> bool:
        if not _type_ok(d.kind, value):
            return False
        if d.kind == "choice" and isinstance(d.choices, tuple):
            return value in [v for v, _ in d.choices]
        return True

    def __getitem__(self, key: str):
        d = self._desc(key)
        if d.kind == "action":
            raise KeyError(f"{key} is an action, it has no value")
        return self._values[key]

    def desc(self, key: str) -> SettingDesc:
        return self._desc(key)

    def set(self, key: str, value, *, save: bool = True) -> bool:
        """store ``value``; True when it changed. raises on a bad value."""
        d = self._desc(key)
        if d.kind == "action" or not self._valid(d, value):
            raise ValueError(f"{key}: {value!r} doesn't fit a {d.kind} setting")
        if self._values.get(key) == value:
            return False
        self._values[key] = value
        if save:
            self.save()
        return True

    def values(self) -> dict:
        return dict(self._values)

    def save(self) -> None:
        lines = [f"{k} = {_toml_value(v)}" for k, v in self._values.items() if v is not None]
        _write_private(self.path, "\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# ctx
# ---------------------------------------------------------------------------

class _Relay(QObject):
    """this plugin's copy of tide's signals. deleting it disconnects the
    plugin from everything at once."""
    track_changed = Signal(object)       # TrackInfo | None
    state_changed = Signal(str)          # idle / loading / playing / paused
    position_changed = Signal(float)     # seconds
    duration_changed = Signal(float)
    speed_changed = Signal(float)
    ended = Signal()
    liked_changed = Signal(bool)
    queue_changed = Signal()
    modes_changed = Signal()             # shuffle or repeat changed
    lyrics_changed = Signal(object)      # (times, lines) or None
    pulse = Signal(float)                # the bass envelope, 0..1, ~60 a second
    lyric_line_changed = Signal(object)  # str or None
    theme_changed = Signal()
    settings_changed = Signal(object)    # set of keys
    job_done = Signal(int, object, object)


class _Playback:
    def __init__(self, host: "PluginHost", relay: _Relay) -> None:
        self._h, self._r = host, relay

    @property
    def track_changed(self):
        return self._r.track_changed

    @property
    def state_changed(self):
        return self._r.state_changed

    @property
    def position_changed(self):
        return self._r.position_changed

    @property
    def duration_changed(self):
        return self._r.duration_changed

    @property
    def speed_changed(self):
        return self._r.speed_changed

    @property
    def ended(self):
        return self._r.ended

    @property
    def liked_changed(self):
        return self._r.liked_changed

    def current(self) -> TrackInfo | None:
        return self._h.current_track()

    def state(self) -> str:
        return self._h.play_state()

    def position(self) -> float:
        return self._h.position

    def duration(self) -> float:
        return float(getattr(self._h.player, "duration", 0.0) or 0.0)

    def toggle(self) -> None:
        self._h.call_window("_on_play_clicked")

    def play(self) -> None:
        if self.state() != "playing":
            self.toggle()

    def pause(self) -> None:
        if self.state() == "playing":
            self.toggle()

    def next(self) -> None:
        self._h.call_window("_on_next_clicked")

    def previous(self) -> None:
        self._h.call_window("_on_prev_clicked")

    def seek(self, seconds: float) -> None:
        self._h.player.seek(max(0.0, float(seconds)))

    def liked(self) -> bool:
        return bool(getattr(self._h.window, "_liked_current", False))

    def like(self, on: bool = True) -> None:
        if self.current() is not None and self.liked() != bool(on):
            self._h.call_window("_on_like_clicked")

    def speed(self) -> float:
        btn = getattr(self._h.window, "speed_btn", None)
        return float(btn.speed()) if btn is not None else 1.0

    def speed_supported(self) -> bool:
        """false on sources that can't change speed (spotify)"""
        btn = getattr(self._h.window, "speed_btn", None)
        probe = getattr(btn, "backend_supported", None)
        return bool(probe()) if callable(probe) else btn is not None

    def set_speed(self, rate: float) -> None:
        """through the bar's speed button, so mpv, fullscreen and the
        saved speed agree. snapped to tide's 0.05 steps, 0.5×–2.0×."""
        btn = getattr(self._h.window, "speed_btn", None)
        if btn is not None:
            from .speed_law import clamp
            btn.set_speed(clamp(rate))

    def duck(self, fraction: float) -> None:
        """drop the music to ``fraction`` of its volume for a moment (while
        someone talks to a mic) without touching the saved volume."""
        p = self._h.player
        if p is not None and hasattr(p, "set_volume"):
            p.set_volume(int(round(self.volume() * max(0.0, min(1.0, fraction)))))

    def unduck(self) -> None:
        p = self._h.player
        if p is not None and hasattr(p, "set_volume"):
            p.set_volume(self.volume())

    def volume(self) -> int:
        vol = getattr(self._h.window, "volume", None)
        return int(vol.volume()) if vol is not None else 0

    def set_volume(self, percent: int) -> None:
        vol = getattr(self._h.window, "volume", None)
        if vol is not None:
            # through the widget, so the bar, mpv and the saved volume agree
            vol.setVolume(int(percent))


class _Queue:
    def __init__(self, host: "PluginHost", relay: _Relay) -> None:
        self._h, self._r = host, relay

    @property
    def changed(self):
        return self._r.queue_changed

    def tracks(self) -> list[TrackInfo]:
        q = self._h.queue
        return [track_info(t) for t in (q.tracks() if q is not None else [])]

    def current_index(self) -> int:
        q = self._h.queue
        return q.current_index() if q is not None else -1

    def play_at(self, index: int) -> None:
        q = self._h.queue
        if q is None:
            return
        tr = q.set_current(int(index))
        if tr is not None:
            self._h.call_window("_play_track", tr)

    @property
    def modes_changed(self):
        return self._r.modes_changed

    def up_next(self) -> TrackInfo | None:
        q = self._h.queue
        return track_info(q.peek_next()) if q is not None and hasattr(q, "peek_next") else None

    def shuffle(self) -> bool:
        return bool(getattr(self._h.queue, "shuffle_enabled", False))

    def set_shuffle(self, on: bool) -> None:
        # through the window, like the bar's button (it also says so in
        # the status bar)
        if self.shuffle() != bool(on):
            self._h.call_window("_on_shuffle_clicked")

    def repeat(self) -> str:
        """off / all / one"""
        mode = getattr(self._h.queue, "repeat_mode", None)
        return str(getattr(mode, "value", mode) or "off")

    def cycle_repeat(self) -> None:
        """off → all → one → off, like the bar's button."""
        self._h.call_window("_on_repeat_clicked")


class _Lyrics:
    """the current song's timed lyrics. nothing is fetched until a plugin
    subscribes; the host shares one tracker between all subscribers."""

    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx
        self._on = False

    @property
    def changed(self):
        """the whole timed sheet, (times, lines), or None."""
        return self._ctx._relay.lyrics_changed

    @property
    def line_changed(self):
        return self._ctx._relay.lyric_line_changed

    def subscribe(self) -> None:
        if not self._on:
            self._on = True
            self._ctx._host._lyrics_subscribe(self._ctx.slug)

    def unsubscribe(self) -> None:
        if self._on:
            self._on = False
            self._ctx._host._lyrics_unsubscribe(self._ctx.slug)

    def sheet(self):
        return self._ctx._host._lyric_sheet

    def lines_around(self, position: float) -> tuple[str | None, str | None, str | None]:
        """(the line before, the line being sung, the next line) at
        ``position`` seconds. all None when there are no timed lyrics."""
        sheet = self.sheet()
        if not sheet:
            return None, None, None
        times, lines = sheet
        from bisect import bisect_right
        from . import lyric_timing
        i = bisect_right(times, float(position) + lyric_timing.offset_s()) - 1

        def at(k):
            if 0 <= k < len(lines):
                return lines[k].strip() or None
            return None
        return at(i - 1), at(i), at(i + 1)


class _Beats:
    """tide's bass pulse, the one the backdrop breathes with: beat maps
    plus the live capture. flows while music plays and a plugin
    subscribes, whether or not the backdrop pulse is on."""

    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx
        self._on = False

    @property
    def pulse(self):
        return self._ctx._relay.pulse

    def subscribe(self) -> None:
        if not self._on:
            self._on = True
            self._ctx._host._pulse_subscribe(self._ctx.slug)

    def unsubscribe(self) -> None:
        if self._on:
            self._on = False
            self._ctx._host._pulse_unsubscribe(self._ctx.slug)


class _PulseSink(QObject):
    """what the ambient controller pushes to; fans out to the plugins."""

    def __init__(self, host: "PluginHost") -> None:
        super().__init__(host)
        self._host = host

    def set_pulse(self, level: float) -> None:
        self._host._emit("pulse", float(level))


class _Music:
    """search, the library, and playing what they found, through the
    active source. lookups run off the gui thread (ctx.jobs) and answer on
    it; ``on_error`` gets the exception (NotSupportedError when the source
    can't do that)."""

    SEARCH = ("songs", "albums", "artists", "playlists")
    LIBRARY = ("playlists", "liked", "albums", "artists")

    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx

    def _source(self):
        from .sources import registry
        src = registry().active
        if src is None:
            from .sources.base import NotSupportedError
            raise NotSupportedError("no source")
        return src

    def _run(self, fn, on_done, on_error) -> None:
        self._ctx.jobs.run(fn, on_done, on_error)

    def search(self, query: str, kind: str, on_done, on_error=None, limit: int = 30) -> None:
        """``kind``: songs / albums / artists / playlists."""
        def work():
            src = self._source()
            if kind == "songs":
                return [track_info(t) for t in src.search_songs(query, limit)]
            if kind == "albums":
                return [item_info("album", e) for e in src.search_albums(query, limit)]
            if kind == "artists":
                return [item_info("artist", e) for e in src.search_artists(query, limit)]
            if kind == "playlists":
                return [item_info("playlist", e) for e in src.search_playlists(query, limit)]
            raise ValueError(kind)
        self._run(work, on_done, on_error)

    def library(self, section: str, on_done, on_error=None) -> None:
        """``section``: playlists / liked / albums / artists."""
        def work():
            src = self._source()
            if section == "playlists":
                return [item_info("playlist", e) for e in src.get_library_playlists(200)
                        if getattr(e, "playlist_id", "") != "LM"]
            if section == "liked":
                # "LM" is liked music on youtube music; spotify mirrors it
                return [track_info(t) for t in src.get_playlist("LM", 500).tracks]
            if section == "albums":
                return [item_info("album", e) for e in src.get_library_albums_list(200)]
            if section == "artists":
                return [item_info("artist", e) for e in src.get_library_artists_list(200)]
            raise ValueError(section)
        self._run(work, on_done, on_error)

    def open(self, item: ItemInfo, on_done, on_error=None) -> None:
        """an album's or playlist's songs, or an artist's top songs."""
        def work():
            src = self._source()
            if item.kind == "playlist":
                tracks = src.get_playlist(item.id, 500).tracks
            elif item.kind == "album":
                detail = src.get_album(item.id)
                tracks = detail.tracks if detail is not None else []
            elif item.kind == "artist":
                detail = src.get_artist(item.id)
                tracks = detail.top_songs if detail is not None else []
            else:
                raise ValueError(item.kind)
            return [track_info(t) for t in tracks]
        self._run(work, on_done, on_error)

    @staticmethod
    def _tracks(infos) -> list:
        return [i.ref for i in infos if i is not None and i.ref is not None]

    def play_now(self, track: TrackInfo, radio: bool | None = None) -> None:
        """play ``track`` on its own. ``radio``: keep going with songs like
        it once it ends. None does what tide does when you play a song from
        search (radio on, unless the user turned that off). without it a
        song from a plugin played once and the queue stopped (2026-10-19)."""
        if track is None or track.ref is None:
            return
        host = self._ctx._host
        seed = (bool(getattr(host.window, "_auto_radio_on_play", True)) if radio is None
                else bool(radio))
        host.call_window("_play_now", track.ref, seed)

    def radio(self, track: TrackInfo) -> None:
        """start a radio from ``track``: it plays now, songs like it follow."""
        if track is not None and track.ref is not None:
            self._ctx._host.call_window("_start_radio", track.ref)

    def play_next(self, track: TrackInfo) -> None:
        if track is not None and track.ref is not None:
            self._ctx._host.call_window("_queue_next", track.ref)

    def play(self, tracks: list, start: int = 0, shuffle: bool = False) -> None:
        """replace the queue with ``tracks`` and play from ``start`` (or a
        shuffled order)."""
        refs = self._tracks(tracks)
        if not refs:
            return
        if shuffle:
            import random
            random.shuffle(refs)
            start = 0
        else:
            start = max(0, min(int(start), len(refs) - 1))
            refs = refs[start:] + refs[:start]
        self._ctx._host.call_window("_play_all", refs)


class _Speech:
    """push-to-talk: ``listen()`` opens the default mic, ``finish()`` closes
    it and turns what it heard into text, on this computer only, with
    whisper (speech_whisper.py). its model is a ~77 MB download on request:
    ``download()``; until then ``available()`` says what's missing."""

    RATE = 16000

    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx
        self._proc = None
        self._buf = bytearray()
        self._thread = None
        ctx.on_stop(self.cancel)

    def available(self) -> tuple[bool, str]:
        from . import speech_whisper
        if not speech_whisper.runtime_available():
            return False, "voice needs onnxruntime (python-onnxruntime-cpu)"
        if not speech_whisper.installed():
            return False, "voice needs its speech model first"
        return True, ""

    def model_size_mb(self) -> int:
        from . import speech_whisper
        return round(speech_whisper.total_bytes() / 1e6)

    def download(self, on_progress=None, on_done=None, on_error=None) -> None:
        """fetch the speech model off the gui thread. ``on_progress(done,
        total)`` may arrive from the download thread: keep it cheap."""
        from . import speech_whisper
        self._ctx.jobs.run(lambda: speech_whisper.download(on_progress), on_done, on_error)

    def listening(self) -> bool:
        return self._proc is not None

    def listen(self) -> None:
        import subprocess
        import threading
        if self._proc is not None:
            return
        self._buf = bytearray()
        self._proc = subprocess.Popen(
            ["parec", "--raw", "--format=s16le", f"--rate={self.RATE}", "--channels=1",
             "--latency-msec=20"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        proc = self._proc

        def pump():
            while proc.stdout is not None:
                chunk = proc.stdout.read(3200)
                if not chunk:
                    return
                self._buf.extend(chunk)
        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()

    def seconds(self) -> float:
        return len(self._buf) / (2 * self.RATE)

    def _close(self) -> bytes:
        proc, self._proc = self._proc, None
        if proc is None:
            return b""
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001 - kill it then
            proc.kill()
        if self._thread is not None:
            self._thread.join(timeout=2)
        return bytes(self._buf)

    def cancel(self) -> None:
        self._close()

    def finish(self, on_done, on_error=None) -> None:
        """stop listening; ``on_done(text)`` with what was said ("" for
        nothing)."""
        pcm = self._close()

        def work():
            import numpy as np
            from . import speech_whisper
            if len(pcm) < self.RATE:            # under half a second: nothing said
                return ""
            audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            return speech_whisper.transcribe(audio)
        self._ctx.jobs.run(work, on_done, on_error)


class _Track:
    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx

    def current(self) -> TrackInfo | None:
        return self._ctx._host.current_track()

    def art(self, url_or_track, callback: Callable) -> None:
        """the cover as a QImage, now if cached, else later. ``callback``
        gets the image (or None); never called after the plugin stops."""
        url = getattr(url_or_track, "art_url", url_or_track) or ""
        from .ui import art_cache
        ctx = self._ctx

        def deliver(img) -> None:
            if ctx.alive:
                ctx._call(callback, img)

        img = art_cache.cache().request(url, deliver)
        if img is not None:
            deliver(img)


class _Theme:
    def __init__(self, relay: _Relay) -> None:
        self._r = relay

    @property
    def changed(self):
        return self._r.theme_changed

    def _current(self):
        from . import theming
        return theming.manager().current_effective() or theming.manager().current()

    def token(self, name: str, default: str = "") -> str:
        t = self._current()
        return t.token(name, default) if t is not None else default

    def aesthetic(self) -> str:
        t = self._current()
        return str(getattr(t, "aesthetic", "") or "brutalist")

    def dark(self) -> bool:
        t = self._current()
        return bool(getattr(t, "dark", True))

    def font(self) -> tuple[str, bool]:
        """(family, mono) of the theme's ui font."""
        t = self._current()
        typo = getattr(t, "typography", {}) or {}
        return str(typo.get("family", "") or ""), bool(typo.get("mono", False))

    def fonts(self) -> dict[str, dict]:
        """the user's type roles (settings, type roles): "ui" (body text),
        "title" (song titles, headings), "numbers" (times, the speed).
        each is {family, weight, size, spacing}: family is never empty
        (a role with none uses the body face), weight 0 means the theme's
        own, size is a percent of the body size, spacing a percent added
        to normal tracking. theme.changed fires when any of it changes."""
        from PySide6.QtWidgets import QApplication
        from .ui import typefaces
        app = QApplication.instance()
        body = app.font().family() if app is not None else ""
        body = body or self.font()[0]
        out = {}
        for name in typefaces.ROLES:
            r = typefaces.role(name)
            if name == "ui" or not r.get("family"):
                r["family"] = body
            out[name] = r
        return out

    def case(self, text: str) -> str:
        """text in the theme's letter case, like tide's own labels."""
        from . import theming
        return theming.styled_case(text)

    def icon(self, key: str, px: int, color: str = ""):
        """one of tide's icons from the user's icon set, as a ``px``-square
        QPixmap in ``color`` (default: the theme's fg). None for a key the
        set doesn't have."""
        from PySide6.QtCore import QByteArray, Qt
        from PySide6.QtGui import QPainter, QPixmap
        from PySide6.QtSvg import QSvgRenderer
        from .ui import icon_sets
        svg = icon_sets.svg_for(key)
        if not svg:
            return None
        ink = color or self.token("fg", "#e6e6e6")
        renderer = QSvgRenderer(QByteArray(svg.replace("currentColor", ink).encode("utf-8")))
        pix = QPixmap(px, px)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        renderer.render(p)
        p.end()
        return pix

    def glyph(self, key: str) -> str:
        """one of tide's control glyphs (play, pause, prev, next, like_on,
        like_off, ...), from the user's glyph pack."""
        from . import glyphs
        return glyphs.glyph(key)


class _Motion:
    """tide's motion rules. intensity off means nothing animates."""

    def intensity(self) -> str:
        from .ui import motion
        return motion.intensity().value

    def off(self) -> bool:
        return self.intensity() == "off"

    def profile(self) -> str:
        from .ui import motion
        return motion.profile()

    def dur(self, key: str) -> int:
        from .ui import motion
        return motion.dur(key)

    def ease(self, key: str):
        from .ui import motion
        return motion.ease(key)


class _JobWorker(QObject):
    done = Signal(int, object, object)

    def __init__(self, job_id: int, fn: Callable) -> None:
        super().__init__()
        self._id, self._fn = job_id, fn

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 - handed to on_error
            self.done.emit(self._id, None, exc)
            return
        self.done.emit(self._id, result, None)


class _Jobs:
    """blocking work off the gui thread; callbacks come back on it."""

    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx
        self._next = 0
        self._pending: dict[int, tuple[Callable | None, Callable | None]] = {}
        # the relay lives on the gui thread, so a worker's signal into it
        # is queued there
        ctx._relay.job_done.connect(self._finish)

    @property
    def group(self) -> str:
        return f"plugin:{self._ctx.slug}"

    def run(self, fn: Callable, on_done: Callable | None = None,
            on_error: Callable | None = None) -> None:
        self._next += 1
        job_id = self._next
        self._pending[job_id] = (on_done, on_error)
        thread = QThread()
        worker = _JobWorker(job_id, fn)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._ctx._relay.job_done)
        worker.done.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        qthreads.retain(thread, worker, self.group)
        thread.start()

    def _finish(self, job_id: int, result, error) -> None:
        on_done, on_error = self._pending.pop(job_id, (None, None))
        if not self._ctx.alive:
            return
        if error is not None:
            if on_error is not None:
                self._ctx._call(on_error, error)
            else:
                self._ctx.log.warning("job failed: %r", error)
        elif on_done is not None:
            self._ctx._call(on_done, result)

    def _dispose(self) -> None:
        self._pending.clear()
        qthreads.join(self.group, wait_ms=1000)


class _TrayAction:
    """what add_tray_action hands back: change it later, or read it."""

    def __init__(self, action) -> None:
        self._a = action

    def set_label(self, text: str) -> None:
        self._a.setText(str(text))

    def set_checked(self, on: bool) -> None:
        # a plugin setting its own item mustn't call its own handler back
        self._a.blockSignals(True)
        self._a.setChecked(bool(on))
        self._a.blockSignals(False)

    def checked(self) -> bool:
        return self._a.isChecked()

    def set_visible(self, on: bool) -> None:
        self._a.setVisible(bool(on))


class _Ui:
    def __init__(self, host: "PluginHost", ctx: "PluginContext | None" = None) -> None:
        self._h = host
        self._ctx = ctx

    def add_tray_action(self, label: str, fn: Callable, checkable: bool = False,
                        checked: bool = False) -> _TrayAction:
        """an item in the tray menu, above quit. ``fn()`` when clicked, or
        ``fn(on)`` for a checkable one. it goes when the plugin stops. no
        tray (some desktops have none): the item exists, nobody sees it."""
        from PySide6.QtGui import QAction
        ctx = self._ctx
        action = QAction(str(label))
        action.setCheckable(bool(checkable))
        action.setChecked(bool(checked))
        if checkable:
            action.toggled.connect(lambda on: ctx._call(fn, bool(on)))
        else:
            action.triggered.connect(lambda _c=False: ctx._call(fn))
        tray = getattr(self._h.window, "_tray", None)
        if tray is not None and hasattr(tray, "add_plugin_action"):
            tray.add_plugin_action(action)
            ctx.on_stop(lambda: tray.remove_plugin_action(action))
        ctx.adopt(action)
        return _TrayAction(action)

    def add_visualizer_tile(self, kind: str, label: str, factory: Callable,
                            knobs: tuple[str, ...] = ()) -> None:
        """a tile each visualizer slot can show. ``factory(engine)`` builds
        one (subclass tide.plugin_vis.PlayTile); ``knobs`` are this plugin's
        choice settings that the tile's menu offers. it goes when the
        plugin stops; a slot showing it keeps the pick for next time."""
        from .ui import vis_play
        ctx = self._ctx
        if not kind or kind in vis_play.KIND_LABELS:
            raise ValueError(f"tide already has a tile called {kind!r}")
        for key in knobs:
            ctx.settings.desc(key)          # KeyError for an undeclared knob

        def set_knob(key, value) -> None:
            if ctx.alive and ctx.settings.set(key, value):
                ctx._host.apply_settings(ctx.slug, {key})

        vis_play.register_tile(vis_play.PluginTile(kind, label, factory, knobs,
                                                   ctx.settings, set_knob))
        ctx.on_stop(lambda: vis_play.unregister_tile(kind))

    def toast(self, text: str, action_label: str | None = None,
              on_action: Callable | None = None) -> None:
        win = self._h.window
        if win is None or not hasattr(win, "toast_host"):
            log.info("toast: %s", text)
            return
        from .ui.toast import show_toast
        show_toast(win.toast_host(), text, action_label=action_label, on_action=on_action)


class _Sources:
    """music sources a plugin adds (soundcloud, local files...). they sit
    in the source panel like tide's own, with the same on switch, and go
    when the plugin stops."""

    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx
        self._mine: list[str] = []
        ctx.on_stop(self._unregister_all)

    def _settings(self):
        return getattr(self._ctx._host.window, "_settings", None)

    def register(self, source, on: bool = True) -> None:
        """add ``source`` to tide. ``on``: whether it starts switched on when
        tide has never had a switch for it; one from before (tide 2's own
        soundcloud, or the source panel since) is kept."""
        from .sources import registry
        slug = getattr(source, "slug", "")
        if not slug:
            raise ValueError("a source needs a slug")
        reg = registry()
        if reg.get(slug) is not None and slug not in self._mine:
            raise ValueError(f"tide already has a source called {slug}")
        s = self._settings()
        enabled_map = dict(getattr(s, "sources_enabled", {}) or {})
        if slug not in enabled_map:
            enabled_map[slug] = bool(on)
            if s is not None:
                s.sources_enabled = enabled_map
                try:
                    from . import settings as settings_module
                    settings_module.save_fields(s, "sources_enabled")
                except Exception:  # noqa: BLE001 - tests run without a file
                    pass
        reg.register(source, enabled=bool(enabled_map.get(slug, True)))
        self._mine.append(slug)
        # the user's active source comes back when its plugin does
        if s is not None and getattr(s, "active_source", "") == slug and reg.is_enabled(slug):
            reg.set_active(slug)

    def is_on(self, slug: str) -> bool:
        """is that source switched on in the source panel?"""
        from .sources import registry
        return registry().is_enabled(slug)

    def _unregister_all(self) -> None:
        from .sources import registry
        reg = registry()
        for slug in reversed(self._mine):
            reg.unregister(slug)
        self._mine.clear()


class _Paths:
    def __init__(self, slug: str) -> None:
        self._slug = slug

    @staticmethod
    def _private(path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass
        return path

    @property
    def data_dir(self) -> Path:
        return self._private(config.PLUGIN_DATA_DIR / self._slug)

    @property
    def cache_dir(self) -> Path:
        return self._private(config.PLUGIN_CACHE_DIR / self._slug)


class PluginContext:
    """everything a plugin gets. see tide.plugin_api for the rules."""

    def __init__(self, host: "PluginHost", manifest: Manifest, settings: PluginSettings) -> None:
        self._host = host
        self.alive = True
        self.slug = manifest.slug
        self.manifest = manifest
        from . import __version__
        self.tide_version = __version__
        self.log = logging.getLogger(f"tide.plugin.{manifest.slug}")
        self._relay = _Relay()
        self.settings = settings
        self.playback = _Playback(host, self._relay)
        self.queue = _Queue(host, self._relay)
        self.track = _Track(self)
        self.lyrics = _Lyrics(self)
        self.beats = _Beats(self)
        self.music = _Music(self)
        self.theme = _Theme(self._relay)
        self.motion = _Motion()
        self.jobs = _Jobs(self)
        self.ui = _Ui(host, self)
        self.paths = _Paths(manifest.slug)
        self._on_stop: list[Callable] = []
        self.sources = _Sources(self)
        self._adopted: list = []
        self.speech = _Speech(self)

    @property
    def settings_changed(self):
        return self._relay.settings_changed

    def set_status(self, text: str) -> None:
        """a short line the plugin list shows under this plugin's name
        ("waiting for steamvr", "connected")."""
        if self.alive:
            self._host._set_detail(self.slug, str(text)[:120])

    def on_stop(self, fn: Callable) -> None:
        """run ``fn`` when the plugin is switched off (after its stop())."""
        self._on_stop.append(fn)

    def adopt(self, obj):
        """hand a parentless qt object (a widget, timer, surface...) to tide:
        it's deleted when the plugin is switched off, right after stop().
        without this, a lambda connected to one of its signals that
        captures the plugin keeps it alive until python exits, when qt is
        half gone and deleting it crashes. returns ``obj``."""
        self._adopted.append(obj)
        return obj

    def _call(self, fn: Callable, *args) -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - counted against the plugin
            self._host._plugin_error(self.slug, sys.exc_info())

    def _dispose(self) -> None:
        if not self.alive:
            return
        self.alive = False
        for fn in reversed(self._on_stop):
            try:
                fn()
            except Exception:  # noqa: BLE001 - teardown keeps going
                log.exception("%s: on_stop callback failed", self.slug)
        self._on_stop.clear()
        self.lyrics.unsubscribe()
        self.beats.unsubscribe()
        self.jobs._dispose()
        import shiboken6
        for obj in reversed(self._adopted):
            try:
                if shiboken6.isValid(obj):
                    shiboken6.delete(obj)
            except Exception:  # noqa: BLE001 - teardown keeps going
                log.exception("%s: couldn't delete an adopted object", self.slug)
        self._adopted.clear()
        self._relay.blockSignals(True)
        self._relay.deleteLater()


# ---------------------------------------------------------------------------
# the host
# ---------------------------------------------------------------------------

@dataclass
class PluginRecord:
    manifest: Manifest
    settings: PluginSettings
    status: str = "off"
    instance: object = None
    ctx: PluginContext | None = None
    errors: deque = field(default_factory=deque)
    overrides: str = ""            # the origin this one replaced, if any
    detail: str = ""               # the plugin's own status line, while it runs
    stamp: float = 0.0             # plugin.toml's mtime when it was read

    def __post_init__(self) -> None:
        self.stamp = _toml_stamp(self.manifest)

    @property
    def running(self) -> bool:
        return self.instance is not None

    def missing(self) -> list[str]:
        out = []
        vendor = self.manifest.vendor
        for name in self.manifest.requires:
            try:
                found = (vendor.is_dir() and importlib.machinery.PathFinder.find_spec(
                    name.split(".")[0], [str(vendor)]) is not None) \
                    or importlib.util.find_spec(name) is not None
            except (ImportError, ValueError):
                found = False
            if not found:
                out.append(name)
        return out

    def blocked(self) -> str:
        """why it can't be switched on, or ""."""
        if self.manifest.api != API_VERSION:
            return "built for a different tide"
        missing = self.missing()
        if missing:
            return "needs " + ", ".join(missing)
        return ""


def _toml_stamp(m: Manifest) -> float:
    try:
        return (m.path / "plugin.toml").stat().st_mtime
    except OSError:
        return 0.0


class PluginHost(QObject):
    notice = Signal(str)        # a line for a toast
    changed = Signal(str)       # a plugin's status or switch changed
    listing_changed = Signal()  # a plugin was installed or removed
    _too_many = Signal(str)     # crosses threads: errors from plugin threads

    def __init__(self, window=None, player=None, queue=None, *,
                 roots: list[tuple[Path, str]] | None = None,
                 state_file: Path | None = None,
                 settings_dir: Path | None = None,
                 stamp: Path | None = None,
                 data_dir: Path | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.window, self.player, self.queue = window, player, queue
        self.state_file = state_file or config.PLUGIN_STATE_FILE
        self.data_dir = data_dir or config.PLUGIN_DATA_DIR
        self.settings_dir = settings_dir or config.PLUGIN_SETTINGS_DIR
        self.stamp = stamp or config.PLUGIN_START_STAMP
        self.position = 0.0
        self._lyric_tracker = None
        self._lyric_subs: set[str] = set()
        self._lyric_sheet = None
        self._pulse_subs: set[str] = set()
        self._pulse_sink: _PulseSink | None = None
        self._roots = roots
        self._hooks_installed = False
        self._prev_excepthook = None
        self._prev_thread_hook = None
        self._logged: set[str] = set()
        self._too_many.connect(self._switch_off_for_errors)
        self._vendor_paths: dict[str, str] = {}
        found = discover(roots)
        self.broken = found.broken
        self._records: dict[str, PluginRecord] = {}
        for slug, m in found.manifests.items():
            rec = PluginRecord(m, PluginSettings(m, self.settings_dir / f"{slug}.toml"))
            rec.overrides = found.overrides.get(slug, "")
            self._records[slug] = rec
        self._enabled = self._load_state()
        self._wire_core()

    # ---- state ----

    def _load_state(self) -> dict[str, bool]:
        raw = _read_toml(self.state_file).get("enabled", {})
        return {k: bool(v) for k, v in raw.items() if isinstance(k, str)}

    def _save_state(self) -> None:
        lines = ["[enabled]"] + [f'"{k}" = {_toml_value(v)}' for k, v in sorted(self._enabled.items())]
        _write_private(self.state_file, "\n".join(lines) + "\n")

    def is_enabled(self, slug: str) -> bool:
        rec = self._records.get(slug)
        if rec is None:
            return False
        # installed from the store = on, unless safe mode or too many
        # errors switched it off since
        return self._enabled.get(slug, rec.manifest.origin == "store")

    def records(self) -> list[PluginRecord]:
        return sorted(self._records.values(), key=lambda r: r.manifest.name.lower())

    def record(self, slug: str) -> PluginRecord | None:
        return self._records.get(slug)

    # ---- core signals -> every live relay ----

    def _relays(self):
        return [r.ctx._relay for r in self._records.values() if r.ctx is not None and r.ctx.alive]

    def _emit(self, name: str, *args) -> None:
        for relay in self._relays():
            getattr(relay, name).emit(*args)

    def _wire_core(self) -> None:
        p, q = self.player, self.queue
        if p is not None:
            p.state_changed.connect(lambda st: self._emit("state_changed", self.play_state(st)))
            p.position_changed.connect(self._on_position)
            p.duration_changed.connect(lambda s: self._emit("duration_changed", float(s)))
            if hasattr(p, "speed_changed"):
                p.speed_changed.connect(lambda r: self._emit("speed_changed", float(r)))
            p.ended.connect(lambda: self._emit("ended"))
        if q is not None:
            q.current_changed.connect(lambda tr: self._emit("track_changed", track_info(tr)))
            if hasattr(q, "modes_changed"):
                q.modes_changed.connect(lambda: self._emit("modes_changed"))
            for sig in ("rowsInserted", "rowsRemoved", "rowsMoved", "modelReset"):
                if hasattr(q, sig):
                    getattr(q, sig).connect(lambda *_: self._emit("queue_changed"))
        try:
            from . import theming
            theming.manager().theme_changed.connect(lambda *_: self._emit("theme_changed"))
        except Exception:  # noqa: BLE001 - no theme manager in some tests
            pass

    def _on_position(self, seconds: float) -> None:
        self.position = float(seconds)
        self._emit("position_changed", self.position)

    def _set_detail(self, slug: str, text: str) -> None:
        rec = self._records.get(slug)
        if rec is not None and rec.detail != text:
            rec.detail = text
            self.changed.emit(slug)

    # ---- lyrics, shared ----

    def _lyrics_subscribe(self, slug: str) -> None:
        self._lyric_subs.add(slug)
        if self._lyric_tracker is None and self.player is not None and self.queue is not None:
            from .lyric_tracker import LyricTracker
            t = LyricTracker(getattr(self.window, "api", None), self.player, self.queue, self)
            t.start_wire()
            t.timeline_changed.connect(self._on_lyric_sheet)
            t.lyric_changed.connect(lambda line: self._emit("lyric_line_changed", line))
            self._lyric_tracker = t
        if self._lyric_tracker is not None:
            self._lyric_tracker.set_enabled(True)

    def _lyrics_unsubscribe(self, slug: str) -> None:
        self._lyric_subs.discard(slug)
        if not self._lyric_subs and self._lyric_tracker is not None:
            self._lyric_tracker.set_enabled(False)

    def _ambient(self):
        return getattr(self.window, "_ambient", None)

    def _pulse_subscribe(self, slug: str) -> None:
        first = not self._pulse_subs
        self._pulse_subs.add(slug)
        amb = self._ambient()
        if first and amb is not None:
            if self._pulse_sink is None:
                self._pulse_sink = _PulseSink(self)
            amb.add_listener(self._pulse_sink)

    def _pulse_unsubscribe(self, slug: str) -> None:
        self._pulse_subs.discard(slug)
        amb = self._ambient()
        if not self._pulse_subs and amb is not None and self._pulse_sink is not None:
            amb.remove_listener(self._pulse_sink)

    def _on_lyric_sheet(self, sheet) -> None:
        self._lyric_sheet = sheet
        self._emit("lyrics_changed", sheet)

    def notify_liked(self, liked: bool) -> None:
        """the window calls this when the like state of the current song
        settles (a click, or a new song's state arriving)."""
        self._emit("liked_changed", bool(liked))

    # ---- what ctx reads ----

    def play_state(self, st=None) -> str:
        st = st if st is not None else getattr(self.player, "state", None)
        return str(getattr(st, "value", st) or "idle")

    def current_track(self) -> TrackInfo | None:
        tr = getattr(self.window, "_current", None)
        if tr is None and self.queue is not None:
            tr = self.queue.current
        return track_info(tr)

    def call_window(self, name: str, *args) -> None:
        fn = getattr(self.window, name, None)
        if callable(fn):
            fn(*args)

    # ---- lifecycle ----

    def start_enabled(self) -> None:
        """at launch: deal with a leftover safe-mode stamp, then start
        everything that's switched on."""
        crashed = self._read_stamp()
        if crashed:
            self._clear_stamp()
            rec = self._records.get(crashed)
            if rec is not None and self.is_enabled(crashed):
                self._enabled[crashed] = False
                self._save_state()
                rec.status = "off: it crashed tide last time"
                self.notice.emit(f"{rec.manifest.name} crashed tide last time. it's off for now")
        for rec in self.records():
            if self.is_enabled(rec.manifest.slug):
                self._start(rec)

    def set_enabled(self, slug: str, on: bool) -> bool:
        """switch a plugin on or off, live. False if it couldn't start."""
        rec = self._records.get(slug)
        if rec is None:
            return False
        self._enabled[slug] = bool(on)
        self._save_state()
        if on and not rec.running:
            ok = self._start(rec)
            if not ok:
                self._enabled[slug] = False
                self._save_state()
            return ok
        if not on and rec.running:
            self._stop(rec)
        elif not on:
            rec.status = "off"
            self.changed.emit(slug)
        return True

    def reload(self, slug: str) -> bool:
        """stop, forget its modules, start again. for plugin authors."""
        rec = self._records.get(slug)
        if rec is None:
            return False
        if rec.running:
            self._stop(rec)
        try:
            m = parse_manifest(rec.manifest.path, rec.manifest.origin)
        except ManifestError as exc:
            rec.status = f"broken: {exc}"
            self.changed.emit(slug)
            return False
        rec.manifest = m
        rec.stamp = _toml_stamp(m)
        rec.settings = PluginSettings(m, self.settings_dir / f"{slug}.toml")
        self.changed.emit(slug)
        return self._start(rec) if self.is_enabled(slug) else True

    def apply_settings(self, slug: str, keys) -> None:
        rec = self._records.get(slug)
        if rec is not None and rec.ctx is not None and rec.ctx.alive:
            rec.ctx._relay.settings_changed.emit(set(keys))

    def shutdown(self) -> None:
        for rec in self.records():
            if rec.running:
                self._stop(rec)
        self.uninstall_hooks()

    # ---- the store adds and removes plugins while tide runs ----

    def rescan(self) -> list[str]:
        """read the roots again: start what's new and switched on, stop and
        forget what's gone, restart what changed folder (an update) or had
        its plugin.toml edited. returns the slugs that came, went or
        changed."""
        found = discover(self._roots)
        broken_changed = found.broken != self.broken
        self.broken = found.broken
        touched = []
        for slug in [s for s in self._records if s not in found.manifests]:
            rec = self._records.pop(slug)
            if rec.running:
                self._stop(rec)
            touched.append(slug)
        for slug, m in found.manifests.items():
            rec = self._records.get(slug)
            if rec is not None and rec.manifest.path == m.path \
                    and rec.manifest.version == m.version and rec.stamp == _toml_stamp(m):
                rec.overrides = found.overrides.get(slug, "")
                continue
            was_running = rec is not None and rec.running
            if was_running:
                self._stop(rec)
            new = PluginRecord(m, PluginSettings(m, self.settings_dir / f"{slug}.toml"))
            new.overrides = found.overrides.get(slug, "")
            self._records[slug] = new
            touched.append(slug)
            if self.is_enabled(slug):
                self._start(new)
        if touched or broken_changed:
            self.listing_changed.emit()
        return touched

    def installed(self, slug: str) -> bool:
        """after the store unpacked ``slug``: pick it up and switch it on.
        False if it didn't start (its status says why)."""
        self._enabled.pop(slug, None)      # installing is switching it on
        self._save_state()
        self.rescan()
        rec = self._records.get(slug)
        if rec is None:
            return False
        if not rec.running and self.is_enabled(slug):
            return self._start(rec)
        return rec.running

    def release(self, slug: str) -> None:
        """before the store moves ``slug``'s folder: stop it."""
        rec = self._records.get(slug)
        if rec is not None and rec.running:
            self._stop(rec)

    def uninstalled(self, slug: str) -> None:
        """after the store removed ``slug``'s folder."""
        self._enabled.pop(slug, None)
        self._save_state()
        self.rescan()

    # ---- start / stop ----

    def _module_prefix(self, m: Manifest) -> str:
        return f"{MODULE_ROOT}.{m.ident}"

    def _import_entry(self, m: Manifest):
        if m.vendor.is_dir():
            path = str(m.vendor)
            if path not in sys.path:
                sys.path.insert(0, path)
            self._vendor_paths[m.slug] = path
        if MODULE_ROOT not in sys.modules:
            root = types.ModuleType(MODULE_ROOT)
            root.__path__ = []
            sys.modules[MODULE_ROOT] = root
        prefix = self._module_prefix(m)
        pkg = types.ModuleType(prefix)
        pkg.__path__ = [str(m.path)]
        pkg.__package__ = prefix
        sys.modules[prefix] = pkg
        mod_name, cls_name = m.entry.split(":")
        mod = importlib.import_module(f"{prefix}.{mod_name}")
        return getattr(mod, cls_name)

    def _purge_modules(self, m: Manifest) -> None:
        prefix = self._module_prefix(m)
        for name in [n for n in sys.modules if n == prefix or n.startswith(prefix + ".")]:
            del sys.modules[name]
        vendor = self._vendor_paths.pop(m.slug, None)
        if vendor is not None:
            while vendor in sys.path:
                sys.path.remove(vendor)
            sys.path_importer_cache.pop(vendor, None)
            under = vendor + os.sep
            for name, mod in list(sys.modules.items()):
                where = getattr(mod, "__file__", None) or ""
                if where.startswith(under):
                    del sys.modules[name]
        importlib.invalidate_caches()

    def _start(self, rec: PluginRecord) -> bool:
        m = rec.manifest
        why = rec.blocked()
        if why:
            rec.status = why
            self.changed.emit(m.slug)
            return False
        rec.errors.clear()
        ctx = PluginContext(self, m, rec.settings)
        self._write_stamp(m.slug)
        try:
            cls = self._import_entry(m)
            instance = cls()
            rec.ctx = ctx
            instance.start(ctx)
        except Exception as exc:  # noqa: BLE001 - a broken plugin can't take tide down
            log.exception("plugin %s failed to start", m.slug)
            ctx._dispose()
            rec.ctx = None
            rec.instance = None
            self._purge_modules(m)
            self._clear_stamp()
            rec.status = f"crashed: {exc}"[:200]
            self.changed.emit(m.slug)
            self.notice.emit(f"{m.name} didn't start: {exc}"[:160])
            return False
        self._clear_stamp()
        rec.instance = instance
        rec.status = "on"
        self.changed.emit(m.slug)
        log.info("plugin %s on", m.slug)
        return True

    def _stop(self, rec: PluginRecord, *, status: str = "off") -> None:
        m = rec.manifest
        stop = getattr(rec.instance, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:  # noqa: BLE001 - still tear it down
                log.exception("plugin %s failed to stop cleanly", m.slug)
        if rec.ctx is not None:
            rec.ctx._dispose()
        rec.ctx = None
        rec.instance = None
        rec.detail = ""
        self._purge_modules(m)
        rec.status = status
        self.changed.emit(m.slug)
        log.info("plugin %s off", m.slug)

    # ---- safe mode ----

    def _write_stamp(self, slug: str) -> None:
        try:
            self.stamp.parent.mkdir(parents=True, exist_ok=True)
            self.stamp.write_text(slug, encoding="utf-8")
        except OSError:
            pass

    def _clear_stamp(self) -> None:
        try:
            self.stamp.unlink(missing_ok=True)
        except OSError:
            pass

    def _read_stamp(self) -> str:
        try:
            return self.stamp.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    # ---- errors ----

    def install_hooks(self) -> None:
        """count uncaught exceptions from plugin code (qt slots, timers and
        threads end up here) against the plugin they came from."""
        if self._hooks_installed:
            return
        self._hooks_installed = True
        self._prev_excepthook = sys.excepthook
        self._prev_thread_hook = threading.excepthook

        def hook(exc_type, exc, tb):
            if not self._attribute(exc_type, exc, tb):
                self._prev_excepthook(exc_type, exc, tb)

        def thread_hook(args):
            if not self._attribute(args.exc_type, args.exc_value, args.exc_traceback):
                self._prev_thread_hook(args)

        sys.excepthook = hook
        threading.excepthook = thread_hook

    def uninstall_hooks(self) -> None:
        if not self._hooks_installed:
            return
        sys.excepthook = self._prev_excepthook
        threading.excepthook = self._prev_thread_hook
        self._hooks_installed = False

    def _owner(self, tb) -> PluginRecord | None:
        paths = [(str(r.manifest.path) + os.sep, r) for r in self._records.values() if r.running]
        while tb is not None:
            fname = tb.tb_frame.f_code.co_filename
            for prefix, rec in paths:
                if fname.startswith(prefix):
                    return rec
            tb = tb.tb_next
        return None

    def _attribute(self, exc_type, exc, tb) -> bool:
        rec = self._owner(tb)
        if rec is None:
            return False
        self._plugin_error(rec.manifest.slug, (exc_type, exc, tb))
        return True

    def _plugin_error(self, slug: str, exc_info) -> None:
        """one uncaught error from a plugin: logged the first time, counted
        always. any thread may call this."""
        rec = self._records.get(slug)
        if rec is None:
            return
        exc_type, exc, tb = exc_info
        key = f"{slug}:{exc_type.__name__}:{exc}"
        if key not in self._logged:
            self._logged.add(key)
            log.error("plugin %s: %s: %s", slug, exc_type.__name__, exc, exc_info=exc_info)
        self._write_error(slug, exc_info)
        now = time.monotonic()
        rec.errors.append(now)
        while rec.errors and now - rec.errors[0] > ERROR_WINDOW_S:
            rec.errors.popleft()
        if len(rec.errors) >= ERROR_LIMIT:
            rec.errors.clear()
            self._too_many.emit(slug)

    def error_log(self, slug: str) -> Path:
        return self.data_dir / slug / "errors.log"

    def _write_error(self, slug: str, exc_info) -> None:
        """every error with its traceback, in the plugin's data folder.
        tide's own log goes to whatever terminal started it, which is
        often none, and then a plugin switching itself off left no trace
        (2026-10-19)."""
        import traceback
        path = self.error_log(slug)
        text = (time.strftime("%Y-%m-%d %H:%M:%S ") + threading.current_thread().name + "\n"
                + "".join(traceback.format_exception(*exc_info)) + "\n")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "a"
            if path.exists() and path.stat().st_size > ERROR_LOG_BYTES:
                mode = "w"
            with open(path, mode, encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass

    def _switch_off_for_errors(self, slug: str) -> None:
        rec = self._records.get(slug)
        if rec is None or not rec.running:
            return
        self._enabled[slug] = False
        self._save_state()
        self._stop(rec, status="off: it kept failing")
        self.notice.emit(f"{rec.manifest.name} kept failing. turned it off "
                         f"(what went wrong is in {self.error_log(slug)})")


_host: PluginHost | None = None


def host() -> PluginHost | None:
    """the app's host, once app.run made one."""
    return _host


def set_host(h: PluginHost | None) -> None:
    global _host
    _host = h

