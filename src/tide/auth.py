"""Authentication for YouTube Music.

We use **browser-cookie auth** as the primary path. YouTube's API regressed
the OAuth (TV-device) flow against music.youtube.com search endpoints in
mid-2024, returning HTTP 400 for WEB_REMIX requests with Bearer tokens.
Browser-cookie auth remains the reliable path.

To stay GUI-only (no config-file digging), the sign-in wizard embeds a
QtWebEngineView pointed at music.youtube.com. The user logs in normally;
we harvest cookies from the webview profile and write a ytmusicapi-compatible
headers dict to ~/.config/tide/browser.json.
"""
from __future__ import annotations

import json
from pathlib import Path

from ytmusicapi import YTMusic
from ytmusicapi.helpers import USER_AGENT, YTM_DOMAIN

from . import config


REQUIRED_COOKIE = "__Secure-3PAPISID"


def have_auth() -> bool:
    return config.BROWSER_AUTH_FILE.is_file()


def save_browser_auth(cookies: dict[str, str], user_agent: str | None = None) -> Path:
    """Persist a browser-style auth dict that ytmusicapi can consume.

    `cookies` is a name->value dict harvested from the embedded webview.
    """
    if REQUIRED_COOKIE not in cookies:
        raise ValueError(f"missing required cookie {REQUIRED_COOKIE} — user not fully signed in")

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = {
        "cookie": cookie_header,
        # ytmusicapi recomputes the real SAPISIDHASH at request time, but it
        # checks the "authorization" header *value* contains "SAPISIDHASH"
        # to detect BROWSER auth type. Any placeholder with that token works.
        "authorization": "SAPISIDHASH placeholder",
        "x-goog-authuser": "0",
        "origin": YTM_DOMAIN,
        "user-agent": user_agent or USER_AGENT,
        "accept": "*/*",
        "accept-encoding": "gzip, deflate",
        "content-type": "application/json",
        "content-encoding": "gzip",
    }

    config.BROWSER_AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.BROWSER_AUTH_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(headers, f, indent=2, sort_keys=True)
    tmp.replace(config.BROWSER_AUTH_FILE)
    try:
        config.BROWSER_AUTH_FILE.chmod(0o600)
    except OSError:
        pass
    return config.BROWSER_AUTH_FILE


def yt_client() -> YTMusic:
    """Return an authenticated YTMusic client, or raise if no auth is saved."""
    if not config.BROWSER_AUTH_FILE.is_file():
        raise RuntimeError("not signed in")
    return YTMusic(auth=str(config.BROWSER_AUTH_FILE))


def clear_saved_auth() -> None:
    config.BROWSER_AUTH_FILE.unlink(missing_ok=True)
    # Old, broken OAuth file from earlier dev — clean it up too.
    config.OAUTH_FILE.unlink(missing_ok=True)
