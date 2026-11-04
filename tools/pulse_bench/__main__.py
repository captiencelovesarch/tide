"""pulse bench: measure the beat map against drum truth, and retrain its
kick model. tools/pulse_lab.py draws what the analyzer did; this says how
right it was.

    python tools/pulse_bench render [--midi a.mid ...] [--voice speech.mp3]
        build the rendered set: generated genre arrangements and any midi
        files with a drum part, mixed and put through opus, with exact
        kick times. --voice adds a "hard" set with speech over the top.
    <venv>/bin/python tools/pulse_bench/refs.py SONGS... [--rendered]
        demucs drum stems and beat this beats for real songs (see refs.py
        for the venv). the only step that needs torch.
    python tools/pulse_bench refcheck
        the stem reference against the rendered set's exact truth.
    python tools/pulse_bench score SONGS... [--skip NAME ...] [--no-rendered]
        how the current analyzer does: kicks shown, pulses with no drum.
    python tools/pulse_bench crossfit SONGS... [--skip NAME ...]
        retrain on one half of the songs (plus the rendered set), score the
        other half, and swap: what a retrained model would do on music it
        never saw.
    python tools/pulse_bench train SONGS... [--skip NAME ...] [--write]
        fit on everything and print (or write) src/tide/kick_model.py.

SONGS are files or folders. results cache in ~/.cache/tide-pulse-bench
(TIDE_PULSE_BENCH moves it). the corpus stays out of git. --skip leaves out
songs whose stem reference can't be trusted: 808-driven songs (demucs files
808s as bass) and murky mixes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import common  # noqa: E402
import score  # noqa: E402
import truth  # noqa: E402
from tide import beat_map as bm  # noqa: E402


def rendered_dir():
    d = common.cache_dir() / "rendered"
    d.mkdir(exist_ok=True)
    return d


def rendered_items():
    """(name, pcm, truth) for every cached rendered song."""
    out = []
    for p in sorted(rendered_dir().glob("*.json")):
        npy = p.with_suffix(".npy")
        if npy.exists():
            out.append((p.stem, np.load(npy), json.loads(p.read_text())))
    return out


def real_items(paths, skip):
    out = []
    for p in common.collect(paths):
        if any(s.lower() in common.key(p).lower() for s in skip):
            continue
        mix = common.load(p)
        t = truth.truth_for(common.key(p), mix)
        if t is None:
            print(f"no reference for {common.key(p)}: run refs.py on it first", file=sys.stderr)
            continue
        out.append((p, mix, t))
    return out


def cmd_render(args):
    import rendered
    import synth
    sets = rendered.bench(args.midi) + (rendered.hard_bench(args.midi) if args.voice else [])
    for song in sets:
        npy = rendered_dir() / (song.name + ".npy")
        if not npy.exists():
            pcm = synth.render(song.events, song.seconds, song.style,
                               seed=zlib.crc32(song.name.encode()) % 1000, voice_path=args.voice)
            np.save(npy, synth.through_opus(pcm))
            print("rendered", song.name, flush=True)
        (rendered_dir() / (song.name + ".json")).write_text(json.dumps(
            dict(kicks=song.kicks, others=song.others, drumless=song.drumless)))


def cmd_refcheck(args):
    truth.check(rendered_items())


def cmd_score(args):
    rows = []
    items = [(common.key(p), mix, t) for p, mix, t in real_items(args.paths, args.skip)]
    if not args.no_rendered:
        items += rendered_items()
    for name, mix, t in items:
        r = score.score(bm.analyze(mix), t)
        rows.append(r)
        print(score.line(name, r), flush=True)
    print(score.total(rows))


def _features(items):
    import train
    out = []
    for name, mix, t in items:
        times, rows = train.candidates(mix)
        y, w = train.labels(times, t)
        out.append((name, mix, t, rows, y, w))
    return out


def cmd_crossfit(args):
    import train
    real = real_items(args.paths, args.skip)
    half = common.folds([p for p, _m, _t in real])
    feats = _features([(p, m, t) for p, m, t in real])
    extra = [(r, y, w) for _n, _m, _t, r, y, w in _features(rendered_items())]
    rows = []
    for held in (0, 1):
        fit_on = [(r, y, w) for p, _m, _t, r, y, w in feats if half[p] != held] + extra
        train.use(*train.fit(fit_on))
        for p, mix, t, *_rest in feats:
            if half[p] == held:
                r = score.score(bm.analyze(mix), t)
                rows.append(r)
                print(score.line(common.key(p), r), flush=True)
    print(score.total(rows))


def cmd_train(args):
    import train
    items = [(common.key(p), m, t) for p, m, t in real_items(args.paths, args.skip)] + rendered_items()
    weights, bias = train.fit([(r, y, w) for *_x, r, y, w in _features(items)])
    if args.write:
        train.write(weights, bias)
        print("wrote", train.MODEL_PATH)
    else:
        print(train.module_text(weights, bias))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--midi", nargs="*", default=[])
    r.add_argument("--voice")
    sub.add_parser("refcheck")
    for name in ("score", "crossfit", "train"):
        s = sub.add_parser(name)
        s.add_argument("paths", nargs="*")
        s.add_argument("--skip", nargs="*", default=[])
        if name == "score":
            s.add_argument("--no-rendered", action="store_true")
        if name == "train":
            s.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    {"render": cmd_render, "refcheck": cmd_refcheck, "score": cmd_score,
     "crossfit": cmd_crossfit, "train": cmd_train}[args.cmd](args)


if __name__ == "__main__":
    main()
