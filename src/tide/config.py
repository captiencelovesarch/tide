"""XDG paths and where the app keeps its state."""
from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "tide"


def _xdg(env: str, default: str) -> Path:
    return Path(os.environ.get(env) or Path.home() / default)


CONFIG_DIR: Path = _xdg("XDG_CONFIG_HOME", ".config") / APP_NAME
CACHE_DIR: Path = _xdg("XDG_CACHE_HOME", ".cache") / APP_NAME
DATA_DIR: Path = _xdg("XDG_DATA_HOME", ".local/share") / APP_NAME

OAUTH_FILE: Path = CONFIG_DIR / "oauth.json"
BROWSER_AUTH_FILE: Path = CONFIG_DIR / "browser.json"
WEBVIEW_PROFILE_DIR: Path = DATA_DIR / "webview"
SETTINGS_FILE: Path = CONFIG_DIR / "settings.toml"
STREAM_CACHE_FILE: Path = CACHE_DIR / "streams.json"
ART_CACHE_DIR: Path = CACHE_DIR / "art"
LYRICS_CACHE_DIR: Path = CACHE_DIR / "lyrics"
USER_THEMES_DIR: Path = CONFIG_DIR / "themes"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, CACHE_DIR, DATA_DIR, ART_CACHE_DIR, LYRICS_CACHE_DIR, USER_THEMES_DIR, WEBVIEW_PROFILE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    try:
        CONFIG_DIR.chmod(0o700)
    except OSError:
        pass
