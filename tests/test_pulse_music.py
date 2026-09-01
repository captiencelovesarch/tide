"""Synthetic PCM through the detector, event recorder, and replay validator."""
import unittest
from unittest import mock

import numpy as np

from tide import audio_capture
from tide.audio_capture import (
    CHUNK, SAMPLE_RATE, PulseFrame, _compute_pulse, _pulse_energy,
    _pulse_kind, _pulse_onset,
)
from tide.pulse_map import PulseRecorder, PulseScheduler


_SPEEDS = (0.5, 1.0, 2.0)
_HITS = tuple(0.5 + index for index in range(8))


def _kick_song(hits, amplitudes=None, *, speed=1.0, end=8.0):
    samples = np.zeros(int(end / speed * SAMPLE_RATE), dtype=np.float32)
    t = np.arange(int(0.1 * SAMPLE_RATE)) / SAMPLE_RATE
    burst = np.sin(2 * np.pi * 60 * t) * np.exp(-t / 0.03)
    if amplitudes is None:
        amplitudes = (0.7,) * len(hits)
    for hit, amplitude in zip(hits, amplitudes):
        start = int(hit / speed * SAMPLE_RATE)
        samples[start:start + len(burst)] += amplitude * burst
    return samples


def _learn(samples, speed=1.0):
    recorder = PulseRecorder()
    state = None
    frames = []
    for index in range(len(samples) // CHUNK):
        chunk = np.ascontiguousarray(samples[index * CHUNK:(index + 1) * CHUNK],
                                     dtype=np.float32)
        state = _compute_pulse(chunk, state)
        kind = _pulse_kind(state.diag)
        # 100 ms capture delay plus the detector's measured 15 ms correction.
        frame = PulseFrame((index + 1) * CHUNK / SAMPLE_RATE + 0.100,
                           float(state.env), kind, 0.115,
                           onset=_pulse_onset(state.diag, kind),
                           energy=_pulse_energy(chunk))
        position = (frame.captured_at - frame.latency) * speed
        recorder.record(position, frame.level, frame.kind, onset=frame.onset,
                        energy=frame.energy, speed=speed)
        frames.append(frame)
    return recorder.snapshot(), tuple(frames)


class PulseMusicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for name in ("Popen", "run"):
            patch = mock.patch.object(audio_capture.subprocess, name,
                                      side_effect=AssertionError("no audio in tests"))
            patch.start()
            cls.addClassCleanup(patch.stop)
        cls.recordings = {speed: _learn(_kick_song(_HITS, speed=speed), speed)
                          for speed in _SPEEDS}

    def test_real_kicks_keep_count_and_phase_at_every_record_and_replay_speed(self):
        for record_speed, (learned, _frames) in self.recordings.items():
            self.assertEqual(len(learned.hits), len(_HITS))
            for observed, actual in zip(learned.hits, _HITS):
                error = abs(observed.media_time - actual) / record_speed
                self.assertLess(error, CHUNK / SAMPLE_RATE)
            for replay_speed in _SPEEDS:
                scheduler = PulseScheduler(learned)
                for hit in _HITS:
                    with self.subTest(record=record_speed, replay=replay_speed, hit=hit):
                        radius = 0.04 * record_speed / replay_speed
                        wall = np.arange(hit / replay_speed - radius,
                                         hit / replay_speed + radius, 0.001)
                        lit = [t for t in wall if (scheduler.value_at(
                            float(t * replay_speed), replay_speed) or 0.0) >= 0.5]
                        self.assertTrue(lit)
                        tolerance = CHUNK / SAMPLE_RATE * record_speed / replay_speed + 0.001
                        self.assertLess(abs(lit[0] - hit / replay_speed), tolerance)

    def test_matching_audio_survives_changed_reference_envelope_width(self):
        for record_speed, (learned, _frames) in self.recordings.items():
            for replay_speed, (_other_map, frames) in self.recordings.items():
                scheduler = PulseScheduler(learned)
                for frame in frames:
                    position = (frame.captured_at - frame.latency) * replay_speed
                    scheduler.observe(position, frame.level, replay_speed, onset=frame.onset)
                with self.subTest(record=record_speed, replay=replay_speed):
                    self.assertFalse(scheduler.disagreed)

    def test_real_quiet_and_loud_sections_keep_dynamics_under_uniform_gain(self):
        hits = tuple(0.5 + i * 0.5 for i in range(6)) + tuple(4.0 + i * 0.5 for i in range(6))
        amplitudes = (0.14,) * 6 + (0.7,) * 6
        samples = _kick_song(hits, amplitudes)
        original, _frames = _learn(samples)
        quieter, _frames = _learn(samples * 0.75)
        self.assertEqual(len(original.hits), len(hits))
        self.assertEqual(len(quieter.hits), len(hits))
        levels = [original.value_at(hit.media_time) for hit in original.hits]
        self.assertLess(max(levels[:6]), min(levels[6:]) * 0.7)
        # both levels clear the detector's absolute quiet guard; below that
        # guard, uniform gain can deliberately remove barely audible hits.
        for original_hit, quieter_hit in zip(original.hits, quieter.hits):
            self.assertAlmostEqual(original.value_at(original_hit.media_time),
                                   quieter.value_at(quieter_hit.media_time), places=5)

    def test_silence_steady_bass_pad_and_noise_do_not_invent_events(self):
        t = np.arange(int(5 * SAMPLE_RATE)) / SAMPLE_RATE
        signals = {
            "silence": np.zeros(len(t)),
            "bass": 0.6 * np.sin(2 * np.pi * 50 * t),
            "pad": 0.5 * np.sin(2 * np.pi * 350 * t) * np.sin(np.pi * t / 5) ** 2,
            "noise": np.random.default_rng(42).standard_normal(len(t)) * 0.25,
        }
        for name, samples in signals.items():
            with self.subTest(signal=name):
                learned, _frames = _learn(samples)
                self.assertEqual(learned.hits, ())
                # no invented hits; sustained bass still breathes at the
                # recorded envelope, capped at the live sustain ceiling
                reference = learned.reference_at(2.5)
                self.assertEqual(learned.value_at(2.5),
                                 min(reference, audio_capture.PULSE_SUSTAIN_FLOOR))
                if name != "bass":
                    self.assertLess(learned.value_at(2.5), 0.02)

    def test_syncopation_missing_beats_and_tempo_changes_preserve_real_attacks(self):
        hits = (0.5, 1.0, 1.5, 2.0, 2.17, 2.5, 3.0, 3.75, 4.5,
                4.75, 5.0, 5.25, 5.5, 5.75, 6.0)
        learned, _frames = _learn(_kick_song(hits))
        self.assertEqual(len(learned.hits), len(hits))
        for observed, actual in zip(learned.hits, hits):
            self.assertLess(abs(observed.media_time - actual), CHUNK / SAMPLE_RATE)
        self.assertFalse(any(abs(hit.media_time - 3.5) < 0.1 for hit in learned.hits))
        self.assertTrue(any(abs(hit.media_time - 2.17) < 0.02 for hit in learned.hits))

    def test_shifted_real_kicks_force_fallback(self):
        learned, _frames = self.recordings[1.0]
        _other_map, frames = _learn(_kick_song(tuple(hit + 0.25 for hit in _HITS)))
        scheduler = PulseScheduler(learned)
        for frame in frames:
            position = frame.captured_at - frame.latency
            scheduler.observe(position, frame.level, onset=frame.onset)
        self.assertTrue(scheduler.disagreed)
