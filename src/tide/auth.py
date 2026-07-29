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
import os
import tempfile
import time
from pathlib import Path

from ytmusicapi import YTMusic
from ytmusicapi.helpers import USER_AGENT, YTM_DOMAIN


def _write_secret(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` as an owner-only (0600) file.

    Creates the temp with mode 0600 up front (via mkstemp) rather than
    writing at the umask default and chmod-ing afterward — the latter leaves
    a window where the file holding auth cookies is briefly 0644. os.replace
    carries the 0600 onto the destination.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp_name, 0o600)  # mkstemp is already 0600; be explicit
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

from . import config


REQUIRED_COOKIE = "__Secure-3PAPISID"


def have_auth() -> bool:
    return config.BROWSER_AUTH_FILE.is_file()


def save_browser_auth(
    cookies: dict[str, str],
    user_agent: str | None = None,
    expires_at: float | None = None,
) -> Path:
    """Persist a browser-style auth dict that ytmusicapi can consume.

    `cookies` is a name->value dict harvested from the embedded webview.

    `expires_at` is the session's earliest auth-cookie expiry (unix time),
    recorded in a **sidecar** file rather than in browser.json — ytmusicapi
    reads that file as a literal headers dict and sends every key it finds,
    so an extra field would end up on the wire as a bogus HTTP header.
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

    _write_secret(
        config.BROWSER_AUTH_FILE,
        json.dumps(headers, indent=2, sort_keys=True),
    )
    _write_secret(
        _meta_file(),
        json.dumps({"expires_at": expires_at, "imported_at": time.time()}, indent=2),
    )
    return config.BROWSER_AUTH_FILE


def _meta_file() -> Path:
    return config.CONFIG_DIR / "browser_meta.json"


def session_expires_at() -> float | None:
    """Unix time the saved YT Music session lapses, or None if unknown.

    Unknown is the honest answer for sessions imported before tide started
    recording this, and for cookie jars whose auth cookies are all
    session-scoped — callers must not treat None as "expired".
    """
    try:
        data = json.loads(_meta_file().read_text(encoding="utf-8"))
    except Exception:
        return None
    value = data.get("expires_at")
    return float(value) if isinstance(value, (int, float)) else None


def seconds_until_expiry() -> float | None:
    """Seconds left on the saved session, or None if unknown. Negative once
    the recorded expiry has passed."""
    at = session_expires_at()
    return None if at is None else at - time.time()


def refresh_from_browser() -> str | None:
    """Re-harvest cookies from whichever browser still holds a live YouTube
    Music session and overwrite the saved auth. Returns the profile label on
    success, or None if no browser had a usable session.

    This is the whole point of the one-click "refresh token" path: an expired
    tide session almost always means *tide's copy* of the cookies went stale
    while the browser itself is still signed in, so re-importing needs no
    interaction at all. Only when every profile comes back signed-out does the
    user actually have to go log in again.

    Blocking (SQLite reads + a kwallet/libsecret round-trip per profile) —
    call it off the GUI thread.
    """
    from . import browser_import as bi

    last_error: Exception | None = None
    for profile in bi.available_profiles():
        try:
            result = bi.import_cookies(profile)
        except Exception as exc:
            last_error = exc
            continue
        if not result.looks_signed_in:
            continue
        try:
            save_browser_auth(result.cookies, expires_at=result.expires_at)
        except Exception as exc:
            last_error = exc
            continue
        return profile.label
    if last_error is not None and not bi.available_profiles():
        raise last_error
    return None


def yt_client() -> YTMusic:
    """Return an authenticated YTMusic client, or raise if no auth is saved."""
    if not config.BROWSER_AUTH_FILE.is_file():
        raise RuntimeError("not signed in")
    return YTMusic(auth=str(config.BROWSER_AUTH_FILE))


def yt_dlp_cookiefile() -> str | None:
    """Return a Netscape cookie file (path) derived from the saved
    ``browser.json``, or ``None`` if the user isn't signed in.

    Stream resolution runs through yt-dlp, which by default talks to YouTube
    *anonymously* — that's what trips "Sign in to confirm you're not a bot",
    age-gates, and premium/region blocks, and it's why playback felt like it
    needed a logged-in browser tab open. Handing yt-dlp the cookies we already
    harvested at sign-in lets it authenticate as the user with **no browser
    running at all**, and unlocks higher-bitrate formats too.

    The file is regenerated only when ``browser.json`` is newer than it, so the
    common case is a cheap mtime check. Written to the config dir (not
    ``browser.json`` itself) because yt-dlp rewrites the cookie file as cookies
    rotate — we don't want it clobbering the ytmusicapi auth blob.
    """
    src = config.BROWSER_AUTH_FILE
    if not src.is_file():
        return None
    out = config.CONFIG_DIR / "yt_cookies.txt"
    try:
        if out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
            return str(out)
    except OSError:
        pass
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception:
        return None
    cookie_header = data.get("cookie") or ""
    pairs: list[tuple[str, str]] = []
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            pairs.append((name, value.strip()))
    if not pairs:
        return None
    # Netscape cookie format: domain, include_subdomains, path, secure,
    # expiry, name, value. All Google auth cookies live on .youtube.com; a
    # far-future expiry keeps yt-dlp from treating them as session-only.
    lines = ["# Netscape HTTP Cookie File", "# generated by tide — do not edit", ""]
    for name, value in pairs:
        lines.append("\t".join([".youtube.com", "TRUE", "/", "TRUE", "2000000000", name, value]))
    try:
        _write_secret(out, "\n".join(lines) + "\n")
    except OSError:
        return None
    return str(out)


def clear_saved_auth() -> None:
    config.BROWSER_AUTH_FILE.unlink(missing_ok=True)
    _meta_file().unlink(missing_ok=True)
    # Old, broken OAuth file from earlier dev — clean it up too.
    config.OAUTH_FILE.unlink(missing_ok=True)
    # Drop the derived yt-dlp cookie jar so a signed-out user resolves
    # streams anonymously again (and a re-sign-in regenerates it fresh).
    (config.CONFIG_DIR / "yt_cookies.txt").unlink(missing_ok=True)
