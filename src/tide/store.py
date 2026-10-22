"""The plugin store: the signed index, downloads, install and uninstall.
design/plugin-store.md is the why.

The index is one json file plus ``index.json.sig``, an ed25519 signature
of its exact bytes. tide has the public key built in and refuses an index
that doesn't check. Everything else the store fetches (zips, banners,
icons, previews) carries a sha256 in that signed index, so one signature
covers all of it.

No qt in here: the store page runs these off the gui thread. Every
``StoreError`` carries a line that can go straight into the ui.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Callable

from . import config

INDEX_URL = ("https://github.com/captiencelovesarch/tide-plugins/releases/download/"
             "index/index.json")
# raw ed25519 public key, hex. its private half is offline with the user;
# losing it means a new key and a tide release (design/plugin-store.md)
PUBLIC_KEY = "f77fcd76d92842b317cd66e3772a3c30c2541cd5253547f8299ad816ccbe39c8"
# dev and tests only: point tide at another index and key. anything that
# can set these can already edit ~/.bashrc, so they're no new way in.
INDEX_ENV = "TIDE_STORE_INDEX"
KEY_ENV = "TIDE_STORE_KEY"

FORMAT = 1
CATEGORIES = ("sources", "lyrics", "visuals", "hardware", "tools")
# "installed" is the store's own folder under <data>/plugins/
RESERVED_SLUGS = frozenset({"installed"})

INDEX_MAX_BYTES = 2 * 1024 * 1024
ASSET_MAX_BYTES = 24 * 1024 * 1024
UNPACKED_MAX_BYTES = 400 * 1024 * 1024
UNPACKED_MAX_FILES = 20_000
TIMEOUT_S = 20
UA = "tide-plugin-store"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class StoreError(Exception):
    """something went wrong; ``str()`` is one line for the ui."""


NO_NETWORK = "couldn't reach the store"
BAD_INDEX = "the store's list didn't check out, so tide isn't using it"
NOT_SET_UP = "the store isn't set up in this build"
DAMAGED = "the download was damaged, try again"
ESCAPES = "that plugin's zip tries to write outside its folder. not installed"
MISMATCH = "that plugin doesn't match the store's list. not installed"


# ---------------------------------------------------------------------------
# the index
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Asset:
    url: str
    sha256: str


@dataclass(frozen=True)
class Need:
    what: str
    check: str = ""
    how: str = ""
    # a hard need greys the install button out ("only works on kde plasma")
    hard: bool = False
    hard_note: str = ""


@dataclass(frozen=True)
class Preview:
    kind: str           # clip / image
    asset: Asset


@dataclass(frozen=True)
class Entry:
    slug: str
    name: str
    blurb: str
    version: str
    api: int
    url: str
    sha256: str
    size: int
    repo: str
    banner: Asset                       # the grid cut, no text
    banner_full: Asset | None = None    # logo + "<plugin> × tide"
    banner_motion: Asset | None = None  # the capture frame moving
    about: str = ""
    min_tide: str = ""
    uses: tuple[str, ...] = ()
    needs: tuple[Need, ...] = ()
    author: str = ""
    official: bool = False
    category: str = "tools"
    icon: Asset | None = None
    icon_filled: Asset | None = None
    previews: tuple[Preview, ...] = ()
    added: str = ""                     # yyyy-mm-dd, for "new"


@dataclass(frozen=True)
class Featured:
    slug: str
    note: str
    until: date | None


@dataclass
class Index:
    generated: str
    entries: dict[str, Entry] = field(default_factory=dict)
    featured: Featured | None = None
    skipped: list[str] = field(default_factory=list)   # entries that didn't parse

    def entry(self, slug: str) -> Entry | None:
        return self.entries.get(slug)


def index_url() -> str:
    return os.environ.get(INDEX_ENV, "").strip() or INDEX_URL


def public_key() -> str:
    return os.environ.get(KEY_ENV, "").strip() or PUBLIC_KEY


def verify(data: bytes, sig: bytes, key_hex: str) -> None:
    """raise unless ``sig`` is ``key_hex``'s signature of ``data``."""
    if not key_hex:
        raise StoreError(NOT_SET_UP)
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex))
    except ValueError:
        raise StoreError(NOT_SET_UP) from None
    sig = sig.strip()
    try:
        # the sig file may be raw (64 bytes) or hex text
        raw = sig if len(sig) == 64 else bytes.fromhex(sig.decode("ascii"))
        key.verify(raw, data)
    except (InvalidSignature, ValueError, UnicodeDecodeError):
        raise StoreError(BAD_INDEX) from None


def _asset(raw, *, required: bool = False) -> Asset | None:
    if raw is None and not required:
        return None
    if isinstance(raw, dict):
        url, sha = raw.get("url"), str(raw.get("sha256", "")).lower()
        if isinstance(url, str) and _url_ok(url) and _SHA_RE.match(sha):
            return Asset(url, sha)
    raise ValueError("an image needs a url and a sha256")


def _url_ok(url: str) -> bool:
    scheme = urllib.parse.urlsplit(url).scheme
    return scheme == "https" or (scheme in ("file", "http") and bool(os.environ.get(INDEX_ENV)))


def _str(raw: dict, key: str, *, required: bool = True) -> str:
    v = raw.get(key, "")
    if not isinstance(v, str) or (required and not v.strip()):
        raise ValueError(f"needs {key}")
    return v


def _entry(raw) -> Entry:
    if not isinstance(raw, dict):
        raise ValueError("not a table")
    slug = _str(raw, "slug")
    if not _SLUG_RE.match(slug) or slug in RESERVED_SLUGS:
        raise ValueError(f"bad slug {slug!r}")
    api, size = raw.get("api"), raw.get("size")
    if not isinstance(api, int) or isinstance(api, bool):
        raise ValueError("needs api")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("needs size")
    sha = _str(raw, "sha256").lower()
    if not _SHA_RE.match(sha):
        raise ValueError("bad sha256")
    url = _str(raw, "url")
    repo = _str(raw, "repo")
    if not _url_ok(url):
        raise ValueError("bad url")
    if urllib.parse.urlsplit(repo).scheme != "https":
        raise ValueError("bad repo")
    banner = raw.get("banner")
    if not isinstance(banner, dict):
        raise ValueError("no banner, no store entry")
    needs = []
    for n in raw.get("needs", []) or []:
        if not isinstance(n, dict) or not isinstance(n.get("what"), str):
            raise ValueError("bad need")
        needs.append(Need(what=n["what"], check=str(n.get("check", "")), how=str(n.get("how", "")),
                          hard=bool(n.get("hard", False)), hard_note=str(n.get("hard_note", ""))))
    previews = []
    for p in raw.get("previews", []) or []:
        if not isinstance(p, dict) or p.get("kind") not in ("clip", "image"):
            raise ValueError("bad preview")
        previews.append(Preview(p["kind"], _asset(p, required=True)))
    category = str(raw.get("category", "tools"))
    uses = raw.get("uses", []) or []
    if not all(isinstance(u, str) for u in uses):
        raise ValueError("bad uses")
    return Entry(
        slug=slug, name=_str(raw, "name"), blurb=_str(raw, "blurb"),
        version=_str(raw, "version"), api=api, url=url, sha256=sha, size=size, repo=repo,
        banner=_asset(banner.get("grid"), required=True),
        banner_full=_asset(banner.get("full")), banner_motion=_asset(banner.get("motion")),
        about=_str(raw, "about", required=False), min_tide=_str(raw, "min_tide", required=False),
        uses=tuple(uses), needs=tuple(needs), author=_str(raw, "author", required=False),
        official=bool(raw.get("official", False)),
        category=category if category in CATEGORIES else "tools",
        icon=_asset(raw.get("icon")), icon_filled=_asset(raw.get("icon_filled")),
        previews=tuple(previews), added=_str(raw, "added", required=False),
    )


def parse_index(data: bytes, sig: bytes, key_hex: str | None = None) -> Index:
    """check the signature, then read the index. one bad entry is
    skipped, not the whole store."""
    verify(data, sig, public_key() if key_hex is None else key_hex)
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise StoreError(BAD_INDEX) from None
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        raise StoreError("the store has moved on. update tide to see it")
    out = Index(generated=str(raw.get("generated", "")))
    for item in raw.get("plugins", []) or []:
        try:
            e = _entry(item)
        except ValueError as exc:
            slug = item.get("slug", "?") if isinstance(item, dict) else "?"
            out.skipped.append(f"{slug}: {exc}")
            continue
        out.entries[e.slug] = e
    feat = raw.get("featured")
    if isinstance(feat, dict) and isinstance(feat.get("slug"), str):
        until = None
        try:
            until = date.fromisoformat(str(feat.get("until", "")))
        except ValueError:
            pass
        out.featured = Featured(feat["slug"], str(feat.get("note", "")), until)
    return out


def pick_featured(index: Index, today: date | None = None) -> tuple[Entry, str, str] | None:
    """(entry, label, note) for the tile on top. past ``until`` (a missed
    week) it's the newest plugin, labelled new, never a stale pick."""
    today = today or date.today()
    f = index.featured
    if f is not None and f.until is not None and today <= f.until and f.slug in index.entries:
        return index.entries[f.slug], "plugin of the week", f.note
    if not index.entries:
        return None
    newest = max(index.entries.values(), key=lambda e: (e.added, e.slug))
    return newest, "new", newest.blurb


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------

def _open(url: str):
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme not in ("https", "http", "file"):
        raise StoreError(NO_NETWORK)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT_S)
    except (urllib.error.URLError, OSError, ValueError):
        raise StoreError(NO_NETWORK) from None


def _read_capped(url: str, limit: int) -> bytes:
    with _open(url) as resp:
        try:
            data = resp.read(limit + 1)
        except OSError:
            raise StoreError(NO_NETWORK) from None
    if len(data) > limit:
        raise StoreError(BAD_INDEX)
    return data


def index_cache(cache_dir: Path | None = None) -> Path:
    return (cache_dir or config.PLUGIN_CACHE_DIR) / "index"


def fetch_index(cache_dir: Path | None = None, url: str | None = None,
                key_hex: str | None = None) -> Index:
    """the index from the network, checked, and kept for offline use."""
    if not (public_key() if key_hex is None else key_hex):
        raise StoreError(NOT_SET_UP)
    url = url or index_url()
    data = _read_capped(url, INDEX_MAX_BYTES)
    sig = _read_capped(url + ".sig", 4096)
    index = parse_index(data, sig, key_hex)
    folder = index_cache(cache_dir)
    folder.mkdir(parents=True, exist_ok=True)
    _write_atomic(folder / "index.json", data)
    _write_atomic(folder / "index.json.sig", sig)
    return index


def cached_index(cache_dir: Path | None = None, key_hex: str | None = None) -> Index | None:
    """the last index that checked out, checked again (the cache is just
    files; a changed one is refused like a bad download)."""
    folder = index_cache(cache_dir)
    try:
        data = (folder / "index.json").read_bytes()
        sig = (folder / "index.json.sig").read_bytes()
    except OSError:
        return None
    try:
        return parse_index(data, sig, key_hex)
    except StoreError:
        return None


def index_age(cache_dir: Path | None = None) -> float | None:
    """seconds since the cached index was fetched, or None."""
    try:
        return max(0.0, time.time() - (index_cache(cache_dir) / "index.json").stat().st_mtime)
    except OSError:
        return None


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _download(url: str, sha256: str, dest: Path, limit: int,
              on_progress: Callable[[int, int], None] | None = None,
              total: int = 0) -> Path:
    """stream ``url`` to ``dest``, refusing anything over ``limit`` bytes
    or whose sha256 isn't ``sha256``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    done = 0
    try:
        with _open(url) as resp, open(part, "wb") as out:
            while True:
                try:
                    chunk = resp.read(64 * 1024)
                except OSError:
                    raise StoreError(NO_NETWORK) from None
                if not chunk:
                    break
                done += len(chunk)
                if done > limit:
                    raise StoreError(DAMAGED)
                h.update(chunk)
                out.write(chunk)
                if on_progress is not None:
                    on_progress(done, total or limit)
        if h.hexdigest() != sha256:
            raise StoreError(DAMAGED)
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return dest


def download(entry: Entry, downloads_dir: Path | None = None,
             on_progress: Callable[[int, int], None] | None = None) -> Path:
    """the plugin's zip, checked against the signed index."""
    folder = downloads_dir or (config.PLUGIN_CACHE_DIR / "downloads")
    dest = folder / f"{entry.slug}-{entry.version}.zip"
    path = _download(entry.url, entry.sha256, dest, entry.size, on_progress, entry.size)
    if path.stat().st_size != entry.size:
        path.unlink(missing_ok=True)
        raise StoreError(DAMAGED)
    return path


def asset_path(asset: Asset, cache_dir: Path | None = None) -> Path | None:
    """a cached banner / icon / preview, if it's here already."""
    folder = (cache_dir or config.PLUGIN_CACHE_DIR) / "previews"
    suffix = PurePosixPath(urllib.parse.urlsplit(asset.url).path).suffix[:8] or ".bin"
    path = folder / f"{asset.sha256}{suffix}"
    return path if path.is_file() else None


def fetch_asset(asset: Asset, cache_dir: Path | None = None) -> Path:
    """a banner, icon or preview, fetched once and kept by its checksum."""
    hit = asset_path(asset, cache_dir)
    if hit is not None:
        return hit
    folder = (cache_dir or config.PLUGIN_CACHE_DIR) / "previews"
    suffix = PurePosixPath(urllib.parse.urlsplit(asset.url).path).suffix[:8] or ".bin"
    return _download(asset.url, asset.sha256, folder / f"{asset.sha256}{suffix}", ASSET_MAX_BYTES)


# ---------------------------------------------------------------------------
# unpacking, installing, uninstalling
# ---------------------------------------------------------------------------

def _safe_names(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    """every member with its path, or StoreError if any of them could land
    outside the plugin's folder (``..``, absolute, a symlink, a device)."""
    out = []
    total = 0
    infos = zf.infolist()
    if len(infos) > UNPACKED_MAX_FILES:
        raise StoreError(ESCAPES)
    for info in infos:
        name = info.filename
        if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
            raise StoreError(ESCAPES)
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise StoreError(ESCAPES)
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if kind and kind not in (stat.S_IFREG, stat.S_IFDIR):
            raise StoreError(ESCAPES)          # symlinks, devices, fifos
        total += info.file_size
        if total > UNPACKED_MAX_BYTES:
            raise StoreError(DAMAGED)
        out.append((info, path))
    return out


def unpack(zip_path: Path, dest: Path) -> Path:
    """unpack into ``dest`` (which mustn't exist yet). the zip's root is the
    plugin's folder: plugin.toml at the top."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        raise StoreError(DAMAGED) from None
    with zf:
        members = _safe_names(zf)
        dest.mkdir(parents=True, exist_ok=False)
        root = dest.resolve()
        for info, path in members:
            target = (root / Path(*path.parts)).resolve()
            if target != root and root not in target.parents:
                raise StoreError(ESCAPES)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 64 * 1024)
            except (zipfile.BadZipFile, OSError, RuntimeError):
                raise StoreError(DAMAGED) from None
    return dest


def install(entry: Entry, root: Path | None = None, *, downloads_dir: Path | None = None,
            trash_dir: Path | None = None, tide_version: str | None = None,
            api_version: int | None = None,
            on_progress: Callable[[int, int], None] | None = None) -> Path:
    """download, check, unpack into ``<root>/<slug>/``. an update swaps
    the folder only once the new one is whole. on any failure nothing of
    the new version is left behind."""
    from .plugin_api import API_VERSION
    from .plugin_host import ManifestError, parse_manifest
    why = cant_install(entry, tide_version, API_VERSION if api_version is None else api_version)
    if why:
        raise StoreError(why)
    root = root or config.PLUGIN_STORE_DIR
    root.mkdir(parents=True, exist_ok=True)
    zip_path = download(entry, downloads_dir, on_progress)
    staging = Path(tempfile.mkdtemp(dir=str(root), prefix=f".{entry.slug}."))
    try:
        staging.rmdir()
        unpack(zip_path, staging)
        try:
            m = parse_manifest(staging, "store")
        except ManifestError:
            raise StoreError(MISMATCH) from None
        if m.slug != entry.slug or m.version != entry.version or m.api != entry.api:
            raise StoreError(MISMATCH)
        final = root / entry.slug
        if final.exists():
            # an update: the old folder steps aside, then goes
            old = _trash_target(trash_dir, entry.slug, "old")
            os.replace(final, old)
            os.replace(staging, final)
            shutil.rmtree(old.parent, ignore_errors=True)
        else:
            os.replace(staging, final)
        return final
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        zip_path.unlink(missing_ok=True)


def _trash_target(trash_dir: Path | None, slug: str, tag: str) -> Path:
    folder = trash_dir or (config.PLUGIN_CACHE_DIR / "trash")
    folder.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=str(folder), prefix=f"{slug}.{tag}.")) / "x"


@dataclass
class Removed:
    """what an uninstall moved aside, so undo can put it back."""
    slug: str
    holder: Path                 # the trash folder holding all of it
    folder: Path                 # where the plugin was
    settings: Path | None        # its settings file, when forgotten
    data: Path | None            # its data folder, when forgotten


def uninstall(slug: str, root: Path | None = None, *, settings_dir: Path | None = None,
              data_dir: Path | None = None, trash_dir: Path | None = None,
              forget: bool = False) -> Removed:
    """move ``<root>/<slug>`` to the trash. its settings stay unless
    ``forget``; its errors.log goes either way. stop the plugin first."""
    root = root or config.PLUGIN_STORE_DIR
    settings_dir = settings_dir or config.PLUGIN_SETTINGS_DIR
    data_dir = data_dir or config.PLUGIN_DATA_DIR
    folder = root / slug
    if not _SLUG_RE.match(slug) or slug in RESERVED_SLUGS or not folder.is_dir():
        raise StoreError("that plugin isn't installed")
    holder = _trash_target(trash_dir, slug, "removed").parent
    os.replace(folder, holder / "plugin")
    (data_dir / slug / "errors.log").unlink(missing_ok=True)
    settings = data = None
    if forget:
        sfile = settings_dir / f"{slug}.toml"
        if sfile.exists():
            os.replace(sfile, holder / "settings.toml")
            settings = sfile
        dfolder = data_dir / slug
        if dfolder.is_dir():
            os.replace(dfolder, holder / "data")
            data = dfolder
    else:
        # an empty data folder (only errors.log was there) goes too
        try:
            (data_dir / slug).rmdir()
        except OSError:
            pass
    return Removed(slug=slug, holder=holder, folder=folder, settings=settings, data=data)


def undo_uninstall(removed: Removed) -> Path:
    """put an uninstalled plugin back where it was, settings and all."""
    if removed.folder.exists():
        raise StoreError("it's back already")
    removed.folder.parent.mkdir(parents=True, exist_ok=True)
    os.replace(removed.holder / "plugin", removed.folder)
    if removed.settings is not None and (removed.holder / "settings.toml").exists():
        removed.settings.parent.mkdir(parents=True, exist_ok=True)
        os.replace(removed.holder / "settings.toml", removed.settings)
    if removed.data is not None and (removed.holder / "data").exists() \
            and not removed.data.exists():
        removed.data.parent.mkdir(parents=True, exist_ok=True)
        os.replace(removed.holder / "data", removed.data)
    shutil.rmtree(removed.holder, ignore_errors=True)
    return removed.folder


def empty_trash(trash_dir: Path | None = None, keep: Path | None = None) -> None:
    """delete what uninstalls moved aside (``keep``: one still undoable)."""
    folder = trash_dir or (config.PLUGIN_CACHE_DIR / "trash")
    if not folder.is_dir():
        return
    for child in folder.iterdir():
        if keep is not None and child == keep:
            continue
        shutil.rmtree(child, ignore_errors=True)


# ---------------------------------------------------------------------------
# versions and needs
# ---------------------------------------------------------------------------

def version_key(text: str) -> tuple[int, ...]:
    nums = []
    for part in str(text).split("."):
        m = re.match(r"\d+", part)
        nums.append(int(m.group(0)) if m else 0)
    return tuple(nums + [0] * (3 - len(nums)))


def newer(a: str, b: str) -> bool:
    """is version ``a`` newer than ``b``?"""
    return version_key(a) > version_key(b)


def cant_install(entry: Entry, tide_version: str | None = None,
                 api_version: int | None = None) -> str:
    """why this tide can't install ``entry``, or ""."""
    if tide_version is None:
        from . import __version__ as tide_version
    if api_version is None:
        from .plugin_api import API_VERSION as api_version
    if entry.min_tide and newer(entry.min_tide, tide_version):
        return f"needs tide {entry.min_tide}"
    if entry.api != api_version:
        return "built for a different tide"
    for need in entry.needs:
        if need.hard and check_need(need) is False:
            return need.hard_note or f"needs {need.what}"
    return ""


def _process_running(name: str) -> bool:
    try:
        procs = os.listdir("/proc")
    except OSError:
        return False
    for pid in procs:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
                if f.read().strip() == name:
                    return True
        except OSError:
            continue
    return False


def _steamvr_installed() -> bool:
    home = Path.home()
    for steam in (home / ".steam" / "steam", home / ".local" / "share" / "Steam",
                  home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"):
        if (steam / "steamapps" / "common" / "SteamVR").is_dir():
            return True
    return False


def _plasma() -> bool:
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "") + ":" + os.environ.get("DESKTOP_SESSION", "")
    return "kde" in desktop.lower() and shutil.which("plasma-apply-colorscheme") is not None


_CHECKS: dict[str, Callable[[], bool]] = {
    "steamvr": _steamvr_installed,
    "openrgb": lambda: shutil.which("openrgb") is not None,
    "plasma": _plasma,
}


def check_need(need: Need) -> bool | None:
    """True when it's there, False when it isn't, None when tide can't
    tell (no check, or one this tide doesn't know)."""
    check = need.check.strip()
    if not check:
        return None
    fn = _CHECKS.get(check)
    if fn is not None:
        return bool(fn())
    kind, _, arg = check.partition(":")
    if not arg:
        return None
    if kind == "program":
        return shutil.which(arg) is not None
    if kind == "process":
        return _process_running(arg)
    if kind == "desktop":
        return arg.lower() in os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if kind == "module":
        import importlib.util
        try:
            return importlib.util.find_spec(arg) is not None
        except (ImportError, ValueError):
            return False
    return None
