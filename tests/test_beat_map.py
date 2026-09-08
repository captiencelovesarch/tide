"""the beat analyzer on synthetic material with known kick times."""
import math
import os
import shutil
import struct
import tempfile
import unittest
import wave

import numpy as np

from tide import beat_map
from tide.beat_map import (
    ATTACK_S, HOLD_S, SR, Analysis, Beat, BeatMap, Hit, analyze, analyze_target,
    decode_pcm, is_analyzable,
)


def _kick(t, hz=60.0, decay=0.04):
    phase = 2 * np.pi * (hz * t + 40.0 * decay * (1 - np.exp(-t / decay)))
    return np.sin(phase) * np.exp(-t / decay)


def _snare(t, rng, decay=0.08):
    n = np.diff(rng.standard_normal(t.size), prepend=0.0)
    return n * np.exp(-t / decay) * 0.5


def song(bpm=120.0, seconds=30.0, *, snare=True, hats=True, intro=2.0,
         extra=None, gain=None, seed=1):
    """kick on odd beats, snare on even ones, hats on eighths. ``extra``
    adds off-grid kicks at these fractions of a period after the given
    beat indices."""
    rng = np.random.default_rng(seed)
    out = np.zeros(int(seconds * SR))
    t = np.arange(int(0.25 * SR)) / SR
    k, s = _kick(t), _snare(t, rng)
    h = _snare(t[: int(0.03 * SR)], rng, decay=0.01)
    kicks = []
    period = 60.0 / bpm
    pos, beat = intro, 0
    while pos < seconds - 0.3:
        start = int(pos * SR)
        g = gain(pos) if gain is not None else 1.0
        if beat % 2 == 0:
            out[start:start + k.size] += 0.8 * g * k[: out.size - start]
            kicks.append(pos)
        elif snare:
            out[start:start + s.size] += 0.35 * g * s[: out.size - start]
        if hats:
            for sub in (0.0, 0.5):
                hs = int((pos + sub * period) * SR)
                if hs + h.size < out.size:
                    out[hs:hs + h.size] += 0.12 * g * h
        if extra and beat in extra:
            at = pos + extra[beat] * period
            st = int(at * SR)
            out[st:st + k.size] += 0.8 * g * k[: out.size - st]
            kicks.append(at)
        pos += period
        beat += 1
    peak = np.abs(out).max() or 1.0
    return (out / peak * 0.9).astype(np.float32), sorted(kicks)


def nearest(times, t):
    times = np.asarray(times)
    if not times.size:
        return math.inf
    j = np.searchsorted(times, t)
    return min((abs(times[i] - t) for i in (j - 1, j) if 0 <= i < times.size),
               default=math.inf)


def pulsed(m):
    return [b for b in m.beats if not (b.period < beat_map.HALF_TIME_PERIOD_S and b.phase % 2)]


class AnalysisTest(unittest.TestCase):
    def test_tempo_and_beats_land_on_the_kicks(self):
        pcm, kicks = song(120.0)
        m = analyze(pcm)
        self.assertTrue(m.complete)
        self.assertAlmostEqual(m.tempo, 120.0, delta=2.5)
        self.assertGreater(m.confidence, 0.8)
        times = [b.time for b in m.beats]
        for t in kicks:
            self.assertLess(nearest(times, t), 0.015, f"kick at {t:.3f} has no beat")
        # kicks read stronger than the snares between them
        by_kick = [b.strength for b in m.beats if nearest(kicks, b.time) < 0.03 and b.time > 2.0]
        by_snare = [b.strength for b in m.beats if nearest(kicks, b.time) > 0.2 and b.time > 2.5]
        self.assertGreater(min(by_kick), max(by_snare))

    def test_identical_kicks_get_identical_strength(self):
        pcm, kicks = song(140.0, snare=False, hats=False)
        m = analyze(pcm)
        strengths = np.array([b.strength for b in m.beats if nearest(kicks, b.time) < 0.03])
        self.assertGreater(strengths.mean(), 0.8)
        self.assertLess(strengths.std() / strengths.mean(), 0.1)

    def test_a_kick_only_song_pulses_on_its_kicks(self):
        # nothing marks the beats between kicks, so the tempo is the kick
        # rate and every beat is a kick
        pcm, kicks = song(120.0, snare=False, hats=False)
        m = analyze(pcm)
        self.assertAlmostEqual(m.tempo, 60.0, delta=1.5)
        times = [b.time for b in m.beats if b.time > 2.5]
        for t in kicks[1:]:
            self.assertLess(nearest(times, t), 0.015)
        self.assertLessEqual(len(times), len(kicks) + 1)

    def test_onset_lead_is_calibrated(self):
        offsets = []
        for bpm, shift in ((90.0, 0.0), (120.0, 0.004), (150.0, 0.008)):
            pcm, kicks = song(bpm, snare=False, hats=False, intro=2.0 + shift)
            m = analyze(pcm)
            times = [b.time for b in m.beats]
            for t in kicks:
                j = int(np.argmin(np.abs(np.array(times) - t)))
                offsets.append(times[j] - t)
        self.assertLess(abs(np.median(offsets)), 0.005)
        self.assertLess(np.percentile(np.abs(offsets), 90), 0.010)

    def test_a_ramp_in_volume_keeps_the_grid_and_orders_strength(self):
        pcm, kicks = song(100.0, gain=lambda pos: 0.3 + 0.7 * min(1.0, pos / 25.0))
        m = analyze(pcm)
        times = [b.time for b in m.beats]
        for t in kicks:
            self.assertLess(nearest(times, t), 0.015)
        early = [b.strength for b in m.beats if 2.0 < b.time < 8.0 and nearest(kicks, b.time) < 0.03]
        late = [b.strength for b in m.beats if 22.0 < b.time < 29.0 and nearest(kicks, b.time) < 0.03]
        self.assertLess(np.mean(early), np.mean(late))
        self.assertLess(m.swell_at(5.0), m.swell_at(26.0))

    def test_fast_music_pulses_on_the_kicks_not_twice_as_often(self):
        pcm, kicks = song(174.0)
        m = analyze(pcm)
        grid = np.arange(5.0, 25.0, 0.002)
        values = np.array([m.value_at(p) for p in grid])
        rising = grid[1:][(values[1:] > 0.6) & (values[:-1] <= 0.6)]   # one crossing per pulse
        expected = [t for t in kicks if 5.0 <= t <= 25.0]
        self.assertAlmostEqual(len(rising), len(expected), delta=1)
        for t in expected:
            self.assertLess(nearest(rising, t), 0.05)
        # and never a pulse on the snare between two kicks
        snares = [(a + b) / 2 for a, b in zip(expected, expected[1:])]
        for t in snares:
            self.assertGreater(nearest(rising, t), 0.15)

    def test_silence_and_noise_earn_no_grid(self):
        quiet = analyze(np.zeros(int(20 * SR), dtype=np.float32))
        self.assertEqual(quiet.beats, ())
        self.assertEqual(quiet.hits, ())
        self.assertEqual(quiet.value_at(5.0), 0.0)
        noise = analyze(np.random.default_rng(2).standard_normal(int(20 * SR)).astype(np.float32) * 0.3)
        self.assertLess(noise.confidence, beat_map.CONFIDENCE_FLOOR)
        self.assertEqual(noise.beats, ())

    def test_syncopated_kick_survives_as_a_hit(self):
        extra = {b: 0.5 for b in range(8, 60, 8)}
        pcm, kicks = song(110.0, extra=extra)
        m = analyze(pcm)
        offgrid = [t for t in kicks if nearest([b.time for b in m.beats], t) > 0.1]
        self.assertGreaterEqual(len(offgrid), 3)
        hit_times = [h.time for h in m.hits]
        for t in offgrid:
            self.assertLess(nearest(hit_times, t), 0.02, f"off-grid kick at {t:.3f} lost")
        # and no phantom hits elsewhere
        self.assertLessEqual(len(m.hits), len(offgrid) + 2)

    def test_partial_prefix_agrees_with_the_final_map(self):
        pcm, kicks = song(120.0, seconds=40.0)
        full = analyze(pcm)
        an = Analysis()
        block = int(2 * SR)
        for start in range(0, int(20 * SR), block):
            an.push(pcm[start:start + block])
        partial = an.build(False)
        self.assertFalse(partial.complete)
        self.assertLess(partial.duration, 21.0)
        early = [b.time for b in full.beats if 2.5 < b.time < 15.0]
        pt = [b.time for b in partial.beats]
        for t in early:
            self.assertLess(nearest(pt, t), 0.02)
        self.assertIsNone(partial.value_at(30.0))
        self.assertIsNotNone(full.value_at(30.0))


class ShapeTest(unittest.TestCase):
    def one(self, strength=1.0, swell=1.0, period=0.5, phase=0, hits=()):
        return BeatMap(10.0, True, 60.0 / period, 1.0,
                       (Beat(5.0, strength, phase, period),), tuple(hits),
                       (swell,) * 42)

    def test_peaks_on_the_beat_and_never_after(self):
        m = self.one()
        grid = np.arange(4.8, 5.6, 0.0005)
        values = [m.value_at(p) for p in grid]
        peak_at = grid[int(np.argmax(values))]
        self.assertGreaterEqual(peak_at, 5.0 - 0.0006)
        self.assertLessEqual(peak_at, 5.0 + HOLD_S + 0.0006)
        self.assertAlmostEqual(m.value_at(5.0), 1.0)
        self.assertAlmostEqual(m.value_at(5.0 - ATTACK_S), 0.25)
        self.assertLess(m.value_at(5.0 - ATTACK_S / 2), m.value_at(5.0))
        self.assertLess(m.value_at(5.3), m.value_at(5.1))

    def test_swell_sets_floor_and_depth(self):
        loud, quiet, silent = self.one(swell=1.0), self.one(swell=0.4), self.one(swell=0.0)
        self.assertAlmostEqual(loud.value_at(3.0), 0.25)
        self.assertAlmostEqual(quiet.value_at(3.0), 0.10)
        self.assertAlmostEqual(silent.value_at(3.0), 0.0)
        self.assertAlmostEqual(loud.value_at(5.0), 1.0)
        self.assertAlmostEqual(quiet.value_at(5.0), 0.10 + 0.90 * 0.70)
        self.assertAlmostEqual(silent.value_at(5.0), 0.5)

    def test_release_follows_the_beat_period(self):
        fast, slow = self.one(period=0.3), self.one(period=1.0)
        age = 5.0 + HOLD_S + 0.15
        self.assertLess(fast.value_at(age), slow.value_at(age))
        self.assertAlmostEqual(fast.value_at(5.0), slow.value_at(5.0))

    def test_half_time_skips_the_weak_phases(self):
        m = BeatMap(10.0, True, 170.0, 1.0,
                    tuple(Beat(4.0 + i * 0.353, 1.0, i % 4, 0.353) for i in range(8)),
                    (), (1.0,) * 42)
        peaks = [m.value_at(b.time) for b in m.beats]
        for i, value in enumerate(peaks):
            if i % 2 == 0:
                self.assertGreater(value, 0.9)
            else:
                self.assertLess(value, 0.9)

    def test_speed_stretches_the_attack_in_media_time(self):
        m = self.one()
        self.assertAlmostEqual(m.value_at(5.0 - ATTACK_S * 2.0, speed=2.0), 0.25)
        self.assertGreater(m.value_at(5.0 - ATTACK_S * 1.0, speed=2.0), 0.25)
        self.assertIsNone(m.value_at(5.0, speed=0.0))

    def test_outside_coverage_is_none(self):
        m = self.one()
        self.assertIsNone(m.value_at(-1.0))
        self.assertIsNone(m.value_at(10.5))
        self.assertIsNotNone(m.value_at(10.1))
        self.assertIsNone(m.value_at(float("nan")))

    def test_hits_pulse_too(self):
        m = self.one(hits=(Hit(7.0, 0.8),))
        self.assertAlmostEqual(m.value_at(7.0), 0.25 + 0.75 * 0.8)


class PayloadTest(unittest.TestCase):
    def test_round_trip(self):
        pcm, _kicks = song(120.0, seconds=12.0)
        m = analyze(pcm)
        back = BeatMap.from_payload(m.payload())
        self.assertIsNotNone(back)
        self.assertEqual(len(back.beats), len(m.beats))
        self.assertEqual(back.complete, m.complete)
        for a, b in zip(m.beats, back.beats):
            self.assertAlmostEqual(a.time, b.time, places=4)
            self.assertAlmostEqual(a.strength, b.strength, places=3)
            self.assertEqual(a.phase, b.phase)
        for p in (3.0, 5.25, 8.9):
            self.assertAlmostEqual(m.value_at(p), back.value_at(p), places=2)

    def test_garbage_is_a_miss(self):
        good = ShapeTest().one().payload()
        self.assertIsNotNone(BeatMap.from_payload(good))
        for bad in (None, [], {"version": 2}, {**good, "beats": [[1, 2]]},
                    {**good, "beats": [[5.0, 1.5, 0, 0.5]]},
                    {**good, "beats": [[5.0, 1.0, 7, 0.5]]},
                    {**good, "swell": [2.0]}, {**good, "duration": float("nan")},
                    {**good, "hits": [[3.0, 0.5], [2.0, 0.5]]}):
            self.assertIsNone(BeatMap.from_payload(bad))


class DecodeTest(unittest.TestCase):
    def test_only_urls_and_files_are_analyzable(self):
        self.assertFalse(is_analyzable(None))
        self.assertFalse(is_analyzable(""))
        self.assertFalse(is_analyzable("edl://foo"))
        self.assertFalse(is_analyzable("lavfi://amovie=x"))
        self.assertFalse(is_analyzable("/definitely/not/here.mp3"))
        self.assertFalse(is_analyzable("spotify:track:abc"))
        if shutil.which("ffmpeg"):
            self.assertTrue(is_analyzable("https://example.invalid/a.webm"))

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_decode_and_analyze_a_wav_file(self):
        pcm, kicks = song(120.0, seconds=16.0)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "kicks.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes(struct.pack(f"<{pcm.size}h", *(pcm * 32767).astype(np.int16)))
            blocks = list(decode_pcm(path))
            decoded = np.concatenate(blocks)
            self.assertAlmostEqual(decoded.size / SR, 16.0, delta=0.05)
            partials = []
            m = analyze_target(path, on_partial=partials.append)
        self.assertTrue(m.complete)
        self.assertAlmostEqual(m.tempo, 120.0, delta=2.5)
        times = [b.time for b in m.beats]
        for t in kicks:
            self.assertLess(nearest(times, t), 0.02)
        self.assertGreaterEqual(len(partials), 1)
        self.assertFalse(partials[0].complete)


if __name__ == "__main__":
    unittest.main()
