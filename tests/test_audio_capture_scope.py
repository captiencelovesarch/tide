"""Auto capture must hear TIDE, not the desktop.

The reported bug: the adaptive backdrop pulsed to *everything* the system
played — Discord pings, browser video, games — because the feed captured
the default sink's monitor, which is the whole desktop's mix. Auto mode
now resolves tide's own sink input (mpv announces itself as ``tide`` via
``audio_client_name``; child backends like librespot register their pid)
and records exactly that stream with ``parec --monitor-stream``. These
tests pin the matcher, the pid registry, the target-resolution order
(explicit user pick > own stream > default-monitor fallback), and the
parec argv for each mode.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import os
import unittest
from unittest import mock

from tide import audio_capture
from tide.audio_capture import AudioVisualizerFeed, _pick_own_sink_input


def _entry(index, corked=False, **props):
    return {"index": index, "corked": corked, "properties": props}


class SinkInputMatcherTest(unittest.TestCase):
    def test_claims_tide_by_name_without_pid(self):
        # mpv's native pipewire AO carries NO application.process.id (seen
        # live) — the name contract from player.py's audio_client_name is
        # what matches. Foreign apps never do.
        entries = [
            _entry(7, **{"application.name": "Firefox",
                         "application.process.id": "999"}),
            _entry(9, **{"application.name": "tide"}),
        ]
        self.assertEqual(_pick_own_sink_input(entries, {"123"}), 9)

    def test_claims_tide_by_application_id(self):
        entries = [_entry(2, **{"application.id": "tide"})]
        self.assertEqual(_pick_own_sink_input(entries, set()), 2)

    def test_claims_registered_child_pid(self):
        # librespot's stream carries the child's pid; it matches only once
        # the backend has registered it.
        entries = [
            _entry(4, **{"application.name": "librespot",
                         "application.process.id": "555"}),
        ]
        self.assertIsNone(_pick_own_sink_input(entries, {"123"}))
        self.assertEqual(_pick_own_sink_input(entries, {"123", "555"}), 4)

    def test_foreign_mpv_is_not_claimed(self):
        # Someone else's mpv keeps the default client name — leaving it
        # unclaimed is the whole point of setting our own.
        entries = [
            _entry(3, **{"application.name": "mpv",
                         "application.process.id": "42"}),
        ]
        self.assertIsNone(_pick_own_sink_input(entries, {"123"}))

    def test_prefers_uncorked_stream(self):
        # tide can own two streams at once (mpv idling corked while
        # librespot plays); the one actually producing audio wins.
        entries = [
            _entry(5, corked=True, **{"application.name": "tide"}),
            _entry(8, corked=False, **{"application.process.id": "555"}),
        ]
        self.assertEqual(_pick_own_sink_input(entries, {"555"}), 8)

    def test_playing_mpv_beats_corked_registered_child(self):
        # The other direction of the two-own-streams state: librespot's
        # registered-pid stream idles corked while the pidless in-process
        # mpv actually plays. Pid-proof must NOT outrank corked here —
        # both streams are ours, and picking the corked one latches the
        # bass pulse / visualizer onto silence while music plays.
        entries = [
            _entry(4, corked=True, **{"application.name": "librespot",
                                      "application.process.id": "555"}),
            _entry(9, corked=False, **{"application.name": "tide"}),
        ]
        self.assertEqual(_pick_own_sink_input(entries, {"555"}), 9)

    def test_both_own_streams_corked_prefers_pid_proof(self):
        # Nothing playing at all: fall back to the strongest ownership
        # proof (the registered pid) rather than the name-only match.
        entries = [
            _entry(4, corked=True, **{"application.name": "librespot",
                                      "application.process.id": "555"}),
            _entry(9, corked=True, **{"application.name": "tide"}),
        ]
        self.assertEqual(_pick_own_sink_input(entries, {"555"}), 4)

    def test_corked_only_stream_still_claimed(self):
        # Paused-but-open is still tide's stream — capture stays scoped
        # and simply reads silence until playback resumes.
        entries = [_entry(5, corked=True, **{"application.name": "tide"})]
        self.assertEqual(_pick_own_sink_input(entries, {"1"}), 5)

    def test_garbage_entries_are_ignored(self):
        entries = [
            "not-a-dict",
            {"index": "x", "properties": {"application.name": "tide"}},
            {"properties": None},
            {"index": 2, "properties": {"application.id": "tide"}},
        ]
        self.assertEqual(_pick_own_sink_input(entries, set()), 2)


class PidPreferenceTest(unittest.TestCase):
    """Two processes named "tide" must not latch onto each other's audio:
    a stream whose pid we actually own outranks any name-only match."""

    def test_own_corked_beats_foreign_uncorked_tide(self):
        # A second tide instance is playing (uncorked, its pid) while ours
        # idles corked — the name match must NOT steal its stream.
        entries = [
            _entry(3, corked=False, **{"application.name": "tide",
                                       "application.process.id": "9999"}),
            _entry(6, corked=True, **{"application.name": "tide",
                                      "application.process.id": "123"}),
        ]
        self.assertEqual(_pick_own_sink_input(entries, {"123"}), 6)

    def test_own_pid_beats_foreign_tide_when_both_playing(self):
        entries = [
            _entry(3, corked=False, **{"application.name": "tide",
                                       "application.process.id": "9999"}),
            _entry(6, corked=False, **{"application.process.id": "123"}),
        ]
        self.assertEqual(_pick_own_sink_input(entries, {"123"}), 6)

    def test_pidless_tide_beats_foreign_pid_tide(self):
        # No pid on the entry is our in-process mpv (its pipewire AO
        # announces none); a "tide" carrying somebody ELSE's pid is
        # almost certainly the other instance.
        entries = [
            _entry(4, corked=False, **{"application.name": "tide",
                                       "application.process.id": "9999"}),
            _entry(5, corked=True, **{"application.name": "tide"}),
        ]
        self.assertEqual(_pick_own_sink_input(entries, {"123"}), 5)

    def test_foreign_pid_tide_is_still_a_last_resort(self):
        # Preference, not exclusion: if the ONLY candidate is a
        # tide-named stream with an unowned pid, claiming it still beats
        # falling back to the whole desktop's monitor.
        entries = [
            _entry(7, corked=False, **{"application.name": "tide",
                                       "application.process.id": "9999"}),
        ]
        self.assertEqual(_pick_own_sink_input(entries, {"123"}), 7)


class StreamPidRegistryTest(unittest.TestCase):
    def test_register_unregister_roundtrip(self):
        audio_capture.register_stream_pid(424242)
        try:
            self.assertIn("424242", audio_capture._own_pids())
        finally:
            audio_capture.unregister_stream_pid(424242)
        self.assertNotIn("424242", audio_capture._own_pids())

    def test_own_pid_always_claimed(self):
        self.assertIn(str(os.getpid()), audio_capture._own_pids())

    def test_none_pid_is_a_noop(self):
        before = audio_capture._own_pids()
        audio_capture.register_stream_pid(None)
        audio_capture.unregister_stream_pid(None)
        self.assertEqual(audio_capture._own_pids(), before)


class ResolveTargetTest(unittest.TestCase):
    def test_explicit_source_wins_over_own_stream(self):
        # A user-picked monitor in settings is an explicit choice — honor
        # it even when tide's own stream is available.
        feed = AudioVisualizerFeed()
        feed._explicit_source = "chosen.monitor"
        with mock.patch.object(audio_capture, "_own_sink_input",
                               return_value=77):
            self.assertEqual(feed._resolve_target(), ("chosen.monitor", None))

    def test_auto_claims_own_stream(self):
        feed = AudioVisualizerFeed()
        with mock.patch.object(audio_capture, "_own_sink_input",
                               return_value=77):
            self.assertEqual(feed._resolve_target(), (None, 77))

    def test_auto_falls_back_to_default_monitor(self):
        feed = AudioVisualizerFeed()
        with mock.patch.object(audio_capture, "_own_sink_input",
                               return_value=None), \
             mock.patch.object(audio_capture, "_default_sink_monitor",
                               return_value="sink.monitor"):
            self.assertEqual(feed._resolve_target(), ("sink.monitor", None))


class SpawnArgvTest(unittest.TestCase):
    def _argv(self, monitor, stream_idx):
        feed = AudioVisualizerFeed()
        with mock.patch("tide.audio_capture.subprocess.Popen") as popen:
            feed._spawn_parec(monitor, stream_idx)
            return popen.call_args[0][0]

    def test_stream_mode_records_one_sink_input(self):
        argv = self._argv(None, 3049)
        i = argv.index("--monitor-stream")
        self.assertEqual(argv[i + 1], "3049")
        self.assertNotIn("-d", argv)

    def test_monitor_mode_records_the_device(self):
        argv = self._argv("some_sink.monitor", None)
        i = argv.index("-d")
        self.assertEqual(argv[i + 1], "some_sink.monitor")
        self.assertNotIn("--monitor-stream", argv)


if __name__ == "__main__":
    unittest.main()
