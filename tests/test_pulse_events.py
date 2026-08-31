"""observed hits, musical dynamics, and replay shapes."""
import base64
import json
import math
import unittest
import zlib

from tide.pulse_map import (
    PulseEvent, PulseHit, PulseMap, PulseRecorder, PulseScheduler, merge_maps,
)


def _hit_map(hits, *, end=8.0, coverage=None, reference=0.0):
    spans = tuple(coverage or ((0.0, end),))
    events = tuple(PulseEvent(t, reference, "sustain")
                   for span in spans for t in span)
    return PulseMap(events, spans, tuple(hits))


def _record_attacks(times, *, speed=1.0, energy=0.3, end=None):
    recorder = PulseRecorder()
    stop = end if end is not None else max(times, default=0.0) + 0.7
    step = 0.01 * speed
    for index in range(int(round(stop / step)) + 1):
        position = index * step
        elapsed = [(position - hit) / speed for hit in times
                   if position >= hit - 1e-10]
        age = min(elapsed, default=math.inf)
        if -1e-9 <= age < 0.025:
            onset = 0.95 if 0.005 <= age < 0.015 else 0.7
        else:
            onset = 0.0
        level = 0.95 * math.exp(-max(0.0, age) / 0.22)
        recorder.record(position, level, "kick" if onset else "sustain",
                        onset=onset, energy=energy if onset else energy * 0.1,
                        speed=speed)
    return recorder.snapshot()


class PulseEventExtractionTest(unittest.TestCase):
    def test_one_attack_spread_across_frames_is_one_hit(self):
        recorder = PulseRecorder()
        attack = {30: 0.25, 31: 0.65, 32: 0.95, 33: 0.8, 34: 0.3, 35: 0.05}
        for index in range(81):
            onset = attack.get(index, 0.0)
            recorder.record(index / 100, max(onset, 0.2), "kick" if onset else "sustain",
                            onset=onset, energy=0.3)
        learned = recorder.snapshot()
        self.assertEqual(len(learned.hits), 1)
        self.assertAlmostEqual(learned.hits[0].media_time, 0.32)
        self.assertGreater(learned.hits[0].confidence, 0.8)
        self.assertEqual(learned.hits[0].kind, "kick")

    def test_renewed_attack_after_a_valley_is_not_swallowed(self):
        recorder = PulseRecorder()
        attack = {30: 0.95, 31: 0.8, **{i: 0.2 for i in range(32, 38)},
                  38: 0.9, 39: 0.7}
        for index in range(81):
            onset = attack.get(index, 0.0)
            recorder.record(index / 100, max(onset, 0.5), "kick" if onset else "sustain",
                            onset=onset, energy=0.3)
        learned = recorder.snapshot()
        self.assertEqual(len(learned.hits), 2)
        self.assertAlmostEqual(learned.hits[0].media_time, 0.3)
        self.assertAlmostEqual(learned.hits[1].media_time, 0.38)

    def test_zero_onset_does_not_turn_bright_sustain_into_hits(self):
        recorder = PulseRecorder()
        for index in range(101):
            recorder.record(index / 100, 0.85, "sustain", onset=0.0, energy=0.3)
        learned = recorder.snapshot()
        self.assertEqual(learned.hits, ())
        self.assertEqual(learned.value_at(0.5), 0.0)
        self.assertAlmostEqual(learned.reference_at(0.5), 0.85)

    def test_silence_has_coverage_without_events(self):
        recorder = PulseRecorder()
        for index in range(101):
            recorder.record(index / 100, 0.0, "sustain", onset=0.0, energy=0.0)
        learned = recorder.snapshot()
        self.assertEqual(learned.hits, ())
        self.assertEqual(learned.value_at(0.5), 0.0)
        self.assertIsNone(learned.value_at(1.01))

    def test_recording_speed_does_not_duplicate_or_remove_attacks(self):
        times = (1.0, 1.5, 2.0, 2.5, 3.0)
        for speed in (0.5, 1.0, 2.0):
            with self.subTest(speed=speed):
                learned = _record_attacks(times, speed=speed)
                self.assertEqual(len(learned.hits), len(times))
                for event, expected in zip(learned.hits, times):
                    self.assertLessEqual(abs(event.media_time - expected), 0.031)

    def test_partial_listens_do_not_bridge_an_unheard_gap(self):
        recorder = PulseRecorder()
        for index in range(41):
            onset = 0.9 if index == 30 else 0.0
            recorder.record(index / 100, onset, "kick", onset=onset, energy=0.3)
        recorder.gap()
        for index in range(100, 141):
            recorder.record(index / 100, 0.0, "sustain", onset=0.0, energy=0.0)
        learned = recorder.snapshot()
        self.assertIsNone(learned.value_at(0.7))
        self.assertEqual(learned.value_at(1.0), 0.0)
        self.assertEqual(len(learned.hits), 1)


class PulseEventShapeTest(unittest.TestCase):
    def test_shape_uses_wall_seconds_at_every_replay_speed(self):
        learned = _hit_map((PulseHit(2.0, 0.9),))
        for age in (0.0, 0.015, 0.06, 0.15, 0.3):
            expected = learned.value_at(2.0 + age, speed=1.0)
            for speed in (0.5, 1.0, 2.0):
                with self.subTest(age=age, speed=speed):
                    self.assertAlmostEqual(learned.value_at(2.0 + age * speed, speed=speed),
                                           expected, places=8)
        self.assertAlmostEqual(learned.value_at(2.015), 0.9)
        self.assertLess(learned.value_at(2.15), learned.value_at(2.06))

    def test_learning_speed_does_not_change_replay_release_width(self):
        maps = [_record_attacks((1.0, 3.0), speed=speed)
                for speed in (0.5, 1.0, 2.0)]
        for replay_speed in (0.5, 1.0, 2.0):
            ratios = []
            for learned in maps:
                hit = learned.hits[0]
                peak = learned.value_at(hit.media_time, replay_speed)
                ratios.append(learned.value_at(hit.media_time + 0.15 * replay_speed,
                                               replay_speed) / peak)
            with self.subTest(replay_speed=replay_speed):
                self.assertLess(max(ratios) - min(ratios), 0.02)

    def test_overlapping_attacks_do_not_add_into_a_full_brightness_plateau(self):
        first = _hit_map((PulseHit(1.0, 0.6),))
        second = _hit_map((PulseHit(1.1, 0.6),))
        together = _hit_map((PulseHit(1.0, 0.6), PulseHit(1.1, 0.6)))
        for position in (1.1, 1.115, 1.15, 1.2):
            self.assertAlmostEqual(together.value_at(position),
                                   max(first.value_at(position), second.value_at(position)))
            self.assertLessEqual(together.value_at(position), 0.6)

    def test_broadband_beat_has_a_shorter_release_than_kick(self):
        kick = _hit_map((PulseHit(1.0, 0.8, "kick"),))
        beat = _hit_map((PulseHit(1.0, 0.8, "beat"),))
        self.assertAlmostEqual(kick.value_at(1.0), beat.value_at(1.0))
        self.assertGreater(kick.value_at(1.15), beat.value_at(1.15))

    def test_previous_coverage_tail_does_not_bleed_into_later_span(self):
        learned = _hit_map((PulseHit(0.99, 0.9),),
                           coverage=((0.0, 1.0), (1.05, 2.0)))
        self.assertGreater(learned.value_at(1.0), 0.8)
        self.assertIsNone(learned.value_at(1.025))
        self.assertEqual(learned.value_at(1.05), 0.0)

    def test_reference_envelope_is_separate_from_shaped_animation(self):
        learned = _hit_map((PulseHit(1.0, 0.9),), reference=0.2)
        self.assertAlmostEqual(learned.reference_at(1.0), 0.2)
        self.assertAlmostEqual(learned.value_at(1.0), 0.9)
        self.assertEqual(learned.value_at(0.5), 0.0)


class PulseDynamicsTest(unittest.TestCase):
    def test_equal_local_onsets_keep_quiet_and_loud_passages_distinct(self):
        learned = _hit_map((PulseHit(1.0, 0.9, energy=0.03),
                            PulseHit(3.0, 0.9, energy=0.3)))
        quiet = learned.value_at(1.0)
        loud = learned.value_at(3.0)
        self.assertGreater(quiet, 0.0)
        self.assertLess(quiet, loud * 0.7)

    def test_uniform_volume_change_preserves_relative_dynamics(self):
        original = _hit_map((PulseHit(1.0, 0.9, energy=0.03),
                             PulseHit(3.0, 0.9, energy=0.3)))
        quieter = _hit_map((PulseHit(1.0, 0.9, energy=0.0075),
                            PulseHit(3.0, 0.9, energy=0.075)))
        for position in (1.0, 1.1, 3.0, 3.1):
            self.assertAlmostEqual(original.value_at(position), quieter.value_at(position))

    def test_merge_recomputes_one_reference_across_earlier_and_later_passages(self):
        quiet_hit = PulseHit(1.0, 0.9, energy=0.03)
        loud_hit = PulseHit(5.0, 0.9, energy=0.3)
        quiet = _hit_map((quiet_hit,), end=2.0)
        loud = _hit_map((loud_hit,), coverage=((4.0, 6.0),))
        merged = merge_maps(quiet, loud)
        together = _hit_map((quiet_hit, loud_hit), coverage=((0.0, 2.0), (4.0, 6.0)))
        self.assertLess(merged.value_at(1.0), quiet.value_at(1.0))
        for position in (1.0, 1.1, 5.0, 5.1):
            self.assertAlmostEqual(merged.value_at(position), together.value_at(position))
        self.assertIsNone(merged.value_at(3.0))

    def test_unknown_energy_does_not_silence_an_observed_hit(self):
        learned = _hit_map((PulseHit(1.0, 0.8, energy=None),))
        self.assertAlmostEqual(learned.value_at(1.0), 0.8)


class PulseRhythmTest(unittest.TestCase):
    def test_regular_observed_attacks_get_a_bounded_hint(self):
        times = tuple(0.5 + index * 0.5 for index in range(12))
        learned = _record_attacks(times)
        self.assertEqual(len(learned.hits), len(times))
        hints = [hit for hit in learned.hits if hit.period is not None]
        self.assertTrue(hints)
        for hit in hints:
            self.assertGreaterEqual(hit.period, 0.3)
            self.assertLessEqual(hit.period, 1.0)
            self.assertGreater(hit.rhythm_confidence, 0.0)
            self.assertLessEqual(hit.rhythm_confidence, 1.0)

    def test_syncopation_keeps_every_observed_timestamp(self):
        times = (0.5, 1.0, 1.5, 2.0, 2.17, 2.5, 3.0, 3.5, 4.0, 4.5)
        learned = _record_attacks(times)
        self.assertEqual(len(learned.hits), len(times))
        for actual, expected in zip(learned.hits, times):
            self.assertLessEqual(abs(actual.media_time - expected), 0.02)
        self.assertTrue(any(abs(hit.media_time - 2.17) <= 0.02 for hit in learned.hits))

    def test_missing_beats_are_not_filled_in(self):
        times = (0.5, 1.0, 1.5, 2.0, 3.0, 3.5, 4.0, 4.5)
        learned = _record_attacks(times)
        self.assertEqual(len(learned.hits), len(times))
        self.assertFalse(any(abs(hit.media_time - 2.5) < 0.1 for hit in learned.hits))

    def test_rapid_percussion_cannot_produce_an_unbounded_fast_hint(self):
        times = tuple(0.5 + index * 0.125 for index in range(32))
        learned = _record_attacks(times)
        self.assertEqual(len(learned.hits), len(times))
        for hit in learned.hits:
            if hit.period is not None:
                self.assertGreaterEqual(hit.period, 0.3)
                self.assertLessEqual(hit.period, 1.0)


class PulseEventValidationTest(unittest.TestCase):
    def test_matching_attacks_survive_different_record_and_replay_speeds(self):
        times = tuple(float(index) for index in range(1, 8))
        for record_speed, replay_speed in ((0.5, 2.0), (2.0, 0.5), (1.0, 1.0)):
            scheduler = PulseScheduler(_record_attacks(times, speed=record_speed, end=8.0))
            step = 0.01 * replay_speed
            for index in range(int(round(8.0 / step)) + 1):
                position = index * step
                ages = [(position - hit) / replay_speed for hit in times
                        if position >= hit - 1e-10]
                age = min(ages, default=math.inf)
                onset = 0.9 if -1e-9 <= age < 0.025 else 0.0
                level = 0.9 * math.exp(-max(0.0, age) / 0.22)
                scheduler.observe(position, level, replay_speed, onset=onset)
            with self.subTest(record_speed=record_speed, replay_speed=replay_speed):
                self.assertFalse(scheduler.disagreed)

    def test_shaped_brightness_difference_does_not_invalidate_matching_audio(self):
        scheduler = PulseScheduler(_hit_map((), end=3.0, reference=0.9))
        for index in range(301):
            scheduler.observe(index / 100, 0.9, onset=0.0)
        self.assertFalse(scheduler.disagreed)

    def test_repeated_extra_attacks_force_fallback(self):
        scheduler = PulseScheduler(_hit_map((), end=5.0))
        for index in range(501):
            position = index / 100
            onset = 0.9 if any(abs(position - hit) < 0.012 for hit in (1.0, 2.0, 3.0, 4.0)) else 0.0
            scheduler.observe(position, onset, onset=onset)
        self.assertTrue(scheduler.disagreed)

    def test_repeated_extra_attacks_are_not_erased_by_matching_beats(self):
        learned = _hit_map(tuple(PulseHit(float(i), 0.9) for i in range(1, 10)), end=10.0)
        scheduler = PulseScheduler(learned)
        for index in range(1001):
            position = index / 100
            onset = 0.9 if 100 <= index < 950 and index % 50 in (0, 1) else 0.0
            scheduler.observe(position, onset, onset=onset)
        self.assertTrue(scheduler.disagreed)

    def test_validation_uses_the_same_debounce_as_recording(self):
        recorder = PulseRecorder()
        observations = []
        for index in range(801):
            onset = 0.0
            if 100 <= index < 750:
                if index % 100 == 0:
                    onset = 0.9
                elif index % 100 == 3:
                    onset = 0.8
            position = index / 100
            recorder.record(position, onset, "kick" if onset else "sustain",
                            onset=onset, energy=0.3)
            observations.append((position, onset))
        learned = recorder.snapshot()
        self.assertEqual(len(learned.hits), 7)
        scheduler = PulseScheduler(learned)
        for position, onset in observations:
            scheduler.observe(position, onset, onset=onset)
        self.assertFalse(scheduler.disagreed)

    def test_broad_attack_then_flam_validates_its_own_recorded_evidence(self):
        recorder = PulseRecorder()
        observations = []
        for index in range(801):
            onset = 0.0
            if 100 <= index < 750:
                onset = {0: 0.25, 1: 0.4, 2: 0.7, 3: 0.95, 7: 0.8}.get(index % 100, 0.0)
            position = index / 100
            recorder.record(position, onset, "kick" if onset else "sustain",
                            onset=onset, energy=0.3)
            observations.append((position, onset))
        learned = recorder.snapshot()
        self.assertEqual(len(learned.hits), 7)
        self.assertAlmostEqual(learned.hits[0].media_time, 1.03)
        scheduler = PulseScheduler(learned)
        for position, onset in observations:
            scheduler.observe(position, onset, onset=onset)
        self.assertFalse(scheduler.disagreed)

    def test_gradually_rising_attack_can_reach_its_learned_peak_after_300ms(self):
        recorder = PulseRecorder()
        observations = []
        for index in range(801):
            onset = 0.0
            phase = index % 100
            if 100 <= index < 750 and phase <= 45:
                onset = 0.25 + 0.7 * phase / 45
            position = index / 100
            recorder.record(position, onset, "kick" if onset else "sustain",
                            onset=onset, energy=0.3)
            observations.append((position, onset))
        learned = recorder.snapshot()
        self.assertEqual(len(learned.hits), 7)
        self.assertAlmostEqual(learned.hits[0].media_time, 1.45)
        scheduler = PulseScheduler(learned)
        for position, onset in observations:
            scheduler.observe(position, onset, onset=onset)
        self.assertFalse(scheduler.disagreed)

    def test_debounce_boundary_preserves_matching_attacks_at_each_speed(self):
        for speed in (0.5, 1.0, 2.0):
            for gap_ms in (49, 51):
                for strengths in ((0.9, 0.8), (0.8, 0.95)):
                    with self.subTest(speed=speed, gap_ms=gap_ms, strengths=strengths):
                        recorder = PulseRecorder()
                        observations = []
                        for index in range(8001):
                            onset = 0.0
                            if 1000 <= index < 7500:
                                if index % 1000 == 0:
                                    onset = strengths[0]
                                elif index % 1000 == gap_ms:
                                    onset = strengths[1]
                            position = index / 1000 * speed
                            recorder.record(position, onset, "kick" if onset else "sustain",
                                            onset=onset, energy=0.3, speed=speed)
                            observations.append((position, onset))
                        learned = recorder.snapshot()
                        self.assertEqual(len(learned.hits), 7 if gap_ms == 49 else 14)
                        scheduler = PulseScheduler(learned)
                        for position, onset in observations:
                            scheduler.observe(position, onset, speed, onset=onset)
                        self.assertFalse(scheduler.disagreed)

    def test_repeated_missing_attacks_force_fallback(self):
        scheduler = PulseScheduler(_hit_map(tuple(PulseHit(float(i), 0.9) for i in range(1, 5)),
                                            end=5.0))
        for index in range(501):
            scheduler.observe(index / 100, 0.0, onset=0.0)
        self.assertTrue(scheduler.disagreed)


class PulseEventPayloadTest(unittest.TestCase):
    def test_hit_and_reference_round_trip(self):
        original = _hit_map((PulseHit(1.0, 0.8, "kick", confidence=0.92,
                                     energy=0.3, period=0.5, rhythm_confidence=0.8),
                             PulseHit(3.0, 0.6, "beat", confidence=0.7, energy=0.03)),
                            reference=0.4)
        restored = PulseMap.from_payload(json.loads(json.dumps(original.payload())))
        self.assertIsNotNone(restored)
        self.assertEqual(len(restored.hits), 2)
        self.assertEqual(restored.hits[0].kind, "kick")
        self.assertAlmostEqual(restored.hits[0].confidence, 0.92, places=5)
        self.assertAlmostEqual(restored.hits[0].energy, 0.3, places=5)
        self.assertAlmostEqual(restored.hits[0].period, 0.5, places=5)
        self.assertAlmostEqual(restored.hits[0].rhythm_confidence, 0.8, places=5)
        for position in (0.5, 1.0, 1.1, 3.0, 3.1):
            self.assertAlmostEqual(restored.reference_at(position), original.reference_at(position), places=5)
            self.assertAlmostEqual(restored.value_at(position), original.value_at(position), places=5)

    def test_structured_silence_stays_structured_after_round_trip(self):
        restored = PulseMap.from_payload(_hit_map((), reference=0.5).payload())
        self.assertIsNotNone(restored)
        self.assertEqual(restored.hits, ())
        self.assertEqual(restored.value_at(1.0), 0.0)
        self.assertAlmostEqual(restored.reference_at(1.0), 0.5)

    def test_malformed_hit_metadata_is_a_cache_miss(self):
        valid = _hit_map((PulseHit(1.0, 0.8),)).payload()
        bad_fields = ((0, math.nan), (0, -0.1), (1, 1.1),
                      (2, "sustain"), (3, -0.1), (3, math.inf),
                      (4, -0.1), (4, math.nan), (5, -1.0),
                      (5, math.inf), (6, 1.1))
        for field, value in bad_fields:
            row = list(valid["hits"][0])
            row[field] = value
            with self.subTest(field=field, value=value):
                self.assertIsNone(PulseMap.from_payload({**valid, "hits": [row]}))
        for rows in ([valid["hits"][0], valid["hits"][0]], [[1.0, 0.8]], "invalid"):
            with self.subTest(rows=rows):
                self.assertIsNone(PulseMap.from_payload({**valid, "hits": rows}))

    def test_v1_envelopes_are_not_loaded_as_discrete_hits(self):
        raw = json.dumps([[0.0, 0.0, "kick"], [1.0, 0.9, "kick"]]).encode()
        legacy = {"version": 1, "coverage": [[0.0, 1.0]],
                  "trace": base64.b64encode(zlib.compress(raw)).decode("ascii")}
        self.assertIsNone(PulseMap.from_payload(legacy))

    def test_legacy_direct_callers_keep_envelope_semantics(self):
        recorder = PulseRecorder()
        recorder.record(0.0, 0.0)
        recorder.record(1.0, 1.0)
        legacy = recorder.snapshot()
        self.assertIsNone(legacy.hits)
        self.assertAlmostEqual(legacy.value_at(0.5), 0.5)
        self.assertAlmostEqual(legacy.reference_at(0.5), 0.5)


if __name__ == "__main__":
    unittest.main()
