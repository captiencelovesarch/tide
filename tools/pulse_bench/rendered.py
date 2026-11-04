"""the rendered set: generated genre arrangements, plus any midi files
with a drum part, each rendered with exact drum truth. the "hard" set adds
a spoken voice over some of them and a drumless gliding-bass stretch."""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
import re

import numpy as np

import smf
from synth import Ev


@dataclass
class Song:
    name: str
    events: list
    seconds: float
    style: dict
    kicks: list          # (t, vel)
    others: list         # snare / clap / tom times: a pulse there is tolerated
    drumless: list       # (start, end) stretches with no drums


# chord roots for a four-bar loop, minor-ish
PROG = [(45, [57, 60, 64]), (41, [53, 57, 60]), (48, [55, 60, 64]), (43, [55, 59, 62])]


def _grid(bpm, bars, steps=16, swing=0.0):
    """time of 16th step s in bar b. swing delays the odd 16ths."""
    sixteenth = 60.0 / bpm / 4

    def at(bar, step):
        t = (bar * steps + step) * sixteenth
        if step % 2 == 1:
            t += swing * sixteenth
        return t
    return at, sixteenth


def build_pattern_song(name, bpm, sections, style, *, seed=0, swing=0.0, human_ms=0.0,
                       drift=0.0):
    """sections: list of (bars, dict(kick=steps, snare=steps, clap=steps,
    hat=steps, ohat=steps, bass=list of (step, dur_steps) or 'held',
    chords=True/False, ghost=steps, fill=bool, kick_alt=steps for odd bars))."""
    rng = np.random.default_rng(seed)
    ev = []
    kicks, others, drumless = [], [], []
    bar0 = 0
    sixteenth = 60.0 / bpm / 4

    def t_of(bar, step):
        base = (bar * 16 + step) * sixteenth
        if step % 2 == 1:
            base += swing * sixteenth
        if drift:
            # slow tempo wander, like a live band: +-drift fraction
            base += drift * 60.0 / bpm * 4 * math.sin(2 * math.pi * bar / 24.0) * 2
        return base

    for bars, sec in sections:
        has_drums = any(sec.get(k) for k in ("kick", "snare", "clap", "hat", "ohat"))
        if not has_drums:
            drumless.append((t_of(bar0, 0), t_of(bar0 + bars, 0)))
        for b in range(bar0, bar0 + bars):
            root, chord = PROG[(b // 1) % 4]
            jitter = lambda: rng.normal(0, human_ms / 1000.0) if human_ms else 0.0
            ksteps = sec.get("kick", [])
            if sec.get("kick_alt") and (b - bar0) % 2 == 1:
                ksteps = sec["kick_alt"]
            fill = sec.get("fill") and (b - bar0) % 8 == 7
            for s in ksteps:
                if fill and s >= 8:
                    continue
                t = t_of(b, s) + jitter()
                v = float(np.clip(rng.normal(0.9, 0.06), 0.6, 1.0))
                ev.append(Ev(t, "kick", v, pitch=root - 12 if style.get("kick") == "808" else 0))
                kicks.append((t, v))
            for s in sec.get("ghost", []):
                t = t_of(b, s) + jitter()
                v = 0.3
                ev.append(Ev(t, "kick", v))
                kicks.append((t, v))
            for kind in ("snare", "clap"):
                for s in sec.get(kind, []):
                    if fill and s >= 8:
                        continue
                    t = t_of(b, s) + jitter()
                    ev.append(Ev(t, kind, float(np.clip(rng.normal(0.85, 0.08), 0.5, 1))))
                    others.append(t)
            for kind in ("hat", "ohat"):
                for s in sec.get(kind, []):
                    ev.append(Ev(t_of(b, s) + jitter(), kind, float(np.clip(rng.normal(0.7, 0.15), 0.2, 1))))
            if fill:
                for k, s in enumerate(range(8, 16, 2)):
                    t = t_of(b, s) + jitter()
                    ev.append(Ev(t, "tom", 0.85, extra={"hz": [180, 140, 110, 85][k]}))
                    others.append(t)
            bassdef = sec.get("bass")
            if bassdef == "held":
                ev.append(Ev(t_of(b, 0), "bass", 0.9, pitch=root - 12, dur=16 * sixteenth * 0.98))
            elif bassdef:
                for s, d in bassdef:
                    ev.append(Ev(t_of(b, s) + jitter(), "bass", float(np.clip(rng.normal(0.85, 0.08), 0.5, 1)),
                                 pitch=root - 12, dur=d * sixteenth * 0.92))
            if sec.get("chords"):
                cs = sec.get("chord_steps", [0])
                for s in cs:
                    ln = sec.get("chord_len", 16 // len(cs))
                    ev.append(Ev(t_of(b, s), "chord", 0.8, dur=ln * sixteenth * 0.95,
                                 extra={"pitches": chord}))
            if sec.get("lead"):
                for s in range(0, 16, 4):
                    ev.append(Ev(t_of(b, s), "lead", 0.7, pitch=chord[(s // 4) % 3] + 12, dur=3.5 * sixteenth))
        bar0 += bars
    seconds = t_of(bar0, 0) + 2.0
    return Song(name, sorted(ev, key=lambda e: e.t), seconds, style, kicks, others, drumless)


def bench(midi=()) -> list[Song]:
    out = []
    eighths_off = [(s, 2) for s in (2, 6, 10, 14)]
    out.append(build_pattern_song(
        "house-124", 124, [
            (4, dict(chords=True, chord_steps=[0], bass="held")),
            (16, dict(kick=[0, 4, 8, 12], clap=[4, 12], ohat=[2, 6, 10, 14], bass=eighths_off,
                      chords=True, chord_steps=[2, 6, 10, 14], chord_len=2)),
            (8, dict(chords=True, chord_steps=[0], bass="held", hat=[2, 6, 10, 14])),
            (16, dict(kick=[0, 4, 8, 12], clap=[4, 12], ohat=[2, 6, 10, 14], bass=eighths_off,
                      chords=True, chord_steps=[2, 6, 10, 14], chord_len=2)),
        ], dict(kick="house", bass="sub", chord="piano", sidechain=True, compress=4, limit=True),
        seed=1))
    out.append(build_pattern_song(
        "trap-140", 140, [
            (4, dict(chords=True, chord_steps=[0], hat=list(range(0, 16, 2)))),
            (16, dict(kick=[0, 6, 10], kick_alt=[0, 3, 7, 11, 14], clap=[8], hat=list(range(0, 16, 2)),
                      chords=True, chord_steps=[0])),
            (4, dict(chords=True, chord_steps=[0])),
            (16, dict(kick=[0, 6, 10], kick_alt=[0, 3, 7, 11, 14], clap=[8], hat=list(range(16)),
                      chords=True, chord_steps=[0])),
        ], dict(kick="808", chord="pad", compress=3, limit=True), seed=2))
    out.append(build_pattern_song(
        "rock-120", 120, [
            (4, dict(chords=True, chord_steps=[0, 4, 8, 12], bass=[(0, 8), (8, 8)])),
            (16, dict(kick=[0, 8, 10], kick_alt=[0, 7, 8], snare=[4, 12], hat=list(range(0, 16, 2)),
                      bass=[(s, 2) for s in range(0, 16, 2)], chords=True, chord_steps=[0, 4, 8, 12],
                      fill=True)),
            (16, dict(kick=[0, 6, 8, 14], snare=[4, 12], ohat=list(range(0, 16, 2)),
                      bass=[(s, 2) for s in range(0, 16, 2)], chords=True, chord_steps=[0, 4, 8, 12],
                      fill=True)),
        ], dict(kick="acoustic", bass="picked", chord="strum", reverb=1.2, wet=0.2, compress=3),
        seed=3, human_ms=8, drift=0.004))
    out.append(build_pattern_song(
        "dnb-174", 174, [
            (8, dict(chords=True, chord_steps=[0], bass="held")),
            (24, dict(kick=[0, 10], snare=[4, 12], hat=list(range(0, 16, 2)), bass=[(0, 8), (8, 8)],
                      chords=True, chord_steps=[0])),
            (8, dict(chords=True, chord_steps=[0], bass="held")),
            (24, dict(kick=[0, 10], kick_alt=[0, 7, 10], snare=[4, 12], hat=list(range(16)),
                      bass=[(0, 8), (8, 8)], chords=True, chord_steps=[0])),
        ], dict(kick="punch", bass="reese", chord="pad", compress=4, limit=True), seed=4))
    out.append(build_pattern_song(
        "ballad-72", 72, [
            (6, dict(chords=True, chord_steps=[0, 4, 8, 12], bass=[(0, 8), (8, 8)])),
            (12, dict(kick=[0, 8], kick_alt=[0, 6, 8], snare=[4, 12], hat=list(range(0, 16, 2)),
                      bass=[(0, 6), (6, 2), (8, 8)], chords=True, chord_steps=[0, 4, 8, 12])),
            (4, dict(chords=True, chord_steps=[0, 4, 8, 12], bass=[(0, 8), (8, 8)])),
            (8, dict(kick=[0, 8], snare=[4, 12], hat=list(range(0, 16, 2)),
                     bass=[(0, 6), (6, 2), (8, 8)], chords=True, chord_steps=[0, 4, 8, 12])),
        ], dict(kick="punch", bass="picked", chord="piano", reverb=2.0, wet=0.3, compress=2),
        seed=5, human_ms=6))
    out.append(build_pattern_song(
        "pop-100-swing", 100, [
            (16, dict(kick=[0, 6, 8], kick_alt=[0, 3, 8, 14], ghost=[11], snare=[4, 12],
                      hat=list(range(16)), bass=[(0, 3), (3, 3), (7, 2), (10, 4), (14, 2)],
                      chords=True, chord_steps=[0, 8])),
            (16, dict(kick=[0, 6, 8], kick_alt=[0, 3, 8, 14], ghost=[11], snare=[4, 12],
                      hat=list(range(16)), bass=[(0, 3), (3, 3), (7, 2), (10, 4), (14, 2)],
                      chords=True, chord_steps=[0, 8], lead=True)),
        ], dict(kick="punch", bass="sub", chord="pad", compress=4, limit=True), seed=6, swing=0.35))
    out.append(build_pattern_song(
        "dubstep-140", 140, [
            (8, dict(chords=True, chord_steps=[0], bass="held", hat=list(range(0, 16, 4)))),
            (16, dict(kick=[0], kick_alt=[0, 10], snare=[8], hat=list(range(0, 16, 2)),
                      bass=[(0, 6), (6, 4), (10, 6)])),
        ], dict(kick="punch", bass="wobble", chord="pad", compress=5, limit=True), seed=7))
    out.append(build_pattern_song(
        "folk-90-nodrums", 90, [
            (24, dict(chords=True, chord_steps=[0, 4, 8, 12], bass=[(0, 4), (8, 4)])),
        ], dict(bass="picked", chord="strum", reverb=1.5, wet=0.25), seed=8, human_ms=10))
    out.append(build_pattern_song(
        "funk-105", 105, [
            (24, dict(kick=[0, 3, 7, 10], kick_alt=[0, 6, 10, 14], ghost=[13], snare=[4, 12],
                      hat=list(range(16)), bass=[(0, 2), (3, 1), (5, 2), (7, 1), (10, 2), (12, 1), (14, 2)],
                      chords=True, chord_steps=[2, 6, 10, 14], chord_len=1)),
        ], dict(kick="acoustic", bass="picked", chord="strum", compress=3), seed=9, human_ms=7))
    out.append(build_pattern_song(
        "techno-132-rumble", 132, [
            (8, dict(kick=[0, 4, 8, 12], hat=[2, 6, 10, 14], bass=[(s, 1) for s in (1, 2, 3, 5, 6, 7, 9, 10, 11, 13, 14, 15)])),
            (8, dict(hat=[2, 6, 10, 14], bass="held", chords=True, chord_steps=[0])),
            (16, dict(kick=[0, 4, 8, 12], clap=[4, 12], hat=list(range(0, 16, 2)),
                      bass=[(s, 1) for s in (1, 2, 3, 5, 6, 7, 9, 10, 11, 13, 14, 15)])),
        ], dict(kick="house", bass="sub", chord="pad", reverb=0.8, wet=0.4, compress=6, limit=True,
                bass_gain=0.4), seed=10))
    out.append(build_pattern_song(
        "punk-168", 168, [
            (32, dict(kick=[0, 4, 8, 12], kick_alt=[0, 2, 8, 10], snare=[4, 12], ohat=list(range(0, 16, 2)),
                      bass=[(s, 2) for s in range(0, 16, 2)], chords=True, chord_steps=list(range(0, 16, 2)),
                      chord_len=2, fill=True)),
        ], dict(kick="acoustic", bass="picked", chord="strum", compress=4, limit=True), seed=11, human_ms=6))
    out.append(build_pattern_song(
        "lofi-84", 84, [
            (4, dict(chords=True, chord_steps=[0, 8], bass=[(0, 8), (8, 8)])),
            (16, dict(kick=[0, 7, 10], snare=[4, 12], hat=list(range(0, 16, 2)),
                      bass=[(0, 7), (7, 3), (10, 6)], chords=True, chord_steps=[0, 8])),
        ], dict(kick="punch", bass="sub", chord="piano", reverb=1.0, wet=0.3, sidechain=True, compress=3),
        seed=12, swing=0.3, human_ms=9))
    out.extend(midi_songs(midi))
    return out


def hard_bench(midi=()) -> list[Song]:
    """the same kinds of songs, with a voice over them and a drumless
    gliding-bass stretch: what real mixes add that the plain set lacks."""
    out = []
    for song in bench(midi):
        if song.name in ("house-124", "trap-140", "rock-120", "pop-100-swing", "dnb-174",
                         "folk-90-nodrums") or song.name.endswith("-rock") and song.name.startswith("midi-"):
            st = dict(song.style)
            st["vox"] = 0.0
            out.append(Song(song.name + "+vox", song.events, song.seconds, st, song.kicks,
                            song.others, song.drumless))
    out.append(glide_song())
    out.append(glide_song(vox=True))
    return out


def glide_song(vox=False):
    """drumless gliding synth bass and pads for 12 bars, then a beat comes in."""
    bpm = 88
    beat = 60.0 / bpm
    ev, kicks, others = [], [], []
    roots = [33, 36, 31, 38, 33, 40, 36, 31]
    t = 0.0
    notes = []
    for bar in range(12):
        for k, s in enumerate((0, 1.5, 2.5, 3)):
            notes.append((bar * 4 * beat + s * beat, roots[(bar * 2 + k) % len(roots)]))
    ev.append(Ev(0.0, "glide", 0.9, dur=12 * 4 * beat, extra={"notes": notes}))
    for bar in range(12):
        ev.append(Ev(bar * 4 * beat, "chord", 0.7, dur=4 * beat * 0.98, extra={"pitches": PROG[bar % 4][1]}))
    drumless = [(0.0, 12 * 4 * beat)]
    t0 = 12 * 4 * beat
    rng = np.random.default_rng(13)
    notes2 = []
    for bar in range(12):
        b0 = t0 + bar * 4 * beat
        for s in (0, 1.75, 2.5):
            v = float(np.clip(rng.normal(0.9, 0.05), 0.7, 1))
            ev.append(Ev(b0 + s * beat, "kick", v))
            kicks.append((b0 + s * beat, v))
        for s in (1, 3):
            ev.append(Ev(b0 + s * beat, "snare", 0.85))
            others.append(b0 + s * beat)
        for s in np.arange(0, 4, 0.5):
            ev.append(Ev(b0 + s * beat, "hat", 0.6))
        for k, s in enumerate((0, 1.5, 2.5, 3)):
            notes2.append((bar * 4 * beat + s * beat, roots[(bar * 2 + k) % len(roots)]))
        ev.append(Ev(b0, "chord", 0.7, dur=4 * beat * 0.98, extra={"pitches": PROG[bar % 4][1]}))
    ev.append(Ev(t0, "glide", 0.9, dur=12 * 4 * beat, extra={"notes": notes2}))
    style = dict(kick="punch", chord="pad", compress=3, limit=True)
    if vox:
        style["vox"] = 0.0
    return Song("glide-88" + ("+vox" if vox else ""), sorted(ev, key=lambda e: e.t), t0 + 12 * 4 * beat + 2,
                style, kicks, others, drumless)


def midi_song(path, name, style, drum_map=None):
    notes, _t, _d = smf.read(path)
    ev, kicks, others = [], [], []
    for n in notes:
        v = n.velocity / 127.0
        if n.channel == 9:
            if n.pitch in (35, 36):
                ev.append(Ev(n.start, "kick", v))
                kicks.append((n.start, v))
            elif n.pitch in (38, 40):
                ev.append(Ev(n.start, "snare", v))
                others.append(n.start)
            elif n.pitch in (39,):
                ev.append(Ev(n.start, "clap", v))
                others.append(n.start)
            elif n.pitch in (41, 43, 45, 47, 48, 50):
                ev.append(Ev(n.start, "tom", v, extra={"hz": 70 + (n.pitch - 41) * 15}))
                others.append(n.start)
            elif n.pitch in (42, 44, 46, 51, 53, 54, 59):
                ev.append(Ev(n.start, "ohat" if n.pitch in (46,) else "hat", v * 0.8))
            elif n.pitch in (49, 52, 55, 57):
                ev.append(Ev(n.start, "crash", v))
        elif 32 <= n.program <= 39:
            ev.append(Ev(n.start, "bass", v, pitch=n.pitch, dur=max(0.05, n.end - n.start)))
        elif n.program < 8 or 24 <= n.program <= 31:
            ev.append(Ev(n.start, "chord", v * 0.6, dur=max(0.05, n.end - n.start), extra={"pitches": [n.pitch]}))
        elif 80 <= n.program <= 103:
            ev.append(Ev(n.start, "lead", v * 0.7, pitch=n.pitch, dur=max(0.05, n.end - n.start)))
    seconds = max(n.end for n in notes) + 2.0
    # drumless stretches: gaps of more than two bars without any drum note
    dts = sorted(n.start for n in notes if n.channel == 9)
    drumless = []
    prev = 0.0
    for t in dts + [seconds]:
        if t - prev > 4.0:
            drumless.append((prev + 0.5, t - 0.1))
        prev = t
    return Song(name, sorted(ev, key=lambda e: e.t), seconds, style, kicks, others, drumless)


MIDI_STYLES = (
    ("pop", dict(kick="punch", bass="picked", chord="piano", compress=3, limit=True)),
    ("rock", dict(kick="acoustic", bass="picked", chord="strum", reverb=1.4, wet=0.25, compress=2)),
)


def midi_songs(paths):
    """midi files with a kick drum in them, styled in turn."""
    out = []
    for path in paths:
        notes, _t, _d = smf.read(path)
        if not any(n.channel == 9 and n.pitch in (35, 36) for n in notes):
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", os.path.splitext(os.path.basename(path))[0].lower()).strip("-")[:24]
        label, style = MIDI_STYLES[len(out) % len(MIDI_STYLES)]
        out.append(midi_song(path, f"midi-{slug}-{label}", style))
    return out
