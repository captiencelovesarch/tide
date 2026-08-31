"""Learned pulse timing, partial coverage, and cache worker boundaries."""
import base64
import json
import math
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock
import zlib

import numpy as np
from PySide6.QtCore import QObject, QThread, Slot
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import cache
from tide.audio_capture import CHUNK, SAMPLE_RATE, PulseFrame, _compute_pulse
from tide.pulse_map import (
    CROSSFADE_SECONDS, NAMESPACE, PulseBlend, PulseEvent, PulseMap,
    PulseMapStore, PulseRecorder, PulseScheduler, map_from_segments, merge_maps, track_key,
)


def _map(rows, coverage=None):
    events = tuple(PulseEvent(*row) for row in rows)
    if coverage is None:
        coverage = ((events[0].media_time, events[-1].media_time),)
    return PulseMap(events, tuple(coverage))


def _payload(rows, coverage):
    raw = json.dumps(rows).encode()
    return {"version": 2, "coverage": coverage,
            "trace": base64.b64encode(zlib.compress(raw)).decode("ascii")}


class PulseMapTest(unittest.TestCase):
    def setUp(self):
        self.trace = _map([(0.0, 0.0), (0.5, 1.0), (1.0, 0.0),
                           (3.0, 0.0), (3.5, 0.0)],
                          ((0.0, 1.0), (3.0, 3.5)))

    def test_endpoints_and_interpolated_release(self):
        expected = {0.0: 0.0, 0.25: 0.5, 0.5: 1.0, 0.75: 0.5, 1.0: 0.0}
        for position, level in expected.items():
            with self.subTest(position=position):
                self.assertAlmostEqual(self.trace.value_at(position), level)

    def test_seeks_are_independent_of_last_query(self):
        scheduler = PulseScheduler(self.trace)
        positions = [3.2, 0.5, 10.0, 0.25, 2.0, 1.0, 0.0]
        self.assertEqual([scheduler.value_at(t) for t in positions],
                         [0.0, 1.0, None, 0.5, None, 0.0, 0.0])

    def test_mapped_silence_is_distinct_from_unheard_audio(self):
        for position in (-0.001, 1.001, 2.0, 2.999, 3.501):
            self.assertIsNone(self.trace.value_at(position))
        for position in (3.0, 3.25, 3.5):
            self.assertEqual(self.trace.value_at(position), 0.0)

    def test_nonfinite_clock_has_no_map(self):
        for position in (math.nan, math.inf, -math.inf):
            self.assertIsNone(self.trace.value_at(position))

    def test_playback_speed_uses_media_seconds(self):
        for speed in (0.5, 1.0, 2.0):
            with self.subTest(speed=speed):
                wall_times = np.arange(0.0, 1.0 / speed + 0.001, 0.005 / speed)
                levels = [self.trace.value_at(float(t * speed)) for t in wall_times]
                hit = wall_times[int(np.argmax(levels))]
                self.assertAlmostEqual(hit, 0.5 / speed)
                self.assertAlmostEqual(self.trace.value_at((0.75 / speed) * speed), 0.5)

    def test_track_identity_includes_source(self):
        yt = SimpleNamespace(video_id="same", source="ytmusic")
        local = SimpleNamespace(video_id="same", source="local")
        self.assertNotEqual(track_key(yt), track_key(local))
        self.assertEqual(track_key(yt), track_key(SimpleNamespace(video_id="same")))
        for track in (None, SimpleNamespace(video_id=""), SimpleNamespace(video_id=3)):
            self.assertIsNone(track_key(track))


class PulseRecorderTest(unittest.TestCase):
    def test_single_frame_does_not_claim_coverage(self):
        recorder = PulseRecorder()
        self.assertIsNone(recorder.snapshot())
        recorder.record(1.0, 0.8)
        self.assertIsNone(recorder.snapshot())
        recorder.gap()
        recorder.record(5.0, 0.0)
        self.assertIsNone(recorder.snapshot())

    def test_gap_preserves_both_listened_sections(self):
        recorder = PulseRecorder()
        for t, level in ((0.0, 0.0), (0.1, 1.0), (0.2, 0.0)):
            recorder.record(t, level)
        recorder.gap()
        recorder.record(4.0, 0.0, "sustain")
        recorder.record(4.1, 0.0, "sustain")
        trace = recorder.snapshot()
        self.assertEqual(trace.coverage, ((0.0, 0.2), (4.0, 4.1)))
        self.assertIsNone(trace.value_at(2.0))
        self.assertEqual(trace.value_at(4.05), 0.0)

    def test_bad_frame_breaks_coverage(self):
        for position, level, kind in ((math.nan, 0.5, "kick"),
                                       (0.2, math.inf, "kick"),
                                       (0.2, -0.1, "kick"),
                                       (0.2, 1.1, "kick"),
                                       (0.2, 0.5, "unknown")):
            with self.subTest(frame=(position, level, kind)):
                recorder = PulseRecorder()
                recorder.record(0.0, 0.0)
                recorder.record(0.1, 0.3)
                recorder.record(position, level, kind)
                recorder.record(0.3, 0.3)
                recorder.record(0.4, 0.0)
                self.assertIsNone(recorder.snapshot().value_at(0.2))

    def test_backward_seek_replaces_only_new_observations(self):
        recorder = PulseRecorder()
        for t in (0.0, 1.0, 2.0, 3.0):
            recorder.record(t, 0.8)
        for t in (1.0, 1.5, 2.0):
            recorder.record(t, 0.0)
        trace = recorder.snapshot()
        self.assertEqual(trace.coverage, ((0.0, 3.0),))
        for t in (0.0, 0.5, math.nextafter(1.0, -math.inf),
                  math.nextafter(2.0, math.inf), 2.5, 3.0):
            self.assertAlmostEqual(trace.value_at(t), 0.8)
        for t in (1.0, 1.25, 1.5, 2.0):
            self.assertEqual(trace.value_at(t), 0.0)

    def test_snapshot_does_not_change_with_later_frames(self):
        recorder = PulseRecorder()
        recorder.record(0.0, 0.0)
        recorder.record(0.1, 1.0)
        snapshot = recorder.snapshot()
        recorder.record(0.2, 0.0)
        self.assertIsNone(snapshot.value_at(0.2))
        self.assertEqual(recorder.snapshot().value_at(0.2), 0.0)

    def test_merge_does_not_fill_holes_in_new_trace(self):
        old = _map([(0.0, 0.8), (6.0, 0.8)])
        new = _map([(1.0, 0.0), (2.0, 0.0), (4.0, 0.2), (5.0, 0.2)],
                   ((1.0, 2.0), (4.0, 5.0)))
        merged = merge_maps(old, new)
        self.assertEqual(merged.coverage, ((0.0, 6.0),))
        for t, value in ((0.5, 0.8), (1.5, 0.0), (3.0, 0.8),
                         (4.5, 0.2), (5.5, 0.8)):
            self.assertAlmostEqual(merged.value_at(t), value)


class PulsePayloadTest(unittest.TestCase):
    def test_round_trip_preserves_real_frame_bounds(self):
        start = CHUNK / SAMPLE_RATE - 0.015
        trace = _map([(start, 0.1), (start + CHUNK / SAMPLE_RATE, 0.9),
                      (start + 2 * CHUNK / SAMPLE_RATE, 0.0)])
        restored = PulseMap.from_payload(json.loads(json.dumps(trace.payload())))
        self.assertIsNotNone(restored)
        self.assertEqual(restored.coverage, trace.coverage)
        for event in trace.events:
            self.assertAlmostEqual(restored.value_at(event.media_time), event.strength, places=5)

    def test_round_trip_preserves_overlap_boundaries(self):
        old = _map([(0.0, 0.8), (3.0, 0.8)])
        new = _map([(1.0, 0.0), (2.0, 0.0)])
        trace = merge_maps(old, new)
        restored = PulseMap.from_payload(json.loads(json.dumps(trace.payload())))
        self.assertIsNotNone(restored)
        for t in (0.0, math.nextafter(1.0, -math.inf), 1.0, 1.5, 2.0,
                  math.nextafter(2.0, math.inf), 3.0):
            self.assertAlmostEqual(restored.value_at(t), trace.value_at(t), places=5)

    def test_corrupt_or_unknown_payload_is_a_cache_miss(self):
        valid = _map([(0.0, 0.0), (1.0, 1.0)]).payload()
        payloads = [None, [], {}, {**valid, "version": 99},
                    {**valid, "trace": "not-base64"},
                    {**valid, "trace": base64.b64encode(b"not-zlib").decode()},
                    {**valid, "coverage": [[0.0, math.inf]]},
                    {**valid, "coverage": [[0.1, 1.0]]},
                    {**valid, "coverage": [[0.0, 0.5], [0.4, 1.0]]}]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertIsNone(PulseMap.from_payload(payload))

    def test_malformed_trace_rows_are_rejected(self):
        invalid_rows = [[], [[0.0, 0.0, "kick"]],
                        [[0.0, 0.0, "kick"], [0.0, 1.0, "kick"]],
                        [[1.0, 0.0, "kick"], [0.0, 1.0, "kick"]],
                        [[-1.0, 0.0, "kick"], [1.0, 1.0, "kick"]],
                        [[0.0, math.nan, "kick"], [1.0, 1.0, "kick"]],
                        [[0.0, 0.0, "kick"], [math.inf, 1.0, "kick"]],
                        [[0.0, 0.0, "kick"], [1.0, 1.1, "kick"]],
                        [[0.0, 0.0, "kick"], [1.0, 1.0, "unknown"]],
                        [[0.0, 0.0, "kick"], [1.0, 1.0, []]],
                        [[0.0, 0.0], [1.0, 1.0, "kick"]]]
        for rows in invalid_rows:
            with self.subTest(rows=rows):
                self.assertIsNone(PulseMap.from_payload(_payload(rows, [[0.0, 1.0]])))

    def test_compressed_payload_cannot_bypass_decoded_size_limit(self):
        payload = _payload([[i / 1000, 0.0, "kick"] for i in range(500)], [[0.0, 0.499]])
        with mock.patch("tide.pulse_map.MAX_PAYLOAD_BYTES", 2048):
            self.assertIsNone(PulseMap.from_payload(payload))


class PulseSchedulerTest(unittest.TestCase):
    def test_sustained_audio_disagreement_falls_back_at_every_speed(self):
        trace = _map([(0.0, 0.8), (10.0, 0.8)])
        for speed in (0.5, 1.0, 2.0):
            with self.subTest(speed=speed):
                scheduler = PulseScheduler(trace)
                for wall_time in np.arange(0.0, 0.30, 0.04):
                    scheduler.observe(float(wall_time * speed), 0.0, speed)
                self.assertFalse(scheduler.disagreed)
                scheduler.observe(0.32 * speed, 0.0, speed)
                self.assertTrue(scheduler.disagreed)
                self.assertIsNone(scheduler.value_at(0.5))
                scheduler.observe(0.36 * speed, 0.8, speed)
                self.assertIsNone(scheduler.value_at(0.5))

    def test_single_mismatch_and_recovery_keep_schedule(self):
        scheduler = PulseScheduler(_map([(0.0, 0.8), (5.0, 0.8)]))
        for t in (0.0, 0.08, 0.16, 0.24):
            scheduler.observe(t, 0.0)
        for t in (0.28, 0.36, 0.44, 0.52, 0.60, 0.68, 0.76, 0.84):
            scheduler.observe(t, 0.8)
        for t in (0.88, 0.96, 1.04):
            scheduler.observe(t, 0.0)
        self.assertFalse(scheduler.disagreed)
        self.assertEqual(scheduler.value_at(0.6), 0.8)

    def test_capture_gap_cannot_accumulate_mismatch(self):
        for reset in ("explicit", "forward", "backward"):
            with self.subTest(reset=reset):
                scheduler = PulseScheduler(_map([(0.0, 0.8), (5.0, 0.8)]))
                for t in (0.2, 0.28, 0.36, 0.44):
                    scheduler.observe(t, 0.0)
                if reset == "explicit":
                    scheduler.gap()
                    start = 0.5
                else:
                    start = 2.0 if reset == "forward" else 0.0
                for offset in (0.0, 0.08, 0.16, 0.24):
                    scheduler.observe(start + offset, 0.0)
                self.assertFalse(scheduler.disagreed)
                scheduler.observe(start + 0.32, 0.0)
                self.assertTrue(scheduler.disagreed)

    def test_wrong_beat_grid_cannot_hide_at_crossings(self):
        times = np.arange(0.0, 5.01, 0.02)
        trace = _map([(float(t), float(np.exp(-(t % 0.5) / 0.22))) for t in times])
        scheduler = PulseScheduler(trace)
        for t in times[times < 2.0]:
            scheduler.observe(float(t), float(np.exp(-((t + 0.25) % 0.5) / 0.22)))
        self.assertTrue(scheduler.disagreed)
        self.assertIsNone(scheduler.value_at(2.0))

    def test_unmapped_interval_resets_mismatch_history(self):
        trace = _map([(0.0, 0.8), (0.24, 0.8), (0.4, 0.8), (1.0, 0.8)],
                     ((0.0, 0.24), (0.4, 1.0)))
        scheduler = PulseScheduler(trace)
        for t in (0.0, 0.08, 0.16, 0.24, 0.32, 0.4, 0.48, 0.56, 0.64):
            scheduler.observe(t, 0.0)
        self.assertFalse(scheduler.disagreed)
        scheduler.observe(0.72, 0.0)
        self.assertTrue(scheduler.disagreed)


class LearnedPulseTimingTest(unittest.TestCase):
    def test_delayed_kicks_replay_on_media_beats_at_all_speeds(self):
        capture_delay = 0.100
        detector_delay = 0.015
        media_hits = np.arange(0.5, 7.6, 1.0)
        for record_speed in (0.5, 1.0, 2.0):
            signal = np.zeros(int(8.0 / record_speed * SAMPLE_RATE))
            burst_t = np.arange(int(0.1 * SAMPLE_RATE)) / SAMPLE_RATE
            kick = 0.7 * np.sin(2 * np.pi * 60 * burst_t) * np.exp(-burst_t / 0.03)
            for hit in media_hits:
                start = int(hit / record_speed * SAMPLE_RATE)
                signal[start:start + len(kick)] += kick
            recorder = PulseRecorder()
            delayed = PulseRecorder()
            state = None
            for index in range(len(signal) // CHUNK):
                samples = np.ascontiguousarray(signal[index * CHUNK:(index + 1) * CHUNK],
                                               dtype=np.float32)
                state = _compute_pulse(samples, state)
                frame = PulseFrame((index + 1) * CHUNK / SAMPLE_RATE + capture_delay,
                                   float(state.env), "kick", capture_delay + detector_delay)
                recorder.record((frame.captured_at - frame.latency) * record_speed,
                                frame.level, frame.kind)
                delayed.record(frame.captured_at * record_speed, frame.level, frame.kind)
            scheduled = PulseScheduler(PulseMap.from_payload(recorder.snapshot().payload()))
            reactive = PulseScheduler(delayed.snapshot())
            for replay_speed in (0.5, 1.0, 2.0):
                for hit in media_hits:
                    with self.subTest(record=record_speed, replay=replay_speed, hit=hit):
                        wall = np.arange((hit - 0.08 * record_speed) / replay_speed,
                                         (hit + 0.16 * record_speed) / replay_speed, 0.001)
                        hits = [t for t in wall if (scheduled.value_at(float(t * replay_speed)) or 0) >= 0.5]
                        late = [t for t in wall if (reactive.value_at(float(t * replay_speed)) or 0) >= 0.5]
                        self.assertTrue(hits)
                        self.assertTrue(late)
                        tolerance = CHUNK / SAMPLE_RATE * record_speed / replay_speed + 0.001
                        self.assertLess(abs(hits[0] - hit / replay_speed), tolerance)
                        self.assertGreater(late[0] - hits[0], 0.08 * record_speed / replay_speed)


class PulseBlendTest(unittest.TestCase):
    def test_source_changes_start_at_the_visible_level(self):
        blend = PulseBlend()
        self.assertEqual(blend.mix(0.25, False, 1.0), 0.25)
        self.assertEqual(blend.mix(1.0, True, 2.0), 0.25)
        self.assertAlmostEqual(blend.mix(1.0, True, 2.0 + CROSSFADE_SECONDS / 2), 0.625)
        self.assertEqual(blend.mix(1.0, True, 2.0 + CROSSFADE_SECONDS + 0.001), 1.0)
        self.assertEqual(blend.mix(0.0, False, 3.0), 1.0)
        self.assertAlmostEqual(blend.mix(0.0, False, 3.0 + CROSSFADE_SECONDS / 2), 0.5)
        self.assertEqual(blend.mix(0.0, False, 3.0 + CROSSFADE_SECONDS + 0.001), 0.0)

    def test_second_switch_during_fade_preserves_continuity(self):
        blend = PulseBlend()
        blend.mix(0.3, False, 1.0)
        blend.mix(0.9, True, 2.0)
        before = blend.mix(0.9, True, 2.03)
        self.assertAlmostEqual(blend.mix(0.1, False, 2.03), before)
        self.assertAlmostEqual(blend.mix(0.1, False, 2.2), 0.1)


class _Loaded(QObject):
    def __init__(self):
        super().__init__()
        self.results = []
        self.threads = []

    @Slot(int, str, object)
    def receive(self, generation, key, pulse_map):
        self.threads.append(QThread.currentThread())
        self.results.append((generation, key, pulse_map))


class PulseMapStoreTest(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication(sys.argv[:1])
        cache.clear_namespace(NAMESPACE)
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.app.processEvents()
        cache.clear_namespace(NAMESPACE)

    def _store(self):
        store = PulseMapStore()
        receiver = _Loaded()
        store.loaded.connect(receiver.receive)
        self.stores.append(store)
        return store, receiver

    def _wait_for(self, receiver, count):
        for _ in range(200):
            if len(receiver.results) >= count:
                break
            QTest.qWait(10)
        self.assertEqual(len(receiver.results), count)
        self.assertTrue(all(thread is self.app.thread() for thread in receiver.threads))

    def test_save_then_load_is_fifo_and_signal_arrives_on_gui_thread(self):
        store, receiver = self._store()
        first = _map([(0.0, 0.1), (1.0, 0.5)])
        later = _map([(3.0, 0.8), (4.0, 0.0)])
        store.save("track", first)
        store.load(7, "track")
        store.save("track", later)
        store.load(8, "track")
        self._wait_for(receiver, 2)
        stale, current = receiver.results
        self.assertEqual(stale[:2], (7, "track"))
        self.assertEqual(current[:2], (8, "track"))
        self.assertIsNone(stale[2].value_at(3.5))
        self.assertAlmostEqual(current[2].value_at(3.5), 0.4)
        self.assertAlmostEqual(current[2].value_at(0.5), 0.3)
        self.assertIsNone(current[2].value_at(2.0))

    def test_recording_merges_off_gui_and_keeps_its_sealed_frames(self):
        recorder = PulseRecorder()
        for t in (0.0, 1.0, 2.0, 3.0):
            recorder.record(t, 0.8)
        recorder.record(1.0, 0.0)
        recorder.record(2.0, 0.0)
        sealed = recorder.segments()
        entered = threading.Event()
        proceed = threading.Event()
        converted_on = []
        gui_thread = threading.get_ident()

        def convert(segments):
            converted_on.append(threading.get_ident())
            entered.set()
            proceed.wait(2.0)
            return map_from_segments(segments)

        store, receiver = self._store()
        with mock.patch("tide.pulse_map.map_from_segments", side_effect=convert):
            store.save_recording("sealed", sealed)
            try:
                self.assertTrue(entered.wait(1.0))
                recorder.record(2.5, 0.2)
                recorder.gap()
                recorder.record(8.0, 0.7)
                recorder.record(9.0, 0.7)
            finally:
                proceed.set()
            store.load(1, "sealed")
            self._wait_for(receiver, 1)
        self.assertEqual(len(converted_on), 1)
        self.assertNotEqual(converted_on[0], gui_thread)
        saved = receiver.results[0][2]
        self.assertEqual(saved.coverage, ((0.0, 3.0),))
        self.assertEqual(saved.value_at(1.5), 0.0)
        self.assertAlmostEqual(saved.value_at(2.5), 0.8)
        self.assertIsNone(saved.value_at(8.5))

    def test_disk_round_trip_after_memory_and_worker_are_gone(self):
        recorder = PulseRecorder()
        for i in range(1, 8):
            recorder.record(i * CHUNK / SAMPLE_RATE, (i % 3) / 2, "beat")
        original = recorder.snapshot()
        store, _receiver = self._store()
        store.save("disk-track", original)
        store.close()
        self.assertTrue(cache._data_file(NAMESPACE).is_file())
        with cache._DATA_LOCK:
            cache._data_mem.pop(NAMESPACE, None)
        fresh, receiver = self._store()
        fresh.load(11, "disk-track")
        self._wait_for(receiver, 1)
        restored = receiver.results[0][2]
        self.assertIsNotNone(restored)
        self.assertEqual(restored.coverage, original.coverage)
        for event in original.events:
            self.assertAlmostEqual(restored.value_at(event.media_time), event.strength, places=5)

    def test_missing_corrupt_and_failed_reads_return_live_fallback(self):
        store, receiver = self._store()
        cache.put_json(NAMESPACE, "corrupt", {"version": 100}, 100)
        store.load(1, "missing")
        store.load(2, "corrupt")
        self._wait_for(receiver, 2)
        self.assertEqual(receiver.results, [(1, "missing", None), (2, "corrupt", None)])
        with mock.patch("tide.pulse_map.cache.get_json", side_effect=OSError("cache unavailable")):
            store.load(3, "unreadable")
            self._wait_for(receiver, 3)
        self.assertEqual(receiver.results[-1], (3, "unreadable", None))
        store.save("working", _map([(0.0, 0.0), (1.0, 1.0)]))
        store.load(4, "working")
        self._wait_for(receiver, 4)
        self.assertAlmostEqual(receiver.results[-1][2].value_at(0.5), 0.5)

    def test_closed_store_rejects_new_jobs(self):
        store, receiver = self._store()
        store.close()
        store.load(1, "ignored")
        store.save("ignored", _map([(0.0, 0.0), (1.0, 1.0)]))
        self.app.processEvents()
        self.assertEqual(receiver.results, [])
        self.assertIsNone(cache.get_json(NAMESPACE, "ignored"))


if __name__ == "__main__":
    unittest.main()
