"""XDG paths and where the app keeps its state."""
from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "tide"

# Set by the flatpak runtime. Inside that sandbox a few things move: the
# single-instance socket, the mpris bus name, the desktop file name, and
# flathub owns updates.
FLATPAK_ID: str | None = os.environ.get("FLATPAK_ID") or None


def in_flatpak() -> bool:
    return FLATPAK_ID is not None


def _xdg(env: str, default: str) -> Path:
    return Path(os.environ.get(env) or Path.home() / default)


CONFIG_DIR: Path = _xdg("XDG_CONFIG_HOME", ".config") / APP_NAME
CACHE_DIR: Path = _xdg("XDG_CACHE_HOME", ".cache") / APP_NAME
DATA_DIR: Path = _xdg("XDG_DATA_HOME", ".local/share") / APP_NAME

OAUTH_FILE: Path = CONFIG_DIR / "oauth.json"
BROWSER_AUTH_FILE: Path = CONFIG_DIR / "browser.json"
SPOTIFY_AUTH_FILE: Path = CONFIG_DIR / "spotify.json"
WEBVIEW_PROFILE_DIR: Path = DATA_DIR / "webview"
SETTINGS_FILE: Path = CONFIG_DIR / "settings.toml"
STREAM_CACHE_FILE: Path = CACHE_DIR / "streams.json"
ART_CACHE_DIR: Path = CACHE_DIR / "art"
LYRICS_CACHE_DIR: Path = CACHE_DIR / "lyrics"
USER_THEMES_DIR: Path = CONFIG_DIR / "themes"
SESSION_FILE: Path = CACHE_DIR / "session.json"
HISTORY_FILE: Path = CACHE_DIR / "history.jsonl"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, CACHE_DIR, DATA_DIR, ART_CACHE_DIR, LYRICS_CACHE_DIR, USER_THEMES_DIR, WEBVIEW_PROFILE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    # Tighten the three top-level state roots to owner-only. CONFIG_DIR holds
    # tokens/passwords; CACHE_DIR and DATA_DIR are just as sensitive — the
    # librespot subprocess writes a reusable Spotify credential file under
    # CACHE_DIR at its own 0644, and a co-user on a distro with 0755 home
    # dirs could read it through a world-traversable cache tree. Hardening
    # the directory boundary (not just per-file modes) closes that whether or
    # not every writer remembers to chmod its own files.
    for d in (CONFIG_DIR, CACHE_DIR, DATA_DIR):
        try:
            d.chmod(0o700)
        except OSError:
            pass
