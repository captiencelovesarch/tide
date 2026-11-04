"""minimal standard midi file reader: notes with absolute seconds."""
from __future__ import annotations

from dataclasses import dataclass
import struct


@dataclass
class Note:
    start: float
    end: float
    channel: int
    pitch: int
    velocity: int
    program: int


def _vlq(data: bytes, i: int) -> tuple[int, int]:
    v = 0
    while True:
        b = data[i]
        i += 1
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            return v, i


def read(path: str) -> tuple[list[Note], list[tuple[int, int]], int]:
    """notes, tempo map [(tick, usec_per_quarter)], ticks per quarter."""
    data = open(path, "rb").read()
    if data[:4] != b"MThd":
        raise ValueError("not a midi file")
    hlen = struct.unpack(">I", data[4:8])[0]
    fmt, ntrk, div = struct.unpack(">HHH", data[8:14])
    if div & 0x8000:
        raise ValueError("smpte timing unsupported")
    pos = 8 + hlen
    events = []  # (tick, order, kind, payload)
    order = 0
    for _ in range(ntrk):
        if data[pos:pos + 4] != b"MTrk":
            break
        tlen = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        trk = data[pos + 8:pos + 8 + tlen]
        pos += 8 + tlen
        i = 0
        tick = 0
        running = None
        while i < len(trk):
            dt, i = _vlq(trk, i)
            tick += dt
            b = trk[i]
            if b == 0xFF:
                typ = trk[i + 1]
                ln, j = _vlq(trk, i + 2)
                payload = trk[j:j + ln]
                i = j + ln
                if typ == 0x51:
                    events.append((tick, order, "tempo", int.from_bytes(payload, "big")))
                    order += 1
                continue
            if b in (0xF0, 0xF7):
                ln, j = _vlq(trk, i + 1)
                i = j + ln
                continue
            if b & 0x80:
                status = b
                i += 1
                running = status
            else:
                status = running
            hi = status & 0xF0
            ch = status & 0x0F
            if hi in (0xC0, 0xD0):
                a = trk[i]
                i += 1
                if hi == 0xC0:
                    events.append((tick, order, "prog", (ch, a)))
                    order += 1
                continue
            a, c = trk[i], trk[i + 1]
            i += 2
            if hi == 0x90 and c > 0:
                events.append((tick, order, "on", (ch, a, c)))
            elif hi == 0x80 or (hi == 0x90 and c == 0):
                events.append((tick, order, "off", (ch, a)))
            order += 1
    events.sort(key=lambda e: (e[0], e[1]))
    tempos = [(0, 500000)]
    for tick, _o, kind, p in events:
        if kind == "tempo":
            if tick == tempos[-1][0]:
                tempos[-1] = (tick, p)
            else:
                tempos.append((tick, p))

    def seconds(tick: int) -> float:
        s = 0.0
        for k, (t0, us) in enumerate(tempos):
            t1 = tempos[k + 1][0] if k + 1 < len(tempos) else None
            if t1 is None or tick < t1:
                return s + (tick - t0) * us / 1e6 / div
            s += (t1 - t0) * us / 1e6 / div
        return s

    programs = [0] * 16
    active: dict[tuple[int, int], list[tuple[float, int, int]]] = {}
    notes: list[Note] = []
    for tick, _o, kind, p in events:
        if kind == "prog":
            programs[p[0]] = p[1]
        elif kind == "on":
            ch, pitch, vel = p
            active.setdefault((ch, pitch), []).append((seconds(tick), vel, programs[ch]))
        elif kind == "off":
            ch, pitch = p
            stack = active.get((ch, pitch))
            if stack:
                st, vel, prog = stack.pop(0)
                notes.append(Note(st, seconds(tick), ch, pitch, vel, prog))
    for (ch, pitch), stack in active.items():
        for st, vel, prog in stack:
            notes.append(Note(st, st + 0.5, ch, pitch, vel, prog))
    notes.sort(key=lambda n: n.start)
    return notes, tempos, div


def beat_times(path: str) -> list[float]:
    """quarter-note times from the tempo map, up to the last note."""
    notes, tempos, div = read(path)
    if not notes:
        return []
    end = max(n.end for n in notes)
    out = []
    tick = 0
    s = 0.0
    k = 0
    while s <= end:
        out.append(s)
        us = tempos[k][1]
        nxt = tick + div
        while k + 1 < len(tempos) and tempos[k + 1][0] <= tick:
            k += 1
            us = tempos[k][1]
        s += us / 1e6
        tick = nxt
        while k + 1 < len(tempos) and tempos[k + 1][0] <= tick:
            k += 1
    return out
