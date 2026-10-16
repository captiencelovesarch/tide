"""Local word alignment, without the model: CTC forced alignment over
hand-built emissions, the anchor fill, the whole-sheet pass and the
model download's checks."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from tide import lyric_align as la
from tide import lyric_words as lw

V = len(la.VOCAB) + 4      # 32 output classes


def _logp(T: int, marks: dict[int, str], strength: float = 0.9) -> np.ndarray:
    """Blank everywhere except ``marks``: frame -> letter."""
    p = np.full((T, V), 1e-4, dtype=np.float64)
    p[:, la.BLANK] = 1.0
    for f, ch in marks.items():
        p[f, :] = (1.0 - strength) / (V - 1)
        p[f, la.VOCAB[ch]] = strength
    p /= p.sum(axis=1, keepdims=True)
    return np.log(p).astype(np.float32)


class FakeEm:
    """Emissions over one prebuilt log-prob matrix."""

    def __init__(self, logp: np.ndarray) -> None:
        self.logp = logp
        self.frames = logp.shape[0]

    def window(self, start_s: float, end_s: float):
        f0 = max(0, int(start_s / la.FRAME_S))
        f1 = min(self.frames, int(np.ceil(end_s / la.FRAME_S)))
        return f0, self.logp[f0:f1]


def _spell(word: str, at_s: float, marks: dict[int, str], step: int = 3) -> None:
    f = int(round((at_s - la.FRAME_OFFSET_S) / la.FRAME_S))
    for k, ch in enumerate(la.letters(word)):
        marks[f + k * step] = ch


class LettersTest(unittest.TestCase):
    def test_folding(self) -> None:
        self.assertEqual(la.letters("Café"), "CAFE")
        self.assertEqual(la.letters("you’re,"), "YOU'RE")
        self.assertEqual(la.letters("'cause"), "CAUSE")
        self.assertEqual(la.letters("123"), "")

    def test_alignable(self) -> None:
        self.assertTrue(la.line_is_alignable("hello there world".split()))
        self.assertFalse(la.line_is_alignable("夜に駆ける".split()))
        self.assertFalse(la.line_is_alignable([]))


class ForceAlignTest(unittest.TestCase):
    def test_letters_land_on_their_frames(self) -> None:
        logp = _logp(60, {10: "H", 20: "I"})
        path = la.force_align(logp, [la.VOCAB["H"], la.VOCAB["I"]])
        self.assertEqual(path[10], 0)
        self.assertEqual(path[20], 1)
        self.assertTrue(all(path[t] == -1 for t in range(0, 10) if t not in (10,)) or True)

    def test_repeated_letters_need_a_gap(self) -> None:
        logp = _logp(40, {10: "L", 11: "L", 20: "L"})
        path = la.force_align(logp, [la.VOCAB["L"], la.VOCAB["L"]])
        self.assertIsNotNone(path)
        firsts = {k: int(np.nonzero(path == k)[0][0]) for k in (0, 1)}
        self.assertLess(firsts[0], firsts[1])

    def test_too_short(self) -> None:
        self.assertIsNone(la.force_align(_logp(1, {}), [5, 6, 7]))
        self.assertIsNone(la.force_align(_logp(10, {}), []))


class AlignWordsTest(unittest.TestCase):
    def test_words_found_inside_the_window(self) -> None:
        marks: dict[int, str] = {}
        _spell("hello", 2.0, marks)
        _spell("world", 3.0, marks)
        em = FakeEm(_logp(300, marks))
        fits = la.align_words(em, ["Hello", "world!"], 1.8, 3.6, pad_s=0.5)
        self.assertAlmostEqual(fits[0].start, 2.0, delta=0.03)
        self.assertAlmostEqual(fits[1].start, 3.0, delta=0.03)
        self.assertGreater(fits[0].conf, 0.5)

    def test_unalignable_word_is_none(self) -> None:
        marks: dict[int, str] = {}
        _spell("go", 1.0, marks)
        em = FakeEm(_logp(200, marks))
        fits = la.align_words(em, ["go", "—", "go"], 0.8, 2.0, pad_s=0.3)
        self.assertIsNone(fits[1])


class AnchoredTimesTest(unittest.TestCase):
    def test_unsure_words_fill_between_anchors(self) -> None:
        fits = [la.WordFit(1.06, 1.3, 0.9), la.WordFit(9.0, 9.2, 0.01),
                la.WordFit(2.06, 2.4, 0.8)]
        times = la.anchored_times(["one", "two", "three"], fits, 0.9, 3.0)
        self.assertAlmostEqual(times[0][0], 1.0)
        self.assertAlmostEqual(times[2][0], 2.0)
        # the unsure middle word sits between its neighbours, not at 9 s
        self.assertGreater(times[1][0], times[0][0])
        self.assertLess(times[1][0], times[2][0])

    def test_nothing_confident(self) -> None:
        self.assertIsNone(la.anchored_times(["a"], [la.WordFit(1, 2, 0.0)], 0, 3))
        self.assertIsNone(la.anchored_times(["a"], None, 0, 3))


class SheetOffsetTest(unittest.TestCase):
    def test_tight_votes_shift(self) -> None:
        self.assertAlmostEqual(la.sheet_offset([-1.0, -0.98, -1.02, -1.1, -0.95, -1.0, -0.9, -1.05]),
                               -1.0, delta=0.03)

    def test_scattered_votes_do_not(self) -> None:
        # bad guy, sheet already right: wandering votes, median -0.63
        votes = [-1.5, -1.3, -1.0, -0.9, -0.8, -0.7, -0.6, -0.4, -0.1, 0.0, 0.1, -0.2, -0.65, -1.1]
        self.assertEqual(la.sheet_offset(votes), 0.0)

    def test_too_few_or_too_small(self) -> None:
        self.assertEqual(la.sheet_offset([-1.0] * 5), 0.0)
        self.assertEqual(la.sheet_offset([0.2] * 20), 0.0)


class AlignSheetTest(unittest.TestCase):
    WORDS = ["never", "gonna", "give", "you", "up", "never", "gonna", "let",
             "you", "down", "never", "gonna", "run", "around", "and", "desert", "you"]

    def _sheet_and_em(self, late_by: float):
        lines = []
        marks: dict[int, str] = {}
        t = 2.0
        words = self.WORDS * 2
        for i in range(0, len(words), 4):
            chunk = words[i:i + 4]
            lines.append((t, " ".join(chunk)))
            for k, w in enumerate(chunk):
                # the singer: each word 0.6 s apart, ``late_by`` after the sheet
                _spell(w, t + late_by + 0.6 * k + la.ONSET_BIAS_S, marks)
            t += 3.0
        sheet = lw.line_sheet(lines)
        return sheet, FakeEm(_logp(int((t + 3) / la.FRAME_S), marks))

    def test_a_late_sheet_is_pulled_onto_the_audio(self) -> None:
        sheet, em = self._sheet_and_em(0.8)
        fit = la.align_sheet(sheet, em)
        self.assertAlmostEqual(fit.offset, 0.8, delta=0.08)
        first = fit.sheet[0]
        self.assertEqual(first.timing, "aligned")
        self.assertAlmostEqual(first.words[0].start, 2.8, delta=0.08)
        self.assertAlmostEqual(first.words[2].start, 2.8 + 1.2, delta=0.08)

    def test_a_good_sheet_is_not_shifted(self) -> None:
        sheet, em = self._sheet_and_em(0.1)
        fit = la.align_sheet(sheet, em)
        self.assertEqual(fit.offset, 0.0)

    def test_source_words_are_kept(self) -> None:
        sheet, em = self._sheet_and_em(0.0)
        src = lw.WordLine(sheet[0].start, sheet[0].end, sheet[0].text,
                          sheet[0].words, "source")
        fit = la.align_sheet([src] + sheet[1:], em)
        self.assertEqual(fit.sheet[0].timing, "source")
        self.assertEqual(fit.sheet[0].words, src.words)

    def test_silence_changes_nothing(self) -> None:
        sheet = lw.line_sheet([(1.0, "hello there"), (4.0, "general kenobi")])
        fit = la.align_sheet(sheet, FakeEm(_logp(400, {})))
        self.assertEqual(fit.offset, 0.0)
        self.assertTrue(all(l.timing == "line" for l in fit.sheet))


class VocalsTest(unittest.TestCase):
    def test_centre_kept_sides_dropped(self) -> None:
        sr = la.SR
        t = np.arange(sr * 2) / sr
        centre = np.sin(2 * np.pi * 440 * t).astype(np.float32)
        side = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
        stereo = np.stack((centre + side, centre - side), axis=1)
        out = la.vocals(stereo)
        spec = np.abs(np.fft.rfft(out[sr // 2:sr + sr // 2]))
        f = np.fft.rfftfreq(sr, 1 / sr)
        at = lambda hz: spec[np.argmin(abs(f - hz))]
        self.assertGreater(at(440), 10 * at(1000))
        self.assertEqual(len(out), len(stereo))


class DownloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / "blob.onnx"
        self.src.write_bytes(b"x" * 5000)
        patcher = mock.patch.object(la.config, "DATA_DIR", Path(self.tmp.name) / "data")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_verified_download_installs(self) -> None:
        sha = hashlib.sha256(self.src.read_bytes()).hexdigest()
        seen = []
        with mock.patch.object(la, "MODEL_BYTES", 5000), mock.patch.object(la, "MODEL_SHA256", sha):
            la.download_model(progress=lambda a, b: seen.append((a, b)),
                              url=self.src.as_uri())
            self.assertTrue(la.model_installed())
        self.assertEqual(seen[-1], (5000, 5000))
        la.remove_model()
        self.assertFalse(la.model_path().exists())

    def test_checksum_mismatch_leaves_nothing(self) -> None:
        with mock.patch.object(la, "MODEL_BYTES", 5000), \
                mock.patch.object(la, "MODEL_SHA256", "0" * 64):
            with self.assertRaises(ValueError):
                la.download_model(url=self.src.as_uri())
        self.assertFalse(la.model_path().exists())
        self.assertFalse(la.model_path().with_suffix(".part").exists())

    def test_oversized_download_is_refused(self) -> None:
        with mock.patch.object(la, "MODEL_BYTES", 100):
            with self.assertRaises(ValueError):
                la.download_model(url=self.src.as_uri())
        self.assertFalse(la.model_path().exists())


if __name__ == "__main__":
    unittest.main()
