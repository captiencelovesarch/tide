"""Read YouTube Music cookies out of the user's real browser.

Google rejects every other way in. Credential entry inside an embedded
webview is blocked outright, and third-party OAuth stopped working against
the YouTube Music endpoints in late 2024: yt-dlp removed its OAuth login,
and ytmusicapi's OAuth path has answered HTTP 400 since September 2025 with
its maintainer calling it a dead end. So the only session tide can use is
the one the user already has in a browser they trust.

The reading itself is delegated to yt-dlp's cookie loader
(``yt_dlp.cookies.extract_cookies_from_browser``). It knows the Chromium
and Firefox database layouts, Chromium's per-desktop key storage (kwallet
5/6 through kwallet-query, gnome-keyring through secretstorage, plain text
when there is no keyring), and it is fixed upstream within days of a
browser changing any of that. tide used to carry its own copy of all of
it, so a Chromium schema bump or a new keyring would have killed sign-in
until someone noticed. This module now only decides which browsers are
present, hands yt-dlp the profile to read, scopes the jar to what a browser
would actually send music.youtube.com, and records when the session lapses.

The user never sees a config file. From their side it is "sign in to YT
Music in your browser, then click import".
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from yt_dlp.cookies import extract_cookies_from_browser


class ImportError_(RuntimeError):
    pass


# ---------- which browsers are on this machine ----------


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


@dataclass(frozen=True)
class _Candidate:
    slug: str                    # yt-dlp browser name
    label: str                   # picker text
    family: str                  # "chromium" or "firefox"
    config: tuple[str, ...]      # roots under $XDG_CONFIG_HOME
    home: tuple[str, ...] = ()   # roots under $HOME (flatpak, snap, legacy)


# Order matters: first match is the picker's default suggestion. Native and
# sandboxed installs of the same browser share one entry; every cookie db
# found under any of the roots is a candidate, newest first.
_CANDIDATES: tuple[_Candidate, ...] = (
    _Candidate("chromium", "chromium", "chromium", ("chromium",),
               (".var/app/org.chromium.Chromium/config/chromium",)),
    _Candidate("chrome", "google chrome", "chromium", ("google-chrome",),
               (".var/app/com.google.Chrome/config/google-chrome",)),
    _Candidate("brave", "brave", "chromium", ("BraveSoftware/Brave-Browser",),
               (".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser",)),
    _Candidate("vivaldi", "vivaldi", "chromium", ("vivaldi",),
               (".var/app/com.vivaldi.Vivaldi/config/vivaldi",)),
    _Candidate("edge", "microsoft edge", "chromium", ("microsoft-edge",),
               (".var/app/com.microsoft.Edge/config/microsoft-edge",)),
    _Candidate("opera", "opera", "chromium", ("opera",)),
    _Candidate("whale", "whale", "chromium", ("naver-whale",)),
    _Candidate("firefox", "firefox", "firefox",
               ("mozilla/firefox",),          # FF147+ follows XDG
               (".mozilla/firefox",           # everything before that
                ".var/app/org.mozilla.firefox/config/mozilla/firefox",
                ".var/app/org.mozilla.firefox/.mozilla/firefox",
                "snap/firefox/common/.mozilla/firefox")),
)

# Where each family keeps the cookie db, relative to a root. Chromium moved
# it from <profile>/Cookies to <profile>/Network/Cookies in v96; Opera has
# no profile dirs at all; Firefox profiles are named "<hash>.default-release".
_DB_PATTERNS: dict[str, tuple[str, ...]] = {
    "chromium": ("Cookies", "*/Cookies", "*/Network/Cookies"),
    "firefox": ("cookies.sqlite", "*/cookies.sqlite", "Profiles/*/cookies.sqlite"),
}


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _cookie_dbs(cand: _Candidate) -> list[Path]:
    roots = [_config_home() / rel for rel in cand.config]
    roots += [Path.home() / rel for rel in cand.home]
    found: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in _DB_PATTERNS[cand.family]:
            for hit in glob.glob(str(root / pattern)):
                path = Path(hit)
                if path.is_file():
                    found.add(path)
    return sorted(found, key=_mtime, reverse=True)


@dataclass
class BrowserProfile:
    slug: str                            # yt-dlp browser name
    label: str                           # human label
    cookies_path: Path                   # newest cookie db for this browser
    family: str = "chromium"
    candidates: tuple[Path, ...] = ()    # every cookie db found, newest first


def available_profiles() -> list[BrowserProfile]:
    out: list[BrowserProfile] = []
    for cand in _CANDIDATES:
        dbs = _cookie_dbs(cand)
        if dbs:
            out.append(BrowserProfile(
                slug=cand.slug, label=cand.label, cookies_path=dbs[0],
                family=cand.family, candidates=tuple(dbs),
            ))
    return out


# ---------- expiry ----------


# Cookies whose lifetime actually gates an authenticated session. If any one
# of these lapses, ytmusicapi starts 401ing, so the session's effective
# expiry is the EARLIEST of them, not the latest.
AUTH_COOKIES = (
    "__Secure-3PAPISID",
    "__Secure-3PSID",
    "SAPISID",
    "SID",
)

# Chromium stores expires_utc as microseconds since 1601-01-01 (the Windows
# FILETIME epoch), which is 11644473600 seconds before the unix epoch.
_CHROME_EPOCH_OFFSET = 11644473600


def _chrome_time_to_unix(expires_utc: float) -> float | None:
    """Convert a Chromium expires_utc to a unix timestamp. 0 means 'session
    cookie' (dies with the browser) and has no meaningful expiry."""
    if not expires_utc:
        return None
    return expires_utc / 1_000_000 - _CHROME_EPOCH_OFFSET


def _cookie_expiry_unix(expires: object) -> float | None:
    """yt-dlp hands Chromium's expires_utc through untouched (microseconds
    since 1601) and Firefox's expiry as unix seconds. Tell them apart by
    size: unix seconds stay under 1e11 until the year 5138, while Chromium's
    count is past 1e16 for any date after 1970."""
    if not expires:
        return None
    try:
        value = float(expires)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value > 1e11:
        return _chrome_time_to_unix(value)
    return value


# ---------- jar -> result ----------


@dataclass
class ImportResult:
    profile: BrowserProfile
    cookies: dict[str, str] = field(default_factory=dict)
    # Unix timestamp at which this session's earliest auth cookie lapses, or
    # None when every auth cookie is session-scoped / unreadable. Surfaced so
    # tide can warn BEFORE playback starts silently degrading.
    expires_at: float | None = None
    # Set when a signed-out result is tide's problem rather than the user's
    # (encrypted store, no key). The wizard shows it instead of "sign in".
    note: str = ""

    @property
    def looks_signed_in(self) -> bool:
        return "__Secure-3PAPISID" in self.cookies


def _sent_to_music(domain: str) -> bool:
    """Mirror the real browser: domain cookies on .youtube.com plus host-only
    cookies for music.youtube.com itself. Nothing from .google.com (the auth
    cookies exist there too with different values, and mixing them in made
    YouTube answer as signed out), nothing host-only from other youtube.com
    subdomains (a browser would not send those either)."""
    return domain.lower().lstrip(".") in ("youtube.com", "music.youtube.com")


def _result_from_jar(profile: BrowserProfile, jar: Iterable) -> ImportResult:
    # name -> (rank, value, expiry). Duplicate names keep the one that
    # expires last; a session cookie (no expiry) loses to any dated one.
    best: dict[str, tuple[float, str, float | None]] = {}
    for cookie in jar:
        if not _sent_to_music(getattr(cookie, "domain", "") or ""):
            continue
        value = getattr(cookie, "value", "") or ""
        if not value:
            continue
        expiry = _cookie_expiry_unix(getattr(cookie, "expires", None))
        rank = expiry if expiry is not None else float("-inf")
        prev = best.get(cookie.name)
        if prev is None or rank > prev[0]:
            best[cookie.name] = (rank, value, expiry)
    cookies = {name: value for name, (_, value, _) in best.items()}
    expiries = [
        expiry for name, (_, _, expiry) in best.items()
        if name in AUTH_COOKIES and expiry is not None
    ]
    return ImportResult(
        profile=profile,
        cookies=cookies,
        expires_at=min(expiries) if expiries else None,
    )


# ---------- reading through yt-dlp ----------


class _Log:
    """Duck-typed stand-in for yt-dlp's YDLLogger. Keeps every line so the
    caller can tell "signed out" apart from "could not decrypt"."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def debug(self, message: object) -> None:
        self.lines.append(str(message))

    info = debug

    def warning(self, message: object, only_once: bool = False, once: bool = False) -> None:
        self.lines.append(str(message))

    def error(self, message: object, is_error: bool = True) -> None:
        self.lines.append(str(message))

    # Phrases yt-dlp logs when the keyring password was missing or wrong.
    _TROUBLE = (
        "could not be decrypted",
        "no key found",
        "failed to read",
        "kwallet-query",
        "secretstorage not available",
        "exception running kwallet",
    )
    _CHOSEN = re.compile(r"chosen keyring: (\w+)", re.IGNORECASE)

    def key_trouble(self) -> bool:
        return any(t in line.lower() for line in self.lines for t in self._TROUBLE)

    def chosen_keyring(self) -> str | None:
        for line in self.lines:
            m = self._CHOSEN.search(line)
            if m:
                return m.group(1).upper()
        return None


# yt-dlp's keyring names, in the order worth retrying. BASICTEXT is left out
# on purpose: it only unlocks v10 cookies, which every attempt already tries.
_KEYRINGS = ("KWALLET6", "KWALLET5", "GNOMEKEYRING", "KWALLET")

_NO_KEY_NOTE = (
    "its cookie store is encrypted and no keyring key was found. "
    "kde: install kwallet. gnome: install python-secretstorage."
)


def _read_jar(slug: str, profile_dir: Path, keyring: str | None) -> tuple[Iterable, _Log]:
    log = _Log()
    jar = extract_cookies_from_browser(slug, profile=str(profile_dir), logger=log, keyring=keyring)
    return jar, log


def _import_one(profile: BrowserProfile, db: Path) -> ImportResult:
    jar, log = _read_jar(profile.slug, db.parent, None)
    result = _result_from_jar(profile, jar)
    if result.looks_signed_in or profile.family != "chromium" or not log.key_trouble():
        return result
    # Chromium encrypts cookie values with a key parked in the desktop
    # keyring, and yt-dlp picks the keyring from $XDG_CURRENT_DESKTOP the
    # way Chromium itself does. When that guess comes back empty (a bare
    # window manager, a browser started with --password-store=, a wallet
    # that answers only one of the two APIs) try the others before giving
    # up. Each pass re-copies the db; that is cheap next to a wrong answer.
    tried = {log.chosen_keyring()}
    for keyring in _KEYRINGS:
        if keyring in tried:
            continue
        tried.add(keyring)
        try:
            jar, _ = _read_jar(profile.slug, db.parent, keyring)
        except Exception:
            continue
        retry = _result_from_jar(profile, jar)
        if retry.looks_signed_in:
            return retry
    result.note = _NO_KEY_NOTE
    return result


def import_cookies(profile: BrowserProfile) -> ImportResult:
    """Pull YouTube cookies from the given browser.

    Every cookie db found for that browser is tried, newest first, until one
    holds a signed-in session. The user's YT Music profile is not always the
    one they browsed with most recently, and the old importer only ever
    looked at ``Default``. A signed-out result from the newest db is kept as
    the answer when none of them is signed in.

    Blocking (db copies + a keyring round-trip per attempt). Call it off the
    GUI thread.
    """
    dbs = tuple(profile.candidates) or (profile.cookies_path,)
    first_miss: ImportResult | None = None
    last_error: Exception | None = None
    for db in dbs:
        try:
            result = _import_one(profile, db)
        except Exception as exc:
            last_error = exc
            continue
        if result.looks_signed_in:
            return result
        if first_miss is None:
            first_miss = result
    if first_miss is not None:
        return first_miss
    raise ImportError_(f"couldn't read cookies from {profile.label}: {last_error}")
