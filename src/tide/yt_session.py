"""The YouTube Music session, kept alive by tide itself.

A signed-in Google session is a set of cookies, and two of them
(``__Secure-1PSIDTS`` / ``__Secure-3PSIDTS``) are short-lived: a browser
swaps them for fresh ones about every ten minutes through
``accounts.youtube.com/RotateCookies``, and Google stops honouring old ones
soon after. tide used to save a snapshot at import and send it unchanged
forever, so its copy went stale within the hour and the only cure was to
read the browser's cookie database again.

Now the snapshot only seeds the session. From then on:

* one in-memory jar holds the live cookies. ytmusicapi's requests session
  uses it directly, and yt-dlp works on a copy that is merged back, so
  every cookie YouTube hands out lands in the same place;
* a keeper thread keeps the PSIDTS pair fresh and writes the jar to disk,
  so a restart picks up where the last run left off.

How the keeper keeps it fresh depends on whose session it is. An import
from a browser (or pasted headers) is the browser's own session: same
SID, same PSIDTS pair. Google lets one copy of the pair live. Whoever
rotates first gets the new pair, and the other copy is signed out the
next time it's used (2026-11-01: a refresh token, then the keeper's first
rotation 20 s later, signed helium out of youtube). So:

* a session tide has to itself (``owned``) is rotated on Google's own
  schedule (the rotation answer carries the next interval, 600 s so far);
* a shared one is never rotated. tide finds the browser holding the same
  sign-in and, whenever that browser's cookie db changes, reads its pair
  back (``follow``). Left alone, nobody rotates it, and a pair nobody
  replaces keeps working.

Re-importing from the browser is still the recovery path when Google
actually ends the session; it rewrites ``browser.json``, and the jar
reloads from it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import requests
from yt_dlp.cookies import YoutubeDLCookieJar

from . import config

ROTATE_URL = "https://accounts.youtube.com/RotateCookies"
_ROTATE_ORIGIN = "https://accounts.youtube.com"
# The body Google's own rotation page posts. Opaque, and accepted as is.
_ROTATE_BODY = '[000,"-0000000000000000000"]'
# What the answer carries when it doesn't say: the interval the browser uses.
DEFAULT_INTERVAL_S = 600.0
# Google answers 429 to a second rotation within about a minute.
_MIN_GAP_S = 90.0
# After a failed rotation (offline, 5xx), try again this much later.
_RETRY_S = 120.0
# How often the keeper wakes to write a changed jar to disk.
_SAVE_EVERY_S = 60.0
# How often a shared session looks at its browser's cookie db.
_FOLLOW_EVERY_S = 60.0
# How long after launch the keeper starts.
_FIRST_TICK_S = 15.0

# These name one sign-in and stay put while the PSIDTS pair rotates.
_IDENTITY = ("SID", "__Secure-1PSID", "__Secure-3PSID")
_PAIR = ("__Secure-1PSIDTS", "__Secure-3PSIDTS")
# What a shared session takes from its browser: the sign-in's cookies and
# the ones Google re-issues alongside them. Nothing else of the browser's.
_ADOPT = _IDENTITY + _PAIR + (
    "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PAPISID",
    "__Secure-3PAPISID", "SIDCC", "__Secure-1PSIDCC", "__Secure-3PSIDCC",
    "LOGIN_INFO")

_INTERVAL_RE = re.compile(r'\["identity\.hfcr",\s*(\d+)\]')


def jar_file() -> Path:
    return config.signin_dir() / "yt_cookies.txt"


def _meta_file() -> Path:
    return config.signin_dir() / "yt_session.json"


class _Jar(YoutubeDLCookieJar):
    """yt-dlp's Netscape jar (it keeps ``#HttpOnly_`` lines, which the
    stdlib MozillaCookieJar drops as comments) that notices changes."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.dirty = False

    def set_cookie(self, cookie) -> None:
        super().set_cookie(cookie)
        self.dirty = True

    def __iter__(self):
        # requests copies the jar into every request while other threads
        # take Set-Cookie into it; the stdlib iterates its dicts unlocked.
        with self._cookies_lock:
            return iter(list(super().__iter__()))


_lock = threading.RLock()
_jar: _Jar | None = None
# browser.json's mtime when the jar was loaded: a newer import reseeds it.
_seeded_from: int | None = None


def _auth_stamp() -> int | None:
    try:
        return config.BROWSER_AUTH_FILE.stat().st_mtime_ns
    except OSError:
        return None


def _seed_file_from_import() -> bool:
    """Write the jar file from ``browser.json``'s cookie header. Only when
    the import is newer than the jar, so a jar that has been rotating keeps
    its newer cookies across restarts. False when there is no import."""
    src = config.BROWSER_AUTH_FILE
    out = jar_file()
    try:
        if out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
            return True
    except OSError:
        return False
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception:
        return False
    jar = _Jar()
    for part in str(data.get("cookie") or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name.strip():
            jar.set_cookie(_cookie(name.strip(), value.strip()))
    if not len(jar):
        return False
    _write(jar, out)
    return True


def _cookie(name: str, value: str):
    import http.cookiejar as cj
    # Every Google auth cookie on music.youtube.com lives on .youtube.com.
    # The far-future expiry is a placeholder until YouTube sends a real one.
    return cj.Cookie(
        0, name, value, None, False, ".youtube.com", True, True, "/", False,
        True, 2_000_000_000, False, None, None, {})


def _write(jar: _Jar, path: Path) -> None:
    """Save atomically as an owner-only file (it is a live session)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.",
                               suffix=".tmp")
    os.close(fd)
    try:
        with jar._cookies_lock:
            jar.save(tmp, ignore_discard=True, ignore_expires=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def jar() -> _Jar | None:
    """The live jar, or None when nobody is signed in. Loaded on first use
    and reloaded in place when a fresh import lands, so every session that
    holds it sees the new cookies."""
    global _jar, _seeded_from
    with _lock:
        stamp = _auth_stamp()
        if stamp is None:
            return None
        if _jar is not None and _seeded_from == stamp:
            return _jar
        if not _seed_file_from_import():
            return None
        if _jar is None:
            _jar = _Jar()
        else:
            _jar.clear()
        try:
            _jar.load(str(jar_file()), ignore_discard=True, ignore_expires=True)
        except (OSError, ValueError):
            return None
        _jar.dirty = False
        _seeded_from = stamp
        return _jar


def save() -> None:
    """Write the jar to disk if anything changed since the last write."""
    with _lock:
        j = _jar
        if j is None or not j.dirty or _seeded_from != _auth_stamp():
            return
        j.dirty = False
        try:
            _write(j, jar_file())
        except OSError:
            j.dirty = True


def session() -> requests.Session | None:
    """A requests session backed by the live jar (for ytmusicapi)."""
    j = jar()
    if j is None:
        return None
    s = requests.Session()
    s.cookies = j
    return s


def snapshot(path: str) -> None:
    """Write the live jar to ``path`` for a yt-dlp pass."""
    with _lock:
        j = jar()
        if j is None:
            raise OSError("not signed in")
        _write(j, Path(path))


def merge(path: str) -> None:
    """Take back what a yt-dlp pass changed in its copy. Last write wins
    per cookie, and both sides only ever hold cookies YouTube issued."""
    other = _Jar()
    try:
        other.load(path, ignore_discard=True, ignore_expires=True)
    except (OSError, ValueError):
        return
    with _lock:
        j = jar()
        if j is None:
            return
        current = {(c.domain, c.path, c.name): c.value for c in j}
        for c in other:
            if current.get((c.domain, c.path, c.name)) != c.value:
                j.set_cookie(c)
    save()


# ---------- rotation ----------

def _load_meta() -> dict:
    try:
        data = json.loads(_meta_file().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


_meta_lock = threading.Lock()


def _store_meta(**fields) -> None:
    with _meta_lock:
        meta = _load_meta()
        meta.update(fields)
        try:
            _meta_file().write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except OSError:
            pass


def owned() -> bool:
    """True when the session is tide's alone (nothing else holds its
    cookies), so rotating it can't sign anything else out. Recorded at
    import; browser imports and pasted headers are never owned."""
    try:
        meta = json.loads((config.signin_dir() / "browser_meta.json")
                          .read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(meta, dict) and meta.get("owned") is True


def rotated_at() -> float | None:
    v = _load_meta().get("rotated_at")
    return float(v) if isinstance(v, (int, float)) else None


def refreshed_at() -> float | None:
    """When the pair was last renewed: rotated, or read back from the
    browser that shares the session."""
    meta = _load_meta()
    times = [float(meta[k]) for k in ("rotated_at", "synced_at")
             if isinstance(meta.get(k), (int, float))]
    return max(times) if times else None


def next_rotation_at() -> float:
    """Wall time the next rotation is due (now, if never rotated)."""
    meta = _load_meta()
    last = meta.get("rotated_at")
    interval = meta.get("interval")
    if not isinstance(last, (int, float)):
        return 0.0
    if not isinstance(interval, (int, float)) or interval <= 0:
        interval = DEFAULT_INTERVAL_S
    return float(last) + float(interval)


_rotate_lock = threading.Lock()
_last_attempt: float = 0.0


def rotate(*, force: bool = False, timeout: float = 15.0) -> bool:
    """Swap the PSIDTS pair for fresh ones, the way a browser tab does.
    True when Google handed out new cookies. Skips (False) when not signed
    in, when the session isn't tide's alone (rotating a browser's session
    signs the browser out), or when it isn't due yet unless ``force``;
    never closer together than Google tolerates either way. Blocking: call
    off the GUI thread."""
    global _last_attempt
    if not owned():
        return False
    with _rotate_lock:
        now = time.time()
        if now - _last_attempt < _MIN_GAP_S:
            return False
        if not force and now < next_rotation_at():
            return False
        s = session()
        if s is None:
            return False
        _last_attempt = now
        ua = _user_agent()
        try:
            r = s.post(ROTATE_URL, data=_ROTATE_BODY, timeout=timeout,
                       headers={"Content-Type": "application/json",
                                "Origin": _ROTATE_ORIGIN,
                                **({"User-Agent": ua} if ua else {})})
        except requests.RequestException:
            return False
        if r.status_code != 200:
            return False
        m = _INTERVAL_RE.search(r.text or "")
        interval = float(m.group(1)) if m else DEFAULT_INTERVAL_S
        _store_meta(rotated_at=time.time(), interval=interval)
        save()
        return True


def _user_agent() -> str | None:
    try:
        data = json.loads(config.BROWSER_AUTH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    ua = data.get("user-agent")
    return ua if isinstance(ua, str) and ua else None


# ---------- following the browser ----------

def _identity(cookies: dict) -> str | None:
    """A short hash of the cookies that name one sign-in, or None without
    a SID (signed out)."""
    if not cookies.get("SID"):
        return None
    joined = "\0".join(str(cookies.get(k) or "") for k in _IDENTITY)
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def _import_identity() -> str | None:
    """The identity of the sign-in ``browser.json`` holds. Read from the
    import, not the live jar: Google can strip a stale copy's SID, and
    tide should still know which browser session it came from."""
    try:
        data = json.loads(config.BROWSER_AUTH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    cookies = {}
    for part in str(data.get("cookie") or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep:
            cookies[name.strip()] = value.strip()
    return _identity(cookies)


def _mtime(path) -> float:
    try:
        return Path(path).stat().st_mtime
    except (OSError, TypeError):
        return 0.0


def _read(profile, db) -> dict | None:
    """The youtube cookies in one cookie db, or None when it isn't signed
    in. Raises when the db can't be read (locked, no keyring)."""
    from . import browser_import as bi
    result = bi.import_cookies(replace(profile, cookies_path=Path(db),
                                       candidates=(Path(db),)))
    return result.cookies if result.looks_signed_in else None


def _discover(want: str):
    """The browser cookie db holding the sign-in ``want``, newest db first:
    (profile, db, cookies), or None when no browser has it."""
    from . import browser_import as bi
    dbs = [(profile, Path(db)) for profile in bi.available_profiles()
           for db in (profile.candidates or (profile.cookies_path,))]
    for profile, db in sorted(dbs, key=lambda x: _mtime(x[1]), reverse=True):
        if bi.has_youtube_session(profile, db) is False:
            continue                # signed out there: skip the slow read
        try:
            cookies = _read(profile, db)
        except Exception:
            continue
        if cookies is not None and _identity(cookies) == want:
            return profile, db, cookies
    return None


def _profile_for(slug: str):
    from . import browser_import as bi
    return next((p for p in bi.available_profiles() if p.slug == slug), None)


def _adopt(cookies: dict) -> bool:
    """Take the browser's sign-in cookies into the live jar when its pair
    differs from ours. True when anything changed."""
    with _lock:
        j = jar()
        if j is None:
            return False
        live = {c.name: c.value for c in j}
        if all(live.get(k) == cookies.get(k) for k in _IDENTITY + _PAIR):
            return False
        for name in _ADOPT:
            value = cookies.get(name)
            if value and live.get(name) != value:
                j.set_cookie(_cookie(name, value))
    _store_meta(synced_at=time.time())
    save()
    return True


_follow_lock = threading.Lock()


def follow(*, look: str = "new") -> bool:
    """Keep a shared session level with the browser it came from. tide
    finds that browser (the db holding the same sign-in) once per import;
    after that, the db is read again only when its youtube PSIDTS rows
    have changed. A browser that signs out or signs in as someone else is
    let go, and tide's copy is left as it is.

    ``look`` says when to search the browsers, which is slow: "new" (no
    search yet for this import), "again" (also when the last search found
    nothing) or "known" (never: only the browser already found). True when
    tide took new cookies. Blocking (db reads, a keyring round-trip and
    yt-dlp's decryption): call off the GUI thread."""
    from . import browser_import as bi
    with _follow_lock:
        stamp = _auth_stamp()
        want = _import_identity()
        if stamp is None or want is None:
            return False
        state = _load_meta().get("follow")
        if not isinstance(state, dict) or state.get("stamp") != stamp:
            state = None
        if look != "known" and (state is None
                                or (look == "again" and not state.get("db"))):
            hit = _discover(want)
            if hit is None:
                _store_meta(follow={"stamp": stamp, "db": None})
                return False
            profile, db, cookies = hit
            _store_meta(follow={"stamp": stamp, "slug": profile.slug,
                                "db": str(db), "mtime": _mtime(db),
                                "mark": bi.pair_mark(profile, db)})
            return _adopt(cookies)
        db = (state or {}).get("db")
        if not db:
            return False
        mtime = _mtime(db)
        if mtime == state.get("mtime"):
            return False
        profile = _profile_for(str(state.get("slug")))
        if profile is None:
            cookies = None
        else:
            # the db changes all the time while the browser runs; a full
            # read only pays off when the session's pair moved
            mark = bi.pair_mark(profile, Path(db))
            if mark is not None and mark == state.get("mark"):
                state["mtime"] = mtime
                _store_meta(follow=state)
                return False
            try:
                cookies = _read(profile, db)
            except Exception:
                return False        # try again on the next change
            state["mark"] = mark
        state["mtime"] = mtime
        if cookies is None or _identity(cookies) != want:
            state["db"] = None
            _store_meta(follow=state)
            return False
        _store_meta(follow=state)
        return _adopt(cookies)


def freshen() -> None:
    """Bring the session up to date before tide first uses it: rotate one
    tide owns if it's due, or catch up with the browser that shares it
    (the browser rotated while tide was closed). Searching for a browser
    is the keeper's job, so launch doesn't wait on it. Blocking: call off
    the GUI thread."""
    if owned():
        rotate()
    else:
        follow(look="known")


# ---------- keeper ----------

_keeper: threading.Thread | None = None
_stop = threading.Event()


def start_keeper() -> None:
    """Run the rotate-or-follow + save loop for the life of the app. Idle
    (and cheap) while signed out; picks a sign-in up on its next wake."""
    global _keeper
    if _keeper is not None and _keeper.is_alive():
        return
    _stop.clear()
    _keeper = threading.Thread(target=_keep, name="tide-yt-session",
                               daemon=True)
    _keeper.start()


def stop_keeper() -> None:
    _stop.set()
    save()


def _keep() -> None:
    retry_at = 0.0
    # once per run, look again for a browser the last run didn't find
    look = "again"
    # launch's own freshen() goes first; a search here would hold it up
    if _stop.wait(_FIRST_TICK_S):
        return
    while not _stop.is_set():
        if config.BROWSER_AUTH_FILE.is_file() and not owned():
            try:
                follow(look=look)
                look = "new"
            except Exception:
                pass
            save()
            wait = _FOLLOW_EVERY_S
        elif config.BROWSER_AUTH_FILE.is_file():
            if time.time() >= max(next_rotation_at(), retry_at):
                if not rotate():
                    retry_at = time.time() + _RETRY_S
            save()
            due = max(next_rotation_at(), retry_at) - time.time()
            wait = min(_SAVE_EVERY_S, max(5.0, due))
        else:
            wait = _SAVE_EVERY_S        # signed out: just look again later
        _stop.wait(wait)
