"""drum truth for a real song, read from its demucs drum stem.

the stem holds only drums, so a sharp rise in its low band is a kick (or a
floor tom). a rise counts when the low band outweighs the snare body right
after it, it is loud enough against the whole mix (separation bleed in a
drumless stretch is not), and it is not the same kick still ringing.
against the rendered set's exact truth (`refcheck`) it scores f 0.84: on
plain kits near perfect, but it misses 808s, which demucs files as bass,
and counts low toms and rumble bass as kicks. a real song whose kicks are
808s reads as drumless, so leave those out (--skip).
"""
from __future__ import annotations

import json

import numpy as np

import common

SR = common.SR
N_FFT = 1024
HOP = 64                      # 2.9 ms: timing resolution for the truth
LOW = (30.0, 150.0)
MID = (180.0, 600.0)
RISE_REL = 0.18               # of the stem's strong low rises
MIX_DB = -30.0                # stem low peak against the mix level: quieter is bleed
LOW_OVER_MID_DB = -3.0
REFRACTORY_S = 0.11
RING_SHARE = 0.35
LAG_S = 0.005                 # stem onsets read 5 ms early against rendered truth
MERGE_S = 0.13                # one pulse cannot show two attacks this close
OTHER_REL = 0.12
DRUMLESS_DB = -24.0           # drum stem this far under the mix: no drums here
DRUMLESS_WIN = 2.0


def _stft(x):
    win = np.hanning(N_FFT).astype(np.float32)
    pad = np.concatenate((np.zeros(N_FFT // 2, np.float32), x.astype(np.float32), np.zeros(N_FFT, np.float32)))
    n = x.size // HOP
    out = np.empty((n, N_FFT // 2 + 1), np.float32)
    for s in range(0, n, 8192):
        e = min(n, s + 8192)
        idx = np.arange(N_FFT)[None, :] + HOP * np.arange(s, e)[:, None]
        out[s:e] = np.abs(np.fft.rfft(pad[idx] * win, axis=1))
    return out


def _band(lo, hi):
    hz = SR / N_FFT
    return max(1, int(round(lo / hz))), int(round(hi / hz)) + 1


def _peaks(x, reach):
    from scipy.ndimage import maximum_filter1d
    mx = maximum_filter1d(x, 2 * reach + 1)
    return np.flatnonzero((x >= mx) & (x > 0))


def kicks(drums, mix):
    """kick times and their level against the mix."""
    mag = _stft(drums)
    lo, hi = _band(*LOW)
    mlo, mhi = _band(*MID)
    low = mag[:, lo:hi].sum(1)
    mid = mag[:, mlo:mhi].sum(1)
    k = 4                         # rise over ~12 ms
    rise = np.zeros_like(low)
    rise[k:] = np.maximum(low[k:] - low[:-k], 0)
    cand = _peaks(rise, int(0.045 * SR / HOP))
    if cand.size == 0:
        return np.zeros(0), np.zeros(0)
    strong = np.percentile(rise[cand], 99) + 1e-9
    mix_level = np.percentile(_stft(mix).sum(1), 95) + 1e-9
    after = int(0.05 * SR / HOP)
    out, amp = [], []
    for i in cand:
        if rise[i] < RISE_REL * strong:
            continue
        lp = low[i:i + after].max()
        if 20 * np.log10(lp / (mid[i:i + after].max() + 1e-9) + 1e-12) < LOW_OVER_MID_DB:
            continue
        if 20 * np.log10(lp / mix_level + 1e-12) < MIX_DB:
            continue
        # onset: the first frame of the rise that reaches half of it
        seg = low[i - k:i + 1]
        reached = np.flatnonzero(seg - seg[0] >= 0.5 * (seg.max() - seg[0]))
        f = i - k + int(reached[0]) if reached.size else i
        if out:
            pf = int(round((out[-1] - LAG_S) * SR / HOP))
            if (f - pf) * HOP / SR < REFRACTORY_S and low[pf:f + 1].min() > RING_SHARE * low[pf:pf + after].max():
                continue
        out.append(f * HOP / SR + LAG_S)
        amp.append(lp / mix_level)
    t, a = np.array(out), np.array(amp)
    if t.size:
        keep_t, keep_a = [t[0]], [a[0]]
        for x, y in zip(t[1:], a[1:]):
            if x - keep_t[-1] < MERGE_S:
                keep_a[-1] = max(keep_a[-1], y)
                continue
            keep_t.append(x)
            keep_a.append(y)
        t, a = np.array(keep_t), np.array(keep_a)
    return t, a


def other_hits(drums, mix):
    """any drum onset in the stem: a pulse there is tolerated."""
    mag = _stft(drums)
    lg = np.log1p(mag * 10)
    rise = np.zeros(lg.shape[0])
    rise[4:] = np.maximum(lg[4:] - lg[:-4], 0).sum(1)
    cand = _peaks(rise, int(0.04 * SR / HOP))
    if cand.size == 0:
        return []
    strong = np.percentile(rise[cand], 99) + 1e-9
    lvl = mag.sum(1)
    mix_level = np.percentile(_stft(mix).sum(1), 95) + 1e-9
    after = int(0.05 * SR / HOP)
    return [float(i * HOP / SR) for i in cand
            if rise[i] >= OTHER_REL * strong
            and 20 * np.log10(lvl[i:i + after].max() / mix_level + 1e-12) >= MIX_DB - 6]


def activity(drums, mix, win_s=DRUMLESS_WIN):
    """per window: drum stem energy over mix energy, db."""
    n = int(SR * win_s)
    m = min(drums.size, mix.size) // n
    d = drums[: m * n].reshape(m, n)
    x = mix[: m * n].reshape(m, n)
    return 10 * np.log10((d ** 2).mean(1) / ((x ** 2).mean(1) + 1e-12) + 1e-12)


def drumless(drums, mix):
    out = []
    for k, a in enumerate(activity(drums, mix)):
        if a < DRUMLESS_DB:
            s, e = k * DRUMLESS_WIN, (k + 1) * DRUMLESS_WIN
            if out and abs(out[-1][1] - s) < 1e-6:
                out[-1] = (out[-1][0], e)
            else:
                out.append((s, e))
    return out


def truth_for(name, mix):
    """the song's truth from its stem in the ref cache (refs.py makes it), cached."""
    path = common.ref_dir() / (name + ".truth.json")
    if path.exists():
        return json.loads(path.read_text())
    stem = common.ref_dir() / (name + ".drums.npy")
    if not stem.exists():
        return None
    drums = np.load(stem)
    n = min(drums.size, mix.size)
    drums, mix = drums[:n], mix[:n]
    kt, amp = kicks(drums, mix)
    vel = (amp / np.median(amp)).tolist() if amp.size else []
    t = dict(kicks=[[float(a), float(min(1.5, v))] for a, v in zip(kt, vel)],
             others=other_hits(drums, mix), drumless=drumless(drums, mix),
             drum_share=float(np.mean(activity(drums, mix) >= DRUMLESS_DB)))
    beats = common.ref_dir() / (name + ".beats.json")
    if beats.exists():
        t.update(json.loads(beats.read_text()))
    path.write_text(json.dumps(t))
    return t


def check(songs_and_truth):
    """the stem reference against exact truth: precision, recall, timing."""
    tp = fp = fn = 0
    errs = []
    for name, mix, truth in songs_and_truth:
        stem = common.ref_dir() / (name + ".drums.npy")
        if not stem.exists():
            continue
        drums = np.load(stem)
        n = min(drums.size, mix.size)
        kt, amp = kicks(drums[:n], mix[:n])
        kt = kt[amp >= 0.55 * np.median(amp)] if amp.size else kt
        tk = np.array(sorted(k[0] for k in truth["kicks"]))
        tv = np.array([k[1] for k in sorted(truth["kicks"])])
        must = tv >= 0.55 * (np.median(tv) if tv.size else 1)
        used = np.zeros(tk.size, bool)
        f_p = 0
        for t in kt:
            j = np.flatnonzero(~used & (np.abs(tk - t) <= 0.05))
            if j.size:
                jj = j[np.argmin(np.abs(tk[j] - t))]
                used[jj] = True
                errs.append(t - tk[jj])
            else:
                f_p += 1
        f_n = int((must & ~used).sum())
        print(f"{name[:30]:30s} truth {int(must.sum()):4d} found {kt.size:4d} extra {f_p:3d} missed {f_n:3d}")
        tp += int((used & must).sum())
        fp += f_p
        fn += f_n
    p, r = tp / max(1, tp + fp), tp / max(1, tp + fn)
    e = np.array(errs) * 1000
    print(f"precision {p:.3f} recall {r:.3f} f {2 * p * r / max(1e-9, p + r):.3f}  "
          f"timing {np.median(e) if e.size else float('nan'):+.1f} ms")
