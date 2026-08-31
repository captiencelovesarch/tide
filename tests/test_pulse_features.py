"""Event evidence stays separate from the live detector's release tail."""
import math
import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest import mock

import numpy as np

from tide import audio_capture
from tide.audio_capture import AudioVisualizerFeed, CHUNK, SAMPLE_RATE, PulseFrame


class _CapturePipe:
    def __init__(self, feed, chunks):
        self.feed = feed
        self.chunks = iter(chunks)

    def read(self, _size):
        try:
            return next(self.chunks)
        except StopIteration:
            self.feed._stop.set()
            return b""


class PulseFeaturesTest(unittest.TestCase):
    def setUp(self):
        for name in ("Popen", "run"):
            patch = mock.patch.object(audio_capture.subprocess, name,
                                      side_effect=AssertionError("no audio in tests"))
            patch.start()
            self.addCleanup(patch.stop)

    def _feed(self):
        feed = AudioVisualizerFeed()
        feed._pulse_latency_override = 0.0
        feed._stream_idx = 7
        frames, levels = [], []
        feed.pulse_frame.connect(frames.append)
        feed.pulse_updated.connect(levels.append)
        return feed, frames, levels

    def _capture(self, samples):
        feed, frames, levels = self._feed()
        samples = np.asarray(samples, dtype=np.float32)
        chunks = [samples[i:i + CHUNK].tobytes()
                  for i in range(0, len(samples) - CHUNK + 1, CHUNK)]
        feed._proc = SimpleNamespace(stdout=_CapturePipe(feed, chunks), poll=lambda: 0)
        feed._process_loop()
        return frames, levels

    def test_old_frame_callers_keep_optional_metadata(self):
        frame = PulseFrame(10.0, 0.7, "kick", 0.025)
        self.assertIsNone(frame.onset)
        self.assertIsNone(frame.energy)
        with self.assertRaises(FrozenInstanceError):
            frame.energy = 0.2
        feed, frames, _levels = self._feed()
        feed._emit_pulse_frame(SimpleNamespace(env=0.7, diag={}))
        self.assertIsNone(frames[0].onset)
        self.assertIsNone(frames[0].energy)

    def test_silence_has_zero_energy_and_no_onsets(self):
        frames, levels = self._capture(np.zeros(CHUNK * 8))
        self.assertEqual(len(frames), 8)
        self.assertEqual(levels, [0.0] * 8)
        self.assertTrue(all(frame.onset == 0.0 for frame in frames))
        self.assertTrue(all(frame.energy == 0.0 for frame in frames))

    def test_sustained_bass_has_energy_without_events(self):
        t = np.arange(CHUNK * 100) / SAMPLE_RATE
        samples = 0.5 * np.sin(2 * np.pi * (SAMPLE_RATE / CHUNK * 2) * t)
        frames, _levels = self._capture(samples)
        self.assertTrue(all(frame.onset == 0.0 for frame in frames))
        for frame in frames:
            self.assertAlmostEqual(frame.energy, 0.5 / math.sqrt(2), places=6)

    def test_kick_attack_and_release_tail_remain_distinct(self):
        samples = np.zeros(CHUNK * 70)
        start = CHUNK * 20
        t = np.arange(CHUNK * 5) / SAMPLE_RATE
        samples[start:start + len(t)] = 0.7 * np.sin(2 * np.pi * 60 * t) * np.exp(-t / 0.03)
        frames, levels = self._capture(samples)
        self.assertGreater(max(frame.onset for frame in frames), 0.5)
        hits = [frame for frame in frames if frame.onset > 0.5]
        self.assertTrue(all(frame.kind == "kick" for frame in hits))
        self.assertTrue(all(frame.energy > 0.0 for frame in hits))
        tails = [frame for frame in frames[26:40] if frame.level > 0.1]
        self.assertTrue(tails)
        self.assertTrue(all(frame.onset == 0.0 for frame in tails))
        self.assertTrue(all(frame.energy == 0.0 for frame in tails))
        # metadata must leave the signal consumed by the live backdrop alone.
        state = None
        expected = []
        for index in range(0, len(samples), CHUNK):
            state = audio_capture._compute_pulse(
                samples[index:index + CHUNK].astype(np.float32), state)
            expected.append(state.env)
        self.assertEqual(levels, expected)

    def test_winning_path_reports_attack_before_envelope_release(self):
        feed, frames, _levels = self._feed()
        cases = (
            ({"gate_quiet": 0.75, "gate_fraction": 0.5, "onset": 0.6,
              "contrast": 0.4, "level": 0.225}, "kick", 0.225),
            ({"gate_quiet": 0.0, "gate_fraction": 0.0, "onset": 0.0,
              "contrast": 0.0, "level": 0.4}, "beat", 0.4),
            ({"gate_quiet": 1.0, "gate_fraction": 1.0, "onset": 0.03,
              "contrast": 1.0, "level": 0.22}, "sustain", 0.0),
        )
        for diag, kind, onset in cases:
            with self.subTest(kind=kind):
                feed._emit_pulse_frame(SimpleNamespace(env=0.9, diag=diag), energy=0.1)
                self.assertEqual(frames[-1].kind, kind)
                self.assertAlmostEqual(frames[-1].onset, onset)
                self.assertEqual(frames[-1].level, 0.9)
                self.assertEqual(frames[-1].energy, 0.1)

    def test_first_chunk_and_recovery_do_not_invent_attacks(self):
        t = np.arange(CHUNK) / SAMPLE_RATE
        loud = 0.7 * np.sin(2 * np.pi * 80 * t)
        samples = np.concatenate((loud, np.full(CHUNK, np.nan), loud))
        frames, levels = self._capture(samples)
        self.assertEqual([frame.reset for frame in frames], [False, True, False])
        self.assertEqual([frame.onset for frame in frames], [0.0, 0.0, 0.0])
        self.assertIsNone(frames[1].energy)
        self.assertGreater(frames[0].energy, 0.0)
        self.assertEqual(frames[0].energy, frames[2].energy)
        self.assertEqual(len(levels), 2)

    def test_reset_marks_missing_audio_instead_of_silent_audio(self):
        feed, frames, _levels = self._feed()
        feed._emit_pulse_frame(None, energy=0.6)
        self.assertTrue(frames[0].reset)
        self.assertEqual(frames[0].onset, 0.0)
        self.assertIsNone(frames[0].energy)

    def test_rms_is_unwindowed_and_preserves_monitor_volume(self):
        quiet, _levels = self._capture(np.full(CHUNK, 0.125))
        loud, _levels = self._capture(np.full(CHUNK, 0.5))
        self.assertEqual(quiet[0].energy, 0.125)
        self.assertEqual(loud[0].energy, 0.5)
        self.assertEqual(loud[0].energy / quiet[0].energy, 4.0)

    def test_rms_uses_no_fft_and_handles_float32_extremes(self):
        with mock.patch.object(audio_capture.np.fft, "rfft",
                               side_effect=AssertionError("no extra fft")):
            self.assertAlmostEqual(audio_capture._pulse_energy(
                np.array([0.3, -0.4], dtype=np.float32)), math.sqrt(0.125))
            limit = np.finfo(np.float32).max
            self.assertEqual(audio_capture._pulse_energy(
                np.full(CHUNK, limit, dtype=np.float32)), float(limit))

    def test_non_finite_evidence_cannot_enter_metadata(self):
        feed, frames, _levels = self._feed()
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                self.assertIsNone(audio_capture._pulse_energy(
                    np.full(CHUNK, value, dtype=np.float32)))
                diag = {"gate_quiet": 1.0, "gate_fraction": 1.0,
                        "onset": value, "contrast": 0.0, "level": value}
                feed._emit_pulse_frame(SimpleNamespace(env=0.7, diag=diag), energy=value)
                frame = frames[-1]
                self.assertTrue(frame.onset is None or math.isfinite(frame.onset))
                self.assertIsNone(frame.energy)
        feed._emit_pulse_frame(SimpleNamespace(env=0.7, diag={}), energy=-0.1)
        self.assertIsNone(frames[-1].energy)
        self.assertIsNone(audio_capture._pulse_energy(np.array([], dtype=np.float32)))
