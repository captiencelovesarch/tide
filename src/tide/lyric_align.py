"""Local word alignment: the song's own audio against its lyric text.

"precise timing" in the lyrics settings. A small speech model
(wav2vec2-base-960h, Apache-2.0, int8 ONNX, ~95 MB) turns the audio into
per-frame letter probabilities; CTC forced alignment then finds where
each word of a KNOWN line falls inside that line's window. That gives
word starts and ends, and the first word's start re-times the line itself,
so a sheet that runs early or late (LRClib's sync often belongs to a
different cut of the song) gets pulled onto the audio.

Optional on two counts: ``onnxruntime`` (python-onnxruntime-cpu, in the
official repos) and the model, which the settings dialog downloads on
request into ``DATA_DIR/models``. Without either, ``available()`` is
False and lyrics fall back to source word timing or line mode.

Measured on five songs against NetEase's human word timing (Oct 2026),
with NetEase itself off: on sheets already in time, median word error
0.26 s -> 0.18 s and words within 150 ms 35% -> 45%, no song worse; a
sheet shifted 1 s late was pulled back onto the audio on 3 of 5 songs
(to 0.11-0.25 s median) and left alone on the other two, where the model
can't hear the vocal through the band (Let Down, Mr. Brightside). A
speech model on a full mix is the limit; there is no source separation.

The model is English-trained and knows A–Z and the apostrophe. Lines with
too few alignable letters (other scripts, "ooh-ooh" ad-libs written as
symbols) are left untouched, and so is any word whose alignment looks
unsure: line mode handles those.

Pure numpy + subprocess + onnxruntime (lazy). No Qt.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import unicodedata
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from . import config

SR = 16_000
FRAME_S = 0.02                  # the model's output stride (320 samples)
FRAME_OFFSET_S = 0.0125         # half the conv receptive field
CHUNK_S = 20.0                  # emission chunk; attention is O(n²)
CHUNK_PAD_S = 1.5               # context each side of a chunk, discarded
LINE_PAD_S = 1.25               # search room around a line's window
MAX_SECONDS = 15 * 60
THREADS = 2                     # leave the rest of the CPU to the game

MODEL_NAME = "wav2vec2-base-960h-int8"
MODEL_REVISION = "729c1a6730fb549c20a1c73a3d3f96f11020225e"
MODEL_URL = ("https://huggingface.co/onnx-community/wav2vec2-base-960h-ONNX/"
             f"resolve/{MODEL_REVISION}/onnx/model_int8.onnx")
MODEL_SHA256 = "2aa77535eea4282df0374ff011c330e7abd84010f5d0cc079e5ed6c8ede2b6c5"
MODEL_BYTES = 95_212_776
# The model's output vocabulary (vocab.json at the pinned revision).
# <pad> is the CTC blank.
BLANK = 0
VOCAB: dict[str, int] = {
    "|": 4, "E": 5, "T": 6, "A": 7, "O": 8, "N": 9, "I": 10, "H": 11,
    "S": 12, "R": 13, "D": 14, "L": 15, "U": 16, "M": 17, "W": 18, "C": 19,
    "F": 20, "G": 21, "Y": 22, "P": 23, "B": 24, "V": 25, "K": 26, "'": 27,
    "X": 28, "J": 29, "Q": 30, "Z": 31,
}
# A word whose letters averaged less than this probability on the frames
# the path gave them is a guess, not a match.
MIN_WORD_CONF = 0.12
# A line needs this share of its words confidently placed to count.
MIN_LINE_SHARE = 0.6


# ---------- model files ----------

def model_dir() -> Path:
    return config.DATA_DIR / "models" / MODEL_NAME


def model_path() -> Path:
    return model_dir() / "model.onnx"


def runtime_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except Exception:
        return False
    return True


def model_installed() -> bool:
    p = model_path()
    try:
        return p.is_file() and p.stat().st_size == MODEL_BYTES
    except OSError:
        return False


def available() -> bool:
    return runtime_available() and model_installed()


def download_model(progress: Callable[[int, int], None] | None = None,
                   should_stop: Callable[[], bool] | None = None,
                   url: str = MODEL_URL) -> None:
    """Fetch the pinned model into ``model_dir()``, verifying its SHA-256.
    Raises on failure; a partial file never takes the final name."""
    target = model_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".part")
    digest = hashlib.sha256()
    done = 0
    req = urllib.request.Request(url, headers={"User-Agent": "tide"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as f:
            while True:
                if should_stop is not None and should_stop():
                    raise InterruptedError("cancelled")
                block = resp.read(1 << 16)
                if not block:
                    break
                done += len(block)
                if done > MODEL_BYTES:
                    raise ValueError("model is larger than expected")
                digest.update(block)
                f.write(block)
                if progress is not None:
                    progress(done, MODEL_BYTES)
        if done != MODEL_BYTES or digest.hexdigest() != MODEL_SHA256:
            raise ValueError("model checksum mismatch")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    _reset_session()


def remove_model() -> None:
    _reset_session()
    try:
        model_path().unlink(missing_ok=True)
    except OSError:
        pass


_session = None
_session_lock = threading.Lock()


def _reset_session() -> None:
    global _session
    with _session_lock:
        _session = None


def _get_session():
    global _session
    with _session_lock:
        if _session is None:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = THREADS
            opts.inter_op_num_threads = 1
            opts.log_severity_level = 3
            _session = ort.InferenceSession(
                str(model_path()), sess_options=opts,
                providers=["CPUExecutionProvider"])
        return _session


# ---------- audio ----------

def decode(target: str, headers: dict | None = None, *,
           max_seconds: float = MAX_SECONDS,
           should_stop: Callable[[], bool] | None = None) -> np.ndarray | None:
    """The whole track as stereo float32 at 16 kHz, shape (n, 2), or None.
    Stereo because the vocal lives in the centre (see :func:`vocals`).
    beat_map's own decode is mono at 22.05 kHz and its maps are cached,
    so a re-listen never decodes there: this is its own pass, run once
    per track (the result is cached by lyric_timing)."""
    from .beat_map import is_analyzable
    if not is_analyzable(target):
        return None
    argv = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if target.lower().startswith(("http://", "https://")):
        argv += ["-reconnect", "1", "-reconnect_streamed", "1",
                 "-reconnect_delay_max", "4"]
        if headers:
            argv += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    argv += ["-i", target, "-vn", "-sn", "-dn", "-map", "0:a:0",
             "-ac", "2", "-ar", str(SR), "-f", "f32le",
             "-t", f"{max_seconds:.3f}", "pipe:1"]
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    parts: list[bytes] = []
    try:
        assert proc.stdout is not None
        while True:
            if should_stop is not None and should_stop():
                return None
            data = proc.stdout.read(SR * 4 * 4)
            if not data:
                break
            parts.append(data)
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=2.0)
        except Exception:
            pass
    raw = b"".join(parts)
    raw = raw[:len(raw) - len(raw) % 8]
    if not raw:
        return None
    return np.nan_to_num(np.frombuffer(raw, dtype=np.float32)).reshape(-1, 2)


# ---------- vocal emphasis ----------

_N_FFT = 1024
_HOP = 256
_WIN = np.hanning(_N_FFT + 1)[:-1].astype(np.float32)
VOCAL_BAND_HZ = (120.0, 7000.0)


def _stft(x: np.ndarray) -> np.ndarray:
    pad = np.pad(x, (_N_FFT // 2, _N_FFT // 2))
    n = max(1, (len(pad) - _N_FFT) // _HOP + 1)
    idx = np.arange(_N_FFT)[None, :] + _HOP * np.arange(n)[:, None]
    return np.fft.rfft(pad[idx] * _WIN, axis=1)


def _istft(spec: np.ndarray, length: int) -> np.ndarray:
    frames = np.fft.irfft(spec, n=_N_FFT, axis=1) * _WIN
    total = _N_FFT + _HOP * (len(spec) - 1)
    out = np.zeros(total, dtype=np.float64)
    norm = np.zeros(total, dtype=np.float64)
    w2 = _WIN.astype(np.float64) ** 2
    for i in range(len(spec)):
        out[i * _HOP:i * _HOP + _N_FFT] += frames[i]
        norm[i * _HOP:i * _HOP + _N_FFT] += w2
    out /= np.maximum(norm, 1e-6)
    return out[_N_FFT // 2:_N_FFT // 2 + length].astype(np.float32)


def vocals(stereo: np.ndarray, block_s: float = 30.0) -> np.ndarray:
    """Centre-channel extraction: keep the time-frequency cells where left
    and right agree (lead vocals are mixed to the centre; most of the band
    is spread across the stereo field), band-limited to the voice. Mono
    input passes through band-limited. On five test songs this lifted the
    share of words placed within 150 ms from 38% to 41%; drum-removal
    (HPSS) on top of it made things worse, so it isn't here."""
    x = np.asarray(stereo, dtype=np.float32)
    if x.ndim == 1:
        x = np.stack((x, x), axis=1)
    freqs = np.fft.rfftfreq(_N_FFT, 1.0 / SR)
    band = ((freqs >= VOCAL_BAND_HZ[0]) & (freqs <= VOCAL_BAND_HZ[1])).astype(np.float32)
    out = np.zeros(len(x), dtype=np.float32)
    step = int(block_s * SR)
    edge = _N_FFT
    for a in range(0, len(x), step):
        lo, hi = max(0, a - edge), min(len(x), a + step + edge)
        left, right = _stft(x[lo:hi, 0]), _stft(x[lo:hi, 1])
        mid = (left + right) * 0.5
        psi = 2.0 * np.abs(left * np.conj(right)) / (
            np.abs(left) ** 2 + np.abs(right) ** 2 + 1e-9)
        y = _istft(mid * (psi ** 2) * band[None, :], hi - lo)
        out[a:min(len(x), a + step)] = y[a - lo:a - lo + min(step, len(x) - a)]
    return out


# ---------- emissions ----------

def _log_softmax(x: np.ndarray) -> np.ndarray:
    m = x.max(axis=-1, keepdims=True)
    y = x - m
    return y - np.log(np.exp(y).sum(axis=-1, keepdims=True))


class Emissions:
    """Per-frame log-probabilities over the vocabulary for a stretch of
    audio, computed lazily by chunk so only the sung parts pay."""

    def __init__(self, pcm: np.ndarray, run: Callable[[np.ndarray], np.ndarray] | None = None) -> None:
        self.pcm = np.asarray(pcm, dtype=np.float32)
        self.frames = max(0, int(len(self.pcm) / (SR * FRAME_S)))
        self._run = run or _run_model
        self._chunks: dict[int, np.ndarray] = {}

    def _chunk(self, i: int) -> np.ndarray:
        got = self._chunks.get(i)
        if got is not None:
            return got
        per = int(round(CHUNK_S / FRAME_S))
        lo = i * CHUNK_S - CHUNK_PAD_S
        hi = (i + 1) * CHUNK_S + CHUNK_PAD_S
        a = max(0, int(lo * SR))
        b = min(len(self.pcm), int(hi * SR))
        seg = self.pcm[a:b]
        if seg.size < SR // 10:
            out = np.full((per, len(VOCAB) + 4), -1e4, dtype=np.float32)
            out[:, BLANK] = 0.0
        else:
            logits = self._run(seg)
            logp = _log_softmax(logits.astype(np.float32))
            # frame f of this run sits at a/SR + f*FRAME_S; keep the ones
            # inside this chunk's own span
            skip = int(round((i * CHUNK_S - a / SR) / FRAME_S))
            out = logp[skip:skip + per]
            if out.shape[0] < per:
                pad = np.full((per - out.shape[0], logp.shape[1]), -1e4, dtype=np.float32)
                pad[:, BLANK] = 0.0
                out = np.vstack((out, pad))
        self._chunks[i] = out
        return out

    def window(self, start_s: float, end_s: float) -> tuple[int, np.ndarray]:
        """(first frame index, log-probs) for [start_s, end_s)."""
        f0 = max(0, int(start_s / FRAME_S))
        f1 = min(self.frames, int(np.ceil(end_s / FRAME_S)))
        if f1 <= f0:
            return f0, np.zeros((0, len(VOCAB) + 4), dtype=np.float32)
        per = int(round(CHUNK_S / FRAME_S))
        parts = []
        for ci in range(f0 // per, (f1 - 1) // per + 1):
            chunk = self._chunk(ci)
            lo = max(f0, ci * per) - ci * per
            hi = min(f1, (ci + 1) * per) - ci * per
            parts.append(chunk[lo:hi])
        return f0, np.vstack(parts)


def _run_model(seg: np.ndarray) -> np.ndarray:
    session = _get_session()
    x = (seg - seg.mean()) / np.sqrt(seg.var() + 1e-7)
    name = session.get_inputs()[0].name
    return session.run(None, {name: x[None, :].astype(np.float32)})[0][0]


# ---------- text ----------

def letters(word: str) -> str:
    """A word as the model spells it: A–Z and ', accents folded."""
    folded = unicodedata.normalize("NFKD", word)
    out = []
    for ch in folded:
        if unicodedata.combining(ch):
            continue
        up = ch.upper()
        if up in VOCAB and up != "|":
            out.append(up)
        elif ch in "’‘`":
            out.append("'")
    s = "".join(out).strip("'")
    return s


# ---------- forced alignment ----------

def force_align(logp: np.ndarray, tokens: list[int]) -> np.ndarray | None:
    """CTC Viterbi: the token index (or -1 for blank) each frame belongs
    to on the best path that spells ``tokens`` in order. None when the
    window is too short to fit them."""
    T = logp.shape[0]
    L = len(tokens)
    if L == 0 or T == 0:
        return None
    S = 2 * L + 1
    labels = np.full(S, BLANK, dtype=np.int64)
    labels[1::2] = tokens
    # skip transition s-2 -> s allowed onto a token that differs from the
    # token two states back (a repeat needs the blank between)
    skip = np.zeros(S, dtype=bool)
    tok_states = np.arange(1, S, 2)
    ok = np.ones(L, dtype=bool)
    ok[1:] = np.asarray(tokens[1:]) != np.asarray(tokens[:-1])
    skip[tok_states] = ok
    skip[1] = False
    neg = -1e30
    score = np.full(S, neg)
    score[0] = logp[0, BLANK]
    score[1] = logp[0, labels[1]]
    back = np.zeros((T, S), dtype=np.int8)
    for t in range(1, T):
        stay = score
        step = np.concatenate(([neg], score[:-1]))
        jump = np.where(skip, np.concatenate(([neg, neg], score[:-2])), neg)
        best = np.maximum(stay, np.maximum(step, jump))
        choice = np.where(best == stay, 0, np.where(best == step, 1, 2))
        back[t] = choice
        score = best + logp[t, labels]
    end = S - 1 if score[S - 1] >= score[S - 2] else S - 2
    if score[end] <= neg / 2:
        return None
    path = np.empty(T, dtype=np.int64)
    s = end
    for t in range(T - 1, -1, -1):
        path[t] = s
        s -= int(back[t, s])
    if path[0] > 1:
        return None
    out = np.where(path % 2 == 1, (path - 1) // 2, -1)
    return out


@dataclass
class WordFit:
    start: float
    end: float
    conf: float


def align_words(em: Emissions, words: list[str], start_s: float, end_s: float,
                pad_s: float = LINE_PAD_S) -> list[WordFit | None] | None:
    """Place ``words`` (display words of one line) inside
    [start_s - pad, end_s + pad]. One entry per word: a WordFit, or None
    for a word with nothing alignable. None overall when the line has too
    little to align."""
    spelled = [letters(w) for w in words]
    tokens: list[int] = []
    owner: list[int] = []
    for wi, sp in enumerate(spelled):
        for ch in sp:
            tokens.append(VOCAB[ch])
            owner.append(wi)
    if len(tokens) < 2:
        return None
    lo = max(0.0, start_s - pad_s)
    hi = end_s + pad_s
    f0, logp = em.window(lo, hi)
    if logp.shape[0] < len(tokens) + 2:
        return None
    path = force_align(logp, tokens)
    if path is None:
        return None
    probs = np.exp(logp)
    fits: list[WordFit | None] = [None] * len(words)
    first: dict[int, int] = {}
    last: dict[int, int] = {}
    conf_sum: dict[int, float] = {}
    conf_n: dict[int, int] = {}
    owner_arr = np.asarray(owner)
    tok_arr = np.asarray(tokens)
    for t in np.nonzero(path >= 0)[0]:
        k = int(path[t])
        wi = int(owner_arr[k])
        first.setdefault(wi, int(t))
        last[wi] = int(t)
        conf_sum[wi] = conf_sum.get(wi, 0.0) + float(probs[t, tok_arr[k]])
        conf_n[wi] = conf_n.get(wi, 0) + 1
    for wi in first:
        s = (f0 + first[wi]) * FRAME_S + FRAME_OFFSET_S
        e = (f0 + last[wi] + 1) * FRAME_S + FRAME_OFFSET_S
        fits[wi] = WordFit(s, e, conf_sum[wi] / max(1, conf_n[wi]))
    return fits


def line_is_alignable(words: Iterable[str]) -> bool:
    words = list(words)
    if not words:
        return False
    good = sum(1 for w in words if len(letters(w)) >= 1)
    total = sum(len(w) for w in words)
    spelled = sum(len(letters(w)) for w in words)
    return good / len(words) >= 0.7 and spelled >= 0.6 * max(1, total - 2)


# ---------- from fits to word times ----------

# CTC emissions peak a little after a word's onset; measured against
# human word timing (NetEase YRC) on five songs the confident fits sat
# ~60 ms late.
ONSET_BIAS_S = 0.06


def anchored_times(words: list[str], fits: list[WordFit | None] | None,
                   start: float, end: float, min_conf: float = MIN_WORD_CONF,
                   ) -> list[tuple[float, float]] | None:
    """Word (start, end) times for one line: confident fits are anchors,
    the words between anchors share the gaps by length. None when nothing
    in the line was confident."""
    if not fits:
        return None
    n = len(words)
    anchors: list[tuple[int, float, float]] = []
    last = -1e9
    for i, f in enumerate(fits):
        if f is None or f.conf < min_conf:
            continue
        s = f.start - ONSET_BIAS_S
        if s <= last:          # out of order: the path doubled back on a guess
            continue
        anchors.append((i, s, max(s + 0.08, f.end - ONSET_BIAS_S)))
        last = s
    if not anchors:
        return None
    weights = [max(1, len(letters(w)) or len(w)) for w in words]
    out: list[tuple[float, float] | None] = [None] * n
    for i, s, e in anchors:
        out[i] = (s, e)
    line_s = min(start, anchors[0][1])
    line_e = max(end, anchors[-1][2])
    # fill runs of unanchored words between known edges
    bounds = [(-1, line_s, line_s)] + anchors + [(n, line_e, line_e)]
    for (ia, _sa, ea), (ib, sb, _eb) in zip(bounds, bounds[1:]):
        run = list(range(ia + 1, ib))
        if not run:
            continue
        lo, hi = ea, max(ea + 0.08 * len(run), sb)
        total = float(sum(weights[k] for k in run))
        acc = 0.0
        for k in run:
            s = lo + (hi - lo) * acc / total
            acc += weights[k]
            out[k] = (s, lo + (hi - lo) * acc / total)
    return [t for t in out if t is not None] if all(t is not None for t in out) else None


# ---------- a whole sheet ----------

# A sheet is only re-timed as a whole when the confident claims agree,
# tightly, that it sits this far off. Measured on five songs: a sheet
# shifted 1 s late drew votes with a spread (MAD) of 0.01-0.12 s, 73-100%
# of them within 0.25 s of the median; a sheet that was already right drew
# scattered votes (bad guy's whispered vocals: median -0.63 s, MAD 0.58,
# 29% agreeing) that would only have hurt.
OFFSET_MIN_S = 0.35
OFFSET_MIN_WORDS = 8           # confident claims (line starts / source words)
OFFSET_AGREE_S = 0.25
OFFSET_MIN_AGREE = 0.6
OFFSET_MAX_MAD = 0.2
# per line: an aligned first word this far from the line's (shifted)
# start has latched onto a neighbouring line
LINE_START_SLACK_S = 0.5


def sheet_offset(devs: list[float]) -> float:
    """The whole-sheet shift the votes support, or 0.0."""
    if len(devs) < OFFSET_MIN_WORDS:
        return 0.0
    d = np.asarray(devs, dtype=np.float64)
    med = float(np.median(d))
    spread = np.abs(d - med)
    if abs(med) < OFFSET_MIN_S:
        return 0.0
    if float(np.mean(spread <= OFFSET_AGREE_S)) < OFFSET_MIN_AGREE:
        return 0.0
    if float(np.median(spread)) > OFFSET_MAX_MAD:
        return 0.0
    return med
ALIGN_PAD_S = 0.5               # search room per line once the offset is known


@dataclass
class SheetFit:
    sheet: list                  # list[lyric_words.WordLine]
    offset: float                # applied whole-sheet shift (0.0 = none)
    aligned_lines: int


def align_sheet(sheet: list, em: Emissions, *,
                should_stop: Callable[[], bool] | None = None) -> SheetFit | None:
    """Re-time ``sheet`` against the audio behind ``em``.

    Two passes. The first aligns every line with generous room and
    measures how far the confident words sit from where the sheet puts
    them; a clear, well-supported median becomes a whole-sheet shift
    (source word timing included — it was matched to the same lines).
    The second aligns line-mode lines again inside a tight window around
    their (shifted) time and anchors their words on the confident fits.
    Lines with source word timing keep their words."""
    from .lyric_words import Word, WordLine, shift, split_words
    sung = [i for i, l in enumerate(sheet) if l.words and line_is_alignable(split_words(l.text))]
    if not sung:
        return None
    devs: list[float] = []
    for i in sung:
        if should_stop is not None and should_stop():
            return None
        line = sheet[i]
        words = split_words(line.text)
        if len(words) != len(line.words):
            continue
        fits = align_words(em, words, line.start, line.end, pad_s=LINE_PAD_S)
        if not fits:
            continue
        if line.timing == "source":
            # every word's time is a claim
            claims = list(zip(fits, (w.start for w in line.words)))
        else:
            # line mode only claims when the line starts
            claims = [(fits[0], line.start)]
        for f, claimed in claims:
            if f is not None and f.conf >= MIN_WORD_CONF:
                devs.append(f.start - ONSET_BIAS_S - claimed)
    offset = sheet_offset(devs)
    out = [shift(l, offset) if offset else l for l in sheet]
    aligned = 0
    for i in sung:
        if should_stop is not None and should_stop():
            return None
        line = out[i]
        if line.timing == "source":
            continue
        words = split_words(line.text)
        fits = align_words(em, words, line.start, line.end, pad_s=ALIGN_PAD_S)
        times = anchored_times(words, fits, line.start, line.end)
        if times is None or abs(times[0][0] - line.start) > LINE_START_SLACK_S:
            continue
        confident = sum(1 for f in fits or [] if f is not None and f.conf >= MIN_WORD_CONF)
        if confident < max(1, round(0.25 * len(words))):
            continue
        new_words = tuple(Word(s, e, w) for (s, e), w in zip(times, words))
        nxt = out[i + 1].start if i + 1 < len(out) else None
        end = max(line.end, new_words[-1].end)
        if nxt is not None:
            end = min(end, max(nxt, new_words[-1].end))
        out[i] = WordLine(min(line.start, new_words[0].start), end, line.text,
                          new_words, "aligned")
        aligned += 1
    return SheetFit(out, offset, aligned)
