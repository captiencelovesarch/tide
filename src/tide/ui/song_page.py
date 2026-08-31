"""Song page — the watch panel, native (v1.5).

One track, everything the community knows about it: header with art +
counts, then three lazy tabs — [related] (the "you might also like" /
"other performances" shelves), [comments] (public YouTube comments, read-
only), [credits] (performed / written / produced by).

Reachable from:
  - clicking the now-playing label on the strip
  - right-click → "song info" on any track row

Everything is capability-gated per tab: a source without ``comments``
simply never shows the tab, so the page works (thinner) for any source.
Tab loads are lazy — nothing fetches until a tab is first opened — and
session-cached per video so tab-hopping and reopening are free. Comments
are the slow one (a full yt-dlp extraction, seconds); the tab owns its
spinner text and the fetch is keyed by generation so a stale worker can't
paint over a newer track's page.

Comment text renders through Qt rich text so timestamps can be seek
links — the ONLY rich-text surface in tide, so the rules are strict:
remote text is html-escaped first, links are only the ``seek:`` hrefs we
build ourselves, and ``openExternalLinks`` stays off.
"""
from __future__ import annotations

import html
import re

from PySide6.QtCore import QObject, QRect, QThread, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import api, qthreads, theming
from ..sources.base import Comment, CreditSection, human_count
from . import art_cache
from .card import Card, ShelfRow
from .headings import line_heading as _line_heading
from .widgets import BracketButton


ART_SIZE = 160

# h:mm:ss or m:ss anywhere in a comment. Group so we can rebuild seconds.
_TIMESTAMP = re.compile(r"\b(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\b")


def _timestamp_to_secs(m: re.Match) -> float:
    h = int(m.group(1) or 0)
    return float(h * 3600 + int(m.group(2)) * 60 + int(m.group(3)))


def comment_html(text: str, accent: str) -> str:
    """Escape remote text, then (and only then) linkify timestamps.

    The escape-first order is the whole security story: after
    ``html.escape`` the only markup in the string is what this function
    appends, and every href is a ``seek:<float>`` we computed ourselves.
    """
    escaped = html.escape(text)

    def _link(m: re.Match) -> str:
        secs = _timestamp_to_secs(m)
        return (f'<a href="seek:{secs}" '
                f'style="color:{accent}; text-decoration:none;">{m.group(0)}</a>')

    linked = _TIMESTAMP.sub(_link, escaped)
    return linked.replace("\n", "<br>")


# ---------- workers ----------


class _RelatedWorker(QObject):
    done = Signal(int, list)          # gen, [Shelf]
    failed = Signal(int, str)

    def __init__(self, source, track: api.Track, gen: int) -> None:
        super().__init__()
        self.source = source
        self.track = track
        self.gen = gen

    def run(self) -> None:
        try:
            self.done.emit(self.gen, self.source.get_related_for(self.track))
        except Exception as exc:
            self.failed.emit(self.gen, str(exc))


class _CommentsWorker(QObject):
    done = Signal(int, str, list)     # gen, sort, [Comment]
    failed = Signal(int, str)

    def __init__(self, source, video_id: str, sort: str, gen: int) -> None:
        super().__init__()
        self.source = source
        self.video_id = video_id
        self.sort = sort
        self.gen = gen

    def run(self) -> None:
        try:
            out = self.source.get_comments(self.video_id, sort=self.sort)
            self.done.emit(self.gen, self.sort, out)
        except Exception as exc:
            self.failed.emit(self.gen, str(exc))


class _CreditsWorker(QObject):
    done = Signal(int, list)          # gen, [CreditSection]
    failed = Signal(int, str)

    def __init__(self, source, track: api.Track, gen: int) -> None:
        super().__init__()
        self.source = source
        self.track = track
        self.gen = gen

    def run(self) -> None:
        try:
            self.done.emit(self.gen, self.source.get_credits_for(self.track))
        except Exception as exc:
            self.failed.emit(self.gen, str(exc))


class _InsightsWorker(QObject):
    done = Signal(int, object)        # gen, SongInsights | None

    def __init__(self, source, video_id: str, gen: int) -> None:
        super().__init__()
        self.source = source
        self.video_id = video_id
        self.gen = gen

    def run(self) -> None:
        try:
            ins = self.source.get_song_insights(self.video_id)
        except Exception:
            ins = None
        # Always emits (None included) — done is what quits the thread.
        self.done.emit(self.gen, ins)


# ---------- comment row ----------


class _Avatar(QLabel):
    """Small circular author avatar, filled in whenever art_cache delivers.
    Paints a dim ring placeholder until then (or forever, for authors
    without a thumbnail) — no layout shift either way."""

    SIZE = 22

    def __init__(self, url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from . import scale as _scale
        self._px = _scale.px(self.SIZE)
        self.setFixedSize(self._px, self._px)
        self._url = url or ""
        self._img = None
        if self._url:
            self._img = art_cache.cache().request(self._url, self._on_loaded)

    def _on_loaded(self, img) -> None:
        self._img = img
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRect(0, 0, self._px, self._px)
        if self._img is None:
            theme = theming.manager().current()
            dim = QColor(theme.token("dim", "#6f6f6f") if theme else "#6f6f6f")
            p.setPen(dim)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(rect.adjusted(1, 1, -1, -1))
            return
        clip = QPainterPath()
        clip.addEllipse(rect)
        p.setClipPath(clip)
        p.drawPixmap(rect, QPixmap.fromImage(self._img).scaled(
            self._px, self._px,
            Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))


class _CommentRow(QWidget):
    """One comment: [avatar] author · age · likes · badges / body text.
    Replies get no avatar and an indent — the thread stays skimmable."""

    seek_requested = Signal(float)

    def __init__(self, comment: Comment, *, reply: bool = False,
                 reply_count: int = 0, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from . import scale as _scale
        theme = theming.manager().current()
        accent = theme.token("accent", "#d4b95e") if theme else "#d4b95e"

        head_bits = [comment.author or "someone"]
        if comment.time_text:
            head_bits.append(comment.time_text)
        likes = human_count(comment.likes)
        if likes:
            head_bits.append(f"{likes} ♥")
        if comment.pinned:
            head_bits.append("pinned")
        if comment.hearted:
            head_bits.append("♥ by artist")
        if comment.by_uploader:
            head_bits.append("uploader")
        head = QLabel(theming.styled_case(" · ".join(head_bits)))
        head.setProperty("class", "dim")
        head.setTextFormat(Qt.PlainText)

        body = QLabel(comment_html(comment.text, accent))
        # Rich text is deliberate and escaped — see module docstring.
        body.setTextFormat(Qt.RichText)
        body.setOpenExternalLinks(False)
        body.setWordWrap(True)
        body.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        body.linkActivated.connect(self._on_link)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        text_col.addWidget(head)
        text_col.addWidget(body)
        if reply_count:
            more = QLabel(theming.styled_case(f"└ {reply_count} replies"))
            more.setProperty("class", "dim")
            more.setTextFormat(Qt.PlainText)
            text_col.addWidget(more)

        row = QHBoxLayout(self)
        # Replies must indent past the parent TEXT, not just the avatar:
        # avatar (22) + spacing (8) is where parent text begins, so 24px
        # rendered visually flat. 48 puts replies clearly inside the thread.
        row.setContentsMargins(_scale.px(48) if reply else 0, 0, 0, 0)
        row.setSpacing(8)
        if not reply:
            row.addWidget(_Avatar(comment.author_thumbnail), alignment=Qt.AlignTop)
        row.addLayout(text_col, stretch=1)

    def _on_link(self, href: str) -> None:
        if href.startswith("seek:"):
            try:
                self.seek_requested.emit(float(href[5:]))
            except ValueError:
                pass


# ---------- the page ----------


class SongPage(QWidget):
    back_requested = Signal()
    play_now_requested = Signal(object, bool)
    queue_add_requested = Signal(object)
    queue_next_requested = Signal(object)
    radio_requested = Signal(object)
    dislike_requested = Signal(object)
    album_requested = Signal(object)          # AlbumEntry
    artist_requested = Signal(object)         # ArtistEntry
    playlist_requested = Signal(object)       # PlaylistEntry
    seek_requested = Signal(object, float)    # track, seconds
    status_message = Signal(str)

    COMMENT_SORTS = ("top", "new")

    def __init__(self, api_obj: api.Api, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api_obj
        self._track: api.Track | None = None
        # Generation guard: bumped on every open_track, checked by every
        # worker result — a slow comments fetch for track A must not land
        # on track B's page.
        self._gen = 0
        self._loaded_tabs: set[str] = set()
        self._comment_sort = "top"
        # Session cache: video_id → {"related": [...], "credits": [...],
        # "comments": {"top": [...], "new": [...]}, "insights": SongInsights}
        self._page_cache: dict[str, dict] = {}
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)
        self._build_ui()

    # ---------- layout ----------

    def _build_ui(self) -> None:
        from . import scale as _scale

        self.back_btn = BracketButton("back")
        self.back_btn.clicked.connect(self.back_requested.emit)
        self.heading = QLabel(_line_heading("song"))
        self.heading.setProperty("class", "dim")
        self.heading.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        top_bar = QHBoxLayout()
        top_bar.addWidget(self.back_btn)
        top_bar.addWidget(self.heading, stretch=1)

        # ---- header ----
        self.art = QLabel()
        self._art_px = _scale.px(ART_SIZE)
        self.art.setFixedSize(self._art_px, self._art_px)
        self.art.setAlignment(Qt.AlignCenter)
        art_cache.cache().image_loaded.connect(self._on_art_loaded)

        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-weight: 600; font-size: 14pt;")
        self.title_label.setWordWrap(True)

        self.byline_label = QLabel("")
        self.byline_label.setProperty("class", "dim")

        self.counts_label = QLabel("")
        self.counts_label.setProperty("class", "dim")

        # Remote metadata is never markup (see album.py for the precedent).
        for _lbl in (self.title_label, self.byline_label, self.counts_label):
            _lbl.setTextFormat(Qt.PlainText)

        self.play_btn = BracketButton("play")
        self.queue_btn = BracketButton("queue")
        self.radio_btn = BracketButton("radio")
        self.less_btn = BracketButton("dislike")
        self.play_btn.clicked.connect(self._on_play)
        self.queue_btn.clicked.connect(self._on_queue)
        self.radio_btn.clicked.connect(self._on_radio)
        self.less_btn.clicked.connect(self._on_less)
        actions_row = QHBoxLayout()
        actions_row.setSpacing(2)
        for b in (self.play_btn, self.queue_btn, self.radio_btn, self.less_btn):
            actions_row.addWidget(b)
        actions_row.addStretch(1)

        meta_col = QVBoxLayout()
        meta_col.setContentsMargins(0, 0, 0, 0)
        meta_col.setSpacing(4)
        meta_col.addWidget(self.title_label)
        meta_col.addWidget(self.byline_label)
        meta_col.addWidget(self.counts_label)
        meta_col.addSpacing(8)
        meta_col.addLayout(actions_row)
        meta_col.addStretch(1)

        header_row = QHBoxLayout()
        header_row.setSpacing(18)
        header_row.addWidget(self.art, alignment=Qt.AlignTop)
        header_row.addLayout(meta_col, stretch=1)

        # ---- tabs ----
        self.tab_related = BracketButton("related")
        self.tab_comments = BracketButton("comments")
        self.tab_credits = BracketButton("credits")
        self.tab_related.clicked.connect(lambda: self._set_tab("related"))
        self.tab_comments.clicked.connect(lambda: self._set_tab("comments"))
        self.tab_credits.clicked.connect(lambda: self._set_tab("credits"))
        tabs_row = QHBoxLayout()
        tabs_row.setSpacing(2)
        tabs_row.addWidget(self.tab_related)
        tabs_row.addWidget(self.tab_comments)
        tabs_row.addWidget(self.tab_credits)
        tabs_row.addStretch(1)

        # ---- tab pages (each a scrollable column) ----
        self._tab_stack = QStackedWidget()
        self._related_col, related_page = self._make_scroll_column()
        self._comments_col, comments_page = self._make_scroll_column()
        self._credits_col, credits_page = self._make_scroll_column()
        self._tab_stack.addWidget(related_page)     # 0
        self._tab_stack.addWidget(comments_page)    # 1
        self._tab_stack.addWidget(credits_page)     # 2
        self._tab_names = {"related": 0, "comments": 1, "credits": 2}

        root = QVBoxLayout(self)
        root.setContentsMargins(*_scale.margins(16, 14, 16, 8))
        root.setSpacing(_scale.px(10))
        root.addLayout(top_bar)
        root.addLayout(header_row)
        root.addLayout(tabs_row)
        root.addWidget(self._tab_stack, stretch=1)

    def _make_scroll_column(self) -> tuple[QVBoxLayout, QScrollArea]:
        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)
        col.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        return col, scroll

    @staticmethod
    def _clear_col(col: QVBoxLayout) -> None:
        # Leave the trailing stretch in place; remove everything before it.
        while col.count() > 1:
            it = col.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

    @staticmethod
    def _add_to_col(col: QVBoxLayout, w: QWidget) -> None:
        col.insertWidget(col.count() - 1, w)

    def _dim_line(self, col: QVBoxLayout, text: str) -> None:
        lbl = QLabel(theming.styled_case(text))
        lbl.setProperty("class", "dim")
        lbl.setWordWrap(True)
        self._add_to_col(col, lbl)

    # ---------- open ----------

    def open_track(self, track: api.Track) -> None:
        if not track or not track.video_id:
            return
        self._gen += 1
        self._track = track
        self._loaded_tabs: set[str] = set()
        self.heading.setText(_line_heading("song"))
        self.title_label.setText(track.title or "?")
        bits = [b for b in (track.artists, track.album) if b]
        self.byline_label.setText("  ·  ".join(bits))
        self.counts_label.setText("")
        self._render_art(track.thumbnail)

        source = self._source_for(track)
        caps = {
            "related": bool(source and source.supports("related")),
            "comments": bool(source and source.supports("comments")),
            "credits": bool(source and source.supports("credits")),
        }
        self.tab_related.setVisible(caps["related"])
        self.tab_comments.setVisible(caps["comments"])
        self.tab_credits.setVisible(caps["credits"])
        self.radio_btn.setVisible(bool(source and source.supports("radio")))
        from ..sources.base import MusicSource
        self.less_btn.setVisible(
            source is not None
            and type(source).dislike_song is not MusicSource.dislike_song
        )
        for col in (self._related_col, self._comments_col, self._credits_col):
            self._clear_col(col)

        # Counts under the byline — cheap-or-cached, off-thread anyway.
        cached = self._page_cache.get(track.video_id, {})
        if cached.get("insights") is not None:
            self._apply_insights(cached["insights"])
        elif source is not None and source.supports("insights"):
            self._spawn(_InsightsWorker(source, track.video_id, self._gen),
                        done=(lambda gen, ins: self._on_insights(gen, ins)))

        # Land on the first visible tab; its fetch starts lazily.
        first = next((n for n in ("related", "comments", "credits") if caps[n]),
                     None)
        if first is None:
            self._tab_stack.setCurrentIndex(0)
            self._dim_line(self._related_col,
                           "no extra info available for this track.")
        else:
            self._set_tab(first)

    def _source_for(self, track: api.Track):
        from ..sources import registry
        try:
            return registry().get(track.source or "ytmusic")
        except Exception:
            return None

    def _render_art(self, url: str) -> None:
        self._art_url = url or ""
        img = art_cache.cache().request(self._art_url, None) if url else None
        if img is None:
            self.art.setText("[no art]")
            self.art.setPixmap(QPixmap())
            return
        self._set_art_pixmap(img)

    def _set_art_pixmap(self, img) -> None:
        self.art.setText("")
        self.art.setPixmap(QPixmap.fromImage(img).scaled(
            self._art_px, self._art_px,
            Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))

    def _on_art_loaded(self, url: str, img) -> None:
        if url and url == getattr(self, "_art_url", "") and img is not None:
            self._set_art_pixmap(img)

    # ---------- insights ----------

    def _on_insights(self, gen: int, insights) -> None:
        if gen != self._gen or self._track is None or insights is None:
            return
        self._page_cache.setdefault(self._track.video_id, {})["insights"] = insights
        self._trim_page_cache()
        self._apply_insights(insights)

    def _apply_insights(self, insights) -> None:
        parts: list[str] = []
        views = human_count(getattr(insights, "views", 0))
        likes = human_count(getattr(insights, "likes", 0))
        comments = human_count(getattr(insights, "comment_count", 0))
        if views:
            parts.append(f"{views} plays")
        if likes:
            parts.append(f"{likes} likes")
        if comments:
            parts.append(f"{comments} comments")
        year = getattr(insights, "year", "")
        if parts and year:
            parts.append(year)
        self.counts_label.setText(theming.styled_case(" · ".join(parts)))

    # ---------- tabs ----------

    def _set_tab(self, name: str) -> None:
        self._tab_stack.setCurrentIndex(self._tab_names[name])
        for btn, n in ((self.tab_related, "related"),
                       (self.tab_comments, "comments"),
                       (self.tab_credits, "credits")):
            btn.setActiveState(n == name)
        if name not in self._loaded_tabs:
            self._loaded_tabs.add(name)
            self._load_tab(name)

    def _load_tab(self, name: str) -> None:
        track = self._track
        if track is None:
            return
        source = self._source_for(track)
        if source is None:
            return
        cached = self._page_cache.get(track.video_id, {})
        if name == "related":
            if "related" in cached:
                self._render_related(cached["related"])
                return
            self._dim_line(self._related_col, "loading related…")
            self._spawn(_RelatedWorker(source, track, self._gen),
                        done=self._on_related, failed=self._on_related_failed)
        elif name == "comments":
            self._load_comments(source, track)
        elif name == "credits":
            if "credits" in cached:
                self._render_credits(cached["credits"])
                return
            self._dim_line(self._credits_col, "loading credits…")
            self._spawn(_CreditsWorker(source, track, self._gen),
                        done=self._on_credits, failed=self._on_credits_failed)

    def _spawn(self, worker: QObject, done=None, failed=None) -> None:
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        if done is not None:
            worker.done.connect(done)
            worker.done.connect(thread.quit)
        if failed is not None:
            worker.failed.connect(failed)
            worker.failed.connect(thread.quit)
        if done is None and failed is None:
            thread.started.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        qthreads.retain(thread, worker)
        thread.start()

    # ---------- related ----------

    def _on_related(self, gen: int, shelves: list) -> None:
        if gen != self._gen or self._track is None:
            return
        self._page_cache.setdefault(self._track.video_id, {})["related"] = shelves
        self._trim_page_cache()
        self._render_related(shelves)

    def _on_related_failed(self, gen: int, _msg: str) -> None:
        if gen != self._gen:
            return
        self._clear_col(self._related_col)
        self._dim_line(self._related_col, "couldn't load related.")

    def _render_related(self, shelves: list) -> None:
        self._clear_col(self._related_col)
        if not shelves:
            self._dim_line(self._related_col, "nothing related found.")
            return
        for shelf in shelves:
            label = QLabel(_line_heading(shelf.title or "related"))
            label.setProperty("class", "dim")
            self._add_to_col(self._related_col, label)
            row = ShelfRow()
            for it in shelf.items:
                c = Card(it.title, it.subtitle, it.thumbnail, it,
                         circular=(it.kind == "artist"))
                c.clicked.connect(lambda item=it: self._dispatch_item(item))
                row.add_card(c)
            row.end_with_stretch()
            self._add_to_col(self._related_col, row)

    def _dispatch_item(self, item) -> None:
        if item.kind in ("song", "video") and item.track is not None:
            self.play_now_requested.emit(item.track, True)
        elif item.kind == "album" and item.album is not None:
            self.album_requested.emit(item.album)
        elif item.kind == "artist" and item.artist is not None:
            self.artist_requested.emit(item.artist)
        elif item.kind == "playlist" and item.playlist is not None:
            self.playlist_requested.emit(item.playlist)

    # ---------- comments ----------

    def _load_comments(self, source, track: api.Track) -> None:
        cached = self._page_cache.get(track.video_id, {})
        by_sort = cached.get("comments", {})
        if self._comment_sort in by_sort:
            self._render_comments(by_sort[self._comment_sort])
            return
        self._clear_col(self._comments_col)
        self._dim_line(self._comments_col,
                       "loading comments… (takes a few seconds)")
        self._spawn(
            _CommentsWorker(source, track.video_id, self._comment_sort, self._gen),
            done=self._on_comments, failed=self._on_comments_failed)

    def _on_comments(self, gen: int, sort: str, comments: list) -> None:
        if gen != self._gen or self._track is None:
            return
        slot = self._page_cache.setdefault(self._track.video_id, {})
        slot.setdefault("comments", {})[sort] = comments
        self._trim_page_cache()
        if sort == self._comment_sort:
            self._render_comments(comments)

    def _on_comments_failed(self, gen: int, _msg: str) -> None:
        if gen != self._gen:
            return
        self._clear_col(self._comments_col)
        self._dim_line(self._comments_col, "couldn't load comments.")

    def _render_comments(self, comments: list) -> None:
        self._clear_col(self._comments_col)

        # Sort toggle header.
        header = QWidget()
        hrow = QHBoxLayout(header)
        hrow.setContentsMargins(0, 0, 0, 0)
        hrow.setSpacing(2)
        sort_lbl = QLabel(theming.styled_case("sort:"))
        sort_lbl.setProperty("class", "dim")
        hrow.addStretch(1)
        hrow.addWidget(sort_lbl)
        for key, label in (("top", "top"), ("new", "newest")):
            btn = BracketButton(label if key != self._comment_sort
                                else f"{label} ✓")
            btn.clicked.connect(lambda _=False, k=key: self._on_sort(k))
            hrow.addWidget(btn)
        self._add_to_col(self._comments_col, header)

        if not comments:
            self._dim_line(self._comments_col,
                           "no comments on this one.")
            return

        # Flat list → threads. Replies keep the fetch order under their
        # parent; orphaned replies (parent beyond our fetch cap) are dropped.
        top_level = [c for c in comments if not c.parent_id]
        replies: dict[str, list[Comment]] = {}
        for c in comments:
            if c.parent_id:
                replies.setdefault(c.parent_id, []).append(c)
        for c in top_level:
            kids = replies.get(c.comment_id, [])
            row = _CommentRow(c, reply_count=len(kids))
            row.seek_requested.connect(self._on_seek)
            self._add_to_col(self._comments_col, row)
            for k in kids:
                krow = _CommentRow(k, reply=True)
                krow.seek_requested.connect(self._on_seek)
                self._add_to_col(self._comments_col, krow)

    def _on_sort(self, key: str) -> None:
        if key == self._comment_sort:
            return
        self._comment_sort = key
        track = self._track
        if track is None:
            return
        source = self._source_for(track)
        if source is not None:
            self._load_comments(source, track)

    def _on_seek(self, secs: float) -> None:
        if self._track is not None:
            self.seek_requested.emit(self._track, secs)

    # ---------- credits ----------

    def _on_credits(self, gen: int, sections: list) -> None:
        if gen != self._gen or self._track is None:
            return
        self._page_cache.setdefault(self._track.video_id, {})["credits"] = sections
        self._trim_page_cache()
        self._render_credits(sections)

    def _on_credits_failed(self, gen: int, _msg: str) -> None:
        if gen != self._gen:
            return
        self._clear_col(self._credits_col)
        self._dim_line(self._credits_col, "couldn't load credits.")

    def _render_credits(self, sections: list) -> None:
        self._clear_col(self._credits_col)
        if not sections:
            self._dim_line(self._credits_col,
                           "no credits available for this recording.")
            return
        for sec in sections:
            title = QLabel(_line_heading(sec.title or "credits"))
            title.setProperty("class", "dim")
            self._add_to_col(self._credits_col, title)
            names = QLabel("\n".join(sec.names))
            names.setTextFormat(Qt.PlainText)
            names.setWordWrap(True)
            self._add_to_col(self._credits_col, names)

    # ---------- actions ----------

    def _on_play(self) -> None:
        if self._track is not None:
            self.play_now_requested.emit(self._track, False)

    def _on_queue(self) -> None:
        if self._track is not None:
            self.queue_add_requested.emit(self._track)

    def _on_radio(self) -> None:
        if self._track is not None:
            self.radio_requested.emit(self._track)

    def _on_less(self) -> None:
        if self._track is not None:
            self.dislike_requested.emit(self._track)
            self.status_message.emit(theming.styled_case("dislike sent"))

    # ---------- misc ----------

    def _trim_page_cache(self) -> None:
        while len(self._page_cache) > 16:
            self._page_cache.pop(next(iter(self._page_cache)))

    def _on_theme(self, theme) -> None:
        self._theme = theme
