"""Word-level lyric timing: parsers, the line-mode fallback, and the
merge that moves a word-synced source onto the video's own lines."""
from __future__ import annotations

import unittest

from tide import lyric_words as lw
from tide.lyric_words import Word, WordLine


YRC = (
    '{"t":0,"c":[{"tx":"作词: "},{"tx":"Someone"}]}\n'
    "[14100,3570](14100,420,0)White (14520,480,0)shirt(15000,240,0), "
    "(15240,480,0)now (15720,420,0)red\n"
    "[17670,1200](17670,500,0)tip(18170,300,0)py (18470,400,0)toes\n"
)


class ParseYrcTest(unittest.TestCase):
    def test_words_and_lines(self) -> None:
        lines = lw.parse_yrc(YRC)
        self.assertEqual(len(lines), 2)           # credit JSON line skipped
        first = lines[0]
        self.assertEqual(first.text, "White shirt, now red")
        self.assertEqual(first.timing, "source")
        self.assertEqual([w.text for w in first.words], ["White", "shirt,", "now", "red"])
        self.assertAlmostEqual(first.start, 14.1)
        self.assertAlmostEqual(first.words[1].start, 14.52)
        # the comma fragment extends "shirt" to its own end
        self.assertAlmostEqual(first.words[1].end, 15.24)

    def test_syllables_join_into_a_word(self) -> None:
        line = lw.parse_yrc(YRC)[1]
        self.assertEqual([w.text for w in line.words], ["tippy", "toes"])
        self.assertAlmostEqual(line.words[0].start, 17.67)
        self.assertAlmostEqual(line.words[0].end, 18.47)

    def test_garbage_is_ignored(self) -> None:
        self.assertEqual(lw.parse_yrc("[x,y](1,2,0)a\nnonsense\n"), [])
        self.assertEqual(lw.parse_yrc(""), [])


class EnhancedLrcTest(unittest.TestCase):
    def test_word_tags(self) -> None:
        text = ("[00:12.00]<00:12.00>Hello <00:12.50>there <00:13.20>world<00:14.00>\n"
                "[00:15.00]plain line\n")
        lines = lw.parse_enhanced_lrc(text)
        self.assertEqual(lines[0].text, "Hello there world")
        self.assertEqual([round(w.start, 2) for w in lines[0].words], [12.0, 12.5, 13.2])
        self.assertAlmostEqual(lines[0].words[-1].end, 14.0)   # trailing tag = end
        self.assertEqual(lines[1].timing, "line")

    def test_plain_lrc_is_not_enhanced(self) -> None:
        self.assertEqual(lw.parse_enhanced_lrc("[00:01.00]just a line"), [])

    def test_lrc_parser_strips_word_tags(self) -> None:
        from tide.lyrics_provider import parse_lrc
        rows = parse_lrc("[00:12.00]<00:12.00>Hello <00:12.50>there")
        self.assertEqual(rows, [(12.0, "Hello there")])


class LyricsfileTest(unittest.TestCase):
    def test_words(self) -> None:
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        text = (
            "version: '1.0'\nmetadata:\n  title: x\n  artist: y\n  offset_ms: 100\n"
            "lines:\n- text: Waiting in a car\n  start_ms: 23500\n  end_ms: 26800\n"
            "  words:\n  - text: 'Waiting '\n    start_ms: 23500\n    end_ms: 24200\n"
            "  - text: 'in '\n    start_ms: 24200\n  - text: 'a '\n    start_ms: 24500\n"
            "  - text: car\n    start_ms: 24800\n    end_ms: 26000\n"
        )
        lines = lw.parse_lyricsfile(text)
        self.assertEqual(len(lines), 1)
        self.assertEqual([w.text for w in lines[0].words], ["Waiting", "in", "a", "car"])
        self.assertAlmostEqual(lines[0].words[0].start, 23.6)      # offset applied
        self.assertAlmostEqual(lines[0].words[1].end, 24.6)        # next word's start

    def test_line_ends_without_words(self) -> None:
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        text = ("version: '1.0'\nlines:\n- text: a\n  start_ms: 13130\n  end_ms: 16560\n"
                "- text: b\n  start_ms: 27160\n")
        ends = lw.lyricsfile_line_ends(text, [(13.13, "a"), (27.16, "b")])
        self.assertEqual(ends, [16.56, None])
        self.assertEqual(lw.lyricsfile_line_ends(text, [(1.0, "x")]), [])

    def test_no_words_means_nothing(self) -> None:
        text = "version: '1.0'\nlines:\n- text: a\n  start_ms: 1\n"
        self.assertEqual(lw.parse_lyricsfile(text), [])
        self.assertEqual(lw.parse_lyricsfile("words: [: broken"), [])


class LineModeTest(unittest.TestCase):
    def test_spread_is_length_weighted_and_fills_the_window(self) -> None:
        words = lw.spread_words("a mississippi", 10.0, 12.0)
        self.assertAlmostEqual(words[0].start, 10.0)
        self.assertAlmostEqual(words[-1].end, 12.0)
        self.assertLess(words[0].end - words[0].start, words[1].end - words[1].start)

    def test_line_end_from_source_and_capped_at_next_line(self) -> None:
        sheet = lw.line_sheet([(0.0, "one two"), (3.0, "three")], [2.0, None])
        self.assertAlmostEqual(sheet[0].end, 2.0)
        self.assertAlmostEqual(sheet[0].words[-1].end, 2.0)
        # an end past the next line's start is clamped to it
        sheet = lw.line_sheet([(0.0, "one two"), (3.0, "three")], [9.0, None])
        self.assertAlmostEqual(sheet[0].end, 3.0)

    def test_instrumental_gap_does_not_drag_the_sweep(self) -> None:
        sheet = lw.line_sheet([(0.0, "short"), (60.0, "late")])
        self.assertLess(sheet[0].end, lw.MAX_LINE_S + 0.01)

    def test_empty_line_has_no_words(self) -> None:
        sheet = lw.line_sheet([(0.0, ""), (2.0, "x")])
        self.assertEqual(sheet[0].words, ())


class AdoptTest(unittest.TestCase):
    def _base(self):
        return lw.line_sheet([(10.0, "White shirt, now red"), (13.0, "my bloody nose"),
                              (16.0, "Sleeping, you're on your tippy toes")])

    def _source(self, shift: float):
        def line(s, words):
            ws = tuple(Word(s + i * 0.4 + shift, s + i * 0.4 + 0.35 + shift, w)
                       for i, w in enumerate(words))
            return WordLine(ws[0].start, ws[-1].end, " ".join(words), ws, "source")
        return [line(10.0, ["White", "shirt,", "now", "red"]),
                line(13.0, ["my", "bloody", "nose"]),
                line(16.0, ["Sleeping,", "you're", "on", "your", "tippy", "toes"])]

    def test_offset_is_measured_and_removed(self) -> None:
        sheet, off = lw.adopt_source_words(self._base(), self._source(2.5))
        self.assertAlmostEqual(off, -2.5, places=3)
        self.assertTrue(all(l.timing == "source" for l in sheet))
        self.assertAlmostEqual(sheet[1].words[1].start, 13.4, places=3)
        self.assertEqual(sheet[0].words[1].text, "shirt,")

    def test_a_different_song_is_refused(self) -> None:
        other = [WordLine(1.0, 2.0, "nothing alike here", (Word(1.0, 2.0, "nothing"),), "source"),
                 WordLine(3.0, 4.0, "completely other words", (Word(3.0, 4.0, "x"),), "source")]
        self.assertIsNone(lw.adopt_source_words(self._base(), other))

    def test_lines_split_differently_find_their_words(self) -> None:
        base = lw.line_sheet([(10.0, "White shirt now red"), (13.0, "my bloody nose"),
                              (20.0, "I'm the bad guy"), (22.0, "Duh"),
                              (30.0, "Sleeping, you're on your tippy toes")])
        src = self._source(0.0) + []
        guy = tuple(Word(20.0 + 0.3 * i, 20.25 + 0.3 * i, w)
                    for i, w in enumerate(["I'm", "the", "bad", "guy,", "duh"]))
        src.insert(2, WordLine(20.0, 21.5, "I'm the bad guy, duh", guy, "source"))
        src[3] = lw.shift(src[3], 14.0)            # "Sleeping…" at 30 s
        sheet, _off = lw.adopt_source_words(base, src)
        self.assertEqual(sheet[2].timing, "source")
        self.assertEqual([w.text for w in sheet[2].words], ["I'm", "the", "bad", "guy"])
        self.assertEqual(sheet[3].timing, "source")
        self.assertAlmostEqual(sheet[3].words[0].start, 21.2, places=2)

    def test_different_word_split_maps_by_letters(self) -> None:
        base = lw.line_sheet([(0.0, "tippy-toes and more"), (5.0, "x y")])
        src = [WordLine(0.0, 3.0, "tippy toes and more",
                        (Word(0.0, 0.5, "tippy"), Word(0.5, 1.0, "toes"),
                         Word(1.0, 2.0, "and"), Word(2.0, 3.0, "more")), "source"),
               WordLine(5.0, 6.0, "x y", (Word(5.0, 5.5, "x"), Word(5.5, 6.0, "y")), "source")]
        sheet, _off = lw.adopt_source_words(base, src)
        words = sheet[0].words
        self.assertEqual([w.text for w in words], ["tippy-toes", "and", "more"])
        self.assertAlmostEqual(words[0].start, 0.0, places=2)
        self.assertAlmostEqual(words[1].start, 1.0, delta=0.05)


class ProgressTest(unittest.TestCase):
    def test_word_progress(self) -> None:
        line = WordLine(0.0, 3.0, "a b", (Word(1.0, 2.0, "a"), Word(2.5, 3.0, "b")), "source")
        self.assertEqual(lw.word_progress(line, 0.5), (0, 0.0))
        idx, frac = lw.word_progress(line, 1.5)
        self.assertEqual(idx, 0)
        self.assertAlmostEqual(frac, 0.5)
        self.assertEqual(lw.word_progress(line, 2.2), (0, 1.0))   # between words
        self.assertEqual(lw.word_progress(line, 9.0), (1, 1.0))

    def test_payload_round_trip(self) -> None:
        sheet = lw.parse_yrc(YRC)
        again = lw.sheet_from_payload(lw.sheet_payload(sheet))
        self.assertEqual([l.text for l in again], [l.text for l in sheet])
        self.assertEqual(again[0].words[1].text, "shirt,")
        self.assertIsNone(lw.sheet_from_payload("[[1, 2]]"))
        self.assertIsNone(lw.sheet_from_payload("not json"))


if __name__ == "__main__":
    unittest.main()
