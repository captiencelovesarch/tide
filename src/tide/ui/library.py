"""Library view: playlists + (v1.5) the rest of the actual library.

The index page grew tabs. "playlists" is the classic list; sources with
the ``library_full`` capability also get songs / albums / artists /
following, each lazily loaded on first open:

  - playlists  → list; activate opens the detail page below
  - songs      → the saved-songs list (recently-added order)
  - albums     → card grid → album_requested
  - artists    → circular card grid → artist_requested (library artists)
  - following  → circular card grid → artist_requested (subscriptions)

The detail page keeps its play-all/shuffle behavior and, when the source
supports ``playlist_edit``, adds [rename] [delete] and a per-track
"remove from playlist" (YT needs the setVideoId that rides in the raw
playlist item — tracks fetched any other way just don't offer removal).

All actions bubble up to MainWindow via signals so playback state stays in
one place.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import api, qthreads, theming
from .card import Card, CardGrid
from .headings import Heading, line_heading as _line_heading
from .track_row import TrackRowDelegate
from .widgets import BracketButton


class _PlaylistsWorker(QObject):
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, api_obj: api.Api) -> None:
        super().__init__()
        self.api = api_obj

    def run(self) -> None:
        try:
            self.done.emit(self.api.get_library_playlists())
        except Exception as exc:
            self.failed.emit(str(exc))


class _PlaylistDetailWorker(QObject):
    done = Signal(int, object)      # request gen, PlaylistDetail
    failed = Signal(int, str)       # request gen, error

    def __init__(self, api_obj: api.Api, playlist_id: str, gen: int) -> None:
        super().__init__()
        self.api = api_obj
        self.playlist_id = playlist_id
        self.gen = gen

    def run(self) -> None:
        try:
            self.done.emit(self.gen, self.api.get_playlist(self.playlist_id))
        except Exception as exc:
            self.failed.emit(self.gen, str(exc))


class _LibraryTabWorker(QObject):
    """One worker shape for the four v1.5 tabs — the tab name rides along
    so a straggler can't paint into a tab it wasn't fetched for."""
    done = Signal(int, str, list)   # gen, tab, entries
    failed = Signal(int, str, str)  # gen, tab, error

    def __init__(self, api_obj: api.Api, tab: str, gen: int) -> None:
        super().__init__()
        self.api = api_obj
        self.tab = tab
        self.gen = gen

    def run(self) -> None:
        try:
            if self.tab == "songs":
                out = self.api.get_library_songs_list(order="recently_added")
            elif self.tab == "albums":
                out = self.api.get_library_albums_list(order="recently_added")
            elif self.tab == "artists":
                out = self.api.get_library_artists_list()
            else:
                out = self.api.get_library_subscriptions_list()
            self.done.emit(self.gen, self.tab, out)
        except Exception as exc:
            self.failed.emit(self.gen, self.tab, str(exc))


class _PlaylistMutationWorker(QObject):
    """Rename / delete / remove-track — one shot, reports back a label."""
    done = Signal(str)              # human label of what happened
    failed = Signal(str)

    def __init__(self, fn, label: str) -> None:
        super().__init__()
        self.fn = fn
        self.label = label

    def run(self) -> None:
        try:
            ok = self.fn()
            if ok:
                self.done.emit(self.label)
            else:
                self.failed.emit(f"{self.label} — source refused")
        except Exception as exc:
            self.failed.emit(f"{self.label} — {exc}")


_TABS = ("playlists", "songs", "albums", "artists", "following")


class LibraryView(QWidget):
    play_now_requested = Signal(object, bool)   # Track, seed_radio
    queue_add_requested = Signal(object)        # Track
    queue_next_requested = Signal(object)       # Track
    radio_requested = Signal(object)            # Track
    play_all_requested = Signal(list)           # tracks (first plays, rest queued)
    album_requested = Signal(object)            # AlbumEntry (v1.5)
    artist_requested = Signal(object)           # ArtistEntry (v1.5)
    status_message = Signal(str)

    def __init__(self, api_obj: api.Api, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api_obj
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)

        self._pls_thread: QThread | None = None
        self._pls_worker: _PlaylistsWorker | None = None
        self._detail_thread: QThread | None = None
        self._detail_worker: _PlaylistDetailWorker | None = None
        # Monotonic id for detail requests: a slow fetch for playlist A must
        # not repaint the pane after the user has already opened playlist B.
        self._detail_gen = 0
        # Separate gen for the v1.5 tabs; bumped by reload + source change.
        self._tabs_gen = 0
        self._tab_loaded: set[str] = set()
        self._active_tab = "playlists"

        self._current_detail: api.PlaylistDetail | None = None
        self._current_entry: api.PlaylistEntry | None = None

        self._build_ui()
        # Lazy-load: only fetch playlists when the view becomes visible.

    def _build_ui(self) -> None:
        self.stack = QStackedWidget()

        # ----- index page -----
        self.index_heading = Heading("your library")
        self.index_heading.setProperty("class", "dim")

        self.refresh_btn = BracketButton("refresh")
        self.refresh_btn.setIconKey("refresh")
        self.refresh_btn.clicked.connect(self.reload_playlists)

        # v1.5 tabs. Beyond-playlists tabs show only for sources with the
        # library_full capability (see _refresh_tab_buttons).
        self._tab_buttons: dict[str, BracketButton] = {}
        tabs_row = QHBoxLayout()
        tabs_row.setSpacing(2)
        for name in _TABS:
            btn = BracketButton(name)
            btn.clicked.connect(lambda _=False, n=name: self._set_tab(n))
            self._tab_buttons[name] = btn
            tabs_row.addWidget(btn)
        tabs_row.addStretch(1)
        tabs_row.addWidget(self.refresh_btn)

        # Tab pages.
        self.playlists_list = QListWidget()
        self.playlists_list.setUniformItemSizes(True)
        self.playlists_list.itemActivated.connect(self._on_playlist_activated)

        self.songs_list = QListWidget()
        self.songs_list.setUniformItemSizes(True)
        self.songs_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.songs_list.customContextMenuRequested.connect(self._on_songs_menu)
        self.songs_list.itemActivated.connect(self._on_song_activated)
        self._songs_delegate = TrackRowDelegate(self)
        self._songs_delegate.attach(self.songs_list)
        self.songs_list.setItemDelegate(self._songs_delegate)

        self._albums_grid, albums_page = self._make_grid_page()
        self._artists_grid, artists_page = self._make_grid_page()
        self._following_grid, following_page = self._make_grid_page()

        self._tab_stack = QStackedWidget()
        self._tab_stack.addWidget(self.playlists_list)   # 0
        self._tab_stack.addWidget(self.songs_list)       # 1
        self._tab_stack.addWidget(albums_page)           # 2
        self._tab_stack.addWidget(artists_page)          # 3
        self._tab_stack.addWidget(following_page)        # 4
        self._tab_index = {n: i for i, n in enumerate(_TABS)}

        from . import scale as _scale
        idx_col = QVBoxLayout()
        idx_col.setContentsMargins(*_scale.margins(16, 14, 16, 8))
        idx_col.setSpacing(_scale.px(8))
        idx_col.addWidget(self.index_heading)
        idx_col.addLayout(tabs_row)
        idx_col.addWidget(self._tab_stack, stretch=1)
        idx_page = QWidget()
        idx_page.setLayout(idx_col)

        # ----- detail page -----
        self.back_btn = BracketButton("back")
        self.back_btn.setIconKey("back")
        self.back_btn.setRole("pill")
        self.back_btn.clicked.connect(self._show_index)
        self.detail_heading = Heading("playlist")
        self.detail_heading.setProperty("class", "dim")
        self.detail_heading.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.play_all_btn = BracketButton("play all")
        self.play_all_btn.clicked.connect(self._on_play_all)
        self.shuffle_play_btn = BracketButton("shuffle")
        self.shuffle_play_btn.clicked.connect(self._on_shuffle_play)
        # v1.5 playlist management — shown only when the source can.
        self.rename_btn = BracketButton("rename")
        self.rename_btn.clicked.connect(self._on_rename_playlist)
        self.delete_btn = BracketButton("delete")
        self.delete_btn.clicked.connect(self._on_delete_playlist)

        detail_top = QHBoxLayout()
        detail_top.addWidget(self.back_btn)
        detail_top.addWidget(self.detail_heading, stretch=1)

        detail_actions = QHBoxLayout()
        detail_actions.addWidget(self.play_all_btn)
        detail_actions.addWidget(self.shuffle_play_btn)
        detail_actions.addStretch(1)
        detail_actions.addWidget(self.rename_btn)
        detail_actions.addWidget(self.delete_btn)

        self.tracks_list = QListWidget()
        self.tracks_list.setUniformItemSizes(True)
        self.tracks_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tracks_list.customContextMenuRequested.connect(self._on_track_menu)
        self.tracks_list.itemActivated.connect(self._on_track_activated)
        self._track_delegate = TrackRowDelegate(self)
        self._track_delegate.attach(self.tracks_list)
        self.tracks_list.setItemDelegate(self._track_delegate)

        det_col = QVBoxLayout()
        det_col.setContentsMargins(*_scale.margins(16, 14, 16, 8))
        det_col.setSpacing(_scale.px(8))
        det_col.addLayout(detail_top)
        det_col.addLayout(detail_actions)
        det_col.addWidget(self.tracks_list, stretch=1)
        det_page = QWidget()
        det_page.setLayout(det_col)

        self.stack.addWidget(idx_page)
        self.stack.addWidget(det_page)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.stack)
        self._refresh_tab_buttons()

    def _make_grid_page(self) -> tuple[CardGrid, QScrollArea]:
        grid = CardGrid()
        scroll = QScrollArea()
        scroll.setWidget(grid)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        return grid, scroll

    # ---------- tabs ----------

    def _full_library(self) -> bool:
        return bool(hasattr(self.api, "supports")
                    and self.api.supports("library_full"))

    def _refresh_tab_buttons(self) -> None:
        full = self._full_library()
        for name, btn in self._tab_buttons.items():
            btn.setVisible(name == "playlists" or full)
            btn.setActiveState(name == self._active_tab)

    def _set_tab(self, name: str) -> None:
        self._active_tab = name
        self._tab_stack.setCurrentIndex(self._tab_index[name])
        self._refresh_tab_buttons()
        if name == "playlists":
            return
        if name not in self._tab_loaded:
            self._tab_loaded.add(name)
            self._load_tab(name)

    def _load_tab(self, name: str) -> None:
        self.index_heading.set_label(f"your library · loading {name}…")
        thread = QThread()
        worker = _LibraryTabWorker(self.api, name, self._tabs_gen)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_tab_done)
        worker.failed.connect(self._on_tab_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        qthreads.retain(thread, worker)
        thread.start()

    def _on_tab_done(self, gen: int, tab: str, entries: list) -> None:
        if gen != self._tabs_gen:
            return
        self.index_heading.set_label(f"your library · {tab} · {len(entries)}")
        if tab == "songs":
            marker = self._list_marker()
            self.songs_list.clear()
            for tr in entries:
                artist = theming.styled_case(tr.artists or "")
                title = theming.styled_case(tr.title or "")
                label = f"{marker}{artist} — {title}"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, tr)
                self.songs_list.addItem(item)
            return
        grid = {"albums": self._albums_grid,
                "artists": self._artists_grid,
                "following": self._following_grid}[tab]
        grid.clear()
        for e in entries:
            if tab == "albums":
                c = Card(e.title, e.artists, e.thumbnail, e)
                c.clicked.connect(self.album_requested.emit)
            else:
                sub = e.subscribers or "artist"
                c = Card(e.name, sub, e.thumbnail, e, circular=True)
                c.clicked.connect(self.artist_requested.emit)
            grid.add_card(c)

    def _on_tab_failed(self, gen: int, tab: str, msg: str) -> None:
        if gen != self._tabs_gen:
            return
        self._tab_loaded.discard(tab)
        self.index_heading.set_label(f"your library · {tab} failed")
        self.status_message.emit(f"library {tab}: {msg}")

    # ---------- index ----------

    def reload_playlists(self) -> None:
        self.playlists_list.clear()
        # Full reload: forget the lazy tabs so they refetch on next open.
        self._tabs_gen += 1
        self._tab_loaded.clear()
        self._refresh_tab_buttons()
        # If the active source doesn't expose a library, render a clean
        # empty state instead of spinning a worker that'll just raise.
        if hasattr(self.api, "supports") and not self.api.supports("library"):
            src_name = theming.styled_case(getattr(self.api, "name", "this source"))
            self.index_heading.set_label(f"{src_name} has no library")
            # Key mention from the live keymap — a hardcoded "(ctrl+7)"
            # goes stale on rebind. No main window (tests) → no mention.
            win = self.window()
            key = (win.binding_display("view_source")
                   if hasattr(win, "binding_display") else "")
            key_part = f" ({key})" if key else ""
            placeholder = QListWidgetItem(
                theming.styled_case(
                    f"  switch active source in [source]{key_part} to one "
                    "with a library, like youtube music or local files."
                )
            )
            placeholder.setFlags(Qt.NoItemFlags)
            self.playlists_list.addItem(placeholder)
            return
        self.index_heading.set_label("loading…")
        thread = QThread()
        worker = _PlaylistsWorker(self.api)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_playlists)
        worker.failed.connect(self._on_playlists_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self._pls_thread = thread
        self._pls_worker = worker
        qthreads.retain(thread, worker)
        thread.start()
        # If the user reloads while sitting on a lazy tab, refresh it too.
        if self._active_tab != "playlists":
            self._tab_loaded.add(self._active_tab)
            self._load_tab(self._active_tab)

    def _on_playlists(self, items: list[api.PlaylistEntry]) -> None:
        marker = self._list_marker()
        self.index_heading.set_label(f"your library · {len(items)}")
        self.playlists_list.clear()
        for p in items:
            label = f"{marker}{theming.styled_case(p.title or '')}"
            if p.description:
                label += f"    {theming.styled_case(p.description)}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, p)
            self.playlists_list.addItem(item)

    def _on_playlists_failed(self, msg: str) -> None:
        self.index_heading.set_label("library load failed")
        self.status_message.emit(f"library: {msg}")

    def _on_playlist_activated(self, item: QListWidgetItem) -> None:
        p: api.PlaylistEntry = item.data(Qt.UserRole)
        if not p:
            return
        self.open_playlist(p)

    # ---------- songs tab interactions ----------

    def _on_song_activated(self, item: QListWidgetItem) -> None:
        tr: api.Track = item.data(Qt.UserRole)
        if tr:
            self.play_now_requested.emit(tr, True)

    def _on_songs_menu(self, pos) -> None:
        item = self.songs_list.itemAt(pos)
        if not item:
            return
        tr: api.Track = item.data(Qt.UserRole)
        if not tr:
            return
        menu = QMenu(self.songs_list)
        a_play = QAction("play now", menu)
        a_next = QAction("play next", menu)
        a_add = QAction("add to queue", menu)
        a_radio = QAction("start radio from here", menu)
        for a in (a_play, a_next, a_add, a_radio):
            menu.addAction(a)
        a_play.triggered.connect(lambda: self.play_now_requested.emit(tr, False))
        a_next.triggered.connect(lambda: self.queue_next_requested.emit(tr))
        a_add.triggered.connect(lambda: self.queue_add_requested.emit(tr))
        a_radio.triggered.connect(lambda: self.radio_requested.emit(tr))
        menu.exec(self.songs_list.viewport().mapToGlobal(pos))

    # ---------- detail ----------

    def open_playlist(self, entry: api.PlaylistEntry) -> None:
        self._detail_gen += 1
        self._current_entry = entry
        self.tracks_list.clear()
        self.detail_heading.set_label(f"{entry.title} · loading…")
        self._refresh_edit_buttons()
        self.stack.setCurrentIndex(1)
        self.status_message.emit(theming.styled_case(f"loading {entry.title}…"))

        thread = QThread()
        worker = _PlaylistDetailWorker(self.api, entry.playlist_id, self._detail_gen)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_detail)
        worker.failed.connect(self._on_detail_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self._detail_thread = thread
        self._detail_worker = worker
        qthreads.retain(thread, worker)
        thread.start()

    def _can_edit_playlists(self) -> bool:
        # "LM" (liked songs) is system-owned — no rename/delete for it.
        return bool(
            hasattr(self.api, "supports")
            and self.api.supports("playlist_edit")
            and self._current_entry is not None
            and self._current_entry.playlist_id not in ("", "LM")
        )

    def _refresh_edit_buttons(self) -> None:
        can = self._can_edit_playlists()
        self.rename_btn.setVisible(can)
        self.delete_btn.setVisible(can)

    def _on_detail(self, gen: int, detail: api.PlaylistDetail) -> None:
        if gen != self._detail_gen:
            return   # straggler for a playlist the user already left
        self._current_detail = detail
        marker = self._list_marker()
        self.detail_heading.set_label(f"{detail.title} · {len(detail.tracks)}")
        self.tracks_list.clear()
        for tr in detail.tracks:
            artist = theming.styled_case(tr.artists or "")
            title = theming.styled_case(tr.title or "")
            dur = tr.duration or ""
            label = f"{marker}{artist} — {title}"
            if dur:
                gap = max(2, 60 - len(label) - len(dur))
                label = f"{label}{' ' * gap}{dur}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, tr)
            self.tracks_list.addItem(item)
        self.status_message.emit(theming.styled_case(f"{detail.title} · {len(detail.tracks)} tracks"))

    def _on_detail_failed(self, gen: int, msg: str) -> None:
        if gen != self._detail_gen:
            return
        self.detail_heading.set_label("playlist load failed")
        self.status_message.emit(f"playlist: {msg}")

    def _show_index(self) -> None:
        # Back to the index invalidates any fetch still in flight, so it
        # can't repaint the (now hidden) detail pane or spam the status bar.
        self._detail_gen += 1
        self.stack.setCurrentIndex(0)

    # ---------- playlist mutations (v1.5) ----------

    def _spawn_mutation(self, fn, label: str, *, and_then=None) -> None:
        thread = QThread()
        worker = _PlaylistMutationWorker(fn, label)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(lambda text: self._on_mutation_done(text, and_then))
        worker.failed.connect(self.status_message.emit)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        qthreads.retain(thread, worker)
        thread.start()

    def _on_mutation_done(self, text: str, and_then) -> None:
        self.status_message.emit(theming.styled_case(text))
        if and_then is not None:
            and_then()

    def _on_rename_playlist(self) -> None:
        entry = self._current_entry
        if entry is None or not self._can_edit_playlists():
            return
        new_title, ok = QInputDialog.getText(
            self, "rename playlist", "new name:", text=entry.title)
        new_title = (new_title or "").strip()
        if not ok or not new_title or new_title == entry.title:
            return
        pid = entry.playlist_id
        entry.title = new_title
        self.detail_heading.set_label(f"{new_title} · …")
        self._spawn_mutation(
            lambda: self.api.edit_playlist_remote(pid, title=new_title),
            f"renamed to {new_title}",
            and_then=self.reload_playlists)

    def _on_delete_playlist(self) -> None:
        entry = self._current_entry
        if entry is None or not self._can_edit_playlists():
            return
        answer = QMessageBox.question(
            self, "delete playlist",
            f"delete “{entry.title}” from your library?\nthis can't be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        pid = entry.playlist_id
        self._spawn_mutation(
            lambda: self.api.delete_playlist_remote(pid),
            f"deleted {entry.title}",
            and_then=lambda: (self._show_index(), self.reload_playlists()))

    def _on_remove_from_playlist(self, track: api.Track) -> None:
        entry = self._current_entry
        if entry is None or not self._can_edit_playlists():
            return
        pid = entry.playlist_id
        self._spawn_mutation(
            lambda: self.api.remove_from_playlist(pid, [track]),
            f"removed {track.title}",
            and_then=lambda: self.open_playlist(entry))

    # ---------- track interactions ----------

    def _on_track_activated(self, item: QListWidgetItem) -> None:
        tr: api.Track = item.data(Qt.UserRole)
        if tr:
            # double-click = play now without seeding radio (the playlist is the queue)
            self.play_now_requested.emit(tr, False)

    def _on_track_menu(self, pos) -> None:
        item = self.tracks_list.itemAt(pos)
        if not item:
            return
        tr: api.Track = item.data(Qt.UserRole)
        if not tr:
            return
        menu = QMenu(self.tracks_list)
        a_play = QAction("play now", menu)
        a_next = QAction("play next", menu)
        a_add  = QAction("add to queue", menu)
        a_radio = QAction("start radio from here", menu)
        a_play_from = QAction("play playlist from here", menu)
        for a in (a_play, a_next, a_add, a_radio, a_play_from):
            menu.addAction(a)
        a_play.triggered.connect(lambda: self.play_now_requested.emit(tr, False))
        a_next.triggered.connect(lambda: self.queue_next_requested.emit(tr))
        a_add.triggered.connect(lambda: self.queue_add_requested.emit(tr))
        a_radio.triggered.connect(lambda: self.radio_requested.emit(tr))
        a_play_from.triggered.connect(lambda: self._play_from_track(tr))
        # Removal needs the playlist item's setVideoId — absent extras
        # means the source can't act on it, so no dead menu entry.
        if (self._can_edit_playlists()
                and (tr.extras or {}).get("setVideoId")):
            menu.addSeparator()
            a_remove = QAction("remove from playlist", menu)
            menu.addAction(a_remove)
            a_remove.triggered.connect(lambda: self._on_remove_from_playlist(tr))
        menu.exec(self.tracks_list.viewport().mapToGlobal(pos))

    def _play_from_track(self, track: api.Track) -> None:
        if not self._current_detail:
            return
        tracks = list(self._current_detail.tracks)
        try:
            i = next(j for j, t in enumerate(tracks) if t.video_id == track.video_id)
        except StopIteration:
            return
        self.play_all_requested.emit(tracks[i:])

    def _on_play_all(self) -> None:
        if self._current_detail and self._current_detail.tracks:
            self.play_all_requested.emit(list(self._current_detail.tracks))

    def _on_shuffle_play(self) -> None:
        if not self._current_detail or not self._current_detail.tracks:
            return
        import random
        shuffled = list(self._current_detail.tracks)
        random.shuffle(shuffled)
        self.play_all_requested.emit(shuffled)

    # ---------- theme ----------

    def _on_theme(self, theme) -> None:
        self._theme = theme

    def _list_marker(self) -> str:
        return str(self._theme.t("layout", "list_marker", "> ")) if self._theme else "> "
