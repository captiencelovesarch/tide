"""Shared thumbnail cache.

Track rows and cards all want the same per-track image. Loading them
independently wastes bandwidth and triggers redundant repaints, so we
centralise here.

Pipeline per URL:
  memory (LRU)  →  disk (~/.cache/tide/art/<sha1>.bin)  →  network

Callers don't worry about lifecycle. They:
  1. call ``request(url, callback)``  where callback receives QImage|None
  2. paint a placeholder for now
  3. when the callback fires (on the GUI thread), repaint with the image

The cache emits a global ``image_loaded(url, image)`` signal too so
delegates can listen once and invalidate the right model index when it
fires, instead of binding a closure per cell.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .. import config


MEM_LRU_LIMIT = 256
# The LRU above holds decoded covers at source size (544 px from YT, 640
# from Spotify: over a megabyte each), so it is also capped by bytes. Rows
# and cards never read these directly; they go through ``scaled``, whose
# display-size pixmaps get a budget of their own.
MEM_BYTES_LIMIT = 48 * 1024 * 1024
PIX_BYTES_LIMIT = 24 * 1024 * 1024

# Thumbnail URLs come straight from third-party server JSON (Subsonic
# coverArt, Bandcamp/SoundCloud/YT thumbnails), so a malicious or MITM'd
# source controls the string. QNetworkAccessManager natively speaks
# file://, data://, ftp://, qrc:// — so an unchecked URL is a local-file
# read / blind-SSRF primitive. Only http(s) may be fetched.
_ALLOWED_ART_SCHEMES = frozenset({"http", "https"})

# Hard ceiling on a single art download. Album art is tens of KB; anything
# past a couple MB is either hostile (memory/disk-fill DoS) or not art.
# Enforced live via downloadProgress so we abort mid-stream, not after
# buffering the whole body.
MAX_ART_BYTES = 8 * 1024 * 1024

# Abort a fetch that stalls or never completes (Qt has no default timeout
# on this path, unlike the urllib surface).
ART_TRANSFER_TIMEOUT_MS = 15_000


def _is_fetchable_art_url(url: str) -> bool:
    """True only for http/https URLs with a host. Blocks file://, data:,
    ftp://, and scheme-relative/garbage strings before they reach QNAM."""
    if not url:
        return False
    qurl = QUrl(url)
    if not qurl.isValid():
        return False
    return qurl.scheme().lower() in _ALLOWED_ART_SCHEMES and bool(qurl.host())


def _hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def _disk_path(url: str) -> Path:
    return config.ART_CACHE_DIR / _hash(url)


class _ArtCache(QObject):
    image_loaded = Signal(str, QImage)  # url, image

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._net = QNetworkAccessManager(self)
        # url -> QImage
        self._mem: "OrderedDict[str, QImage]" = OrderedDict()
        self._mem_bytes = 0
        # (url, w, h, dpr%) -> display-size pixmap, see scaled()
        self._pix: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        self._pix_bytes = 0
        # url -> list of one-shot callbacks queued before the image arrives
        self._waiting: dict[str, list[Callable[[QImage | None], None]]] = {}
        # currently in-flight QNetworkReplies, keyed by url (for dedup)
        self._inflight: set[str] = set()

    # ---------- public API ----------

    def get(self, url: str) -> QImage | None:
        """Return the cached image if available right now, else None."""
        if not url:
            return None
        img = self._mem.get(url)
        if img is not None:
            self._touch(url)
            return img
        return self._maybe_load_disk(url)

    def scaled(self, url: str, w: int, h: int, dpr: float = 1.0) -> QPixmap | None:
        """``url``'s art as ``QPixmap.scaled(w, h, KeepAspectRatioByExpanding,
        SmoothTransformation)`` at device pixel ratio ``dpr``, or None when
        it isn't cached yet. Painters call this every frame, so the scaled
        result is kept: scaling a full cover per paint was most of what a
        repaint of a list cost. A cover only on disk is decoded for this one
        size and not put in the full-size LRU."""
        if not url or w <= 0 or h <= 0:
            return None
        key = (url, w, h, int(round(dpr * 100)))
        pix = self._pix.get(key)
        if pix is not None:
            self._pix.move_to_end(key)
            return pix
        img = self._mem.get(url)
        if img is not None:
            self._touch(url)
        else:
            img = self._read_disk(url)
            if img is None:
                return None
        pix = QPixmap.fromImage(img).scaled(
            max(1, round(w * dpr)), max(1, round(h * dpr)),
            Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        pix.setDevicePixelRatio(dpr)
        self._pix[key] = pix
        self._pix_bytes += _pix_bytes(pix)
        while self._pix_bytes > PIX_BYTES_LIMIT and len(self._pix) > 1:
            _, old = self._pix.popitem(last=False)
            self._pix_bytes -= _pix_bytes(old)
        return pix

    def request(self, url: str, callback: Callable[[QImage | None], None] | None = None) -> QImage | None:
        """Get-or-fetch. Returns image immediately if cached, else None and
        invokes ``callback`` later (on GUI thread). Always safe to call.
        """
        if not url:
            if callback:
                callback(None)
            return None
        img = self._mem.get(url)
        if img is not None:
            self._touch(url)
            return img
        img = self._maybe_load_disk(url)
        if img is not None:
            return img
        if callback is not None:
            self._waiting.setdefault(url, []).append(callback)
        self._fetch(url)
        return None

    def warm(self, urls: list[str]) -> None:
        """Pre-fetch a batch (e.g. all visible thumbs in a shelf)."""
        for u in urls:
            self.request(u)

    # ---------- internals ----------

    def _touch(self, url: str) -> None:
        try:
            self._mem.move_to_end(url)
        except KeyError:
            pass

    def _store(self, url: str, img: QImage) -> None:
        old = self._mem.pop(url, None)
        if old is not None:
            self._mem_bytes -= old.sizeInBytes()
        self._mem[url] = img
        self._mem_bytes += img.sizeInBytes()
        while len(self._mem) > 1 and (len(self._mem) > MEM_LRU_LIMIT
                                      or self._mem_bytes > MEM_BYTES_LIMIT):
            _, gone = self._mem.popitem(last=False)
            self._mem_bytes -= gone.sizeInBytes()

    def _read_disk(self, url: str) -> QImage | None:
        path = _disk_path(url)
        if not path.is_file():
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        img = QImage()
        return img if img.loadFromData(data) else None

    def _maybe_load_disk(self, url: str) -> QImage | None:
        img = self._read_disk(url)
        if img is None:
            return None
        self._store(url, img)
        return img

    def _save_disk(self, url: str, raw: bytes) -> None:
        path = _disk_path(url)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write via a unique temp + atomic replace rather than opening the
            # deterministic sha1 target directly: if someone pre-plants a
            # symlink at that name, write_bytes would follow it and clobber an
            # arbitrary file. mkstemp creates a fresh 0600 regular file, and
            # os.replace onto the final name never traverses a symlink target.
            fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".art.", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
                os.replace(tmp_name, path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError:
            pass

    def _fetch(self, url: str) -> None:
        if url in self._inflight:
            return
        # Reject non-http(s) before touching the network stack — a
        # file:///… or data: "thumbnail" from a hostile source must never
        # reach QNAM. Report a miss so callers drop the placeholder.
        if not _is_fetchable_art_url(url):
            self._fire_callbacks(url, None)
            return
        self._inflight.add(url)
        req = QNetworkRequest(QUrl(url))
        req.setTransferTimeout(ART_TRANSFER_TIMEOUT_MS)
        reply = self._net.get(req)

        def on_progress(received: int, _total: int) -> None:
            # Kill an oversized body mid-stream so a multi-GB / slow-loris
            # response can't grow client memory unbounded.
            if received > MAX_ART_BYTES:
                reply.abort()

        def on_finished():
            try:
                err = reply.error()
            except RuntimeError:
                self._inflight.discard(url)
                return
            if err != QNetworkReply.NoError:
                reply.deleteLater()
                self._inflight.discard(url)
                self._fire_callbacks(url, None)
                return
            data = bytes(reply.readAll().data())
            reply.deleteLater()
            self._inflight.discard(url)
            # Belt-and-suspenders: also drop anything that slipped past the
            # progress guard (e.g. a body delivered in one chunk).
            if len(data) > MAX_ART_BYTES:
                self._fire_callbacks(url, None)
                return
            img = QImage()
            if not img.loadFromData(data):
                self._fire_callbacks(url, None)
                return
            self._store(url, img)
            self._save_disk(url, data)
            self.image_loaded.emit(url, img)
            self._fire_callbacks(url, img)

        reply.downloadProgress.connect(on_progress)
        reply.finished.connect(on_finished)

    def _fire_callbacks(self, url: str, img: QImage | None) -> None:
        cbs = self._waiting.pop(url, [])
        for cb in cbs:
            try:
                cb(img)
            except Exception:
                pass


def _pix_bytes(pix: QPixmap) -> int:
    return pix.width() * pix.height() * 4


_instance: _ArtCache | None = None


def cache() -> _ArtCache:
    global _instance
    if _instance is None:
        _instance = _ArtCache()
    return _instance
