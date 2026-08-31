"""Pulse recording keeps audio-thread timing and gap boundaries."""
import json
import os
import threading
import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest import mock

import numpy as np
from PySide6.QtCore import QObject, Slot
from PySide6.QtWidgets import QApplication

from tide import audio_capture
from tide.audio_capture import AudioVisualizerFeed, CHUNK, PulseFrame


class _CapturePipe:
    def __init__(self, feed, reads):
        self._feed = feed
        self._reads = iter(reads)

    def read(self, _size):
        try:
            return next(self._reads)
        except StopIteration:
            self._feed._stop.set()
            return b""


class _Receiver(QObject):
    def __init__(self):
        super().__init__()
        self.frames = []
        self.threads = []

    @Slot(object)
    def receive(self, frame):
        self.frames.append(frame)
        self.threads.append(threading.get_ident())


class PulseCaptureTest(unittest.TestCase):
    def setUp(self):
        patches = (
            mock.patch.dict(os.environ, {"TIDE_PULSE_LATENCY_MS": ""}),
            mock.patch.object(audio_capture.subprocess, "Popen",
                              side_effect=AssertionError("no audio in tests")),
            mock.patch.object(audio_capture.subprocess, "run",
                              side_effect=AssertionError("no server in tests")),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _feed(self, reads, *, scoped=True):
        feed = AudioVisualizerFeed()
        feed._stream_idx = 7 if scoped else None
        feed._explicit_source = None if scoped else "desktop.monitor"
        feed._proc = SimpleNamespace(
            stdout=_CapturePipe(feed, reads), poll=lambda: 0)
        frames = []
        levels = []
        feed.pulse_frame.connect(frames.append)
        feed.pulse_updated.connect(levels.append)
        return feed, frames, levels

    def test_metadata_stamps_processing_end_and_preserves_reactive_level(self):
        samples = np.zeros(CHUNK, dtype=np.float32).tobytes()
        feed, frames, levels = self._feed([samples])
        with mock.patch.object(audio_capture.time, "monotonic",
                               side_effect=[10.0, 10.006]):
            feed._process_loop()
        self.assertEqual(len(frames), 1)
        frame = frames[0]
        self.assertEqual(frame.captured_at, 10.006)
        self.assertAlmostEqual(frame.latency, 0.031)
        self.assertEqual([frame.level], levels)
        self.assertFalse(frame.reset)
        self.assertTrue(frame.scoped)
        self.assertEqual(frame.kind, "sustain")
        with self.assertRaises(FrozenInstanceError):
            frame.level = 1.0

    def test_short_reads_produce_one_frame_per_complete_chunk(self):
        samples = np.zeros(CHUNK, dtype=np.float32).tobytes()
        feed, frames, levels = self._feed([samples[:900], samples[900:]])
        feed._process_loop()
        self.assertEqual(len(frames), 1)
        self.assertEqual(levels, [0.0])

    def test_non_finite_chunk_marks_gap_without_reactive_flash(self):
        zero = np.zeros(CHUNK, dtype=np.float32).tobytes()
        bad = np.full(CHUNK, np.nan, dtype=np.float32).tobytes()
        feed, frames, levels = self._feed([zero, bad, zero])
        feed._process_loop()
        self.assertEqual([frame.reset for frame in frames], [False, True, False])
        self.assertEqual(levels, [0.0, 0.0])
        self.assertEqual(frames[1].level, 0.0)

    def test_analysis_exception_marks_gap(self):
        zero = np.zeros(CHUNK, dtype=np.float32).tobytes()
        feed, frames, levels = self._feed([zero])
        with mock.patch.object(audio_capture, "_compute_pulse",
                               side_effect=ValueError("bad frame")):
            feed._process_loop()
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].reset)
        self.assertEqual(levels, [])

    def test_reset_is_emitted_without_debug_csv(self):
        feed, frames, _levels = self._feed([])
        feed._trace_reset()
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].reset)
        self.assertTrue(frames[0].scoped)

    def test_desktop_monitor_frames_are_not_track_scoped(self):
        zero = np.zeros(CHUNK, dtype=np.float32).tobytes()
        feed, frames, _levels = self._feed([zero], scoped=False)
        feed._process_loop()
        feed._trace_reset()
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(not frame.scoped for frame in frames))

    def test_calibrated_override_is_total_not_an_extra_offset(self):
        with mock.patch.dict(os.environ, {"TIDE_PULSE_LATENCY_MS": "83.5"}):
            feed, frames, _levels = self._feed([])
        state = SimpleNamespace(env=0.7, diag={})
        with mock.patch.object(audio_capture.time, "monotonic", return_value=20.0):
            feed._emit_pulse_frame(state, started_at=19.8)
        self.assertAlmostEqual(frames[0].latency, 0.0835)

    def test_invalid_calibration_uses_estimate(self):
        for value in ("", "bad", "nan", "inf", "-1", "1e999"):
            with self.subTest(value=value), mock.patch.dict(
                    os.environ, {"TIDE_PULSE_LATENCY_MS": value}):
                self.assertIsNone(audio_capture._configured_pulse_latency())
        with mock.patch.dict(os.environ, {"TIDE_PULSE_LATENCY_MS": "0"}):
            self.assertEqual(audio_capture._configured_pulse_latency(), 0.0)

    def test_kind_follows_winning_detector_path(self):
        bass = {"gate_quiet": 1.0, "gate_fraction": 1.0,
                "onset": 0.8, "contrast": 0.5, "level": 0.8}
        percussion = {"gate_quiet": 0.0, "gate_fraction": 0.0,
                      "onset": 0.0, "contrast": 0.0, "level": 0.5}
        sustain = {"gate_quiet": 1.0, "gate_fraction": 1.0,
                   "onset": 0.0, "contrast": 0.5, "level": 0.11}
        self.assertEqual(audio_capture._pulse_kind(bass), "kick")
        self.assertEqual(audio_capture._pulse_kind(percussion), "beat")
        self.assertEqual(audio_capture._pulse_kind(sustain), "sustain")

    def test_worker_frame_reaches_bound_qt_receiver_on_gui_thread(self):
        app = QApplication.instance() or QApplication([])
        feed = AudioVisualizerFeed()
        receiver = _Receiver()
        feed.pulse_frame.connect(receiver.receive)
        frame = PulseFrame(10.0, 0.8, "kick", 0.025)
        thread = threading.Thread(target=feed.pulse_frame.emit, args=(frame,))
        thread.start()
        thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(receiver.frames, [])
        app.processEvents()
        self.assertEqual(receiver.frames, [frame])
        self.assertEqual(receiver.threads, [threading.get_ident()])


def _server_info():
    # pulseaudio/src/utils/pactl.c JSON emitters: a source's monitor_source
    # stores its owning sink name; corked is emitted as a JSON boolean.
    return {
        "source_outputs": [{"index": 11, "source": 5, "corked": False,
                            "buffer_latency_usec": 8_000.0,
                            "source_latency_usec": 3_000.0,
                            "properties": {"application.process.id": "2222",
                                           "application.name": "tide-visualizer"}}],
        "sink_inputs": [{"index": 9, "sink": 4, "corked": False,
                         "sink_latency_usec": 24_000.0}],
        "sinks": [{"index": 4, "name": "speaker", "monitor_source": "speaker.monitor"}],
        "sources": [{"index": 5, "name": "speaker.monitor", "monitor_source": "speaker"}],
    }


class CaptureLatencyTest(unittest.TestCase):
    def test_native_monitor_timing_keeps_signed_sink_compensation(self):
        self.assertAlmostEqual(
            audio_capture._capture_latency_from_info(_server_info(), 2222, 9), -0.013)

    def test_exact_process_and_route_are_required(self):
        self.assertIsNone(audio_capture._capture_latency_from_info(_server_info(), 3333, 9))
        self.assertIsNone(audio_capture._capture_latency_from_info(_server_info(), 2222, 8))
        for collection, field, value in (
                ("sources", "monitor_source", "another-sink"),
                ("sinks", "monitor_source", "another.monitor"),
                ("source_outputs", "source", 99),
                ("sink_inputs", "sink", 99),
                ("source_outputs", "corked", True)):
            info = _server_info()
            info[collection][0][field] = value
            with self.subTest(collection=collection, field=field):
                self.assertIsNone(audio_capture._capture_latency_from_info(info, 2222, 9))
        info = _server_info()
        info["source_outputs"].append(dict(info["source_outputs"][0]))
        self.assertIsNone(audio_capture._capture_latency_from_info(info, 2222, 9))

    def test_malformed_native_timing_is_ignored(self):
        for value in (None, "8000", -1, float("nan"), float("inf"), True, 10 ** 1000):
            info = _server_info()
            info["source_outputs"][0]["buffer_latency_usec"] = value
            with self.subTest(value=value):
                self.assertIsNone(audio_capture._capture_latency_from_info(info, 2222, 9))
        for value in (None, [], {}, {"source_outputs": "wrong"}):
            self.assertIsNone(audio_capture._capture_latency_from_info(value, 2222, 9))

    def test_query_is_bounded_and_failures_keep_fallback(self):
        with mock.patch.object(audio_capture.shutil, "which", return_value="pactl"), \
             mock.patch.object(audio_capture.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(_server_info()))
            self.assertAlmostEqual(audio_capture._query_capture_latency(2222, 9), -0.013)
            self.assertEqual(run.call_args.kwargs["timeout"], 0.5)
            run.side_effect = audio_capture.subprocess.TimeoutExpired("pactl", 0.5)
            self.assertIsNone(audio_capture._query_capture_latency(2222, 9))
            run.side_effect = None
            run.return_value = SimpleNamespace(returncode=0, stdout="invalid json")
            self.assertIsNone(audio_capture._query_capture_latency(2222, 9))

    def test_stale_probe_cannot_set_next_process_latency(self):
        feed = AudioVisualizerFeed()
        old = SimpleNamespace(pid=2222)
        current = SimpleNamespace(pid=3333)
        feed._proc = current
        feed._stream_idx = 9
        with mock.patch.object(audio_capture, "_query_capture_latency", return_value=-0.013):
            feed._measure_pulse_latency(old, 9)
            self.assertIsNone(feed._pulse_capture_latency)
            feed._measure_pulse_latency(current, 8)
            self.assertIsNone(feed._pulse_capture_latency)
            feed._measure_pulse_latency(current, 9)
            self.assertEqual(feed._pulse_capture_latency, (current, -0.013))

    def test_measured_component_and_calibration_priority(self):
        feed = AudioVisualizerFeed()
        feed._pulse_latency_override = None
        feed._proc = SimpleNamespace(pid=2222)
        feed._pulse_capture_latency = (feed._proc, -0.013)
        frames = []
        feed.pulse_frame.connect(frames.append)
        state = SimpleNamespace(env=0.7, diag={})
        with mock.patch.object(audio_capture.time, "monotonic", return_value=10.0):
            feed._emit_pulse_frame(state, 9.997)
            self.assertAlmostEqual(frames[-1].latency, 0.005)
            feed._pulse_latency_override = 0.0835
            feed._emit_pulse_frame(state, 9.997)
            self.assertAlmostEqual(frames[-1].latency, 0.0835)

    def test_probe_runs_once_off_the_calling_thread(self):
        feed = AudioVisualizerFeed()
        feed._pulse_latency_override = None
        feed._proc = SimpleNamespace(pid=2222)
        feed._stream_idx = 9
        entered = threading.Event()
        release = threading.Event()
        threads = []
        workers = []
        real_thread = threading.Thread

        def query(_pid, _stream):
            threads.append(threading.get_ident())
            entered.set()
            release.wait(timeout=1.0)
            return -0.013

        def make_thread(*args, **kwargs):
            worker = real_thread(*args, **kwargs)
            workers.append(worker)
            return worker

        with mock.patch.object(audio_capture, "_query_capture_latency", side_effect=query), \
             mock.patch.object(audio_capture.threading, "Thread", side_effect=make_thread):
            try:
                feed._start_pulse_latency_probe(feed._proc)
                self.assertTrue(entered.wait(timeout=1.0))
                feed._start_pulse_latency_probe(feed._proc)
                self.assertEqual(len(workers), 1)
                self.assertTrue(workers[0].daemon)
                self.assertNotEqual(threads[0], threading.get_ident())
            finally:
                release.set()
                for worker in workers:
                    worker.join(timeout=1.0)
        self.assertEqual(feed._pulse_capture_latency, (feed._proc, -0.013))

    def test_inactive_feed_and_explicit_monitor_never_launch_probe(self):
        with mock.patch.object(audio_capture.threading, "Thread") as thread, \
             mock.patch.object(audio_capture.subprocess, "Popen") as popen, \
             mock.patch.object(audio_capture.subprocess, "run") as run:
            feed = AudioVisualizerFeed()
            thread.assert_not_called()
            popen.assert_not_called()
            run.assert_not_called()
            feed._pulse_latency_override = None
            feed._start_pulse_latency_probe(SimpleNamespace(pid=2222))
            thread.assert_not_called()
            feed._stream_idx = 9
            feed._pulse_latency_override = 0.05
            feed._start_pulse_latency_probe(SimpleNamespace(pid=2222))
            thread.assert_not_called()
