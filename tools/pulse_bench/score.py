"""score a beat map against drum truth: kicks the screen showed, kicks it
missed (and why), and pulses with no drum under them. then, from the pulse
as rendered: how often it flashes more than three times a second, and how
many beats with a kick on them it visibly lands.

truth is {"kicks": [[time, velocity]], "others": [time], "drumless":
[[start, end]]}, plus "beats" when beat this ran. kicks softer than
MUST_VEL of the song's median are ghosts: optional either way. a pulse on
another drum hit (snare, tom) is tolerated.
"""
from __future__ import annotations

import numpy as np

import common  # noqa: F401  (puts tide on the path)
from tide import beat_map as bm

TOL = 0.05          # a pulse within 50 ms of a kick shows that kick
VIS = 0.15          # bump above the floor that reads as a pulse
CLUSTER = 0.07      # pulses closer than this read as one
MUST_VEL = 0.55
FPS = 60.0          # the pulse is rendered at this rate for the next two
FLASH_JUMP = 0.3    # a flash: the level climbing this much (plugin_api.FlashLimiter)
LAND_RISE = 0.25    # a beat lands when the level climbs this much into it


def pulse_events(m) -> list[tuple[float, float, str]]:
    """visible pulse events: (time, amplitude above the floor, source)."""
    ev = []
    for b in m.beats:
        if b.pulse:
            ev.append((b.time, b.strength, "beat"))
    for h in m.hits:
        ev.append((h.time, h.strength, "hit"))
    out = []
    for t, s, src in sorted(ev):
        sw = m.swell_at(t)
        amp = (1 - bm.FLOOR_FROM_SWELL * sw) * (bm.DEPTH_MIN + (1 - bm.DEPTH_MIN) * sw) * s
        if out and t - out[-1][0] < CLUSTER:
            if amp > out[-1][1]:
                out[-1] = (t, amp, src)
            continue
        out.append((t, amp, src))
    return out


def _nearest(arr, t):
    if arr.size == 0:
        return np.inf
    j = int(np.searchsorted(arr, t))
    return min((abs(arr[k] - t) for k in (j - 1, j) if 0 <= k < arr.size), default=np.inf)


def rendered(m) -> np.ndarray:
    return np.array([m.value_at(i / FPS) or 0.0 for i in range(int(m.duration * FPS))])


def flash_times(v: np.ndarray) -> np.ndarray:
    """each peak that stands FLASH_JUMP over the dip before it."""
    out = []
    low = v[0] if v.size else 0.0
    for i in range(1, v.size - 1):
        low = min(low, v[i])
        if v[i] >= v[i - 1] and v[i] > v[i + 1]:
            if v[i] - low >= FLASH_JUMP:
                out.append(i / FPS)
            low = v[i]
    return np.array(out)


def over_three(flashes: np.ndarray, duration: float) -> float:
    """share of one-second windows (every 0.25 s) holding more than three flashes."""
    starts = np.arange(0.0, max(0.0, duration - 1.0), 0.25)
    if starts.size == 0:
        return 0.0
    count = np.searchsorted(flashes, starts + 1.0) - np.searchsorted(flashes, starts)
    return float(np.mean(count > 3))


def landed(v: np.ndarray, truth) -> tuple[int, int]:
    """reference beats with a kick on them, and how many the level climbs
    LAND_RISE into (its peak just after against its dip just before)."""
    beats = np.array(truth.get("beats", []))
    kicks = np.array(sorted(k[0] for k in truth["kicks"]))
    if beats.size == 0 or kicks.size == 0:
        return 0, 0
    want = [b for b in beats if _nearest(kicks, b) <= TOL]
    got = 0
    for b in want:
        i = int(round(b * FPS))
        peak = v[max(0, i - 2): i + 5]
        dip = v[max(0, i - 9): max(1, i)]
        got += bool(peak.size and dip.size and peak.max() - dip.min() >= LAND_RISE)
    return len(want), got


def score(m, truth, vis=VIS) -> dict:
    pairs = sorted((float(t), float(v)) for t, v in truth["kicks"])
    kicks = np.array([t for t, _v in pairs])
    kvel = np.array([v for _t, v in pairs])
    others = np.array(sorted(truth["others"]))
    drumless = truth["drumless"]
    shown = [(t, a, s) for t, a, s in pulse_events(m) if a >= vis]
    taken = np.zeros(kicks.size, dtype=bool)
    errors, phantom, drumless_phantom = [], 0, 0
    for t, _a, _s in shown:
        lo = np.searchsorted(kicks, t - TOL)
        hi = np.searchsorted(kicks, t + TOL)
        free = [k for k in range(lo, hi) if not taken[k]]
        if free:
            k = min(free, key=lambda k: abs(kicks[k] - t))
            taken[k] = True
            errors.append(t - kicks[k])
        elif _nearest(others, t) > TOL and _nearest(kicks, t) > TOL:
            phantom += 1
            drumless_phantom += any(a <= t <= b for a, b in drumless)
    must = kvel >= MUST_VEL * (np.median(kvel) if kvel.size else 1.0)
    n_must = int(must.sum())
    hit = int((must & taken).sum())
    e = np.array(errors) * 1000.0
    v = rendered(m)
    beats, beats_landed = landed(v, truth)
    return dict(kicks=n_must, hit=hit, recall=hit / max(1, n_must), shown=len(shown),
                phantom=phantom, drumless_phantom=drumless_phantom, minutes=m.duration / 60.0,
                err_med=float(np.median(e)) if e.size else float("nan"),
                over3=over_three(flash_times(v), m.duration), beats=beats, landed=beats_landed)


def line(name: str, r: dict) -> str:
    return (f"{name[:40]:40s} kicks {r['kicks']:4d} shown {r['recall'] * 100:5.1f}% | pulses {r['shown']:4d} "
            f"no drum {r['phantom']:4d} ({r['phantom'] / max(1, r['shown']) * 100:4.1f}%, drumless "
            f"{r['drumless_phantom']:3d}) | timing {r['err_med']:+5.1f} ms | >3 flashes/s "
            f"{r['over3'] * 100:4.1f}% | beats land {r['landed'] / max(1, r['beats']) * 100:5.1f}%")


def total(rows: list[dict]) -> str:
    k = sum(r["kicks"] for r in rows)
    h = sum(r["hit"] for r in rows)
    s = sum(r["shown"] for r in rows)
    p = sum(r["phantom"] for r in rows)
    mins = sum(r["minutes"] for r in rows)
    over = sum(r["over3"] * r["minutes"] for r in rows) / max(1e-9, mins)
    b = sum(r["beats"] for r in rows)
    land = sum(r["landed"] for r in rows)
    return (f"TOTAL kicks shown {h / max(1, k) * 100:.1f}%  pulses with no drum {p} of {s} "
            f"({p / max(1, s) * 100:.1f}%, {p / max(1e-9, mins):.1f}/min)  over 3 flashes/s "
            f"{over * 100:.1f}% of the time  beats land {land / max(1, b) * 100:.1f}%")
