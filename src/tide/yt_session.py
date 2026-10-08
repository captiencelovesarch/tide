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
* a keeper thread rotates the PSIDTS pair on Google's own schedule (the
  rotation answer carries the next interval, 600 s so far) and writes the
  jar to disk, so a restart picks up where the last run left off.

Re-importing from the browser is still the recovery path when Google
actually ends the session; it rewrites ``browser.json``, and the jar
reloads from it.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
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

_INTERVAL_RE = re.compile(r'\["identity\.hfcr",\s*(\d+)\]')


def jar_file() -> Path:
    return config.CONFIG_DIR / "yt_cookies.txt"


def _meta_file() -> Path:
    return config.CONFIG_DIR / "yt_session.json"


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


def _store_meta(**fields) -> None:
    meta = _load_meta()
    meta.update(fields)
    try:
        _meta_file().write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError:
        pass


def rotated_at() -> float | None:
    v = _load_meta().get("rotated_at")
    return float(v) if isinstance(v, (int, float)) else None


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
    in, or when it isn't due yet unless ``force``; never closer together
    than Google tolerates either way. Blocking: call off the GUI thread."""
    global _last_attempt
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


# ---------- keeper ----------

_keeper: threading.Thread | None = None
_stop = threading.Event()


def start_keeper() -> None:
    """Run the rotation + save loop for the life of the app. Idle (and
    cheap) while signed out; picks a sign-in up on its next wake."""
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
    while not _stop.is_set():
        if config.BROWSER_AUTH_FILE.is_file():
            if time.time() >= max(next_rotation_at(), retry_at):
                if not rotate():
                    retry_at = time.time() + _RETRY_S
            save()
            due = max(next_rotation_at(), retry_at) - time.time()
            wait = min(_SAVE_EVERY_S, max(5.0, due))
        else:
            wait = _SAVE_EVERY_S        # signed out: just look again later
        _stop.wait(wait)
