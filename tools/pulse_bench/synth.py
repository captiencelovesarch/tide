"""instruments and a mix bus: arrangements rendered with exact drum truth.

an arrangement is a list of events: (time, kind, params). kinds: kick,
snare, clap, hat, ohat, tom, bass, chord, pad, lead, strum. each kind has
a few production styles so the same arrangement can be mixed like an
acoustic record, an 808 trap beat or a sidechained house track.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
import subprocess
import tempfile

import numpy as np

SR = 44_100


@dataclass
class Ev:
    t: float
    kind: str
    vel: float = 1.0          # 0..1
    pitch: float = 0.0        # midi note for pitched kinds
    dur: float = 0.25         # seconds for pitched kinds
    extra: dict = field(default_factory=dict)


def hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def _rng(seed):
    return np.random.default_rng(seed)


def _onepole_lp(x, cutoff):
    from scipy.signal import lfilter
    a = math.exp(-2 * math.pi * cutoff / SR)
    return lfilter([1 - a], [1, -a], x)


def _hp(x, cutoff):
    return x - _onepole_lp(x, cutoff)


def _bp(x, lo, hi, order=2):
    from scipy.signal import butter, sosfilt
    sos = butter(order, [lo, hi], btype="band", fs=SR, output="sos")
    return sosfilt(sos, x)


# ---------- instruments ----------

def kick(style: str, vel: float, rng, pitch: float = 0.0) -> np.ndarray:
    if style == "acoustic":
        n = int(0.35 * SR)
        t = np.arange(n) / SR
        f0, f1, sweep, dec = 110.0, 58.0, 0.03, 0.09
        click = 0.5
    elif style == "house":
        n = int(0.45 * SR)
        t = np.arange(n) / SR
        f0, f1, sweep, dec = 160.0, 50.0, 0.025, 0.16
        click = 0.35
    elif style == "808":
        n = int(1.2 * SR)
        t = np.arange(n) / SR
        f1 = hz(pitch) if pitch else 49.0
        f0, sweep, dec = 3.2 * f1, 0.03, 0.45
        click = 0.15
    elif style == "punch":   # tight modern pop kick, sub plus knock
        n = int(0.3 * SR)
        t = np.arange(n) / SR
        f0, f1, sweep, dec = 200.0, 55.0, 0.02, 0.07
        click = 0.6
    else:
        raise ValueError(style)
    f = f1 + (f0 - f1) * np.exp(-t / sweep)
    ph = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(ph) * np.exp(-t / dec)
    if style == "808":
        body = np.tanh(1.8 * body) / np.tanh(1.8)
    nclick = int(0.006 * SR)
    cl = np.zeros(n)
    noise = rng.standard_normal(nclick)
    cl[:nclick] = _bp(noise, 1500, 6000) * np.exp(-np.arange(nclick) / (0.0015 * SR))
    out = body + click * cl * 3.0
    return vel * out


def snare(vel, rng, style="acoustic"):
    n = int(0.3 * SR)
    t = np.arange(n) / SR
    noise = _bp(rng.standard_normal(n), 1200, 9000) * np.exp(-t / 0.07)
    tone = np.sin(2 * np.pi * 185 * t) * np.exp(-t / 0.05) * 0.6
    tone += np.sin(2 * np.pi * 330 * t) * np.exp(-t / 0.03) * 0.3
    if style == "trap":   # snappy clap-snare with a body
        noise *= 1.3
    return vel * (noise * 1.1 + tone)


def clap(vel, rng):
    n = int(0.25 * SR)
    t = np.arange(n) / SR
    env = np.zeros(n)
    for k, d in enumerate((0.0, 0.009, 0.018)):
        s = int(d * SR)
        env[s:] += np.exp(-(t[: n - s]) / 0.006) * (0.7 if k < 2 else 1.0)
    env += np.exp(-t / 0.08) * 0.5 * (t > 0.02)
    return vel * _bp(rng.standard_normal(n), 900, 7000) * env * 1.2


def hat(vel, rng, open_=False):
    n = int((0.35 if open_ else 0.05) * SR)
    t = np.arange(n) / SR
    x = _hp(_hp(rng.standard_normal(n), 7000), 7000)
    return vel * x * np.exp(-t / (0.12 if open_ else 0.012)) * 0.5


def tom(vel, rng, pitch_hz):
    n = int(0.5 * SR)
    t = np.arange(n) / SR
    f = pitch_hz * (1 + 0.5 * np.exp(-t / 0.02))
    ph = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(ph) * np.exp(-t / 0.18)
    att = _bp(rng.standard_normal(n), 800, 5000) * np.exp(-t / 0.008) * 0.4
    return vel * (body + att)


def bass(style, pitch, dur, vel, rng):
    f = hz(pitch)
    n = int((dur + 0.08) * SR)
    t = np.arange(n) / SR
    gate = np.clip((dur - t) / 0.03 + 1, 0, 1)
    if style == "sub":
        att = np.minimum(1, t / 0.004)
        x = np.sin(2 * np.pi * f * t) + 0.15 * np.sin(4 * np.pi * f * t)
        env = att * gate
    elif style == "picked":   # electric bass, plucked: sharp attack, decays
        att = np.minimum(1, t / 0.002)
        x = sum(np.sin(2 * np.pi * f * k * t) / k ** 1.3 * math.exp(-k * 0.12)
                for k in range(1, 9))
        x += 0.25 * _bp(rng.standard_normal(n), 500, 3000) * np.exp(-t / 0.006)
        env = att * np.exp(-t / 0.6) * gate
    elif style == "reese":    # detuned saws, sustained, slow-ish attack
        att = np.minimum(1, t / 0.02)
        saw = lambda ff: 2 * ((ff * t) % 1.0) - 1
        x = _onepole_lp(saw(f) + saw(f * 1.007) + saw(f * 0.993), 400) * 1.2
        env = att * gate
    elif style == "wobble":
        att = np.minimum(1, t / 0.005)
        saw = 2 * ((f * t) % 1.0) - 1
        lfo = 0.5 + 0.5 * np.sin(2 * np.pi * (1 / max(dur, 0.1) * 2) * t)
        # time-varying low-pass by crossfade of two filtered copies
        lo = _onepole_lp(saw, 120)
        hi = _onepole_lp(saw, 1500)
        x = (lo * (1 - lfo) + hi * lfo) * 1.4
        env = att * gate
    elif style == "808":
        return kick("808", vel, rng, pitch)[: n]
    else:
        raise ValueError(style)
    return vel * x * env


def chord(pitches, dur, vel, rng, style="pad"):
    n = int((dur + (0.6 if style != "pad" else 0.3)) * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for k, p in enumerate(pitches):
        f = hz(p)
        if style == "pad":
            saw = 2 * ((f * t * (1 + 0.002 * k)) % 1.0) - 1
            env = np.minimum(1, t / 0.25) * np.clip((dur - t) / 0.3 + 1, 0, 1)
            out += _onepole_lp(saw, 1800) * env
        elif style == "piano":
            x = sum(np.sin(2 * np.pi * f * h * t) / h ** 1.5 for h in range(1, 7))
            env = np.minimum(1, t / 0.003) * np.exp(-t / 0.9) * np.clip((dur - t) / 0.1 + 1, 0, 1)
            out += x * env
        elif style == "strum":
            d = k * 0.012
            s = int(d * SR)
            tt = t[: n - s]
            x = sum(np.sin(2 * np.pi * f * h * tt) / h ** 1.2 for h in range(1, 10))
            env = np.minimum(1, tt / 0.002) * np.exp(-tt / 0.5) * np.clip((dur - tt) / 0.08 + 1, 0, 1)
            out[s:] += x * env
    return vel * out / max(1, len(pitches)) * 1.4


def lead(pitch, dur, vel, rng):
    f = hz(pitch)
    n = int((dur + 0.1) * SR)
    t = np.arange(n) / SR
    vib = 1 + 0.004 * np.sin(2 * np.pi * 5.5 * t)
    ph = 2 * np.pi * np.cumsum(f * vib) / SR
    x = np.sign(np.sin(ph)) * 0.3 + np.sin(ph)
    env = np.minimum(1, t / 0.03) * np.clip((dur - t) / 0.05 + 1, 0, 1)
    return vel * _onepole_lp(x, 3000) * env * 0.5


# ---------- mixing ----------

def _place(buf, x, t):
    s = int(round(t * SR))
    if s >= buf.size or s + x.size <= 0:
        return
    if s < 0:
        x = x[-s:]
        s = 0
    e = min(buf.size, s + x.size)
    buf[s:e] += x[: e - s]


def _reverb(x, seconds, wet, rng):
    from scipy.signal import fftconvolve
    n = int(seconds * SR)
    t = np.arange(n) / SR
    ir = rng.standard_normal(n) * np.exp(-t / (seconds / 6.9))
    ir = _onepole_lp(ir, 5000)
    ir[: int(0.02 * SR)] = 0
    ir /= np.sqrt((ir ** 2).sum())
    return x + wet * fftconvolve(x, ir)[: x.size]


def _compress(x, thresh_db=-18, ratio=4.0, attack=0.005, release=0.12, makeup_db=6):
    lvl = np.abs(x)
    e = 0.0
    # a peak follower at 1 ms resolution
    blk = int(0.001 * SR)
    m = lvl[: lvl.size // blk * blk].reshape(-1, blk).max(axis=1)
    ev = np.empty_like(m)
    aa = math.exp(-1 / (attack * 1000))
    ar = math.exp(-1 / (release * 1000))
    for i, v in enumerate(m):
        e = aa * e + (1 - aa) * v if v > e else ar * e + (1 - ar) * v
        ev[i] = e
    env = np.repeat(ev, blk)
    env = np.concatenate((env, np.full(lvl.size - env.size, ev[-1] if ev.size else 0)))
    db = 20 * np.log10(np.maximum(env, 1e-6))
    over = np.maximum(0, db - thresh_db)
    gain_db = -over * (1 - 1 / ratio) + makeup_db
    return x * 10 ** (gain_db / 20)


def render(events: list[Ev], seconds: float, style: dict, seed: int = 0,
           voice_path: str | None = None) -> np.ndarray:
    rng = _rng(seed)
    drums = np.zeros(int(seconds * SR) + SR)
    music = np.zeros_like(drums)
    kick_style = style.get("kick", "acoustic")
    bass_style = style.get("bass", "picked")
    chord_style = style.get("chord", "pad")
    for ev in events:
        if ev.kind == "kick":
            _place(drums, kick(kick_style, ev.vel, rng, ev.pitch), ev.t)
        elif ev.kind == "snare":
            _place(drums, 0.55 * snare(ev.vel, rng), ev.t)
        elif ev.kind == "clap":
            _place(drums, 0.5 * clap(ev.vel, rng), ev.t)
        elif ev.kind in ("hat", "ohat"):
            _place(drums, 0.35 * hat(ev.vel, rng, ev.kind == "ohat"), ev.t)
        elif ev.kind == "tom":
            _place(drums, 0.5 * tom(ev.vel, rng, ev.extra.get("hz", 110.0)), ev.t)
        elif ev.kind == "crash":
            _place(drums, 0.25 * hat(ev.vel, rng, True), ev.t)
        elif ev.kind == "bass":
            _place(music, style.get("bass_gain", 0.55) * bass(bass_style, ev.pitch, ev.dur, ev.vel, rng), ev.t)
        elif ev.kind == "chord":
            _place(music, style.get("chord_gain", 0.35) * chord(ev.extra["pitches"], ev.dur, ev.vel, rng, chord_style), ev.t)
        elif ev.kind == "glide":
            _place(music, style.get("bass_gain", 0.55) * glide_bass(ev.extra["notes"], ev.dur, ev.vel), ev.t)
        elif ev.kind == "lead":
            _place(music, 0.25 * lead(ev.pitch, ev.dur, ev.vel, rng), ev.t)
    kicks = [e.t for e in events if e.kind == "kick"]
    if style.get("sidechain"):
        g = np.ones_like(music)
        n = int(0.25 * SR)
        shape = 1 - 0.7 * np.exp(-np.arange(n) / (0.08 * SR))
        for kt in kicks:
            s = int(kt * SR)
            e = min(g.size, s + n)
            if s < g.size:
                g[s:e] = np.minimum(g[s:e], shape[: e - s])
        music = music * g
    mix = drums * style.get("drum_gain", 1.0) + music
    if style.get("vox") is not None and voice_path:
        v = voice(voice_path, mix.size, seed)
        rms_mix = np.sqrt(np.mean(mix ** 2)) + 1e-9
        rms_v = np.sqrt(np.mean(v ** 2)) + 1e-9
        mix = mix + v * rms_mix / rms_v * 10 ** (style["vox"] / 20)
    if style.get("reverb"):
        mix = _reverb(mix, style["reverb"], style.get("wet", 0.25), rng)
    if style.get("compress"):
        mix = _compress(mix / (np.abs(mix).max() + 1e-9), thresh_db=-16, ratio=style["compress"])
    mix = mix / (np.abs(mix).max() + 1e-9)
    if style.get("limit"):
        mix = np.tanh(2.2 * mix) / np.tanh(2.2)
    return (0.9 * mix[: int(seconds * SR)]).astype(np.float32)


def through_opus(pcm: np.ndarray, kbps: int = 128) -> np.ndarray:
    """encode and decode like a youtube stream; returns 22.05 khz mono."""
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "a.f32")
        enc = os.path.join(d, "a.opus")
        pcm.astype(np.float32).tofile(src)
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "f32le", "-ar", str(SR),
                        "-ac", "1", "-i", src, "-c:a", "libopus", "-b:a", f"{kbps}k", enc], check=True)
        out = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", enc, "-ac", "1",
                              "-ar", "22050", "-f", "f32le", "pipe:1"], check=True, capture_output=True).stdout
        return np.frombuffer(out, dtype=np.float32).copy()


def voice(path, n, seed):
    """a spoken clip looped with small gaps, every other pass a few semitones up."""
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-ac", "1",
                          "-ar", str(SR), "-f", "f32le", "pipe:1"], check=True, capture_output=True).stdout
    base = np.frombuffer(raw, np.float32).astype(np.float64)
    up = np.interp(np.arange(0, base.size, 1.19), np.arange(base.size), base)   # ~3 semitones up
    out = np.zeros(n)
    rng = _rng(seed + 99)
    pos = int(rng.uniform(0.2, 1.5) * SR)
    k = 0
    while pos < n:
        clip = base if k % 2 == 0 else up
        e = min(n, pos + clip.size)
        out[pos:e] += clip[: e - pos]
        pos = e + int(rng.uniform(0.3, 1.2) * SR)
        k += 1
    return out


def glide_bass(notes, dur, vel):
    """one legato synth bass line: pitch slides between notes, no new attacks."""
    n = int((dur + 0.05) * SR)
    t = np.arange(n) / SR
    f = np.full(n, hz(notes[0][1]))
    for (t0, p) in notes:
        s = int(t0 * SR)
        target = hz(p)
        g = min(n - s, int(0.06 * SR))
        if s >= n:
            break
        start = f[s - 1] if s > 0 else target
        f[s:s + g] = np.linspace(start, target, g) if g > 0 else target
        f[s + g:] = target
    ph = 2 * np.pi * np.cumsum(f) / SR
    saw = 2 * ((ph / (2 * np.pi)) % 1.0) - 1
    x = _onepole_lp(saw, 300) * 1.3 + 0.6 * np.sin(ph)
    env = np.minimum(1, t / 0.01) * np.clip((dur - t) / 0.05 + 1, 0, 1)
    return vel * x * env
