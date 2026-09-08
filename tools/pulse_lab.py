"""pulse lab: run the beat analyzer on real files and look at what it did.

    python tools/pulse_lab.py ~/Music/some-track.mp3 another.flac --out /tmp/lab
    python tools/pulse_lab.py ~/Music --out /tmp/lab          # every audio file in a folder
    python tools/pulse_lab.py --synth --out /tmp/lab           # built-in synthetic songs
    python tools/pulse_lab.py --calibrate                      # onset lag on known kicks
    python tools/pulse_lab.py track.mp3 --progressive          # partial maps vs final

per track it prints tempo, confidence, beat count, how steady the beat
spacing is, and how steady the strengths are. with --out it writes one png
per track: low-band onset curve with the beats it chose, the full-band
curve the grid was locked to, and the pulse value the screen would see.
the corpus stays out of git; point it at your own files.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from tide import beat_map  # noqa: E402
from tide.beat_map import (  # noqa: E402
    Analysis, BeatMap, FRAME_S, SR, analyze, analyze_target, decode_pcm,
)


AUDIO_EXT = (".mp3", ".flac", ".opus", ".ogg", ".oga", ".m4a", ".aac", ".wav", ".wma", ".webm")


# ---------- synthetic material ----------

def kick(t: np.ndarray, hz: float = 60.0, decay: float = 0.04) -> np.ndarray:
    # pitch drops through the hit like a real drum sample
    phase = 2 * np.pi * (hz * t + 40.0 * decay * (1 - np.exp(-t / decay)))
    return np.sin(phase) * np.exp(-t / decay)


def noise_burst(t: np.ndarray, decay: float, rng, lo_hz: float = 2000.0) -> np.ndarray:
    n = rng.standard_normal(t.size)
    # crude high-pass: difference filter, then decay
    n = np.diff(n, prepend=0.0)
    return n * np.exp(-t / decay) * 0.5


def synth_song(bpm: float = 120.0, seconds: float = 40.0, *, drift: float = 0.0,
               snare: bool = True, hats: bool = True, bass: bool = True,
               intro: float = 4.0, ramp: bool = False, seed: int = 1) -> tuple[np.ndarray, list[float]]:
    """kick on 1 and 3, snare on 2 and 4, hats on eighths, a sub bassline,
    optional volume ramp and tempo drift. returns pcm and true kick times."""
    rng = np.random.default_rng(seed)
    out = np.zeros(int(seconds * SR), dtype=np.float64)
    hit_t = np.arange(int(0.25 * SR)) / SR
    k = kick(hit_t)
    s = noise_burst(hit_t, 0.08, rng)
    h = noise_burst(hit_t[: int(0.03 * SR)], 0.01, rng)
    kicks: list[float] = []
    t = intro
    beat = 0
    while t < seconds - 0.3:
        period = 60.0 / (bpm * (1.0 + drift * (t / seconds)))
        gain = (0.35 + 0.65 * min(1.0, t / seconds * 1.6)) if ramp else 1.0
        start = int(t * SR)
        if beat % 2 == 0:
            out[start:start + k.size] += 0.8 * gain * k[: out.size - start]
            kicks.append(t)
        elif snare:
            out[start:start + s.size] += 0.35 * gain * s[: out.size - start]
        if hats:
            for sub in (0.0, 0.5):
                hs = int((t + sub * period) * SR)
                if hs + h.size < out.size:
                    out[hs:hs + h.size] += 0.12 * gain * h
        if bass:
            n = min(int(period * SR), out.size - start)
            tt = np.arange(n) / SR
            note = 55.0 if (beat // 4) % 2 == 0 else 41.2
            out[start:start + n] += (0.25 * gain * np.sin(2 * np.pi * note * tt)
                                     * np.minimum(1.0, tt / 0.01))
        t += period
        beat += 1
    peak = np.abs(out).max() or 1.0
    return (out / peak * 0.9).astype(np.float32), kicks


SYNTH_SET = {
    "pop-120": dict(bpm=120.0),
    "slow-72": dict(bpm=72.0, hats=False),
    "dnb-174": dict(bpm=174.0, snare=True),
    "drift-128": dict(bpm=128.0, drift=0.06),
    "ramp-100": dict(bpm=100.0, ramp=True),
    "kicks-only-140": dict(bpm=140.0, snare=False, hats=False, bass=False),
}


# ---------- reporting ----------

def summarize(name: str, m: BeatMap | None, elapsed: float, truth: list[float] | None = None) -> None:
    if m is None:
        print(f"{name}: no map")
        return
    times = np.array([b.time for b in m.beats])
    pulsed = [b for b in m.beats
              if not (b.period < beat_map.HALF_TIME_PERIOD_S and b.phase % 2)]
    strengths = np.array([b.strength for b in pulsed if b.strength > 0.05])
    ibi = np.diff(times) if times.size > 2 else np.zeros(0)
    ibi_cv = float(ibi.std() / ibi.mean()) if ibi.size and ibi.mean() > 0 else float("nan")
    s_cv = float(strengths.std() / strengths.mean()) if strengths.size and strengths.mean() > 0 else float("nan")
    swell = np.array(m.swell) if m.swell else np.zeros(1)
    line = (f"{name}: {m.duration:6.1f}s  bpm {m.tempo:6.1f}  conf {m.confidence:.2f}  "
            f"beats {len(m.beats):4d} (pulsed {len(pulsed):4d})  ibi cv {ibi_cv:.3f}  "
            f"strength mean {strengths.mean() if strengths.size else 0:.2f} cv {s_cv:.2f}  "
            f"hits {len(m.hits):3d}  swell {swell.min():.2f}..{swell.max():.2f}  {elapsed:.1f}s")
    if truth:
        tr = np.array(truth)
        # nearest pulsed beat per true kick
        pt = np.array([b.time for b in pulsed]) if pulsed else np.zeros(0)
        if pt.size:
            idx = np.searchsorted(pt, tr)
            errs = []
            for k, t in zip(idx, tr):
                cands = [pt[j] for j in (k - 1, k) if 0 <= j < pt.size]
                errs.append(min(cands, key=lambda c: abs(c - t)) - t)
            errs = np.array(errs) * 1000.0
            found = np.mean(np.abs(errs) <= 40.0)
            line += f"  | kicks found {found * 100:5.1f}%  offset {np.median(errs):+.1f}ms (mad {np.median(np.abs(errs - np.median(errs))):.1f})"
    print(line)


def plot(name: str, m: BeatMap, low: np.ndarray, full: np.ndarray, out_dir: str,
         truth: list[float] | None = None, window: tuple[float, float] | None = None) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n = low.size
    t = (np.arange(n) * beat_map.HOP + beat_map.N_FFT / 2.0) / SR + beat_map.ONSET_LEAD_S
    if window is None:
        # the most interesting 24 seconds: around the loudest swell
        center = float(np.argmax(m.swell) / m.swell_rate) if m.swell else m.duration / 2
        window = (max(0.0, center - 12.0), min(m.duration, center + 12.0))
    a, b = window
    sel = (t >= a) & (t <= b)
    fig, axes = plt.subplots(3, 1, figsize=(16, 9), sharex=True)
    ax = axes[0]
    ax.plot(t[sel], low[sel], lw=0.8, color="#3b6ea5", label="low-band onset (35-200 Hz)")
    for bt in m.beats:
        if a <= bt.time <= b:
            faint = bt.period < beat_map.HALF_TIME_PERIOD_S and bt.phase % 2
            ax.axvline(bt.time, color="#d9534f" if not faint else "#f0b0ad",
                       alpha=0.25 + 0.75 * bt.strength, lw=1.0)
    for h in m.hits:
        if a <= h.time <= b:
            ax.axvline(h.time, color="#f0ad4e", alpha=0.9, lw=1.0, ls="--")
    if truth:
        for tt in truth:
            if a <= tt <= b:
                ax.axvline(tt, color="#2ca02c", alpha=0.6, lw=0.6, ymin=0.9)
    ax.set_ylabel("low flux")
    ax.legend(loc="upper right")
    ax.set_title(f"{name}   bpm {m.tempo:.1f}   confidence {m.confidence:.2f}   red = beat (alpha = strength), dashed = off-grid hit, green ticks = truth")
    ax = axes[1]
    ax.plot(t[sel], full[sel], lw=0.8, color="#555", label="full-band onset")
    for bt in m.beats:
        if a <= bt.time <= b:
            ax.axvline(bt.time, color="#d9534f", alpha=0.3, lw=0.8)
    ax.set_ylabel("full flux")
    ax.legend(loc="upper right")
    ax = axes[2]
    grid = np.arange(a, b, 1 / 60.0)
    vals = np.array([m.value_at(p) or 0.0 for p in grid])
    sw = np.array([m.swell_at(p) for p in grid])
    ax.fill_between(grid, 0, vals, color="#7b5ea7", alpha=0.7, label="pulse value (what the screen sees)")
    ax.plot(grid, sw, color="#999", lw=1.0, label="swell")
    ax.set_ylim(0, 1.02)
    ax.set_ylabel("pulse")
    ax.set_xlabel("media seconds")
    ax.legend(loc="upper right")
    fig.tight_layout()
    os.makedirs(out_dir, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:80]
    path = os.path.join(out_dir, f"{safe}.png")
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def curves(pcm: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    an = Analysis()
    an.push(pcm)
    return np.concatenate(an._low), np.concatenate(an._full)


# ---------- modes ----------

def run_calibrate() -> None:
    """true kick times vs detected beat times, on dry kicks at several
    tempos and phases. a nonzero median offset is a correction to
    ONSET_LEAD_S: subtract it from the current value."""
    offsets = []
    for bpm in (90.0, 120.0, 150.0):
        for phase in (0.0, 0.004, 0.008):
            pcm, truth = synth_song(bpm=bpm, seconds=30.0, snare=False, hats=False,
                                    bass=False, intro=2.0 + phase)
            m = analyze(pcm)
            if m is None or not m.beats:
                print(f"bpm {bpm}: no beats")
                continue
            bt = np.array([b.time for b in m.beats])
            for t in truth:
                j = np.searchsorted(bt, t)
                cands = [bt[k] for k in (j - 1, j) if 0 <= k < bt.size]
                if cands:
                    offsets.append(min(cands, key=lambda c: abs(c - t)) - t)
    offsets = np.array(offsets) * 1000.0
    print(f"detected minus true, ms: median {np.median(offsets):+.2f}  "
          f"p10 {np.percentile(offsets, 10):+.2f}  p90 {np.percentile(offsets, 90):+.2f}  "
          f"(current ONSET_LEAD_S = {beat_map.ONSET_LEAD_S * 1000:.1f} ms; subtract the median from it)")


def run_progressive(target: str, out_dir: str | None) -> None:
    partials: list[BeatMap] = []
    t0 = time.monotonic()
    final = analyze_target(target, on_partial=partials.append)
    elapsed = time.monotonic() - t0
    name = os.path.basename(target)
    summarize(name + " [final]", final, elapsed)
    if final is None:
        return
    ft = np.array([b.time for b in final.beats])
    for p in partials:
        pt = np.array([b.time for b in p.beats])
        moved = 0
        for t in pt:
            j = np.searchsorted(ft, t)
            cands = [ft[k] for k in (j - 1, j) if 0 <= k < ft.size]
            if not cands or abs(min(cands, key=lambda c: abs(c - t)) - t) > 0.03:
                moved += 1
        print(f"   partial @ {p.duration:6.1f}s: bpm {p.tempo:6.1f} conf {p.confidence:.2f} "
              f"beats {len(p.beats):4d}, {moved} not within 30 ms of a final beat")


def collect(paths: list[str]) -> list[str]:
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, names in os.walk(p):
                for n in sorted(names):
                    if n.lower().endswith(AUDIO_EXT):
                        files.append(os.path.join(root, n))
        elif os.path.isfile(p):
            files.append(p)
        else:
            print(f"skip {p}: not a file or folder")
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="audio files or folders")
    ap.add_argument("--out", help="write one png per track here")
    ap.add_argument("--synth", action="store_true", help="run the built-in synthetic songs")
    ap.add_argument("--calibrate", action="store_true", help="measure onset lag on known kicks")
    ap.add_argument("--progressive", action="store_true", help="compare partial maps against the final one")
    ap.add_argument("--seconds", type=float, default=beat_map.MAX_SECONDS, help="analyze at most this much audio")
    ap.add_argument("--window", type=float, nargs=2, metavar=("FROM", "TO"), help="plot this media-time window instead of the loudest stretch")
    args = ap.parse_args(argv)

    if args.calibrate:
        run_calibrate()
        return 0
    if args.synth:
        for name, kwargs in SYNTH_SET.items():
            pcm, truth = synth_song(**kwargs)
            t0 = time.monotonic()
            m = analyze(pcm)
            summarize(name, m, time.monotonic() - t0, truth)
            if args.out and m is not None:
                low, full = curves(pcm)
                print("   ->", plot(name, m, low, full, args.out, truth, tuple(args.window) if args.window else None))
        return 0
    files = collect(args.paths)
    if not files:
        ap.print_help()
        return 1
    for path in files:
        name = os.path.basename(path)
        if args.progressive:
            run_progressive(path, args.out)
            continue
        t0 = time.monotonic()
        blocks = list(decode_pcm(path, max_seconds=args.seconds))
        if not blocks:
            print(f"{name}: ffmpeg produced no audio")
            continue
        pcm = np.concatenate(blocks)
        decode_s = time.monotonic() - t0
        t1 = time.monotonic()
        an = Analysis()
        an.push(pcm)
        m = an.build(True)
        summarize(name, m, time.monotonic() - t1)
        print(f"   decode {decode_s:.1f}s")
        if args.out and m is not None:
            low = np.concatenate(an._low)
            full = np.concatenate(an._full)
            print("   ->", plot(name, m, low, full, args.out, None, tuple(args.window) if args.window else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
