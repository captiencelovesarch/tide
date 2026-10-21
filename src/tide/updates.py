"""Once-a-day check for a newer GitHub release.

We hit ``api.github.com/repos/.../releases/latest`` in the background,
compare the tag's version to the bundled ``__version__``, and surface a
toast with a `[view]` action if newer. Result is cached in
``~/.cache/tide/update_check.json`` so we don't spam GitHub.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

from . import __version__, config


GITHUB_REPO = "captiencelovesarch/tide"
LATEST_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
CACHE_PATH = config.CACHE_DIR / "update_check.json"
CHECK_INTERVAL_SECONDS = 24 * 3600
# Derived, not hardcoded — a literal here drifted behind the real version once.
USER_AGENT = f"tide/{__version__}"


def _parse_semver(tag: str) -> tuple[int, ...] | None:
    """Parse a dotted numeric version into a comparison tuple.

    tide ships 4-segment patch tags (``v1.2.3.1``, ``v1.2.3.2``) on top of
    the usual 3-segment releases, so a fixed 3-group regex silently dropped
    the 4th field and made ``1.2.3.1`` compare *equal* to ``1.2.3`` — the
    exact scheme this project uses, so point releases never notified. Accept
    any number of numeric components instead.
    """
    m = re.match(r"^v?(\d+(?:\.\d+)*)", tag.strip())
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def _version_gt(remote: tuple[int, ...], current: tuple[int, ...]) -> bool:
    """True if ``remote`` is a newer version than ``current``, comparing
    component-wise after zero-padding to equal length (so ``1.2.3.1`` beats
    ``1.2.3`` and ``1.2.4`` beats ``1.2.3.9``)."""
    width = max(len(remote), len(current))
    r = remote + (0,) * (width - len(remote))
    c = current + (0,) * (width - len(current))
    return r > c


def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(data: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _fetch_latest() -> tuple[str, str] | None:
    """Return (tag_name, html_url) or None on failure."""
    req = urllib.request.Request(LATEST_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read(4 * 1024 * 1024).decode("utf-8"))
    except Exception:
        return None
    tag = data.get("tag_name") or ""
    url = data.get("html_url") or ""
    if not tag:
        return None
    # html_url is opened in the user's browser via QDesktopServices. It comes
    # from the API response, so constrain it to a real GitHub release link —
    # never let a javascript:/file:/off-platform URL through to openUrl even
    # if the endpoint (or a compromise of it) returns something unexpected.
    if not url.startswith("https://github.com/"):
        url = ""
    return tag, url


def check_in_background(current_version: str, on_newer: Callable[[str, str], None]) -> None:
    """Spawn a daemon thread. If a newer release exists, ``on_newer`` is
    called with (tag, html_url). Skips if we checked within 24h.
    """
    cache = _load_cache()
    last = float(cache.get("last_checked", 0))
    if time.time() - last < CHECK_INTERVAL_SECONDS:
        return

    cur = _parse_semver(current_version)
    if cur is None:
        return

    def run() -> None:
        result = _fetch_latest()
        now = time.time()
        _save_cache({"last_checked": now, "latest_tag": result[0] if result else ""})
        if not result:
            return
        tag, url = result
        remote = _parse_semver(tag)
        if remote is None or not _version_gt(remote, cur):
            return
        on_newer(tag, url)

    threading.Thread(target=run, name="tide-update-check", daemon=True).start()


# ---------- Windows: quiet self-update ----------
#
# The Windows build updates itself with no questions asked: each launch
# checks the latest release, downloads its tide-setup.exe in the background
# and says so once. A running exe can't replace its own files, so the
# installer runs silently when tide quits (or right away from the toast's
# restart, which also opens tide again afterwards).

SETUP_ASSET = "tide-setup.exe"
_DOWNLOAD_PREFIX = f"https://github.com/{GITHUB_REPO}/releases/download/"
UPDATE_DIR = config.CACHE_DIR / "update"
_pending: Path | None = None


def windows_updates() -> bool:
    """True in the installed Windows build, the only place it applies."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def _fetch_latest_setup() -> tuple[str, str, int, str] | None:
    """(tag, download url, size, sha256 or "") of the latest release's
    installer, or None."""
    req = urllib.request.Request(LATEST_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read(4 * 1024 * 1024).decode("utf-8"))
    except Exception:
        return None
    tag = data.get("tag_name") or ""
    for asset in data.get("assets") or []:
        if asset.get("name") != SETUP_ASSET:
            continue
        url = asset.get("browser_download_url") or ""
        if not url.startswith(_DOWNLOAD_PREFIX):
            return None
        digest = asset.get("digest") or ""
        sha = digest[7:] if digest.startswith("sha256:") else ""
        return tag, url, int(asset.get("size") or 0), sha
    return None


def _download(url: str, size: int, sha: str, dest: Path) -> bool:
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp, open(part, "wb") as f:
            while chunk := resp.read(1 << 16):
                f.write(chunk)
                h.update(chunk)
        if (size and part.stat().st_size != size) or (sha and h.hexdigest() != sha):
            part.unlink(missing_ok=True)
            return False
        os.replace(part, dest)
        return True
    except Exception:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def auto_update_in_background(current_version: str, on_ready: Callable[[str], None]) -> None:
    """Windows build only. Fetches a newer installer if there is one and
    calls ``on_ready(tag)`` from a worker thread once it's on disk. Silent
    on every failure; the next launch simply tries again."""
    cur = _parse_semver(current_version)
    if cur is None:
        return

    def run() -> None:
        global _pending
        UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        # an installer for this version or older already ran; clear it out
        for old in UPDATE_DIR.glob("tide-setup-*.exe"):
            v = _parse_semver(old.stem.removeprefix("tide-setup-"))
            if v is None or not _version_gt(v, cur):
                old.unlink(missing_ok=True)
        found = _fetch_latest_setup()
        if not found:
            return
        tag, url, size, sha = found
        remote = _parse_semver(tag)
        if remote is None or not _version_gt(remote, cur):
            return
        dest = UPDATE_DIR / f"tide-setup-{tag.lstrip('v')}.exe"
        if not dest.is_file() and not _download(url, size, sha, dest):
            return
        _pending = dest
        on_ready(tag)

    threading.Thread(target=run, name="tide-auto-update", daemon=True).start()


def install_pending(relaunch: bool) -> None:
    """Start the downloaded installer, silently, as tide exits. It waits
    for this process to close, replaces the files, and with ``relaunch``
    opens tide again."""
    if _pending is None or not _pending.is_file():
        return
    args = [str(_pending), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOCANCEL"]
    if relaunch:
        args.append("/RELAUNCH=1")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(args, creationflags=flags, close_fds=True)
    except OSError:
        pass
