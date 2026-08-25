"""v1.5 song page: comment rendering safety + structure.

The comments tab is the ONLY rich-text surface in tide (timestamps become
seek links), so the escape-first contract is load-bearing: remote comment
text must never reach the label as live markup. Also covered: timestamp
parsing, thread grouping (replies under parents, orphans dropped), and the
generation guard that keeps a slow fetch for track A off track B's page.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from tide.sources.base import Comment, Track
from tide.ui import song_page
from tide.ui.song_page import SongPage, _TIMESTAMP, comment_html, _timestamp_to_secs


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class CommentHtmlTest(unittest.TestCase):
    def test_markup_is_neutralized(self) -> None:
        out = comment_html('<img src="file:///etc/passwd"> <b>bold</b>', "#fff")
        self.assertNotIn("<img", out)
        self.assertNotIn("<b>", out)
        self.assertIn("&lt;img", out)

    def test_timestamps_become_seek_links(self) -> None:
        out = comment_html("the beat switch at 3:31 is unreal", "#fff")
        self.assertIn('href="seek:211.0"', out)
        self.assertIn(">3:31</a>", out)

    def test_hours_form(self) -> None:
        m = _TIMESTAMP.search("see 1:02:03 for the drop")
        self.assertEqual(_timestamp_to_secs(m), 3723.0)

    def test_a_crafted_link_cannot_survive(self) -> None:
        # Attacker writes their own anchor: the escape turns it into text,
        # and the only hrefs in the output are the seek: ones we built.
        out = comment_html('<a href="https://evil.example">2:00</a>', "#fff")
        self.assertNotIn('href="https://evil.example"', out)
        self.assertIn('href="seek:120.0"', out)

    def test_newlines_break(self) -> None:
        self.assertIn("<br>", comment_html("line one\nline two", "#fff"))


class _FakeSource:
    slug = "ytmusic"

    def __init__(self, caps: set[str]) -> None:
        self._caps = caps

    def supports(self, cap: str) -> bool:
        return cap in self._caps

    # SongPage checks whether dislike is overridden; base-identical means
    # hidden. Give the fake its own so the button shows for full-caps runs.
    def dislike_song(self, video_id: str) -> None:
        pass

    def get_related_for(self, track):
        return []

    def get_credits_for(self, track):
        return []

    def get_comments(self, video_id, *, sort="top", limit=60):
        return []

    def get_song_insights(self, video_id):
        return None


class TabGatingTest(unittest.TestCase):
    """SongPage resolves the track's source through tide.sources.registry
    at call time (fresh import per call), so patching the package attribute
    reroutes it to the fake."""

    def _open_with(self, caps: set[str]) -> SongPage:
        _app()
        page = SongPage(api_obj=None)
        fake = _FakeSource(caps)
        reg = mock.Mock()
        reg.get.return_value = fake
        patcher = mock.patch("tide.sources.registry", return_value=reg)
        patcher.start()
        self.addCleanup(patcher.stop)
        page.open_track(Track(video_id="v1", title="t", artists="a"))
        # Let any lazily-spawned tab worker (against the fake, instant)
        # drain before the page dies.
        from PySide6.QtTest import QTest
        QTest.qWait(50)
        return page

    def test_tabs_follow_capabilities(self) -> None:
        page = self._open_with({"related", "comments"})
        self.assertTrue(page.tab_related.isVisibleTo(page))
        self.assertTrue(page.tab_comments.isVisibleTo(page))
        self.assertFalse(page.tab_credits.isVisibleTo(page))

    def test_no_capabilities_no_tabs(self) -> None:
        page = self._open_with(set())
        self.assertFalse(page.tab_related.isVisibleTo(page))
        self.assertFalse(page.tab_comments.isVisibleTo(page))
        self.assertFalse(page.tab_credits.isVisibleTo(page))


class CommentThreadingTest(unittest.TestCase):
    def test_replies_group_and_orphans_drop(self) -> None:
        _app()
        page = SongPage(api_obj=None)
        page._track = Track(video_id="v1", title="t", artists="a")
        comments = [
            Comment(comment_id="c1", author="@a", text="top comment"),
            Comment(comment_id="c2", author="@b", text="reply", parent_id="c1"),
            Comment(comment_id="c3", author="@c", text="orphan",
                    parent_id="never-fetched"),
        ]
        page._render_comments(comments)
        rows = [page._comments_col.itemAt(i).widget()
                for i in range(page._comments_col.count() - 1)]
        row_types = [type(w).__name__ for w in rows if w is not None]
        # header widget + parent row + its reply; the orphan is gone.
        self.assertEqual(row_types.count("_CommentRow"), 2)

    def test_stale_generation_is_ignored(self) -> None:
        _app()
        page = SongPage(api_obj=None)
        page._track = Track(video_id="v2", title="t", artists="a")
        page._gen = 5
        page._on_comments(4, "top", [
            Comment(comment_id="c1", author="@a", text="stale")])
        # Nothing rendered, nothing cached.
        self.assertNotIn("v2", page._page_cache)


if __name__ == "__main__":
    unittest.main()
