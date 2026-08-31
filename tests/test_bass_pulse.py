"""The adaptive backdrop must pulse on kicks and only on kicks.

The reported bug: "sometimes the backdrop just lights up for nothing".
Three causes in the old detector, all pinned here:
  * the 30-200 Hz band was wide enough that voice fundamentals, snare
    bodies and low-mid mud counted as bass,
  * any sustained energy above the adaptive threshold pulsed — no
    transient required, so pads and swells lit the backdrop,
  * on steady material the floor/peak contrast could saturate: with the
    local span pinned at its minimum, small wiggles mapped to large
    envelope values, and the absolute quiet gate let moderate broadband
    loudness through even with no bass in it.

The rework adds an onset (spectral-flux) requirement and a bass-fraction
gate on top of a narrowed 35-130 Hz kick band. These tests drive
``_compute_pulse`` chunk-by-chunk exactly like ``_process_loop`` does,
with synthetic 44.1 kHz signals, and assert the envelope quantitatively:
spikes on kick hits, decay between them, adaptation on sustained bass,
and near-zero output on loud non-bass content.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import unittest

import numpy as np

from tide.audio_capture import CHUNK, SAMPLE_RATE, _compute_pulse


def _run_envelope(signal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Feed ``signal`` through _compute_pulse in CHUNK-sized frames, the way
    _process_loop does. Returns (times, envelope) — one point per chunk,
    timestamped at the chunk's end."""
    state = None
    envs = []
    n_chunks = len(signal) // CHUNK
    for i in range(n_chunks):
        frame = np.ascontiguousarray(
            signal[i * CHUNK:(i + 1) * CHUNK], dtype=np.float32
        )
        state = _compute_pulse(frame, state)
        envs.append(state.env)
    envs = np.asarray(envs)
    times = (np.arange(len(envs)) + 1) * (CHUNK / SAMPLE_RATE)
    return times, envs


def _kick_pattern(duration_s: float, amp: float = 0.7, period_s: float = 0.5,
                  burst_s: float = 0.10, freq_hz: float = 60.0,
                  decay_tau_s: float = 0.03, start_s: float = 0.0) -> np.ndarray:
    """60 Hz decaying sine bursts (~100 ms, exponential decay) every 500 ms —
    a bare-bones kick drum."""
    n = int(duration_s * SAMPLE_RATE)
    sig = np.zeros(n)
    start = start_s
    while start < duration_s:
        i0 = int(start * SAMPLE_RATE)
        i1 = min(n, i0 + int(burst_s * SAMPLE_RATE))
        t = np.arange(i1 - i0) / SAMPLE_RATE
        sig[i0:i1] += amp * np.sin(2 * np.pi * freq_hz * t) * np.exp(-t / decay_tau_s)
        start += period_s
    return sig


def _bandpass_noise(duration_s: float, lo_hz: float, hi_hz: float,
                    rms: float, rng: np.random.Generator) -> np.ndarray:
    """White noise brick-walled to [lo_hz, hi_hz] via FFT, scaled to ``rms``."""
    n = int(duration_s * SAMPLE_RATE)
    spec = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1.0 / SAMPLE_RATE)
    spec[(freqs < lo_hz) | (freqs > hi_hz)] = 0.0
    out = np.fft.irfft(spec, n)
    return out * (rms / max(1e-12, out.std()))


def _perc_hits(duration_s: float, rng: np.random.Generator, amp: float = 0.4,
               period_s: float = 0.5, burst_s: float = 0.03,
               start_s: float = 0.5, lo_hz: float = 2000.0,
               hi_hz: float = 6000.0) -> np.ndarray:
    """Snare/clap stand-in: 30 ms bandpassed-noise bursts with a sharp
    attack and a 10 ms decay — percussive rhythm with zero bass content."""
    n = int(duration_s * SAMPLE_RATE)
    sig = np.zeros(n)
    nb = int(burst_s * SAMPLE_RATE)
    env = np.exp(-np.arange(nb) / (0.010 * SAMPLE_RATE))
    start = start_s
    while start < duration_s:
        i0 = int(start * SAMPLE_RATE)
        i1 = min(n, i0 + nb)
        noise = _bandpass_noise(burst_s + 0.01, lo_hz, hi_hz, 1.0, rng)
        sig[i0:i1] += amp * noise[: i1 - i0] * env[: i1 - i0]
        start += period_s
    return sig


def _hit_peaks_and_troughs(times: np.ndarray, envs: np.ndarray,
                           period_s: float = 0.5, start_s: float = 0.0,
                           ) -> tuple[list[float], list[float]]:
    """Per hit: the envelope max during the burst window and the min in the
    stretch just before the next hit."""
    peaks: list[float] = []
    troughs: list[float] = []
    hit = start_s
    while hit + period_s <= times[-1]:
        in_burst = (times >= hit) & (times < hit + 0.15)
        pre_next = (times >= hit + 0.35) & (times < hit + period_s)
        if in_burst.any() and pre_next.any():
            peaks.append(float(envs[in_burst].max()))
            troughs.append(float(envs[pre_next].min()))
        hit += period_s
    return peaks, troughs


class BassPulseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rng = np.random.default_rng(42)

    def test_kick_pattern_spikes_and_releases(self):
        # (a) kicks over a -40 dB noise floor. Every hit must spike hard and
        # the envelope must be back down before the next one. Hits start at
        # 0.5 s so the detector has a moment of context first, as it always
        # does in real playback.
        duration = 4.5
        sig = _kick_pattern(duration, start_s=0.5)
        sig += self.rng.standard_normal(int(duration * SAMPLE_RATE)) * 0.01
        times, envs = _run_envelope(sig)
        peaks, troughs = _hit_peaks_and_troughs(times, envs, start_s=0.5)
        self.assertGreaterEqual(len(peaks), 7)
        for peak in peaks:
            self.assertGreater(peak, 0.5)
        for trough in troughs:
            self.assertLess(trough, 0.15)

    def test_sustained_bass_adapts_down(self):
        # (b) a 10 s unbroken 50 Hz sine. The initial edge may pulse, but the
        # adaptive floor must re-baseline within a couple of seconds — no
        # permanent pin.
        t = np.arange(int(10 * SAMPLE_RATE)) / SAMPLE_RATE
        times, envs = _run_envelope(0.6 * np.sin(2 * np.pi * 50 * t))
        self.assertLess(float(envs[times > 3.0].max()), 0.15)
        self.assertLess(float(envs[times > 5.0].max()), 0.05)
        # And sustained level with no onset never counts as a full hit.
        self.assertLess(float(envs.max()), 0.5)

    def test_loud_white_noise_stays_low(self):
        # (c) loud full-band noise has real energy at 35-130 Hz, but its bass
        # *fraction* is a sliver — the fraction gate must keep it dark.
        sig = self.rng.standard_normal(int(4 * SAMPLE_RATE)) * 0.25
        times, envs = _run_envelope(sig)
        self.assertLess(float(envs[times > 1.0].max()), 0.25)
        self.assertLess(float(envs[times > 1.0].mean()), 0.1)

    def test_mid_band_noise_stays_dark(self):
        # (d) loud 300-3000 Hz noise — a vocal/mid stand-in. No bass at all,
        # so no pulse at all.
        sig = _bandpass_noise(4.0, 300.0, 3000.0, 0.25, self.rng)
        _, envs = _run_envelope(sig)
        self.assertLess(float(envs.max()), 0.1)

    def test_silence_and_quiet_music_are_zero(self):
        # (e) silence is exactly zero; barely-audible music sits under the
        # absolute quiet gate and must not wiggle the backdrop either.
        _, envs = _run_envelope(np.zeros(int(2 * SAMPLE_RATE)))
        self.assertEqual(float(envs.max()), 0.0)
        quiet = _kick_pattern(4.0, amp=0.01, start_s=0.5)
        quiet += self.rng.standard_normal(int(4 * SAMPLE_RATE)) * 0.0003
        _, envs = _run_envelope(quiet)
        self.assertLess(float(envs.max()), 0.05)

    def test_kicks_survive_a_mid_band_bed(self):
        # (f) the realistic mix: kicks under a sustained mid-band bed. The
        # bed alone must not pulse, and the hits must still stand clearly
        # above the between-hit level.
        duration = 4.5
        sig = _kick_pattern(duration, amp=0.6, start_s=0.5)
        sig += _bandpass_noise(duration, 300.0, 3000.0, 0.10, self.rng)
        times, envs = _run_envelope(sig)
        peaks, troughs = _hit_peaks_and_troughs(times, envs, start_s=0.5)
        self.assertGreaterEqual(len(peaks), 7)
        for peak in peaks:
            self.assertGreater(peak, 0.5)
        for trough in troughs:
            self.assertLess(trough, 0.2)
        # Clear separation, not just thresholds scraped past.
        self.assertGreater(min(peaks) - max(troughs), 0.35)

    def test_nan_chunk_resets_instead_of_poisoning(self):
        # A non-finite chunk (broken client writing NaN into the sink mix)
        # must drop the baseline, not stick NaN into the floor/peak
        # followers. Before the guard, one bad chunk capped every later
        # kick at the sustain floor for the rest of the capture session.
        state = None
        warmup = _kick_pattern(1.5, start_s=0.5)
        for i in range(len(warmup) // CHUNK):
            frame = np.ascontiguousarray(
                warmup[i * CHUNK:(i + 1) * CHUNK], dtype=np.float32)
            state = _compute_pulse(frame, state)
        bad = np.full(CHUNK, np.nan, dtype=np.float32)
        state = _compute_pulse(bad, state)
        self.assertIsNone(state)
        # Kicks after the reset must still register at full strength.
        after = _kick_pattern(3.0, start_s=0.5)
        after += self.rng.standard_normal(len(after)) * 0.01
        times, envs = _run_envelope(after)
        peaks, _ = _hit_peaks_and_troughs(times, envs, start_s=0.5)
        self.assertTrue(peaks and min(peaks) > 0.5)

    def test_kick_in_first_chunk_after_reset_stays_capped(self):
        # The flip side of the no-fabricated-onsets rule: a kick landing in
        # the exact first chunk after a baseline reset (capture start,
        # stale-audio drop, parec respawn, NaN recovery) has no history to
        # flux against, so it is capped at the sustain floor. One softened
        # hit per gap is the accepted price for gap edges never flashing
        # the backdrop — pinned here so a future "fix" that uncaps it has
        # to face the tradeoff deliberately.
        # 0.45 s = one burst at t=0 only; a second hit would have history
        # and correctly fire at full strength.
        kick = _kick_pattern(0.45, start_s=0.0)
        _, envs = _run_envelope(kick)   # state starts at None, like post-gap
        self.assertLessEqual(float(envs.max()), 0.25)
        # It still registers as a modest sustained-level nudge, not nothing.
        self.assertGreater(float(envs[0]), 0.1)

    # ---------- the four reported real-world failure modes ----------
    # Field report on the 1.6 detector: "works on maybe 60% of songs".
    # Each test below isolates one measured cause. The common thread: the
    # old pipeline multiplied every signal into every other (contrast ×
    # quiet × onset × fraction, all against fixed absolute thresholds), so
    # one compressed term silenced a hit that every other term saw clearly.

    def test_kicks_over_loud_sub_bed(self):
        # A sustained sub bed keeps the band's floor high, and log
        # compression makes the kick's *summed-band* rise tiny: measured
        # level_contrast at the hits was 0.12 with every gate open — the
        # envelope showed hits at 0.13 on a signal where the kicks are
        # plainly audible over the bed. Per-bin flux sees the kick's own
        # bins rise regardless of the bed, and the onset path must not be
        # multiplied by the bed-compressed contrast.
        duration = 6.0
        t = np.arange(int(duration * SAMPLE_RATE)) / SAMPLE_RATE
        bed = 0.45 * np.sin(2 * np.pi * 45 * t)
        sig = bed + _kick_pattern(duration, amp=0.6, freq_hz=95.0, start_s=0.5)
        times, envs = _run_envelope(sig)
        peaks, troughs = _hit_peaks_and_troughs(times, envs, start_s=0.5)
        # Skip the adaptation window: the first hits land while the floor
        # is still learning the bed, which is genuinely ambiguous.
        settled_peaks = [p for p, tr in zip(peaks, troughs)][2:]
        settled_troughs = troughs[2:]
        self.assertGreaterEqual(len(settled_peaks), 7)
        for peak in settled_peaks:
            self.assertGreater(peak, 0.55)
        # Between hits the sustained-breathing path deliberately keeps a
        # glow going — the room IS full of bass — so the trough bound here
        # is about visible pulse contrast, not darkness.
        for trough in settled_troughs:
            self.assertLess(trough, 0.35)
        self.assertGreater(min(settled_peaks) - max(settled_troughs), 0.3)

    def test_dense_mix_kick_thump_pulses(self):
        # The wall-of-sound case: broadband program at 0.4 RMS, kick mixed
        # well inside it. The kick-band share of total power never clears
        # the old absolute fraction floor, so the whole song stayed dark.
        # What identifies the kick is the *surge* of bass share — fraction
        # jumping relative to its own recent baseline — not its absolute
        # value. The wall starts at 120 Hz: real guitar/synth walls carry
        # tonal, steady low-mids, not white noise inside the kick band
        # (white sub-noise would be indistinguishable from kicks for any
        # detector at this frame size, ours or otherwise).
        duration = 6.0
        wall = _bandpass_noise(duration, 120.0, 4000.0, 0.40, self.rng)
        sig = wall + _kick_pattern(duration, amp=0.30, freq_hz=80.0, start_s=0.5)
        times, envs = _run_envelope(sig)
        peaks, troughs = _hit_peaks_and_troughs(times, envs, start_s=0.5)
        settled_peaks = peaks[2:]
        settled_troughs = troughs[2:]
        self.assertGreaterEqual(len(settled_peaks), 7)
        for peak in settled_peaks:
            self.assertGreater(peak, 0.45)
        for trough in settled_troughs:
            self.assertLess(trough, 0.2)
        # And the wall alone must stay dark — the surge path must not
        # reopen the door the absolute fraction gate exists to close.
        _, envs_wall = _run_envelope(
            _bandpass_noise(duration, 120.0, 4000.0, 0.40, self.rng))
        self.assertLess(float(envs_wall[len(envs_wall) // 3:].max()), 0.2)

    def test_no_bass_rhythm_falls_back_to_percussion(self):
        # Songs that keep their rhythm entirely out of the bass: measured
        # everything-zero (gate_quiet=0 AND gate_fraction=0 at every hit).
        # When the bass channel has been silent a while and the broadband
        # flux is spiky-percussive, the detector should pulse on those
        # hits — at reduced strength, "beat" rather than "kick".
        duration = 6.0
        sig = _perc_hits(duration, self.rng, amp=0.4)
        times, envs = _run_envelope(sig)
        peaks, troughs = _hit_peaks_and_troughs(times, envs, start_s=0.5)
        settled_peaks = peaks[2:]
        settled_troughs = troughs[2:]
        self.assertGreaterEqual(len(settled_peaks), 7)
        for peak in settled_peaks:
            self.assertGreater(peak, 0.3)
        for trough in settled_troughs:
            self.assertLess(trough, 0.15)

    def test_slow_mid_swells_do_not_trip_the_fallback(self):
        # The guard on the fallback: sustained vocal/pad-like material with
        # slow attacks has broadband energy but no percussive spikes. It
        # must stay dark even though it, too, has no bass activity.
        duration = 6.0
        n = int(duration * SAMPLE_RATE)
        swell = _bandpass_noise(duration, 300.0, 3000.0, 0.22, self.rng)
        lfo = 0.5 + 0.5 * np.sin(2 * np.pi * 0.8 * np.arange(n) / SAMPLE_RATE)
        _, envs = _run_envelope(swell * lfo)
        self.assertLess(float(envs.max()), 0.2)

    def test_moderately_quiet_kicks_still_pulse(self):
        # Volume headroom: at amp 0.03 (≈ -30 dBFS in the kick band, a
        # normal low-listening-volume monitor level) the old absolute
        # quiet gate read 0.05 and the envelope never cleared it. Clearly
        # audible music must pulse; the amp-0.01 barely-audible guarantee
        # in test (e) above still holds.
        duration = 6.0
        sig = _kick_pattern(duration, amp=0.03, start_s=0.5)
        sig += self.rng.standard_normal(int(duration * SAMPLE_RATE)) * 0.0006
        times, envs = _run_envelope(sig)
        peaks, _ = _hit_peaks_and_troughs(times, envs, start_s=0.5)
        settled_peaks = peaks[2:]
        self.assertGreaterEqual(len(settled_peaks), 7)
        for peak in settled_peaks:
            self.assertGreater(peak, 0.5)

    def test_reanchored_state_cannot_fabricate_an_onset(self):
        # After a stream gap (parec respawn, stale-audio drop, paused
        # monitor) the loop resets the state to None. The first chunks of a
        # sustained bass bed arriving right after must re-anchor quietly:
        # the prev=None branch computes no flux, so "quiet before the gap,
        # loud after" cannot read as a kick that never happened.
        t = np.arange(int(3 * SAMPLE_RATE)) / SAMPLE_RATE
        bed = 0.5 * np.sin(2 * np.pi * 80 * t)
        _, envs = _run_envelope(bed)   # state starts at None, like post-gap
        self.assertLess(float(envs.max()), 0.5)


if __name__ == "__main__":
    unittest.main()
