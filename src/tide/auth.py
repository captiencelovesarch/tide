"""Authentication for YouTube Music.

Google's OAuth flows no longer work against YouTube Music (TV-device tokens
are refused on WEB_REMIX, and Google blocks sign-in inside embedded
webviews), so a signed-in browser session is the only way in. The sign-in
wizard imports one from the user's own browser (browser_import), or from
pasted request headers, into ~/.config/tide/browser.json.

That import only seeds the session. yt_session keeps it alive from then on:
one live cookie jar shared by ytmusicapi and yt-dlp, rotated on Google's
schedule, so tide's copy doesn't go stale while the app runs.
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

    `cookies` is a name->value dict from a browser import or a paste.

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


# ytmusicapi fetches the whole music.youtube.com page on construction just
# to read one value (VISITOR_DATA) unless the headers already carry it.
# That fetch ran on the GUI thread before the window came up: ~1.2s on a
# good connection, with no timeout on a bad one. The value is derived from
# the VISITOR_INFO1_LIVE cookie, which YouTube keeps for months, so a
# cached copy is as good as a fresh one.
_VISITOR_NS = "yt-visitor"
_VISITOR_TTL = 30 * 24 * 3600
_VISITOR_HEADER = "X-Goog-Visitor-Id"


def _auth_stamp() -> int:
    try:
        return config.BROWSER_AUTH_FILE.stat().st_mtime_ns
    except OSError:
        return 0


def yt_client() -> YTMusic:
    """Return an authenticated YTMusic client, or raise if no auth is saved."""
    if not config.BROWSER_AUTH_FILE.is_file():
        raise RuntimeError("not signed in")
    from . import cache
    try:
        headers = json.loads(config.BROWSER_AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        headers = None
    if not isinstance(headers, dict):
        # let ytmusicapi raise its own error about the file
        return YTMusic(auth=str(config.BROWSER_AUTH_FILE))
    stamp = _auth_stamp()
    # keyed to this sign-in: a fresh browser.json fetches its own id
    hit = cache.get_json(_VISITOR_NS, "id")
    cached = hit.get("id") if isinstance(hit, dict) and hit.get("auth") == stamp else None
    if cached and not any(k.lower() == _VISITOR_HEADER.lower() for k in headers):
        headers[_VISITOR_HEADER] = cached
    from . import yt_session
    live = yt_session.session()
    yt = YTMusic(auth=headers, requests_session=live)
    if live is not None:
        # ytmusicapi read SAPISID from the header at init (it doesn't
        # rotate); every request from here on carries the live jar instead
        # of the import-time snapshot.
        yt.base_headers.pop("cookie", None)
    if not cached:
        fetched = yt.base_headers.get(_VISITOR_HEADER)
        if fetched:
            cache.put_json(_VISITOR_NS, "id", {"id": fetched, "auth": stamp},
                           _VISITOR_TTL)
    return yt


def yt_dlp_cookiefile() -> str | None:
    """The live session as a Netscape cookie file (path) for yt-dlp, or
    ``None`` if the user isn't signed in.

    Stream resolution runs through yt-dlp, which by default talks to YouTube
    *anonymously* — that's what trips "Sign in to confirm you're not a bot",
    age-gates, and premium/region blocks. Handing it the session lets it
    authenticate as the user with no browser running at all, and unlocks
    higher-bitrate formats too. The file is yt_session's jar; resolves work
    on a copy of it and merge back what YouTube rotated.
    """
    from . import yt_session
    if yt_session.jar() is None:
        return None
    yt_session.save()
    path = yt_session.jar_file()
    return str(path) if path.is_file() else None


def parse_pasted_headers(raw: str) -> tuple[dict[str, str], str | None]:
    """Turn a browser "copy request headers" paste into (cookies, user_agent).

    The manual sign-in path, for when tide can't read the browser on its own:
    a browser it doesn't know, a keyring it can't unlock, or cookies that
    rotate too fast to catch. The user signs in at music.youtube.com, opens
    devtools, and copies the request headers of any music.youtube.com call.
    Those headers carry the httpOnly auth cookies that page JavaScript can't
    read, which is exactly the session we need.

    Accepts Chrome / Firefox "name: value" per-line dumps (pseudo-headers like
    ":authority:" are skipped), or a bare "k=v; k2=v2" cookie string. Raises
    ValueError with a plain, user-facing message when there's no usable
    session in the paste.
    """
    cookie_line = ""
    user_agent: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(":"):
            continue
        key, sep, value = stripped.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "cookie" and value:
            cookie_line = value
        elif key == "user-agent" and value:
            user_agent = value

    if not cookie_line:
        # They may have pasted just the cookie string itself, no header name.
        if "=" in raw and REQUIRED_COOKIE in raw:
            cookie_line = " ".join(raw.split())
        else:
            raise ValueError(
                "no cookie header in the pasted text. copy the request headers "
                "of a music.youtube.com call (not the response)."
            )

    cookies: dict[str, str] = {}
    for part in cookie_line.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, val = part.partition("=")
        name = name.strip()
        if name:
            cookies[name] = val.strip()

    if REQUIRED_COOKIE not in cookies:
        raise ValueError(
            "the paste isn't a signed-in session (no "
            f"{REQUIRED_COOKIE}). sign in at music.youtube.com first, then copy "
            "the request headers again."
        )
    return cookies, user_agent


def clear_saved_auth() -> None:
    config.BROWSER_AUTH_FILE.unlink(missing_ok=True)
    _meta_file().unlink(missing_ok=True)
    # Old, broken OAuth file from earlier dev — clean it up too.
    config.OAUTH_FILE.unlink(missing_ok=True)
    # Drop the live jar so a signed-out user resolves streams anonymously
    # again (and a re-sign-in seeds it fresh), with its rotation record.
    (config.CONFIG_DIR / "yt_cookies.txt").unlink(missing_ok=True)
    (config.CONFIG_DIR / "yt_session.json").unlink(missing_ok=True)
