"""Features that left tide's core for the plugin store, and the people
who had them set up (design/plugin-store.md, "discord and listenbrainz
moving out"; listenbrainz was dropped instead, 2026-10-21, so a token
left in settings.toml just goes with the next save).

At launch, before anything saves settings.toml (Settings drops keys it
no longer has), ``collect()`` copies a moved feature's old keys into that
plugin's settings file and puts the plugin on a pending list. The store
installs what's pending once it can reach the index, says so in a toast,
and tries again next launch when it can't. Onboarding uses the same list
for the integrations a new user picks.

Each feature is looked at once: after that it's on the ``done`` list, so
uninstalling the plugin later sticks.
"""
from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config

log = logging.getLogger("tide.plugins")


@dataclass(frozen=True)
class Moved:
    slug: str
    name: str
    # was it set up? (reads the raw settings.toml table)
    used: Callable[[dict], bool]
    # old settings key -> the plugin's setting key
    keys: dict[str, str]


def _on_with(flag: str, value: str) -> Callable[[dict], bool]:
    return lambda raw: raw.get(flag) is True and bool(str(raw.get(value, "") or "").strip())


def _customised(raw: dict) -> bool:
    """you made something with the editors: a glyph swap, a rebound key, or
    a theme of your own in ~/.config/tide/themes."""
    if raw.get("glyph_overrides") or raw.get("keymap"):
        return True
    try:
        return any(p.is_dir() for p in config.USER_THEMES_DIR.iterdir())
    except OSError:
        return False


_SLOTS_SHOWN = {"single": 1, "split": 2, "stack": 2, "trio": 3, "quad": 4}


def _tile_shown(*kinds: str) -> Callable[[dict], bool]:
    """one of ``kinds`` in a visualizer slot the layout actually shows
    (a quad's fourth tile is no pick when the layout is a stack)."""
    def used(raw: dict) -> bool:
        layout = raw.get("visualizer_layout", "stack")
        shown = _SLOTS_SHOWN.get(layout if isinstance(layout, str) else "", 2)
        return any(raw.get(f"visualizer_tile_{i}") in kinds for i in range(1, shown + 1))
    return used


def _source_on(*slugs: str, needs: str = "") -> Callable[[dict], bool]:
    """one of ``slugs`` switched on in the source panel and, with
    ``needs``, that setting filled in (or the source was the active one:
    local files on the default ~/Music had no folder saved)."""
    def used(raw: dict) -> bool:
        on = raw.get("sources_enabled")
        if not isinstance(on, dict) or not any(on.get(s) is True for s in slugs):
            return False
        if not needs or raw.get("active_source") in slugs:
            return True
        return bool(str(raw.get(needs, "") or "").strip())
    return used


MOVED: tuple[Moved, ...] = (
    Moved("discord", "discord presence", _on_with("discord_enabled", "discord_app_id"), {
        "discord_app_id": "app_id",
        "discord_lyrics_enabled": "lyrics",
        "discord_details_template": "details_template",
        "discord_state_template": "state_template",
        "discord_show_paused": "show_paused",
        "discord_show_progress": "show_progress",
        "discord_activity_type": "activity_type",
        "discord_status_display": "status_display",
        "discord_links": "links",
        "discord_hidden": "hidden",
        "discord_hide_local": "hide_local",
    }),
    Moved("extra-sources", "extra sources", _source_on("soundcloud", "bandcamp", "mixcloud"), {}),
    Moved("powertools", "tide powertools", _customised, {}),
    Moved("lyric-timing", "precise lyric timing", lambda raw: raw.get("lyrics_precise") is True, {}),
    Moved("vis-tiles", "more visualizer tiles",
          _tile_shown("river", "spectrogram", "halo", "tapper"), {
              "visualizer_river_view": "river_view",
              "visualizer_spectrogram_history": "spectrogram_history",
              "visualizer_spectrogram_contrast": "spectrogram_contrast",
              "visualizer_halo_spokes": "halo_spokes",
              "visualizer_halo_punch": "halo_punch",
              "visualizer_tapper_target": "tapper_target",
              "visualizer_tapper_window": "tapper_window",
              "visualizer_tapper_offset": "tapper_offset",
          }),
    Moved("local-files", "local files", _source_on("local", needs="local_music_dir"), {
        "local_music_dir": "folder",
        "local_auto_index": "watch",
    }),
)
_BY_SLUG = {m.slug: m for m in MOVED}

# sources that left core, and the plugin that brings each back
SOURCE_PLUGINS: dict[str, str] = {
    "soundcloud": "extra-sources",
    "bandcamp": "extra-sources",
    "mixcloud": "extra-sources",
    "local": "local-files",
}


@dataclass
class Pending:
    install: list[str]
    moved: list[str]       # installs that get the "is a plugin now" toast
    done: list[str]


def _read_pending(path: Path) -> Pending:
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        raw = {}

    def strs(key: str) -> list[str]:
        v = raw.get(key, [])
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    return Pending(strs("install"), strs("moved"), strs("done"))


def _write_pending(path: Path, p: Pending) -> None:
    from .plugin_host import _toml_value, _write_private
    lines = [f"{k} = {_toml_value(v)}" for k, v in
             (("install", p.install), ("moved", p.moved), ("done", p.done))]
    _write_private(path, "# plugins tide will install from the store once it can\n"
                   + "\n".join(lines) + "\n")


def _write_settings(path: Path, values: dict) -> None:
    """a plugin's settings file, unless it has one already (a reinstall
    after a downgrade mustn't overwrite what the user set since)."""
    from .plugin_host import _toml_value, _write_private
    if path.exists() or not values:
        return
    _write_private(path, "\n".join(f"{k} = {_toml_value(v)}" for k, v in values.items()) + "\n")


def collect(settings_file: Path | None = None, settings_dir: Path | None = None,
            pending_file: Path | None = None) -> list[str]:
    """carry moved features' settings over; returns the slugs it queued."""
    settings_file = settings_file or config.SETTINGS_FILE
    settings_dir = settings_dir or config.PLUGIN_SETTINGS_DIR
    pending_file = pending_file or config.PLUGIN_PENDING_FILE
    try:
        with open(settings_file, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    p = _read_pending(pending_file)
    queued = []
    looked = False
    for m in MOVED:
        if m.slug in p.done:
            continue
        p.done.append(m.slug)
        looked = True
        if not m.used(raw):
            continue
        values = {new: raw[old] for old, new in m.keys.items() if old in raw}
        _write_settings(settings_dir / f"{m.slug}.toml", values)
        if m.slug not in p.install:
            p.install.append(m.slug)
            p.moved.append(m.slug)
        queued.append(m.slug)
        log.info("%s moved to the plugin store; it'll be installed", m.slug)
    if looked:
        _write_pending(pending_file, p)
    return queued


def queue_install(slug: str, values: dict | None = None, *, settings_dir: Path | None = None,
                  pending_file: Path | None = None) -> None:
    """install ``slug`` from the store as soon as it can (onboarding's
    integrations step). ``values`` seed its settings."""
    settings_dir = settings_dir or config.PLUGIN_SETTINGS_DIR
    pending_file = pending_file or config.PLUGIN_PENDING_FILE
    p = _read_pending(pending_file)
    if slug not in p.install:
        p.install.append(slug)
    if slug not in p.done:
        p.done.append(slug)
    _write_settings(settings_dir / f"{slug}.toml", dict(values or {}))
    _write_pending(pending_file, p)


def pending(pending_file: Path | None = None) -> Pending:
    return _read_pending(pending_file or config.PLUGIN_PENDING_FILE)


def finished(slug: str, pending_file: Path | None = None) -> None:
    """``slug`` is installed (or was already): off the list."""
    pending_file = pending_file or config.PLUGIN_PENDING_FILE
    p = _read_pending(pending_file)
    if slug in p.install or slug in p.moved:
        p.install = [s for s in p.install if s != slug]
        p.moved = [s for s in p.moved if s != slug]
        _write_pending(pending_file, p)


def toast_for(slug: str, moved: bool, name: str) -> str:
    if moved and slug in _BY_SLUG:
        return f"{_BY_SLUG[slug].name} is a plugin now. we installed it for you."
    return f"installed {name}"
