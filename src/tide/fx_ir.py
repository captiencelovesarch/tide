"""Synthetic impulse responses for the convolution reverb.

The old reverb was ffmpeg ``aecho`` — literally a handful of discrete
delay taps, which reads as "the song again, quieter" rather than a
room. Real reverb is a dense, exponentially decaying wash with no
audible repeats, and the cheapest honest way to get one is convolution
against an impulse response. Rather than shipping recorded IRs (large,
licensing, taste) we synthesize them: exponentially decaying colored
noise is a textbook stand-in for a diffuse reverb tail and sounds
remarkably close to the real thing.

Recipe per preset:
  * pre-delay — silence before the wet signal starts; reads as room size.
  * early reflections — a sparse cluster of discrete taps right after
    the pre-delay, decorrelated between channels; the "walls".
  * diffuse tail — Gaussian noise split into three frequency bands via
    FFT masks, each band enveloped with its own exponential decay. The
    high band decays fastest, so high-frequency damping *increases*
    over the tail exactly like air absorption in a real room.
  * stereo width — each channel mixes a shared noise source with an
    independent one; less correlation = wider, more enveloping tail.

Files land in ``CACHE_DIR/fx_ir/`` as 16-bit 44.1 kHz stereo WAVs with
a version tag in the filename, regenerated when missing or when
``IR_VERSION`` is bumped. Generation is deterministic (fixed seeds) so
a regenerated file sounds identical to the one it replaces.

Pure Python + numpy + stdlib wave — no Qt, no mpv. ``audio_fx`` calls
``ensure_ir()`` lazily from ``build_filter_chain``.
"""
from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import CACHE_DIR

# Bump when the synthesis recipe changes so stale cached IRs regenerate.
IR_VERSION = 1
IR_SAMPLE_RATE = 44100
IR_DIR: Path = CACHE_DIR / "fx_ir"

# -60 dB in natural log — RT60 is the time for the tail to fall 60 dB,
# so the band envelope is exp(-LN_60DB * t / rt60).
_LN_60DB = 6.9078


@dataclass(frozen=True)
class IrSpec:
    """Knobs for one synthesized room.

    ``damping`` 0..1 — how much faster the mid/high bands die compared
    to the lows. ``width`` 0..1 — L/R decorrelation of the tail (0 =
    dual-mono, 1 = fully independent channels).
    """
    rt60: float          # seconds for the low band to decay 60 dB
    predelay_ms: float
    damping: float
    er_ms: float         # early-reflection cluster window length
    er_gain: float       # early-reflection level relative to tail peak
    width: float
    seed: int            # deterministic per-preset noise


# One spec per reverb preset name (audio_fx.REVERB_PRESETS holds the
# user-facing list + wet gains; this is the acoustics).
IR_SPECS: dict[str, IrSpec] = {
    # Small, tight, dark fast — barely more than a thickening.
    "room":      IrSpec(rt60=0.45, predelay_ms=8.0,  damping=0.60,
                        er_ms=22.0, er_gain=0.65, width=0.55, seed=101),
    # The default "put it in a nice hall" reverb.
    "hall":      IrSpec(rt60=1.90, predelay_ms=22.0, damping=0.50,
                        er_ms=48.0, er_gain=0.50, width=0.75, seed=102),
    # Plate: near-zero pre-delay, dense from the first millisecond,
    # bright (little damping) — the classic vocal sheen.
    "plate":     IrSpec(rt60=1.40, predelay_ms=3.0,  damping=0.25,
                        er_ms=8.0,  er_gain=0.25, width=0.85, seed=103),
    # Huge stone space, long pre-delay, heavy air absorption.
    "cathedral": IrSpec(rt60=3.60, predelay_ms=34.0, damping=0.65,
                        er_ms=75.0, er_gain=0.45, width=0.80, seed=104),
    # Signature tide preset — big, dark, maximally wide. Pairs with
    # speed < 1.0 + pitch-shift off (the "slowed + reverb" aesthetic).
    "slowed":    IrSpec(rt60=5.20, predelay_ms=44.0, damping=0.75,
                        er_ms=85.0, er_gain=0.35, width=0.95, seed=105),
}

# Band split points for the three-band decay (Hz). Lows keep the full
# RT60, mids and highs decay faster in proportion to ``damping``.
_BAND_EDGES = (400.0, 2800.0)


def ir_path(preset: str) -> Path:
    """Where the WAV for ``preset`` lives (may not exist yet)."""
    return IR_DIR / f"{preset}_v{IR_VERSION}.wav"


def _ir_looks_valid(path: Path) -> bool:
    """Cheap sanity check on a cached IR: the WAV header must parse and
    claim actual audio. ``wave.open`` only reads the header, so this is
    a few hundred bytes of I/O per call — cheap enough to run on every
    ensure, and it catches the file a crashed write / bit rot / a
    helpful cache cleaner left corrupt. Anything wrong means
    'regenerate', so all failure modes collapse to False."""
    try:
        with wave.open(str(path), "rb") as w:
            return (
                w.getnchannels() > 0
                and w.getframerate() > 0
                and w.getnframes() > 0
            )
    except Exception:
        return False


def ensure_ir(preset: str) -> Path | None:
    """Return the IR file for ``preset``, generating it on first use
    and regenerating it whenever the cached copy fails the header
    check. Returns None for unknown presets or if generation/writing
    fails (read-only cache, disk full) — the chain builder then simply
    skips the reverb entry rather than handing mpv a path that would
    kill the whole filter chain, and playback with it.

    Residual risk we accept: the file is validated here but opened by
    mpv slightly later, so a delete landing in that gap (or mid-session
    after a chain was already pushed) still reaches amovie. That needs
    an actor racing us inside the cache dir; the next chain rebuild
    heals it."""
    spec = IR_SPECS.get(preset)
    if spec is None:
        return None
    path = ir_path(preset)
    if path.is_file() and _ir_looks_valid(path):
        return path
    try:
        IR_DIR.mkdir(parents=True, exist_ok=True)
        data = generate_ir(spec)
        # Write to a temp name then rename so a crash mid-write can't
        # leave a truncated WAV that mpv would choke on forever.
        tmp = path.with_suffix(".tmp")
        _write_wav(tmp, data)
        tmp.replace(path)
        return path
    except OSError:
        return None


def generate_ir(spec: IrSpec) -> np.ndarray:
    """Synthesize one stereo IR. Returns float samples in [-1, 1],
    shape (n, 2). Wet-only: no unit impulse at t=0 — the dry path is
    afir's ``dry`` parameter, so the IR is pure room."""
    sr = IR_SAMPLE_RATE
    rng = np.random.default_rng(spec.seed)
    n_tail = int(sr * spec.rt60)
    t = np.arange(n_tail) / sr

    # Per-band RT60 multipliers: low band keeps the nominal RT60, the
    # upper bands die faster the more damping the preset asks for.
    band_mults = (1.0, 1.0 - 0.40 * spec.damping, 1.0 - 0.72 * spec.damping)

    # Decorrelated stereo noise: mix a shared source with per-channel
    # independent sources. correlation = 1 - width.
    corr = max(0.0, min(1.0, 1.0 - spec.width))
    shared = rng.standard_normal(n_tail)
    tail = np.zeros((n_tail, 2))
    freqs = np.fft.rfftfreq(n_tail, 1.0 / sr)
    lo_mask = freqs < _BAND_EDGES[0]
    mid_mask = (freqs >= _BAND_EDGES[0]) & (freqs < _BAND_EDGES[1])
    hi_mask = freqs >= _BAND_EDGES[1]
    for ch in range(2):
        indep = rng.standard_normal(n_tail)
        noise = np.sqrt(corr) * shared + np.sqrt(1.0 - corr) * indep
        spec_f = np.fft.rfft(noise)
        sig = np.zeros(n_tail)
        for mask, mult in zip((lo_mask, mid_mask, hi_mask), band_mults):
            band = np.fft.irfft(np.where(mask, spec_f, 0.0), n_tail)
            sig += band * np.exp(-_LN_60DB * t / (spec.rt60 * mult))
        tail[:, ch] = sig
    # Short attack ramp (~8 ms) — real rooms build up rather than
    # slamming to full density instantly; also avoids a metallic edge.
    tail *= (1.0 - np.exp(-t / 0.008))[:, None]

    # Early reflections: a handful of discrete taps inside the er
    # window, decaying, alternating sign, channel-offset for width.
    er_n = int(sr * spec.er_ms / 1000.0)
    if er_n > 8:
        er = np.zeros((er_n, 2))
        taps = 7
        peak = float(np.abs(tail).max()) or 1.0
        for k in range(taps):
            frac = (k + 1) / (taps + 1)
            gain = spec.er_gain * peak * (1.0 - frac) * (-1.0 if k % 2 else 1.0)
            for ch in range(2):
                # Deterministic jitter, different per channel.
                jitter = rng.uniform(-0.06, 0.06)
                pos = int(er_n * min(0.98, max(0.02, frac + jitter)))
                er[pos, ch] += gain
        tail[:er_n] += er

    # Energy-normalize so every preset lands at a comparable wet level
    # (unit-energy IR ≈ wet loudness matching dry for noise-like music),
    # then peak-limit for the 16-bit container.
    energy = float(np.sqrt((tail ** 2).sum() / 2.0)) or 1.0
    tail /= energy
    peak = float(np.abs(tail).max())
    if peak > 0.98:
        tail *= 0.98 / peak

    predelay = np.zeros((int(sr * spec.predelay_ms / 1000.0), 2))
    return np.vstack([predelay, tail])


def _write_wav(path: Path, data: np.ndarray) -> None:
    """16-bit PCM stereo WAV via stdlib wave. 16 bits put the
    quantization floor ~96 dB down — inaudible under a reverb tail."""
    pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(IR_SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


__all__ = [
    "IR_DIR",
    "IR_SAMPLE_RATE",
    "IR_SPECS",
    "IR_VERSION",
    "IrSpec",
    "ensure_ir",
    "generate_ir",
    "ir_path",
]
