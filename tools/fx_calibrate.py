"""Measure what each audio fx does to loudness, for audio_fx._LEVEL_TABLES.

Renders 30 s of each song through mpv with the chain tide would build
(so mpv parses the af string exactly as it does in the app), measures
integrated loudness and true peak with ffmpeg's ebur128, and prints the
loudness each effect takes away at each amount, averaged over the songs.

    python tools/fx_calibrate.py            # raw losses, level stage off
    python tools/fx_calibrate.py --check    # with the level stage on: the
                                            # deltas should sit near 0

Songs default to the site's CC tracks; pass paths to use others.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tide import audio_fx as A  # noqa: E402

AMOUNTS = (0.25, 0.5, 0.75, 1.0)


def render(song: str, chain: str, out: str) -> None:
    cmd = ["mpv", "--no-config", "--no-video", "--really-quiet", "--ao=pcm",
           f"--ao-pcm-file={out}", "--ao-pcm-waveheader=yes",
           "--start=40", "--length=30", "--audio-samplerate=48000", song]
    if chain:
        cmd.insert(1, f"--af={chain}")
    subprocess.run(cmd, check=True, timeout=300)


def measure(path: str) -> tuple[float, float]:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True).stderr
    i = float(re.findall(r"I:\s+(-?[\d.]+) LUFS", out)[-1])
    p = float(re.findall(r"Peak:\s+(-?[\d.]+) dBFS", out)[-1])
    return i, p


def run(song: str, chain: str) -> tuple[float, float]:
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "o.wav")
        render(song, chain, out)
        return measure(out)


def state(**kw) -> A.AudioFxState:
    s = A.AudioFxState(master_enabled=True)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def cases() -> dict[str, A.AudioFxState]:
    c: dict[str, A.AudioFxState] = {}
    for a in AMOUNTS:
        c[f"chorus {a}"] = state(chorus=True, chorus_amount=a)
        c[f"flanger {a}"] = state(flanger=True, flanger_amount=a)
        c[f"phaser {a}"] = state(phaser=True, phaser_amount=a)
        c[f"lofi {a}"] = state(lofi=True, lofi_amount=a)
        c[f"compressor {a}"] = state(compressor=True, compressor_amount=a)
        c[f"exciter {a}"] = state(exciter=True, exciter_amount=a * 5)
        c[f"crossfeed {a}"] = state(crossfeed=True, crossfeed_strength=a)
        c[f"eightd {a}"] = state(eightd=True, eightd_speed=a)
        for preset in ("room", "hall", "plate", "cathedral", "slowed"):
            c[f"reverb {preset} {a}"] = state(reverb_preset=preset, reverb_wet=a)
    c["tremolo 0.5"] = state(tremolo=True)
    for w in (0.5, 1.5, 2.0):
        c[f"width {w}"] = state(stereo_width=w)
    for name in A.SOUNDS:
        if name != "flat":
            s = state()
            s.apply_sound(name)
            s.master_enabled = True
            c[f"sound: {name}"] = s
    return c


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("songs", nargs="*")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    songs = args.songs or sorted(str(p) for p in (ROOT / "site" / "audio").glob("*.m4a"))
    if not args.check:
        A._LEVEL_TABLES.clear()
    jobs = {"bypass": ""}
    jobs.update({k: A.build_filter_chain(v) for k, v in cases().items()})
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        futs = {(name, song): pool.submit(run, song, chain)
                for name, chain in jobs.items() for song in songs}
        res = {k: f.result() for k, f in futs.items()}
    base = {song: res[("bypass", song)] for song in songs}
    print(f"{'case':24s} {'loss dB':>8s} {'max peak':>9s}")
    for name in jobs:
        if name == "bypass":
            continue
        losses = [base[s][0] - res[(name, s)][0] for s in songs]
        peaks = [res[(name, s)][1] for s in songs]
        print(f"{name:24s} {sum(losses) / len(losses):8.2f} {max(peaks):9.2f}")


if __name__ == "__main__":
    main()
