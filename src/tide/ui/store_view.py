"""The plugin store: its own page in the main window (design/plugin-store.md).

Two parts. ``StoreController`` owns the index, installs and uninstalls
off the gui thread, and knows what each plugin's button should say; it
has no widgets, so tests drive it directly. ``StoreView`` is the page: the
grid of cards with chips, search and the plugin of the week, and the
plugin's own page with its previews, needs, trust line, button and live
settings.

Installed means on, so there's no switch: install starts it, uninstall
stops it and moves its folder aside for the length of an undo toast.
Plugins you dropped in ``~/.config/tide/plugins/`` show here too, marked
"not from the store", with a turn on / turn off button instead.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QByteArray, QObject, QRectF, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QMovie, QPainter, QPainterPath, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QBoxLayout, QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
    QPushButton, QScrollArea, QSizePolicy, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from .. import config, qthreads, store, theming
from .flow import FlowLayout, ResponsiveGrid
from .headings import Heading
from .widgets import BracketButton

FETCH_AGE_S = 3600            # opening the page refetches an index older than this
DAILY_S = 24 * 3600           # and tide checks once a day while it runs
UNDO_MS = 10_000              # how long an uninstall can be undone

CHIPS = (("all", "all"), ("installed", "installed"), ("updates", "updates")) + tuple(
    (c, c) for c in store.CATEGORIES)

# what a plugin.toml / index `uses` word means, said plainly
CAN = {
    "playback": "see and control what's playing",
    "queue": "read and change the queue",
    "lyrics": "read the lyrics",
    "audio": "hear the music",
    "network": "talk to the network",
    "files": "read and write your files",
    "ui": "add things to tide's window",
    "vr": "draw in vr",
}


def can_line(uses) -> str:
    bits = [CAN[u] for u in uses if u in CAN]
    head = "can " + ", ".join(bits) + ". " if bits else ""
    return head + "it runs as you, with your permissions, like any plugin."


def size_text(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} mb"
    return f"{max(1, round(n / 1024))} kb"


def _dim(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setProperty("class", "dim")
    label.setWordWrap(True)
    label.setTextFormat(Qt.PlainText)
    return label


def _motion_ok() -> bool:
    from . import motion
    return motion.intensity().value != "off" and not motion.reduced_motion()


def _thumbnails() -> bool:
    from .track_row import show_thumbnails_for
    return show_thumbnails_for(theming.manager().current_effective())


# ---------------------------------------------------------------------------
# work off the gui thread
# ---------------------------------------------------------------------------

class _Worker(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(self, fn: Callable) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn(self.progress.emit)
        except store.StoreError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - one line for the ui
            self.failed.emit(f"something went wrong: {exc}"[:160])
            return
        self.done.emit(result)


GROUP = "store"


def _run(parent: QObject, fn: Callable, on_done: Callable, on_failed: Callable,
         on_progress: Callable | None = None) -> None:
    thread = QThread()
    worker = _Worker(fn)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    # through a receiver on the gui thread, so the callbacks run there
    relay = _Relay(parent, on_done, on_failed, on_progress)
    worker.done.connect(relay.done)
    worker.failed.connect(relay.failed)
    worker.progress.connect(relay.progress)
    worker.done.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.finished.connect(thread.deleteLater)
    thread.finished.connect(relay.deleteLater)
    qthreads.retain(thread, worker, GROUP)
    thread.start()


class _Relay(QObject):
    def __init__(self, parent, on_done, on_failed, on_progress) -> None:
        super().__init__(parent)
        self._done, self._failed, self._progress = on_done, on_failed, on_progress

    def done(self, result) -> None:
        self._done(result)

    def failed(self, text: str) -> None:
        self._failed(text)

    def progress(self, done: int, total: int) -> None:
        if self._progress is not None:
            self._progress(done, total)


# ---------------------------------------------------------------------------
# the controller
# ---------------------------------------------------------------------------

class StoreController(QObject):
    index_changed = Signal()           # a new index, or the offline line changed
    state_changed = Signal(str)        # one plugin's button or status
    progress = Signal(str, int, int)   # slug, bytes done, bytes total
    asset_ready = Signal(str)          # a banner / icon / preview landed (its sha256)
    updates_changed = Signal(int)

    def __init__(self, host, *, root: Path | None = None, cache_dir: Path | None = None,
                 settings_dir: Path | None = None, data_dir: Path | None = None,
                 pending_file: Path | None = None, toast: Callable | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.host = host
        self.root = root or config.PLUGIN_STORE_DIR
        self.cache_dir = cache_dir or config.PLUGIN_CACHE_DIR
        self.settings_dir = settings_dir or getattr(host, "settings_dir", config.PLUGIN_SETTINGS_DIR)
        self.data_dir = data_dir or getattr(host, "data_dir", config.PLUGIN_DATA_DIR)
        self.trash_dir = self.cache_dir / "trash"
        self.pending_file = pending_file or config.PLUGIN_PENDING_FILE
        self._pending_toasts: dict[str, str] = {}
        self._toast = toast
        self.index: store.Index | None = store.cached_index(self.cache_dir)
        self.offline = ""                 # why the store can't be reached, or ""
        self._fetching = False
        self._busy: dict[str, str] = {}   # slug -> installing / updating
        self._progress: dict[str, tuple[int, int]] = {}
        self._errors: dict[str, str] = {}
        self._assets_pending: set[str] = set()
        self._removed: store.Removed | None = None
        self._updates = self.update_count()
        # a quit inside an undo window leaves its trash behind
        store.empty_trash(self.trash_dir)
        host.changed.connect(self.state_changed)
        host.listing_changed.connect(self._on_listing)
        self._daily = QTimer(self)
        self._daily.setInterval(3600 * 1000)
        self._daily.timeout.connect(self.check_daily)

    def start_daily(self) -> None:
        """once a day while tide runs, and once now if it's been a day."""
        self._daily.start()
        self.check_daily()
        if self.index is not None and not self._fetching:
            self.install_pending()

    def check_daily(self) -> None:
        age = store.index_age(self.cache_dir)
        if age is None or age > DAILY_S:
            self.refresh(force=True)

    # ---- the index ----

    def refresh(self, force: bool = False) -> None:
        if self._fetching:
            return
        age = store.index_age(self.cache_dir)
        if not force and self.index is not None and age is not None and age < FETCH_AGE_S:
            return
        self._fetching = True
        cache = self.cache_dir
        _run(self, lambda _p: store.fetch_index(cache), self._fetched, self._fetch_failed)

    def _fetched(self, index: store.Index) -> None:
        self._fetching = False
        self.index = index
        self.offline = ""
        self.index_changed.emit()
        self._count_updates()
        self.install_pending()

    def install_pending(self) -> None:
        """what a moved feature or onboarding left for the store. anything
        that can't go in now stays on the list for next launch."""
        from .. import plugin_migrate
        p = plugin_migrate.pending(self.pending_file)
        for slug in list(p.install):
            if self.host.record(slug) is not None:
                plugin_migrate.finished(slug, self.pending_file)
                continue
            entry = self.entry(slug)
            if entry is None or store.cant_install(entry) or slug in self._busy:
                continue
            self._pending_toasts[slug] = plugin_migrate.toast_for(slug, slug in p.moved, entry.name)
            self.install(slug)

    def _fetch_failed(self, text: str) -> None:
        self._fetching = False
        self.offline = text
        self.index_changed.emit()

    def fetching(self) -> bool:
        return self._fetching

    def entries(self) -> list[store.Entry]:
        if self.index is None:
            return []
        return sorted(self.index.entries.values(), key=lambda e: e.name.lower())

    def entry(self, slug: str) -> store.Entry | None:
        return self.index.entry(slug) if self.index is not None else None

    def featured(self):
        return store.pick_featured(self.index) if self.index is not None else None

    def local_records(self) -> list:
        """plugins on this computer the index doesn't list: your own
        (``~/.config/tide/plugins``), a package's, or a store plugin the
        store has since dropped."""
        listed = set(self.index.entries) if self.index is not None else set()
        return [r for r in self.host.records()
                if r.manifest.origin != "store" or r.manifest.slug not in listed]

    def origin_note(self, rec) -> str:
        """how a plugin the index doesn't list got here."""
        if rec.manifest.origin != "store":
            return "not from the store"
        return "no longer in the store" if self.index is not None else ""

    def _on_listing(self) -> None:
        self.index_changed.emit()
        self._count_updates()

    # ---- what a plugin's button says ----

    def state(self, slug: str) -> tuple[str, str]:
        """(kind, label). kind: busy / install / cant / installed / update /
        yours. the label is the button's text, or the reason for cant."""
        busy = self._busy.get(slug)
        if busy:
            return "busy", busy
        rec = self.host.record(slug)
        entry = self.entry(slug)
        if rec is not None and rec.manifest.origin != "store":
            return "yours", "turn off" if self.host.is_enabled(slug) else "turn on"
        if rec is not None:
            if entry is not None and store.newer(entry.version, rec.manifest.version):
                why = store.cant_install(entry)
                return ("update", "update") if not why else ("installed", "uninstall")
            return "installed", "uninstall"
        if entry is None:
            return "cant", "not in the store"
        why = store.cant_install(entry)
        if why:
            return "cant", why
        return "install", "install"

    def status(self, slug: str) -> str:
        """a line under the button: what went wrong, or the plugin's own."""
        if slug in self._errors:
            return self._errors[slug]
        rec = self.host.record(slug)
        if rec is None:
            return ""
        if rec.manifest.origin == "yours" and self.entry(slug) is not None:
            # a folder of yours with the store's slug wins over the store's
            return (f"your own copy in {rec.manifest.path} is the one tide uses; "
                    "remove it to get the store's")
        if rec.running:
            return rec.detail or "on"
        return rec.blocked() or (rec.status if rec.status != "on" else "off")

    def progress_of(self, slug: str) -> tuple[int, int]:
        return self._progress.get(slug, (0, 0))

    def update_count(self) -> int:
        return sum(1 for r in self.host.records()
                   if r.manifest.origin == "store" and self.state(r.manifest.slug)[0] == "update")

    def _count_updates(self) -> None:
        n = self.update_count()
        if n != self._updates:
            self._updates = n
            self.updates_changed.emit(n)

    # ---- actions ----

    def press(self, slug: str, *, forget: bool = False) -> None:
        """what the plugin's main button does in its current state."""
        kind, _ = self.state(slug)
        if kind in ("install", "update"):
            self.install(slug)
        elif kind == "installed":
            self.uninstall(slug, forget=forget)
        elif kind == "yours":
            self.host.set_enabled(slug, not self.host.is_enabled(slug))
            self.state_changed.emit(slug)

    def install(self, slug: str) -> None:
        entry = self.entry(slug)
        if entry is None or slug in self._busy:
            return
        updating = self.host.record(slug) is not None
        self._busy[slug] = "updating" if updating else "installing"
        self._errors.pop(slug, None)
        self._progress[slug] = (0, entry.size)
        self.state_changed.emit(slug)
        root, cache, trash = self.root, self.cache_dir, self.trash_dir

        def work(progress):
            return store.install(entry, root, downloads_dir=cache / "downloads",
                                 trash_dir=trash, on_progress=progress)

        _run(self, work, lambda _p: self._installed(slug), lambda t: self._install_failed(slug, t),
             lambda d, t: self._on_progress(slug, d, t))

    def _on_progress(self, slug: str, done: int, total: int) -> None:
        self._progress[slug] = (done, total)
        self.progress.emit(slug, done, total)

    def _installed(self, slug: str) -> None:
        self._busy.pop(slug, None)
        self._progress.pop(slug, None)
        self.host.installed(slug)
        self.state_changed.emit(slug)
        self._count_updates()
        toast = self._pending_toasts.pop(slug, None)
        if toast is not None:
            from .. import plugin_migrate
            plugin_migrate.finished(slug, self.pending_file)
            self.toast(toast)

    def _install_failed(self, slug: str, text: str) -> None:
        self._pending_toasts.pop(slug, None)      # still pending: next launch
        self._busy.pop(slug, None)
        self._progress.pop(slug, None)
        self._errors[slug] = text
        self.state_changed.emit(slug)

    def uninstall(self, slug: str, *, forget: bool = False) -> store.Removed | None:
        rec = self.host.record(slug)
        if rec is None or rec.manifest.origin != "store" or slug in self._busy:
            return None
        name = rec.manifest.name
        self._finish_undo()               # one undo at a time
        self.host.release(slug)
        try:
            removed = store.uninstall(slug, self.root, settings_dir=self.settings_dir,
                                      data_dir=self.data_dir, trash_dir=self.trash_dir,
                                      forget=forget)
        except (store.StoreError, OSError) as exc:
            self._errors[slug] = str(exc)
            self.host.installed(slug)     # it's still there: start it again
            self.state_changed.emit(slug)
            return None
        self._errors.pop(slug, None)
        self.host.uninstalled(slug)
        self._removed = removed
        QTimer.singleShot(UNDO_MS + 1000, lambda r=removed: self._finish_undo(r))
        self.toast(f"uninstalled {name}", "undo", lambda r=removed: self.undo(r))
        self.state_changed.emit(slug)
        self._count_updates()
        return removed

    def undo(self, removed: store.Removed | None = None) -> bool:
        removed = removed or self._removed
        if removed is None or removed is not self._removed:
            return False
        self._removed = None
        try:
            store.undo_uninstall(removed)
        except (store.StoreError, OSError):
            return False
        self.host.installed(removed.slug)
        self.state_changed.emit(removed.slug)
        self._count_updates()
        return True

    def _finish_undo(self, removed: store.Removed | None = None) -> None:
        """the undo window closed: delete what the uninstall set aside."""
        if self._removed is None or (removed is not None and removed is not self._removed):
            return
        import shutil
        shutil.rmtree(self._removed.holder, ignore_errors=True)
        self._removed = None

    def toast(self, text: str, action_label: str | None = None,
              on_action: Callable | None = None) -> None:
        if self._toast is not None:
            self._toast(text, action_label, on_action)
            return
        win = getattr(self.host, "window", None)
        if win is not None and hasattr(win, "toast_host"):
            from .toast import show_toast
            show_toast(win.toast_host(), text, action_label=action_label, on_action=on_action,
                       lifetime_ms=UNDO_MS)

    # ---- pictures ----

    def asset(self, asset: store.Asset | None) -> Path | None:
        """the file if it's here, else None and asset_ready when it is."""
        if asset is None:
            return None
        hit = store.asset_path(asset, self.cache_dir)
        if hit is not None:
            return hit
        if asset.sha256 not in self._assets_pending:
            self._assets_pending.add(asset.sha256)
            cache = self.cache_dir

            def landed(_path, sha=asset.sha256):
                self._assets_pending.discard(sha)
                self.asset_ready.emit(sha)

            _run(self, lambda _p: store.fetch_asset(asset, cache), landed,
                 lambda _t, sha=asset.sha256: self._assets_pending.discard(sha))
        return None


# ---------------------------------------------------------------------------
# pictures
# ---------------------------------------------------------------------------

def _ink(key: str, default: str) -> QColor:
    t = theming.manager().current_effective()
    return QColor(t.token(key, default) if t is not None else default)


def _icon_pixmap(path: Path | None, px: int, color: QColor) -> QPixmap | None:
    """a plugin's icon.svg (24px outline in currentColor) in ``color``,
    stroked like the active icon set."""
    if path is None:
        return None
    try:
        svg = path.read_text(encoding="utf-8")
    except OSError:
        return None
    from . import icon_sets
    width = icon_sets._STROKE.get(icon_sets.active())
    svg = icon_sets._restroke(svg, width) if width else svg
    renderer = QSvgRenderer(QByteArray(svg.replace("currentColor", color.name()).encode("utf-8")))
    if not renderer.isValid():
        return None
    pix = QPixmap(px, px)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    renderer.render(p)
    p.end()
    return pix


class Banner(QWidget):
    """a 16:9 picture: the banner still, its moving cut while hovered (or
    always, for the plugin of the week), or the icon tile when the theme
    drops pictures."""

    def __init__(self, ctl: StoreController, *, autoplay: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctl = ctl
        self._autoplay = autoplay
        self._name = ""
        self._still: store.Asset | None = None
        self._motion: store.Asset | None = None
        self._icon: store.Asset | None = None
        self._icon_file: Path | None = None
        self._pix: QPixmap | None = None
        self._movie: QMovie | None = None
        self._hover = False
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        ctl.asset_ready.connect(self._on_asset)

    def set_art(self, name: str, still: store.Asset | None, motion: store.Asset | None = None,
                icon: store.Asset | None = None, icon_file: Path | None = None) -> None:
        self._name, self._still, self._motion, self._icon = name, still, motion, icon
        self._icon_file = icon_file
        self._load()

    ICON_ROW = 32       # its height when the theme drops pictures

    def set_width(self, w: int) -> None:
        self.setFixedSize(w, int(round(w * 9 / 16)) if _thumbnails() else self.ICON_ROW)

    def _load(self) -> None:
        self._pix = None
        path = self._ctl.asset(self._still) if _thumbnails() else None
        if path is not None:
            pix = QPixmap(str(path))
            self._pix = pix if not pix.isNull() else None
        if self._icon is not None and self._icon_file is None:
            self._icon_file = self._ctl.asset(self._icon)
        self._sync_movie()
        self.update()

    def _on_asset(self, sha: str) -> None:
        for a in (self._still, self._motion, self._icon):
            if a is not None and a.sha256 == sha:
                if a is self._icon:
                    self._icon_file = None
                self._load()
                return

    def _sync_movie(self) -> None:
        # only while it's on screen: a hidden store page used to decode the
        # week's moving banner from launch on, and tide lagged
        want = (self._motion is not None and _thumbnails() and _motion_ok()
                and (self._autoplay or self._hover) and self.isVisible())
        path = self._ctl.asset(self._motion) if want else None
        if path is None:
            if self._movie is not None:
                self._movie.stop()
                self._movie.deleteLater()
                self._movie = None
                self.update()
            return
        if self._movie is None:
            self._movie = QMovie(str(path), parent=self)
            self._movie.frameChanged.connect(lambda _f: self.update())
            self._movie.start()

    def enterEvent(self, ev) -> None:  # noqa: N802
        self._hover = True
        self._sync_movie()
        super().enterEvent(ev)

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        self._sync_movie()

    def hideEvent(self, ev) -> None:  # noqa: N802
        super().hideEvent(ev)
        self._sync_movie()

    def leaveEvent(self, ev) -> None:  # noqa: N802
        self._hover = False
        self._sync_movie()
        super().leaveEvent(ev)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        rect = QRectF(self.rect())
        modern = getattr(theming.manager().current_effective(), "aesthetic", "") == "modern"
        clip = QPainterPath()
        radius = 10.0 if modern else 0.0
        clip.addRoundedRect(rect, radius, radius)
        p.setClipPath(clip)
        frame = None
        if self._movie is not None:
            frame = self._movie.currentPixmap()
            frame = frame if not frame.isNull() else None
        pix = frame or self._pix
        if pix is not None:
            if frame is None:
                # the still: scaled once per size, not on every paint
                key = (pix.cacheKey(), self.width(), self.height())
                if getattr(self, "_scaled_key", None) != key:
                    self._scaled_key = key
                    self._scaled = pix.scaled(self.size(), Qt.KeepAspectRatioByExpanding,
                                              Qt.SmoothTransformation)
                scaled = self._scaled
            else:
                scaled = pix.scaled(self.size(), Qt.KeepAspectRatioByExpanding,
                                    Qt.FastTransformation)
            x = (scaled.width() - self.width()) // 2
            y = (scaled.height() - self.height()) // 2
            p.drawPixmap(0, 0, scaled, x, y, self.width(), self.height())
            return
        fg = _ink("fg", "#e6e6e6")
        if not _thumbnails():
            # the theme drops pictures: the icon, small, like a nav icon
            side = 24
            icon = _icon_pixmap(self._icon_file, side, fg)
            if icon is not None:
                p.drawPixmap(0, (self.height() - side) // 2, icon)
            else:
                p.setPen(fg)
                p.drawText(QRectF(0, 0, side, self.height()), Qt.AlignCenter,
                           f"[{self._name[:1] or '?'}]")
            return
        # no picture yet: the icon tile
        p.fillRect(rect, _ink("bg_alt", "#1a1a1a"))
        side = int(min(self.width(), self.height()) * 0.36)
        icon = _icon_pixmap(self._icon_file, side, fg)
        if icon is not None:
            p.drawPixmap((self.width() - side) // 2, (self.height() - side) // 2, icon)
            return
        f = QFont(self.font())
        f.setPixelSize(max(12, side))
        p.setFont(f)
        p.setPen(fg)
        p.drawText(rect, Qt.AlignCenter, (self._name[:1] or "?"))


# ---------------------------------------------------------------------------
# the button every plugin has
# ---------------------------------------------------------------------------

class InstalledMark(QWidget):
    """a drawn check and "installed": the ✓ character isn't in every
    font, and a fallback glyph read as a square root sign."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from . import icon_sets
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.icon = QLabel()
        svg = icon_sets.svg_for("check")
        ink = _ink("accent", "#d4b95e")
        if svg:
            renderer = QSvgRenderer(QByteArray(svg.replace("currentColor", ink.name()).encode()))
            pix = QPixmap(16, 16)
            pix.fill(Qt.transparent)
            p = QPainter(pix)
            p.setRenderHint(QPainter.Antialiasing)
            renderer.render(p)
            p.end()
            self.icon.setPixmap(pix)
        self.text = _dim(theming.styled_case("installed"))
        row.addWidget(self.icon)
        row.addWidget(self.text)
        row.addStretch(1)


class OfficialBadge(QWidget):
    """a filled circle with a check cut out, in the live accent, so it
    follows the cover when the adaptive colour is on. Filled so it doesn't
    read as the installed mark's plain check."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        side = max(12, self.fontMetrics().ascent())
        self.setFixedSize(side, side)
        self.setToolTip(theming.styled_case("official"))
        theming.manager().theme_changed.connect(self._on_theme)

    def _on_theme(self, *_args) -> None:
        self.update()

    def paintEvent(self, _ev) -> None:  # noqa: N802
        s = float(min(self.width(), self.height()))
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(_ink("accent", "#d4b95e"))
        p.drawEllipse(QRectF(0, 0, s, s))
        from PySide6.QtGui import QPen
        pen = QPen(_ink("bg", "#101010"), max(1.4, s * 0.13))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        tick = QPainterPath()
        tick.moveTo(s * 0.28, s * 0.52)
        tick.lineTo(s * 0.44, s * 0.68)
        tick.lineTo(s * 0.73, s * 0.36)
        p.drawPath(tick)
        p.end()


class AuthorLine(QWidget):
    """who made it, then the official badge when the store says so."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.text = _dim()
        self.text.setWordWrap(False)
        self.badge = OfficialBadge()
        self.badge.hide()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)
        row.addWidget(self.text)
        row.addWidget(self.badge, 0, Qt.AlignVCenter)
        row.addStretch(1)

    def set_author(self, text: str, official: bool) -> None:
        self.text.setText(text)
        self.badge.setVisible(official)

    def official(self) -> bool:
        return not self.badge.isHidden()


class PluginButton(QWidget):
    """install / installing (a bar) / ✓ installed / uninstall / update /
    the reason it can't. ``compact`` is the card's: an installed plugin
    shows the mark instead of a button."""

    def __init__(self, ctl: StoreController, slug: str, *, compact: bool = False,
                 forget: Callable[[], bool] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctl, self.slug, self._compact = ctl, slug, compact
        self._forget = forget
        self.button = BracketButton("install")
        self.button.clicked.connect(self._press)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.hide()
        self.mark = InstalledMark()
        self.mark.hide()
        self.why = _dim()
        self.why.hide()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self.button)
        row.addWidget(self.bar, 1)
        row.addWidget(self.mark)
        row.addWidget(self.why, 1)
        row.addStretch(0)
        ctl.state_changed.connect(self._on_state)
        ctl.progress.connect(self._on_progress)
        self.refresh()

    def _press(self) -> None:
        self._ctl.press(self.slug, forget=bool(self._forget and self._forget()))

    def _on_state(self, slug: str) -> None:
        if slug == self.slug:
            self.refresh()

    def _on_progress(self, slug: str, done: int, total: int) -> None:
        if slug == self.slug and total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000 * min(done, total) / total))

    def refresh(self) -> None:
        kind, label = self._ctl.state(self.slug)
        self.button.setLabel(label)
        busy = kind == "busy"
        self.bar.setVisible(busy)
        if busy:
            done, total = self._ctl.progress_of(self.slug)
            self.bar.setRange(0, 1000 if total else 0)
            if total:
                self.bar.setValue(int(1000 * min(done, total) / total))
        show_mark = self._compact and kind == "installed"
        self.mark.setVisible(show_mark)
        self.why.setVisible(kind == "cant")
        self.why.setText(label if kind == "cant" else "")
        self.button.setVisible(not busy and not show_mark and kind != "cant")

    def kind(self) -> str:
        return self._ctl.state(self.slug)[0]


# ---------------------------------------------------------------------------
# the grid
# ---------------------------------------------------------------------------

class StoreCard(QFrame):
    """banner, name, author, one line, the button."""

    opened = Signal(str)

    def __init__(self, ctl: StoreController, slug: str, name: str, author: str, official: bool,
                 blurb: str, *, banner=None, motion=None, icon=None, icon_file=None,
                 note: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("storeCard")
        self.slug = slug
        self.setCursor(Qt.PointingHandCursor)
        self.banner = Banner(ctl)
        self.banner.set_art(name, banner, motion, icon, icon_file)
        self.name = QLabel(theming.styled_case(name))
        self.name.setTextFormat(Qt.PlainText)
        who = author or "someone"
        self.author = AuthorLine()
        self.author.set_author(f"{who} · {note}" if note else who, official)
        self.blurb = _dim(blurb)
        # always three lines tall (a mono font needs the third), so the
        # buttons in a row line up
        self.blurb.setFixedHeight(self.blurb.fontMetrics().lineSpacing() * 3 + 2)
        self.blurb.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.button = PluginButton(ctl, slug, compact=True)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 6)
        col.setSpacing(4)
        col.addWidget(self.banner)
        col.addWidget(self.name)
        col.addWidget(self.author)
        col.addWidget(self.blurb)
        col.addWidget(self.button)

    # ResponsiveGrid's tile protocol
    def set_tile_width(self, w: int) -> None:
        self.setFixedWidth(w)
        self.banner.set_width(w)

    def tile_height(self, w: int) -> int:
        self.set_tile_width(w)
        lay = self.layout()
        return lay.totalHeightForWidth(w) if lay.hasHeightForWidth() else lay.totalSizeHint().height()

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton:
            self.opened.emit(self.slug)
        super().mouseReleaseEvent(ev)


class FeaturedTile(QFrame):
    """the plugin of the week, or the newest one labelled new."""

    opened = Signal(str)

    def __init__(self, ctl: StoreController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("storeFeatured")
        self._ctl = ctl
        self.slug = ""
        self.setCursor(Qt.PointingHandCursor)
        self.banner = Banner(ctl, autoplay=True)
        # "plugin of the week" / "new", as a small accent tag
        self.label = QLabel()
        self.label.setObjectName("storeFeaturedTag")
        self.label.setTextFormat(Qt.PlainText)
        self.label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.name = Heading("", 40)
        self.author = AuthorLine()
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setTextFormat(Qt.PlainText)
        self.button_slot = QHBoxLayout()
        self.button: PluginButton | None = None
        text = QVBoxLayout()
        text.setSpacing(8)
        text.addStretch(1)
        text.addWidget(self.label)
        text.addWidget(self.name)
        text.addWidget(self.author)
        text.addWidget(self.note)
        text.addSpacing(4)
        text.addLayout(self.button_slot)
        text.addStretch(1)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(24)
        row.addWidget(self.banner, 0, Qt.AlignVCenter)
        row.addLayout(text, 1)
        theming.manager().theme_changed.connect(self._style_tag)
        self._style_tag()

    def _style_tag(self, *_args) -> None:
        accent = _ink("accent", "#d4b95e").name()
        modern = getattr(theming.manager().current_effective(), "aesthetic", "") == "modern"
        radius = 9 if modern else 0
        self.label.setStyleSheet(
            f"#storeFeaturedTag {{ color: {accent}; border: 1px solid {accent}; "
            f"border-radius: {radius}px; padding: 1px 8px; }}")

    def show_pick(self, pick) -> None:
        if pick is None:
            self.hide()
            return
        entry, label, note = pick
        self.slug = entry.slug
        self.label.setText(theming.styled_case(label))
        self.name.set_label(entry.name)
        who = entry.author or "someone"
        self.author.set_author(who, entry.official)
        self.note.setText(note)
        self.banner.set_art(entry.name, entry.banner_full or entry.banner, entry.banner_motion,
                            entry.icon)
        self.banner.setHidden(not _thumbnails())
        if self.button is not None:
            self.button.setParent(None)
            self.button.deleteLater()
        self.button = PluginButton(self._ctl, entry.slug, compact=True)
        self.button_slot.addWidget(self.button)
        self.show()

    def resizeEvent(self, ev) -> None:  # noqa: N802
        # half the row, and never taller than 360 px of a wide window
        w = max(160, int(self.width() * 0.55))
        self.banner.set_width(min(w, 640))
        super().resizeEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton and self.slug:
            self.opened.emit(self.slug)
        super().mouseReleaseEvent(ev)


class _GridPage(QWidget):
    opened = Signal(str)

    def __init__(self, ctl: StoreController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctl = ctl
        self.filter = "all"
        self.heading = Heading("plugins")
        self.search = QLineEdit()
        self.search.setPlaceholderText("search plugins")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(260)
        self.search.textChanged.connect(lambda _t: self.rebuild())
        top = QHBoxLayout()
        top.addWidget(self.heading, 1)
        top.addWidget(self.search)

        self.chips: dict[str, BracketButton] = {}
        chips = FlowLayout(h_gap=6, v_gap=6)
        for key, label in CHIPS:
            b = BracketButton(label)
            b.setRole("pill")
            b.clicked.connect(lambda _c=False, k=key: self.set_filter(k))
            self.chips[key] = b
            chips.addWidget(b)
        chip_box = QWidget()
        chip_box.setLayout(chips)

        self.line = _dim()
        self.featured = FeaturedTile(ctl)
        self.featured.opened.connect(self.opened)
        # cards grow to fill a row (ten plugins fill rows of four at 1700 px)
        self.grid = ResponsiveGrid(min_tile=240, max_tile=460, gap=16, row_gap=22)
        self.empty = _dim()
        self.broken_head = Heading("couldn't read", 34)
        self.broken = _dim()
        self.broken.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.trust = _dim("plugins are python that runs inside tide with your permissions. "
                          "every one in the store has its code a click away on its page.")

        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(24, 18, 24, 24)
        col.setSpacing(12)
        col.addLayout(top)
        col.addWidget(chip_box)
        col.addWidget(self.line)
        col.addWidget(self.featured)
        col.addWidget(self.grid)
        col.addWidget(self.empty)
        col.addWidget(self.broken_head)
        col.addWidget(self.broken)
        col.addStretch(1)
        col.addWidget(self.trust)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        ctl.index_changed.connect(self.rebuild)
        ctl.state_changed.connect(self._on_state)
        # pictures or icons follow the theme's show_thumbnails
        theming.manager().theme_changed.connect(self._on_theme)
        self._sync_chips()
        self.rebuild()

    def _on_theme(self, *_args) -> None:
        self.rebuild()

    def set_filter(self, key: str) -> None:
        self.filter = key
        self._sync_chips()
        self.rebuild()

    def _sync_chips(self) -> None:
        for key, b in self.chips.items():
            b.setActiveState(key == self.filter)

    def _on_state(self, _slug: str) -> None:
        # installed / updates filters change membership when a state does
        if self.filter in ("installed", "updates"):
            self.rebuild()

    def _matches(self, slug: str, name: str, blurb: str, category: str, author: str) -> bool:
        q = self.search.text().strip().lower()
        if q and q not in f"{name} {blurb} {author} {slug}".lower():
            return False
        kind = self._ctl.state(slug)[0]
        if self.filter == "installed":
            return kind in ("installed", "update", "yours", "busy")
        if self.filter == "updates":
            return kind == "update"
        if self.filter in store.CATEGORIES:
            return category == self.filter
        return True

    def cards(self) -> list[StoreCard]:
        return [t for t in self.grid.tiles() if isinstance(t, StoreCard)]

    def rebuild(self) -> None:
        ctl = self._ctl
        self.grid.clear()
        if ctl.index is None and not ctl.offline:
            self.line.setText("looking for the store..." if ctl.fetching() else "")
        elif ctl.offline:
            self.line.setText(f"{ctl.offline}. your plugins still work.")
        else:
            self.line.setText("")
        if getattr(ctl.host, "paused_run", False):
            self.line.setText("plugins are off for this run (tide --no-plugins). "
                              "what you install starts next launch.")
        self.line.setVisible(bool(self.line.text()))
        pick = ctl.featured()
        show_pick = pick is not None and self.filter == "all" and not self.search.text().strip()
        self.featured.show_pick(pick if show_pick else None)
        shown = 0
        for e in ctl.entries():
            if not self._matches(e.slug, e.name, e.blurb, e.category, e.author):
                continue
            card = StoreCard(ctl, e.slug, e.name, e.author, e.official, e.blurb,
                             banner=e.banner, motion=e.banner_motion, icon=e.icon)
            card.opened.connect(self.opened)
            self.grid.add(card)
            shown += 1
        for rec in ctl.local_records():
            m = rec.manifest
            if not self._matches(m.slug, m.name, m.description, "", ""):
                continue
            icon = m.path / "icon.svg"
            note = ctl.origin_note(rec)
            card = StoreCard(ctl, m.slug, m.name, "", False, m.description,
                             icon_file=icon if icon.is_file() else None)
            card.author.set_author(note, False)
            card.author.setVisible(bool(note))
            card.opened.connect(self.opened)
            self.grid.add(card)
            shown += 1
        if shown:
            self.empty.setText("")
        elif self.search.text().strip():
            self.empty.setText("nothing matches that.")
        elif self.filter == "updates":
            self.empty.setText("everything's up to date.")
        elif self.filter == "installed":
            self.empty.setText("nothing installed yet.")
        elif ctl.index is None:
            self.empty.setText("")
        else:
            self.empty.setText("nothing here yet.")
        self.empty.setVisible(bool(self.empty.text()))
        # plugin folders whose plugin.toml didn't parse, so a typo isn't silent
        broken = getattr(ctl.host, "broken", []) or []
        self.broken.setText("\n".join(f"{path}: {why}" for path, why in broken))
        self.broken_head.setVisible(bool(broken))
        self.broken.setVisible(bool(broken))


# ---------------------------------------------------------------------------
# a plugin's page
# ---------------------------------------------------------------------------

class SettingsForm(QWidget):
    """a plugin's plugin.toml settings, applied as you change them."""

    def __init__(self, host, slug: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.host, self.slug = host, slug
        self.widgets: dict[str, QWidget] = {}
        self.form = QFormLayout(self)
        self.form.setContentsMargins(0, 0, 0, 0)
        rec = host.record(slug)
        if rec is None:
            return
        for d in rec.manifest.settings:
            w = self._widget(rec, d)
            self.widgets[d.key] = w
            row: QWidget | QHBoxLayout = w
            if d.help_url:
                row = QHBoxLayout()
                row.addWidget(w, 1)
                helper = QPushButton("get one  →")
                helper.clicked.connect(lambda _c=False, url=d.help_url:
                                       QDesktopServices.openUrl(QUrl(url)))
                row.addWidget(helper)
            if d.kind in ("bool", "action"):
                self.form.addRow("", row)
            else:
                self.form.addRow(f"{d.label}:", row)
        self.sync()

    def _widget(self, rec, d) -> QWidget:
        value = None if d.kind == "action" else rec.settings[d.key]
        key = d.key
        if d.kind == "bool":
            w = QCheckBox(d.label)
            w.setChecked(bool(value))
            w.toggled.connect(lambda on, k=key: self._apply(k, bool(on)))
        elif d.kind == "choice":
            w = QComboBox()
            rows = d.choices if isinstance(d.choices, tuple) else ((value, str(value)),)
            for v, label in rows:
                w.addItem(label, v)
            w.setCurrentIndex(max(0, w.findData(value)))
            w.currentIndexChanged.connect(lambda _i, k=key, box=w: self._apply(k, box.currentData()))
        elif d.kind == "int":
            w = QSpinBox()
            w.setRange(-1_000_000, 1_000_000)
            w.setValue(int(value))
            w.setKeyboardTracking(False)
            w.valueChanged.connect(lambda v, k=key: self._apply(k, int(v)))
        elif d.kind == "str":
            w = QLineEdit(str(value))
            if d.secret:
                w.setEchoMode(QLineEdit.Password)
            w.editingFinished.connect(lambda k=key, box=w: self._apply(k, box.text()))
        else:                                  # action
            w = QPushButton(d.label)
            w.clicked.connect(lambda _c=False, k=key: self._action(k))
        if d.tooltip:
            w.setToolTip(d.tooltip)
        return w

    def _apply(self, key: str, value) -> None:
        rec = self.host.record(self.slug)
        if rec is None:
            return
        try:
            changed = rec.settings.set(key, value)
        except ValueError:
            return
        if changed and rec.running:
            self.host.apply_settings(self.slug, {key})

    def _action(self, key: str) -> None:
        rec = self.host.record(self.slug)
        handler = getattr(rec.instance if rec is not None else None, "on_action", None)
        if callable(handler):
            handler(key)

    def sync(self) -> None:
        """actions need a running plugin; values stay editable."""
        rec = self.host.record(self.slug)
        running = rec is not None and rec.running
        for key, w in self.widgets.items():
            if rec is not None and rec.settings.desc(key).kind == "action":
                w.setEnabled(running)


class _Thumb(QWidget):
    """one preview in the strip under the viewer. a clip gets a play mark."""

    picked = Signal(int)

    def __init__(self, ctl: StoreController, i: int, item: store.Preview,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctl, self._i, self._item = ctl, i, item
        self.current = False
        self.setFixedSize(96, 54)
        self.setCursor(Qt.PointingHandCursor)
        ctl.asset_ready.connect(self._on_asset)

    def _on_asset(self, sha: str) -> None:
        if sha == self._item.asset.sha256:
            self.update()

    def set_current(self, on: bool) -> None:
        self.current = bool(on)
        self.update()

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton:
            self.picked.emit(self._i)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = self._ctl.asset(self._item.asset)
        pix = QPixmap(str(path)) if path is not None else QPixmap()
        if not pix.isNull():
            scaled = pix.scaled(self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            p.drawPixmap(0, 0, scaled, (scaled.width() - self.width()) // 2,
                         (scaled.height() - self.height()) // 2, self.width(), self.height())
        else:
            p.fillRect(rect, _ink("bg_alt", "#1a1a1a"))
        if self._item.kind == "clip":
            p.setPen(QColor("#ffffff"))
            p.drawText(rect, Qt.AlignCenter, "▶")
        if self.current:
            p.setPen(_ink("accent", "#d4b95e"))
            p.setBrush(Qt.NoBrush)
            p.drawRect(rect)


class Previews(QWidget):
    """the banner, then the author's clips and screenshots in their order:
    one big viewer and a strip of thumbnails under it. clips play muted
    and loop while they're the big one."""

    MAX_W = 760

    def __init__(self, ctl: StoreController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctl = ctl
        self._name = ""
        self._items: list[store.Preview] = []
        self.big = Banner(ctl, autoplay=True)
        self.strip = QHBoxLayout()
        self.strip.setSpacing(8)
        self._thumbs: list[_Thumb] = []
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        col.addWidget(self.big)
        col.addLayout(self.strip)

    def set_items(self, name: str, items) -> None:
        self._name = name
        self._items = list(items)
        while self.strip.count():
            item = self.strip.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._thumbs = []
        if len(self._items) > 1:
            for i, item in enumerate(self._items):
                t = _Thumb(self._ctl, i, item)
                t.picked.connect(self.show_item)
                self.strip.addWidget(t)
                self._thumbs.append(t)
            self.strip.addStretch(1)
        self.setHidden(not self._items or not _thumbnails())
        if self._items:
            self.show_item(0)

    def current(self) -> int:
        return next((i for i, t in enumerate(self._thumbs) if t.current), 0)

    def show_item(self, i: int) -> None:
        item = self._items[i]
        if item.kind == "clip":
            # its first frame is the still (reduced motion stops there)
            self.big.set_art(self._name, item.asset, item.asset)
        else:
            self.big.set_art(self._name, item.asset)
        for k, t in enumerate(self._thumbs):
            t.set_current(k == i)

    def resizeEvent(self, ev) -> None:  # noqa: N802
        self.big.set_width(min(self.width(), self.MAX_W))
        super().resizeEvent(ev)


class PluginPage(QWidget):
    back = Signal()
    opened = Signal(str)        # another plugin, from "more plugins"

    INFO_MAX = 620          # a readable line, however wide the window
    SIDE_BY_SIDE = 1150     # from this width, pictures left and facts right

    def __init__(self, ctl: StoreController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctl = ctl
        self.slug = ""
        self.back_btn = BracketButton("back")
        self.back_btn.setIconKey("back")
        self.back_btn.clicked.connect(self.back)
        self.banner = Banner(ctl)
        self.previews = Previews(ctl)
        self.name = Heading("", 40)
        self.author = AuthorLine()
        self.blurb = QLabel()
        self.blurb.setWordWrap(True)
        self.blurb.setTextFormat(Qt.PlainText)
        self.about = _dim()
        self.needs_head = _dim("needs")
        self.needs = QVBoxLayout()
        self.needs.setSpacing(4)
        self.can = _dim()
        self.facts = _dim()
        self.repo_btn = BracketButton("read the code")
        self.repo_btn.clicked.connect(self._open_repo)
        self._repo = ""
        self.forget = QCheckBox("forget my settings")
        self.reload_btn = BracketButton("reload")
        self.reload_btn.setToolTip("stop it, read its plugin.toml and code again, start it")
        self.reload_btn.clicked.connect(self._reload)
        self.button_row = QHBoxLayout()
        self.button: PluginButton | None = None
        self.status = _dim()
        self.settings_head = Heading("settings", 34)
        self.settings_slot = QVBoxLayout()
        self.form: SettingsForm | None = None

        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(24, 18, 24, 24)
        col.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(self.back_btn)
        top.addStretch(1)
        top.addWidget(self.repo_btn)
        col.addLayout(top)
        # pictures and the facts side by side on a wide window, stacked on
        # a narrow one (resizeEvent flips it)
        self._split = QBoxLayout(QBoxLayout.TopToBottom)
        self._split.setSpacing(24)
        pics = QVBoxLayout()
        pics.setSpacing(10)
        pics.addWidget(self.banner, 0, Qt.AlignLeft)
        pics.addWidget(self.previews, 0, Qt.AlignLeft)
        pics.addStretch(1)
        self.info = QWidget()
        self.info.setMaximumWidth(self.INFO_MAX)
        info = QVBoxLayout(self.info)
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(10)
        info.addWidget(self.name)
        info.addWidget(self.author)
        info.addWidget(self.blurb)
        info.addWidget(self.about)
        info.addWidget(self.needs_head)
        info.addLayout(self.needs)
        info.addWidget(self.can)
        info.addWidget(self.facts)
        info.addLayout(self.button_row)
        info.addWidget(self.status)
        info.addWidget(self.settings_head)
        info.addLayout(self.settings_slot)
        info.addStretch(1)
        self._split.addLayout(pics)
        self._split.addWidget(self.info, 1)
        self._split.addStretch(0)
        col.addLayout(self._split)
        # under it: a row of other plugins, same kind first, so the page
        # doesn't end in a screen of nothing
        col.addSpacing(18)
        self.more_head = Heading("more plugins", 34)
        self.more = ResponsiveGrid(min_tile=240, max_tile=460, gap=16, row_gap=22, max_rows=1)
        col.addWidget(self.more_head)
        col.addWidget(self.more)
        col.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(body)
        self._scroll = scroll
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        ctl.state_changed.connect(self._on_state)
        ctl.index_changed.connect(self._on_index)

    def _reload(self) -> None:
        if self.slug:
            self._ctl.host.reload(self.slug)
            self.show_plugin(self.slug)

    def _on_index(self) -> None:
        if self.slug:
            self.show_plugin(self.slug)

    def _open_repo(self) -> None:
        if self._repo:
            QDesktopServices.openUrl(QUrl(self._repo))

    def show_plugin(self, slug: str) -> None:
        ctl = self._ctl
        self.slug = slug
        entry = ctl.entry(slug)
        rec = ctl.host.record(slug)
        if entry is None and rec is None:
            self.back.emit()
            return
        m = rec.manifest if rec is not None else None
        name = entry.name if entry is not None else m.name
        self.name.set_label(name)
        if entry is not None:
            who = entry.author or "someone"
            self.author.set_author(f"by {who}", entry.official)
            self.blurb.setText(entry.blurb)
            self.about.setText(entry.about)
            # the icon tile, for themes that drop pictures
            self.banner.set_art(name, None, None, entry.icon)
            head = store.Preview("image", entry.banner_full or entry.banner)
            self.previews.set_items(name, (head,) + tuple(entry.previews))
            self._repo = entry.repo
            installed = f" · you have {m.version}" if m is not None and m.version != entry.version \
                else ""
            self.facts.setText(f"version {entry.version}{installed} · {size_text(entry.size)}")
            uses = entry.uses
            needs = entry.needs
        else:
            self.author.set_author(ctl.origin_note(rec), False)
            self.blurb.setText(m.description)
            self.about.setText("")
            icon = m.path / "icon.svg"
            self.banner.set_art(name, None, None, None, icon if icon.is_file() else None)
            self.previews.set_items(name, ())
            self._repo = ""
            self.facts.setText(f"version {m.version} · in {m.path}")
            uses = m.uses
            needs = ()
        self.about.setVisible(bool(self.about.text()))
        self.banner.setHidden(not self.previews.isHidden())
        self.repo_btn.setVisible(bool(self._repo))
        self._fill_needs(needs)
        self.can.setText(can_line(uses))
        if self.button is not None:
            self.button_row.removeWidget(self.button)
            self.button.setParent(None)
            self.button.deleteLater()
        self.button = PluginButton(ctl, slug, forget=self.forget.isChecked)
        self.button_row.addWidget(self.button, 1)
        self.button_row.addWidget(self.forget)
        self.button_row.addWidget(self.reload_btn)
        self.forget.setChecked(False)
        self._build_form()
        self._fill_more(entry)
        self._on_state(slug)
        self._scroll.verticalScrollBar().setValue(0)

    MORE = 4

    def _fill_more(self, entry) -> None:
        self.more.clear()
        others = [e for e in self._ctl.entries() if e.slug != self.slug]
        if entry is not None:
            # same category first, then the rest, newest first within each
            others.sort(key=lambda e: (e.category != entry.category, e.added == "", e.name))
        picks = others[:self.MORE]
        for e in picks:
            card = StoreCard(self._ctl, e.slug, e.name, e.author, e.official, e.blurb,
                             banner=e.banner, motion=e.banner_motion, icon=e.icon)
            card.opened.connect(self.opened)
            self.more.add(card)
        self.more_head.setVisible(bool(picks))
        self.more.setVisible(bool(picks))

    def _fill_needs(self, needs) -> None:
        while self.needs.count():
            item = self.needs.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for need in needs:
            ok = store.check_need(need)
            mark = {True: "✓", False: "✗", None: "·"}[ok]
            text = f"{mark} {need.what}"
            if ok is False and need.how:
                text += f": {need.how}"
            label = _dim(text)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)   # copy the command
            self.needs.addWidget(label)
        self.needs_head.setVisible(bool(needs))

    def _build_form(self) -> None:
        if self.form is not None:
            self.settings_slot.removeWidget(self.form)
            self.form.setParent(None)
            self.form.deleteLater()
            self.form = None
        rec = self._ctl.host.record(self.slug)
        if rec is not None and rec.manifest.settings:
            self.form = SettingsForm(self._ctl.host, self.slug)
            self.settings_slot.addWidget(self.form)
        self.settings_head.setVisible(self.form is not None)

    def _on_state(self, slug: str) -> None:
        if slug != self.slug or not slug:
            return
        rec = self._ctl.host.record(slug)
        kind = self._ctl.state(slug)[0]
        self.forget.setVisible(kind in ("installed", "update"))
        self.reload_btn.setVisible(kind == "yours")
        self.status.setText(self._ctl.status(slug))
        self.status.setVisible(bool(self.status.text()))
        # a plugin that came or went gets its settings form built or dropped
        has_form = self.form is not None
        wants_form = rec is not None and bool(rec.manifest.settings)
        if has_form != wants_form:
            self._build_form()
        elif self.form is not None:
            self.form.sync()

    def resizeEvent(self, ev) -> None:  # noqa: N802
        wide = self.width() >= self.SIDE_BY_SIDE
        self._split.setDirection(QBoxLayout.LeftToRight if wide else QBoxLayout.TopToBottom)
        room = self.width() - 48 - (self.INFO_MAX + 24 if wide else 0)
        self.banner.set_width(min(max(200, room), Previews.MAX_W))
        self.previews.setFixedWidth(min(max(200, room), Previews.MAX_W))
        super().resizeEvent(ev)


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------

class StoreView(QWidget):
    def __init__(self, ctl: StoreController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctl = ctl
        self.stack = QStackedWidget()
        self.grid = _GridPage(ctl)
        self.page = PluginPage(ctl)
        self.stack.addWidget(self.grid)
        self.stack.addWidget(self.page)
        self.grid.opened.connect(self.open_plugin)
        self.page.back.connect(lambda: self.stack.setCurrentIndex(0))
        self.page.opened.connect(self.open_plugin)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.addWidget(self.stack)

    def shown(self) -> None:
        """the nav button brought the page up: a plugin folder you added or
        edited shows up now, and the index refreshes if it's old."""
        self.ctl.host.rescan()
        self.ctl.refresh()

    def open_plugin(self, slug: str) -> None:
        self.page.show_plugin(slug)
        self.stack.setCurrentIndex(1)


class UpdateDot(QWidget):
    """the dot on the nav button when updates are waiting."""

    def __init__(self, button: QWidget) -> None:
        super().__init__(button)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFixedSize(QSize(8, 8))
        button.installEventFilter(self)
        self.hide()

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        if ev.type() in (ev.Type.Resize, ev.Type.Show):
            self._place()
        return False

    def _place(self) -> None:
        btn = self.parentWidget()
        self.move(btn.width() - self.width() - 6, 6)
        self.raise_()

    def set_count(self, n: int) -> None:
        self.setVisible(n > 0)
        self.setToolTip(f"{n} update{'s' if n != 1 else ''} waiting" if n else "")
        self._place()

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(_ink("accent", "#d4b95e"))
        p.drawEllipse(self.rect())

