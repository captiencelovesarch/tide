"""Once-a-day check for a newer GitHub release.

We hit ``api.github.com/repos/.../releases/latest`` in the background,
compare the tag's version to the bundled ``__version__``, and surface a
toast with a `[view]` action if newer. Result is cached in
``~/.cache/tide/update_check.json`` so we don't spam GitHub.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
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
