"""reference material for the bench, run in a research venv with torch:
a drum stem from demucs (htdemucs) and beats from beat this (final0).

    uv venv -p 3.13 ~/.cache/tide-pulse-bench/venv
    VIRTUAL_ENV=~/.cache/tide-pulse-bench/venv uv pip install torch torchaudio demucs soxr scipy \\
        "beat_this @ https://github.com/CPJKU/beat_this/archive/main.zip"
    ~/.cache/tide-pulse-bench/venv/bin/python tools/pulse_bench/refs.py ~/Music/some-songs [--rendered]

writes <cache>/ref/<name>.drums.npy (22.05 khz mono) and <name>.beats.json.
about 0.4x realtime on a 12-core cpu. --rendered also does the rendered set
(cached by `pulse_bench render`), so `pulse_bench refcheck` can measure the
reference against exact truth. nothing here ships with tide.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import common  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--rendered", action="store_true")
    args = ap.parse_args(argv)
    import soxr
    import torch
    from beat_this.inference import Audio2Beats
    from demucs.apply import apply_model
    from demucs.pretrained import get_model
    torch.set_num_threads(os.cpu_count() or 4)
    sep = get_model("htdemucs")
    sep.eval()
    drums_i = sep.sources.index("drums")
    tracker = Audio2Beats(checkpoint_path="final0", device="cpu", dbn=False)
    items = [(common.key(p), p, None) for p in common.collect(args.paths)]
    if args.rendered:
        rd = common.cache_dir() / "rendered"
        items += [(p.stem, None, p) for p in sorted(rd.glob("*.npy"))]
    for name, path, npy in items:
        dst = common.ref_dir() / (name + ".drums.npy")
        bj = common.ref_dir() / (name + ".beats.json")
        if dst.exists() and bj.exists():
            continue
        t0 = time.monotonic()
        if npy is not None:
            up = soxr.resample(np.load(npy).astype(np.float32), common.SR, 44100).astype(np.float32)
            wav = np.stack((up, up))
        else:
            wav = common.decode(path, 44100, channels=2)
        x = torch.from_numpy(np.ascontiguousarray(wav))
        ref = x.mean(0)
        mu, sd = ref.mean(), ref.std() + 1e-8
        with torch.inference_mode():
            out = apply_model(sep, ((x - mu) / sd)[None], shifts=1, split=True, overlap=0.25,
                              progress=False, device="cpu")[0]
        drums = (out[drums_i] * sd + mu).mean(0).numpy()
        np.save(dst, soxr.resample(drums, 44100, common.SR).astype(np.float32))
        beats, downbeats = tracker(wav.mean(0), 44100)
        bj.write_text(json.dumps(dict(beats=[float(b) for b in beats],
                                      downbeats=[float(b) for b in downbeats])))
        print(f"{name[:50]:50s} {wav.shape[1] / 44100:6.1f}s audio  {time.monotonic() - t0:6.1f}s", flush=True)


if __name__ == "__main__":
    main()
