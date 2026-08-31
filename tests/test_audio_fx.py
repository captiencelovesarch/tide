"""Audio FX remaster tests.

Three layers:
  1. Pure chain-builder units — every fx toggles its filter in, chain
     order holds, master-off/wet-zero bypass, JSON round-trip + old
     payload compatibility.
  2. IR synthesis — files land in the (sandboxed) cache with the right
     format, tails decay monotonically-ish, RT60 lands in the ballpark
     each preset asked for.
  3. mpv end-to-end — the real /usr/bin/mpv parses and *runs* a
     combined chain (eq + reverb + loudnorm + mono) including the
     lavfi amovie+afir graph, and a rendered tone provably grows a
     reverb tail that the dry render doesn't have. These skip with a
     clear reason only if mpv is genuinely absent.

conftest.py sandboxes XDG dirs, so fx_ir writes under a temp cache.
"""
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from tide import fx_ir
from tide.audio_fx import (
    AudioFxState,
    EQ_BAND_COUNT,
    REVERB_PRESETS,
    build_filter_chain,
)

MPV = shutil.which("mpv")


def _tail_rms(path: str, t0: float, t1: float) -> float:
    """RMS of all channels of ``path`` between ``t0`` and ``t1`` secs."""
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        data = np.frombuffer(
            w.readframes(w.getnframes()), dtype="<i2"
        ).astype(np.float64) / 32768.0
    a = int(t0 * sr) * ch
    b = min(int(t1 * sr) * ch, len(data))
    seg = data[a:b]
    return float(np.sqrt((seg ** 2).mean())) if len(seg) else 0.0


def _full_state() -> AudioFxState:
    """Everything on at once — the stress configuration."""
    s = AudioFxState(master_enabled=True)
    s.eq_bands[0] = 3.0
    s.eq_bands[5] = -2.0
    s.bass_db = 3.0
    s.treble_db = 2.0
    s.stereo_width = 1.4
    s.compressor = True
    s.reverb_preset = "hall"
    s.reverb_wet = 0.7
    s.loudness_norm = True
    s.mono = True
    s.chorus = True
    s.flanger = True
    s.phaser = True
    s.tremolo = True
    s.tremolo_speed = 6.0
    s.exciter = True
    s.exciter_amount = 2.5
    s.crossfeed = True
    s.crossfeed_strength = 0.5
    s.lofi = True
    return s


class ChainBuilderTests(unittest.TestCase):

    def test_master_off_yields_empty_chain(self):
        s = _full_state()
        s.master_enabled = False
        self.assertEqual(build_filter_chain(s), "")

    def test_all_defaults_yield_empty_chain(self):
        self.assertEqual(build_filter_chain(AudioFxState(master_enabled=True)), "")

    def test_each_fx_toggles_its_filter(self):
        cases = [
            ("chorus", True, "chorus="),
            ("flanger", True, "flanger="),
            ("phaser", True, "aphaser="),
            ("tremolo", True, "tremolo="),
            ("exciter", True, "crystalizer="),
            ("crossfeed", True, "crossfeed="),
            ("lofi", True, "acrusher="),
            ("compressor", True, "acompressor="),
            ("loudness_norm", True, "loudnorm="),
            ("mono", True, "pan=mono"),
        ]
        for attr, value, marker in cases:
            with self.subTest(fx=attr):
                s = AudioFxState(master_enabled=True)
                setattr(s, attr, value)
                chain = build_filter_chain(s)
                self.assertIn(marker, chain)
                # And absent when off.
                setattr(s, attr, False)
                self.assertNotIn(marker, build_filter_chain(s))

    def test_lofi_is_the_full_composite(self):
        s = AudioFxState(master_enabled=True, lofi=True)
        chain = build_filter_chain(s)
        for part in ("highpass=", "lowpass=", "acrusher=", "vibrato="):
            self.assertIn(part, chain)

    def test_parameter_knobs_reach_the_chain(self):
        s = AudioFxState(master_enabled=True)
        s.tremolo = True
        s.tremolo_speed = 7.5
        s.exciter = True
        s.exciter_amount = 3.5
        s.crossfeed = True
        s.crossfeed_strength = 0.6
        chain = build_filter_chain(s)
        self.assertIn("tremolo=f=7.5", chain)
        self.assertIn("crystalizer=i=3.5", chain)
        self.assertIn("crossfeed=strength=0.6", chain)

    def test_chain_order(self):
        chain = build_filter_chain(_full_state())
        markers = [
            "equalizer=",     # eq bands
            "crystalizer=",   # exciter
            "bass=",          # bass shelf
            "treble=",        # treble shelf
            "acrusher=",      # lofi composite
            "extrastereo=",   # stereo width
            "acompressor=",   # compressor
            "chorus=",        # modulation…
            "flanger=",
            "aphaser=",
            "tremolo=",
            "afir=",          # reverb
            "crossfeed=",
            "loudnorm=",
            "pan=mono",       # mono fold last
        ]
        positions = [chain.index(m) for m in markers]
        self.assertEqual(positions, sorted(positions),
                         f"chain order broke: {chain}")

    def test_mono_uses_lavfi_wrapper(self):
        # mpv 0.41's native af parser rejects pan's 'mono|c0=…' syntax
        # ("AVOption 'mono|c0' not found") — the fold must go through
        # a lavfi graph entry.
        s = AudioFxState(master_enabled=True, mono=True)
        self.assertEqual(build_filter_chain(s), "lavfi=[pan=mono|c0=0.5*c0+0.5*c1]")

    def test_reverb_wet_zero_bypasses(self):
        s = AudioFxState(master_enabled=True)
        s.reverb_preset = "hall"
        s.reverb_wet = 0.0
        chain = build_filter_chain(s)
        self.assertNotIn("afir", chain)
        self.assertNotIn("lavfi", chain)

    def test_reverb_off_preset_bypasses(self):
        s = AudioFxState(master_enabled=True)
        s.reverb_preset = "off"
        s.reverb_wet = 1.0
        self.assertNotIn("afir", build_filter_chain(s))

    def test_reverb_entry_references_generated_ir(self):
        s = AudioFxState(master_enabled=True)
        s.reverb_preset = "hall"
        s.reverb_wet = 0.5
        chain = build_filter_chain(s)
        self.assertIn("afir=", chain)
        self.assertIn("amovie=filename=", chain)
        self.assertIn("amix=", chain)
        path = fx_ir.ir_path("hall")
        self.assertTrue(path.is_file(), "IR file was not generated")
        self.assertIn(str(path), chain)
        # No aecho anywhere — the pseudo-reverb is gone.
        self.assertNotIn("aecho", chain)

    def test_reverb_wet_scales_mix_weight(self):
        s = AudioFxState(master_enabled=True)
        s.reverb_preset = "hall"
        s.reverb_wet = 1.0
        full = build_filter_chain(s)
        s.reverb_wet = 0.5
        half = build_filter_chain(s)

        def weight(chain: str) -> float:
            return float(chain.split("weights='1 ")[1].split("'")[0])

        self.assertAlmostEqual(weight(half) * 2.0, weight(full), places=2)

    def test_every_preset_has_ir_spec(self):
        for name in REVERB_PRESETS:
            if name == "off":
                continue
            self.assertIn(name, fx_ir.IR_SPECS)

    def test_json_round_trip(self):
        s = _full_state()
        s2 = AudioFxState.from_json(s.to_json())
        self.assertEqual(s.to_dict(), s2.to_dict())

    def test_old_payload_still_loads(self):
        # A settings blob written by a pre-remaster build: aecho-era
        # reverb preset names, none of the new fx keys.
        old = (
            '{"master_enabled":true,"eq_bands":[1,0,0,0,0,0,0,0,0,0],'
            '"bass_db":2.0,"treble_db":-1.0,"reverb_preset":"hall",'
            '"reverb_wet":0.8,"loudness_norm":true,"stereo_width":1.2,'
            '"compressor":true,"mono":false,"custom_slots":[]}'
        )
        s = AudioFxState.from_json(old)
        self.assertTrue(s.master_enabled)
        self.assertEqual(s.reverb_preset, "hall")
        self.assertAlmostEqual(s.reverb_wet, 0.8)
        self.assertEqual(len(s.eq_bands), EQ_BAND_COUNT)
        # New fx default off with sane knob defaults.
        for attr in ("chorus", "flanger", "phaser", "tremolo", "exciter",
                     "crossfeed", "lofi"):
            self.assertFalse(getattr(s, attr))
        self.assertAlmostEqual(s.tremolo_speed, 5.0)
        self.assertAlmostEqual(s.exciter_amount, 2.0)
        self.assertAlmostEqual(s.crossfeed_strength, 0.4)
        # And the old preset maps onto the new convolution engine.
        self.assertIn("afir=", build_filter_chain(s))

    def test_bogus_values_are_clamped(self):
        s = AudioFxState.from_dict({
            "reverb_preset": "shower", "reverb_wet": 9.0,
            "tremolo_speed": 99, "exciter_amount": -5,
            "crossfeed_strength": "nonsense",
        })
        self.assertEqual(s.reverb_preset, "off")
        self.assertAlmostEqual(s.reverb_wet, 1.0)
        self.assertAlmostEqual(s.tremolo_speed, 10.0)
        self.assertAlmostEqual(s.exciter_amount, 0.0)
        self.assertAlmostEqual(s.crossfeed_strength, 0.4)

    def test_reverb_entry_uses_length_prefixed_quoting(self):
        # The af-list layer must be %len% quoting, not balanced [...] —
        # an unbalanced ']' in the cache path makes the bracket form
        # unparseable and mpv then drops the entire chain (no audio).
        s = AudioFxState(master_enabled=True)
        s.reverb_preset = "hall"
        s.reverb_wet = 0.5
        chain = build_filter_chain(s)
        self.assertTrue(chain.startswith("lavfi=%"), chain)
        declared = int(chain.split("%")[1])
        graph = chain.split("%", 2)[2]
        # The prefix counts bytes; a wrong count truncates the graph
        # mid-token inside mpv.
        self.assertEqual(declared, len(graph.encode("utf-8")))


class StateFuzzTests(unittest.TestCase):
    """``from_json`` runs at startup on a settings string anything may
    have scribbled over. Junk in, defaults out — never an exception,
    or a corrupt settings file crashes tide at every launch."""

    # The adversarial-review fuzz corpus: valid JSON of the wrong
    # shape, wrong-typed fields, infinities, NaN, nested garbage.
    PAYLOADS = [
        "[1,2,3]",
        '"hello"',
        "123",
        "true",
        '{"custom_slots": {"a": 1}}',
        '{"custom_slots": "abc"}',
        '{"custom_slots": [null, 5, "x"]}',
        '{"eq_bands": [null]*10}',
        '{"eq_bands": [null,null,null,null,null,null,null,null,null,null]}',
        '{"eq_bands": ["a","b","c","d","e","f","g","h","i","j"]}',
        '{"reverb_wet": "junk"}',
        '{"reverb_wet": null}',
        '{"stereo_width": "x"}',
        '{"bass_db": [1,2]}',
        '{"master_enabled": {"x":1}}',
        '{"reverb_preset": null}',
        '{"tremolo_speed": [1]}',
        '{"eq_bands": [1e400,-1e400,0,0,0,0,0,0,0,0]}',
        '{"reverb_wet": 1e400}',
        '{"eq_bands": [null,0,0,0,0,0,0,0,0,0],'
        ' "custom_slots":[{"name":5,"bands":[1,2]}]}',
        '{"reverb_wet": NaN, "eq_bands":[NaN,0,0,0,0,0,0,0,0,0],'
        ' "master_enabled": true, "reverb_preset": "hall"}',
    ]

    def test_junk_payloads_never_raise(self):
        for payload in self.PAYLOADS:
            with self.subTest(payload=payload):
                s = AudioFxState.from_json(payload)
                # Whatever came in, what comes out is a normal state
                # with every knob inside its range.
                self.assertIsInstance(s, AudioFxState)
                self.assertEqual(len(s.eq_bands), EQ_BAND_COUNT)
                self.assertEqual(len(s.custom_slots), 3)
                self.assertTrue(0.0 <= s.reverb_wet <= 1.0)
                self.assertTrue(all(-12.0 <= b <= 12.0 for b in s.eq_bands))
                self.assertIn(s.reverb_preset, REVERB_PRESETS)
                # And the chain builder accepts it without blowing up.
                build_filter_chain(s)


class IrGenerationTests(unittest.TestCase):

    def test_files_created_with_correct_format(self):
        for name in fx_ir.IR_SPECS:
            with self.subTest(preset=name):
                path = fx_ir.ensure_ir(name)
                self.assertIsNotNone(path)
                self.assertTrue(path.is_file())
                self.assertIn(f"_v{fx_ir.IR_VERSION}", path.name)
                with wave.open(str(path), "rb") as w:
                    self.assertEqual(w.getframerate(), fx_ir.IR_SAMPLE_RATE)
                    self.assertEqual(w.getnchannels(), 2)
                    self.assertEqual(w.getsampwidth(), 2)
                    self.assertGreater(w.getnframes(), 0)

    def test_unknown_preset_returns_none(self):
        self.assertIsNone(fx_ir.ensure_ir("shower"))
        self.assertIsNone(fx_ir.ensure_ir("off"))

    def test_generation_is_deterministic(self):
        spec = fx_ir.IR_SPECS["room"]
        a = fx_ir.generate_ir(spec)
        b = fx_ir.generate_ir(spec)
        self.assertTrue(np.array_equal(a, b))

    def test_tail_decays_monotonically_ish(self):
        sr = fx_ir.IR_SAMPLE_RATE
        for name, spec in fx_ir.IR_SPECS.items():
            with self.subTest(preset=name):
                ir = fx_ir.generate_ir(spec)
                mono = ir.mean(axis=1)
                # Skip pre-delay + early reflections, then RMS in
                # 100 ms blocks over the diffuse tail.
                start = int(sr * (spec.predelay_ms + spec.er_ms) / 1000.0)
                tail = mono[start:]
                block = sr // 10
                rms = [
                    float(np.sqrt((tail[i:i + block] ** 2).mean()))
                    for i in range(0, len(tail) - block, block)
                ]
                self.assertGreater(len(rms), 2)
                # Adjacent 100 ms blocks of narrowband noise wiggle a
                # bit, so compare across a 3-block stride where the
                # decay term dominates the wiggle even for the slow
                # dark presets.
                stride = min(3, len(rms) - 1)
                for i in range(len(rms) - stride):
                    self.assertLessEqual(rms[i + stride], rms[i] * 1.05)
                # And it must be a real decay, not a plateau.
                self.assertLess(rms[-1], rms[0] * 0.2)

    def test_rt60_ballpark(self):
        # Schroeder backward integration, -5…-25 dB slope extrapolated
        # to 60 dB. The broadband estimate sits below the nominal
        # (low-band) RT60 because the damped upper bands die faster —
        # measured ratios are 0.6–0.85, so accept 0.4–1.2.
        sr = fx_ir.IR_SAMPLE_RATE
        estimates = {}
        for name, spec in fx_ir.IR_SPECS.items():
            ir = fx_ir.generate_ir(spec)
            e = ir.mean(axis=1) ** 2
            sch = np.cumsum(e[::-1])[::-1]
            db = 10 * np.log10(np.maximum(sch / sch[0], 1e-12))
            t5 = int(np.argmax(db <= -5.0))
            t25 = int(np.argmax(db <= -25.0))
            estimates[name] = (t25 - t5) / sr * 3.0
            with self.subTest(preset=name):
                self.assertGreater(estimates[name], spec.rt60 * 0.4)
                self.assertLess(estimates[name], spec.rt60 * 1.2)
        # Relative sizes hold: room is the smallest space, slowed the
        # biggest.
        self.assertLess(estimates["room"], estimates["hall"])
        self.assertLess(estimates["hall"], estimates["cathedral"])
        self.assertLess(estimates["cathedral"], estimates["slowed"])

    def test_predelay_is_silent(self):
        spec = fx_ir.IR_SPECS["cathedral"]
        ir = fx_ir.generate_ir(spec)
        n_pre = int(fx_ir.IR_SAMPLE_RATE * spec.predelay_ms / 1000.0) - 1
        self.assertEqual(float(np.abs(ir[:n_pre]).max()), 0.0)

    def test_corrupt_cached_ir_regenerates(self):
        # A nonempty-but-garbage cached file (crashed write, bit rot, a
        # cache cleaner's leavings) used to be trusted forever and then
        # killed the whole af chain inside mpv. ensure_ir must spot it
        # and rebuild.
        path = fx_ir.ensure_ir("room")
        self.assertIsNotNone(path)
        path.write_bytes(b"RIFFgarbage-not-a-wav" * 10)
        again = fx_ir.ensure_ir("room")
        self.assertIsNotNone(again)
        with wave.open(str(again), "rb") as w:
            self.assertEqual(w.getframerate(), fx_ir.IR_SAMPLE_RATE)
            self.assertGreater(w.getnframes(), 0)

    def test_truncated_cached_ir_regenerates(self):
        path = fx_ir.ensure_ir("room")
        self.assertIsNotNone(path)
        # Chop the file mid-header — wave.open raises rather than
        # returning zeros for this one.
        path.write_bytes(path.read_bytes()[:16])
        again = fx_ir.ensure_ir("room")
        self.assertIsNotNone(again)
        with wave.open(str(again), "rb") as w:
            self.assertGreater(w.getnframes(), 0)

    def test_headerless_empty_body_ir_regenerates(self):
        # A structurally valid WAV with zero frames passes wave.open —
        # the frame-count sanity check has to catch it.
        path = fx_ir.ensure_ir("room")
        self.assertIsNotNone(path)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(fx_ir.IR_SAMPLE_RATE)
            w.writeframes(b"")
        again = fx_ir.ensure_ir("room")
        self.assertIsNotNone(again)
        with wave.open(str(again), "rb") as w:
            self.assertGreater(w.getnframes(), 0)


@unittest.skipUnless(MPV, "mpv binary not installed — cannot run end-to-end af parse/render tests")
class MpvEndToEndTests(unittest.TestCase):
    """Feed real chains to the real mpv and render real audio."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix="tide-fx-e2e-")
        # 0.25 s of 440 Hz then 2.25 s of silence — room for a tail.
        sr = 44100
        t = np.arange(int(sr * 2.5)) / sr
        tone = np.sin(2 * np.pi * 440 * t) * 0.5
        tone[int(sr * 0.25):] = 0.0
        pcm = (np.stack([tone, tone], axis=1) * 32767.0).astype("<i2")
        cls.tone_wav = str(Path(cls._dir) / "tone.wav")
        with wave.open(cls.tone_wav, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def _render(self, chain: str, out_name: str) -> subprocess.CompletedProcess:
        out = str(Path(self._dir) / out_name)
        proc = subprocess.run(
            [
                # --audio-format=s16 because ao=pcm otherwise writes
                # float WAVs (extensible format) that stdlib wave
                # refuses to open.
                MPV, "--no-config", "--no-video", "--ao=pcm",
                "--audio-format=s16",
                f"--ao-pcm-file={out}", f"--af={chain}", self.tone_wav,
            ],
            capture_output=True, text=True, timeout=120,
        )
        return proc, out

    def test_combined_chain_parses_and_runs(self):
        # The stress chain: eq bands + every fx + convolution reverb +
        # loudnorm + mono, all at once. mpv must parse the af list
        # (lavfi graphs with commas/quotes inside included), build the
        # graph, and render without a single filter failure.
        chain = build_filter_chain(_full_state())
        self.assertIn("afir=", chain)
        proc, out = self._render(chain, "combined.wav")
        log = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, f"mpv failed:\n{log}")
        for bad in ("failed", "error", "invalid"):
            self.assertNotIn(bad, log.lower(), f"mpv complained:\n{log}")
        self.assertGreater(Path(out).stat().st_size, 1000)

    def test_reverb_actually_reverberates(self):
        # Render the tone through eq+reverb and through eq alone, then
        # measure energy in the window well after the dry tone ended
        # (0.6–1.6 s vs. tone ending at 0.25 s). The old aecho taps are
        # gone; this asserts a genuine energy tail exists.
        wet_state = AudioFxState(master_enabled=True)
        wet_state.eq_bands[4] = 1.0
        wet_state.reverb_preset = "hall"
        wet_state.reverb_wet = 0.8
        dry_state = AudioFxState(master_enabled=True)
        dry_state.eq_bands[4] = 1.0

        proc_wet, out_wet = self._render(build_filter_chain(wet_state), "wet.wav")
        proc_dry, out_dry = self._render(build_filter_chain(dry_state), "dry.wav")
        self.assertEqual(proc_wet.returncode, 0, proc_wet.stderr)
        self.assertEqual(proc_dry.returncode, 0, proc_dry.stderr)

        tone_rms = _tail_rms(out_wet, 0.05, 0.20)
        wet_tail = _tail_rms(out_wet, 0.6, 1.6)
        dry_tail = _tail_rms(out_dry, 0.6, 1.6)
        self.assertGreater(tone_rms, 0.05, "render produced no signal at all")
        self.assertLess(dry_tail, 1e-4, "dry render should be silent after the tone")
        self.assertGreater(wet_tail, 1e-3, "no reverb tail — reverb is not reverberating")
        self.assertGreater(wet_tail, dry_tail * 10 + 1e-4)

    def test_reverb_renders_from_hostile_cache_path(self):
        # The exact characters that used to kill af init when a themed
        # $XDG_CACHE_HOME contained them: apostrophe, colon, unbalanced
        # ']', comma, semicolon. The reverb must still render a tail.
        hostile = Path(self._dir) / "o'brien x]y a:b c,d;e"
        saved = fx_ir.IR_DIR
        fx_ir.IR_DIR = hostile
        try:
            s = AudioFxState(master_enabled=True)
            s.reverb_preset = "hall"
            s.reverb_wet = 0.8
            chain = build_filter_chain(s)
            self.assertIn("afir=", chain)
            proc, out = self._render(chain, "hostile.wav")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertGreater(_tail_rms(out, 0.05, 0.20), 0.05,
                               "hostile path chain produced no signal")
            self.assertGreater(_tail_rms(out, 0.6, 1.6), 1e-3,
                               "no reverb tail from hostile cache path")
        finally:
            fx_ir.IR_DIR = saved

    def test_mono_fold_runs_in_mpv(self):
        # Regression: the old bare 'pan=mono|…' entry died at filter
        # creation on mpv 0.41. The lavfi-wrapped form must render.
        s = AudioFxState(master_enabled=True, mono=True)
        proc, out = self._render(build_filter_chain(s), "mono.wav")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreater(Path(out).stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
