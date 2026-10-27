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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from yt_dlp.cookies import extract_cookies_from_browser

from . import config


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
    windows: tuple[str, ...] = ()  # "<ENV VAR>/<path>", e.g. LOCALAPPDATA/...
    reader: str = ""             # yt-dlp's name for it, when slug isn't one


# Order matters: first match is the picker's default suggestion. Native and
# sandboxed installs of the same browser share one entry; every cookie db
# found under any of the roots is a candidate, newest first.
_CANDIDATES: tuple[_Candidate, ...] = (
    _Candidate("chromium", "chromium", "chromium", ("chromium",),
               (".var/app/org.chromium.Chromium/config/chromium",),
               ("LOCALAPPDATA/Chromium/User Data",)),
    # helium (imput) is chromium underneath, down to the "Chromium Safe
    # Storage" keyring entry, so yt-dlp's chromium reader opens it
    _Candidate("helium", "helium", "chromium", ("net.imput.helium",),
               (".var/app/net.imput.helium/config/net.imput.helium",), reader="chromium"),
    _Candidate("chrome", "google chrome", "chromium", ("google-chrome",),
               (".var/app/com.google.Chrome/config/google-chrome",),
               ("LOCALAPPDATA/Google/Chrome/User Data",)),
    _Candidate("brave", "brave", "chromium", ("BraveSoftware/Brave-Browser",),
               (".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser",),
               ("LOCALAPPDATA/BraveSoftware/Brave-Browser/User Data",)),
    _Candidate("vivaldi", "vivaldi", "chromium", ("vivaldi",),
               (".var/app/com.vivaldi.Vivaldi/config/vivaldi",),
               ("LOCALAPPDATA/Vivaldi/User Data",)),
    _Candidate("edge", "microsoft edge", "chromium", ("microsoft-edge",),
               (".var/app/com.microsoft.Edge/config/microsoft-edge",),
               ("LOCALAPPDATA/Microsoft/Edge/User Data",)),
    _Candidate("opera", "opera", "chromium", ("opera",), (),
               ("APPDATA/Opera Software/Opera Stable",
                "APPDATA/Opera Software/Opera GX Stable")),
    _Candidate("whale", "whale", "chromium", ("naver-whale",), (),
               ("LOCALAPPDATA/Naver/Naver Whale/User Data",)),
    _Candidate("firefox", "firefox", "firefox",
               ("mozilla/firefox",),          # FF147+ follows XDG
               (".mozilla/firefox",           # everything before that
                ".var/app/org.mozilla.firefox/config/mozilla/firefox",
                ".var/app/org.mozilla.firefox/.mozilla/firefox",
                "snap/firefox/common/.mozilla/firefox"),
               ("APPDATA/Mozilla/Firefox",)),
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
    if sys.platform == "win32":
        roots = []
        for spec in cand.windows:
            env, _, rel = spec.partition("/")
            if os.environ.get(env):
                roots.append(Path(os.environ[env]) / rel)
    else:
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
    reader: str = ""                     # yt-dlp's name, when slug isn't one

    @property
    def ytdlp_name(self) -> str:
        return self.reader or self.slug


def available_profiles() -> list[BrowserProfile]:
    out: list[BrowserProfile] = []
    for cand in _CANDIDATES:
        if sys.platform == "win32" and cand.family == "chromium":
            # locked while the browser runs (edge never really stops) and
            # encrypted for the browser itself: an import can't work, so
            # it isn't offered. the sign-in window covers these browsers.
            continue
        dbs = _cookie_dbs(cand)
        if dbs:
            out.append(BrowserProfile(
                slug=cand.slug, label=cand.label, cookies_path=dbs[0],
                family=cand.family, candidates=tuple(dbs), reader=cand.reader,
            ))
    return out


# ---------- expiry ----------


# Cookies whose lifetime actually gates an authenticated session. If any one
# of these lapses, ytmusicapi starts 401ing, so the session's effective
# expiry is the EARLIEST of them, not the latest.
# one of these besides __Secure-3PAPISID: the session itself, not just
# cross-site leftovers (see ImportResult.looks_signed_in)
_FIRST_PARTY = ("SAPISID", "SID", "__Secure-1PSID")

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
        # the cross-site pair alone isn't a session: a browser that was
        # never signed in to youtube (or signed out) can keep a stray
        # __Secure-3PAPISID, and importing that made youtube answer as
        # signed out. a real session has a first-party one too.
        return ("__Secure-3PAPISID" in self.cookies
                and any(k in self.cookies for k in _FIRST_PARTY))


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
        "failed to decrypt with dpapi",   # windows
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


_WINDOWS_LOCKED_NOTE = (
    "chrome and edge on windows keep their cookies locked away from other "
    "apps. sign in to youtube music in firefox, then import from firefox."
)


def _read_jar(slug: str, profile_dir: Path, keyring: str | None) -> tuple[Iterable, _Log]:
    log = _Log()
    if sys.platform == "win32" and profile_dir.name == "Network":
        # yt-dlp takes the browser root (where "Local State" and its key
        # live) as the profile's parent, so hand it the profile itself
        profile_dir = profile_dir.parent
    jar = extract_cookies_from_browser(slug, profile=str(profile_dir), logger=log, keyring=keyring)
    return jar, log


def _import_one(profile: BrowserProfile, db: Path) -> ImportResult:
    jar, log = _read_jar(profile.ytdlp_name, db.parent, None)
    result = _result_from_jar(profile, jar)
    if sys.platform == "win32":
        # no keyrings to retry; signed out with trouble means app-bound
        # encryption, which yt-dlp can't open
        if not result.looks_signed_in and profile.family == "chromium" and log.key_trouble():
            result.note = _WINDOWS_LOCKED_NOTE
        return result
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
            jar, _ = _read_jar(profile.ytdlp_name, db.parent, keyring)
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
    if sys.platform == "win32" and profile.family == "chromium":
        # the browser holds its cookie file open while it runs
        raise ImportError_(
            f"couldn't read {profile.label}. close it completely and try "
            "again, or sign in with firefox and import from there."
        )
    raise ImportError_(f"couldn't read cookies from {profile.label}: {last_error}")


# ---------- sign-in window ----------
#
# On Windows the cookie files are a dead end: Chrome, Edge and Opera hold
# them locked while they run (often in the background with no window), and
# Chrome encrypts them so only Chrome itself can read them. So tide opens
# the user's own browser on a fresh profile of its own, with the DevTools
# port on, and lets them sign in there like anywhere else. The browser
# hands over its cookies already decrypted through DevTools, and the
# window closes itself once YouTube Music is signed in.

SIGNIN_URL = "https://music.youtube.com/"
SIGNIN_DIR = config.DATA_DIR / "signin-browser"


@dataclass(frozen=True)
class SignInBrowser:
    label: str
    exe: Path


def _app_path(exe_name: str) -> list[Path]:
    """What Windows itself has registered for ``exe_name`` (App Paths)."""
    try:
        import winreg
    except ImportError:
        return []
    out = []
    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key) as k:
                out.append(Path(winreg.QueryValue(k, None).strip('"')))
        except OSError:
            pass
    return out


def signin_browsers() -> list[SignInBrowser]:
    """Browsers that can host the sign-in window, best first. Edge comes
    with every Windows install, so there's nearly always one."""
    if sys.platform != "win32":
        return []
    env = os.environ
    roots = [env.get(v) for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    roots = [Path(r) for r in roots if r]
    known = (
        ("microsoft edge", "msedge.exe", "Microsoft/Edge/Application"),
        ("google chrome", "chrome.exe", "Google/Chrome/Application"),
        ("brave", "brave.exe", "BraveSoftware/Brave-Browser/Application"),
    )
    out = []
    for label, exe_name, rel in known:
        for path in _app_path(exe_name) + [r / rel / exe_name for r in roots]:
            if path.is_file():
                out.append(SignInBrowser(label, path))
                break
    return out


def _ws_call(port: int, path: str, method: str) -> dict:
    """One DevTools command over a bare-bones websocket, enough for a
    local, unencrypted, single request and reply."""
    import base64
    import json
    import socket
    import struct

    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("devtools closed the connection")
            buf += chunk
        head, buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("devtools refused the connection")

        payload = json.dumps({"id": 1, "method": method}).encode()
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

        def take(count: int) -> bytes:
            nonlocal buf
            while len(buf) < count:
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError("devtools closed the connection")
                buf += chunk
            out, buf = buf[:count], buf[count:]
            return out

        message = b""
        while True:
            b1, b2 = take(2)
            length = b2 & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", take(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", take(8))
            data = take(length)
            opcode = b1 & 0x0F
            if opcode == 8:
                raise ConnectionError("devtools closed the connection")
            if opcode in (0, 1):
                message += data
                if b1 & 0x80:
                    reply = json.loads(message)
                    if reply.get("id") == 1:
                        return reply
                    message = b""
    finally:
        sock.close()


class _DevtoolsCookie:
    def __init__(self, raw: dict) -> None:
        self.name = raw.get("name", "")
        self.value = raw.get("value", "")
        self.domain = raw.get("domain", "")
        expires = raw.get("expires") or 0
        # DevTools: unix seconds, -1 for a session cookie
        self.expires = expires if expires > 0 else None


def _profile_in_use(user_data_dir: Path) -> bool:
    """A browser still has this profile open. Chromium holds "lockfile"
    open on windows for as long as it runs; elsewhere it keeps a
    SingletonLock symlink."""
    singleton = user_data_dir / "SingletonLock"
    if singleton.is_symlink():
        # "<host>-<pid>", and it can outlive the browser: in use only while
        # that pid runs (chromium checks it the same way)
        try:
            pid = int(os.readlink(singleton).rsplit("-", 1)[1])
            os.kill(pid, 0)
            return True
        except (OSError, ValueError, IndexError):
            return False
    lock = user_data_dir / "lockfile"
    try:
        with open(lock, "a"):
            return False
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _read_profile_cookies(browser: SignInBrowser, timeout_s: float = 30) -> list:
    """Start ``browser`` headless on tide's sign-in profile with devtools on
    and read its cookies, already decrypted. Nothing signs in here, so
    google's check on remote-controlled browsers doesn't come into it."""
    import subprocess
    import time

    port_file = SIGNIN_DIR / "DevToolsActivePort"
    port_file.unlink(missing_ok=True)
    proc = subprocess.Popen([
        str(browser.exe),
        "--headless=new",
        f"--user-data-dir={SIGNIN_DIR}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ])
    try:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                lines = port_file.read_text().split()
                port, path = int(lines[0]), lines[1]
            except (OSError, ValueError, IndexError):
                if proc.poll() is not None:
                    raise ImportError_(f"{browser.label} wouldn't start to read the sign-in.")
                continue
            reply = _ws_call(port, path, "Storage.getCookies")
            try:
                _ws_call(port, path, "Browser.close")
            except OSError:
                pass
            return [_DevtoolsCookie(c) for c in reply.get("result", {}).get("cookies", [])]
        raise ImportError_(f"{browser.label} took too long to start.")
    finally:
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def _no_background_mode(user_data_dir: Path) -> None:
    """Chromium (edge especially) keeps running after its last window
    closes when background mode is on, so "closed" would never come. Off
    for tide's own profile, in its Local State."""
    import json
    state_file = user_data_dir / "Local State"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    state.setdefault("background_mode", {})["enabled"] = False
    try:
        state_file.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def _log(msg: str) -> None:
    # stderr is tide.log in the windows build; the steps of a sign-in that
    # stalls are otherwise invisible
    print(f"tide: signin {msg}", file=sys.stderr, flush=True)


def _browser_pids(browser: SignInBrowser, user_data_dir: Path) -> list[int]:
    """The main process of ``browser`` on tide's sign-in profile (windows).
    Not necessarily the one tide started: edge often hands off to a new
    process and the first one quits."""
    import subprocess
    if sys.platform != "win32":
        return []
    exe = browser.exe.name.replace("'", "''")
    folder = str(user_data_dir).replace("'", "''")
    script = (
        f"Get-CimInstance Win32_Process -Filter \"Name='{exe}'\" | "
        f"Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains('{folder}') -and "
        "-not $_.CommandLine.Contains('--type=') } | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                              "-Command", script],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(t) for t in out.split() if t.isdigit()]


def _close_browser(browser: SignInBrowser, proc, force: bool = False) -> None:
    """Close the sign-in browser. Gently first, the way its window's X
    does, so it writes the session to disk (a kill can lose the last few
    seconds); ``force`` when that didn't work."""
    import subprocess
    if sys.platform == "win32":
        pids = _browser_pids(browser, SIGNIN_DIR) or [proc.pid]
        _log(f"closing {'(forced) ' if force else ''}pids {pids}")
        for pid in pids:
            # taskkill without /F sends the windows WM_CLOSE
            args = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
            r = subprocess.run(args, capture_output=True, text=True, check=False)
            _log(f"taskkill {pid}: {r.returncode} {(r.stdout or r.stderr).strip()[:120]}")
    elif force:
        proc.kill()
    else:
        proc.terminate()


def sign_in_with_window(browser: SignInBrowser, should_stop=lambda: False,
                        timeout_s: float = 900,
                        should_finish=lambda: False,
                        notify=lambda msg: None) -> ImportResult:
    """Open ``browser`` on tide's own profile at YouTube Music, wait for the
    user to sign in and close the window, then read the session from that
    profile. Blocking; call it off the GUI thread. Raises ImportError_ if
    the window closes without a sign-in, or it times out.

    The window is a plain browser: google won't sign anyone in to one
    started with remote debugging ("this browser or app may not be
    secure"), so devtools only come on afterwards, headless, to read the
    cookies (see _read_profile_cookies). ``should_finish`` turning true
    ("i'm signed in" in the dialog) closes the window for the user."""
    import subprocess
    import time

    SIGNIN_DIR.mkdir(parents=True, exist_ok=True)
    _no_background_mode(SIGNIN_DIR)
    proc = subprocess.Popen([
        str(browser.exe),
        f"--user-data-dir={SIGNIN_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        SIGNIN_URL,
    ])
    profile = BrowserProfile(slug="window", label=browser.label,
                             cookies_path=SIGNIN_DIR, family="window")
    _log(f"started {browser.label}, pid {proc.pid}")
    deadline = time.monotonic() + timeout_s
    started = time.monotonic()
    asked_at = 0.0
    forced = False
    told = False
    while time.monotonic() < deadline and not should_stop():
        time.sleep(1.0)
        if should_finish() and not asked_at:
            _close_browser(browser, proc)
            asked_at = time.monotonic()
        elif asked_at and not forced and time.monotonic() - asked_at > 15:
            # still open 15 s after asking: the window ignored it
            _close_browser(browser, proc, force=True)
            forced = True
        elif forced and not told and time.monotonic() - asked_at > 30:
            told = True
            notify(f"tide couldn't close the {browser.label} window. close it "
                   "yourself and tide carries on.")
        # the first process can hand off to another and exit at once, so
        # "closed" is the profile being free, not just our process gone
        if proc.poll() is None or _profile_in_use(SIGNIN_DIR):
            continue
        if time.monotonic() - started < 3.0:
            continue        # still starting up
        _log("window closed, reading the session")
        result = _result_from_jar(profile, _read_profile_cookies(browser))
        _log(f"signed in: {result.looks_signed_in}")
        if not result.looks_signed_in:
            raise ImportError_(f"the {browser.label} window closed before you signed in.")
        return result
    raise ImportError_("stopped waiting for the sign-in.")
