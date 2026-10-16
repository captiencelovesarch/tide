"""Word-level lyric timing: the data model, the parsers for word-synced
formats, and the merge that puts source words onto the playing video's
lines.

A sheet is a list of :class:`WordLine` — a line's start/end and its words,
each with its own start/end. Where the words come from is the line's
``timing``:

  * ``"source"``  — a word-synced lyric source (NetEase YRC, LRClib's
    lyricsfile ``words``, enhanced LRC ``<mm:ss.xx>`` tags),
  * ``"aligned"`` — the local aligner (lyric_align) placed them,
  * ``"line"``    — no word timing: words are spread across the line's
    window by length (the karaoke fallback; it still sweeps).

Pure Python, no Qt — the karaoke widget, the timing service and the
tests all read the same functions.
"""
from __future__ import annotations

import json
import re
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from statistics import median

# A line with no known end sings for at most this long (then the sweep
# rests on the last word until the next line).
MAX_LINE_S = 6.0
# Length-weighted fallback: per-character budget used to bound the
# window when the next line is far off (an instrumental break).
SECONDS_PER_CHAR = 0.30
MIN_WORD_S = 0.08


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class WordLine:
    start: float
    end: float
    text: str
    words: tuple[Word, ...] = ()
    timing: str = "line"          # "source" | "aligned" | "line"

    def payload(self) -> list:
        return [round(self.start, 3), round(self.end, 3), self.text, self.timing,
                [[round(w.start, 3), round(w.end, 3), w.text] for w in self.words]]

    @classmethod
    def from_payload(cls, row) -> "WordLine | None":
        try:
            start, end, text, timing, words = row
            ws = tuple(Word(float(a), float(b), str(t)) for a, b, t in words)
            if not isinstance(text, str) or timing not in ("source", "aligned", "line"):
                return None
            return cls(float(start), float(end), text, ws, timing)
        except (TypeError, ValueError):
            return None


# ---------- text ----------

def split_words(text: str) -> list[str]:
    return text.split()


def norm(text: str) -> str:
    """Comparison form: lowercase letters/digits only, accents folded."""
    folded = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in folded.lower()
                   if ch.isalnum() and not unicodedata.combining(ch))


def similarity(a: str, b: str) -> float:
    """0..1 overlap of two lines' character bigrams (Dice)."""
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ga = [a[i:i + 2] for i in range(max(1, len(a) - 1))]
    gb = [b[i:i + 2] for i in range(max(1, len(b) - 1))]
    pool: dict[str, int] = {}
    for g in gb:
        pool[g] = pool.get(g, 0) + 1
    hits = 0
    for g in ga:
        if pool.get(g, 0) > 0:
            pool[g] -= 1
            hits += 1
    return 2.0 * hits / (len(ga) + len(gb))


# ---------- parsers ----------

_YRC_LINE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
_YRC_WORD = re.compile(r"\((\d+),(\d+),\d+\)([^()]*)")


def parse_yrc(text: str) -> list[WordLine]:
    """NetEase's word-synced format: ``[start,dur](ws,wd,0)word…`` in ms.
    JSON credit lines (``{"t":…}``) are skipped."""
    out: list[WordLine] = []
    for raw in (text or "").splitlines():
        raw = raw.strip()
        m = _YRC_LINE.match(raw)
        if not m:
            continue
        try:
            ls, ld = int(m.group(1)) / 1000.0, int(m.group(2)) / 1000.0
        except ValueError:
            continue
        pieces: list[tuple[float, float, str]] = []
        for wm in _YRC_WORD.finditer(m.group(3)):
            try:
                ws, wd = int(wm.group(1)) / 1000.0, int(wm.group(2)) / 1000.0
            except ValueError:
                continue
            pieces.append((ws, ws + wd, wm.group(3)))
        words = _join_pieces(pieces)
        line_text = "".join(p[2] for p in pieces).strip()
        if not line_text:
            continue
        out.append(WordLine(ls, ls + ld, " ".join(line_text.split()),
                            tuple(words), "source" if words else "line"))
    out.sort(key=lambda l: l.start)
    return out


def _join_pieces(pieces: list[tuple[float, float, str]]) -> list[Word]:
    """Timed fragments → display words. Fragments without a space between
    them belong to one word ("Sleeping" + "," → "Sleeping,"; syllables
    "tip"+"py" → "tippy")."""
    words: list[Word] = []
    cur_text = ""
    cur_s = cur_e = 0.0
    for s, e, frag in pieces:
        if not frag:
            continue
        body = frag.strip()
        if cur_text and (frag[:1].isspace() or not body):
            words.append(Word(cur_s, cur_e, cur_text))
            cur_text = ""
        if body:
            if cur_text:
                cur_e = max(cur_e, e)
            else:
                cur_s, cur_e = s, e
            cur_text += body
        if frag[-1:].isspace() and cur_text:
            words.append(Word(cur_s, cur_e, cur_text))
            cur_text = ""
    if cur_text:
        words.append(Word(cur_s, cur_e, cur_text))
    # A word made only of punctuation (" -", "…") rides on its neighbour.
    merged: list[Word] = []
    for w in words:
        if merged and not any(ch.isalnum() for ch in w.text):
            prev = merged[-1]
            merged[-1] = Word(prev.start, max(prev.end, w.end), prev.text + w.text)
        else:
            merged.append(w)
    return merged


_LRC_TS = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:\.\d{1,3})?)\]")
_LRC_WORD_TS = re.compile(r"<(\d{1,3}):(\d{1,2}(?:\.\d{1,3})?)>")


def parse_enhanced_lrc(text: str) -> list[WordLine]:
    """Enhanced (A2) LRC: ``[00:12.00]<00:12.00>word <00:12.40>word``.
    Lines with no word tags come back with ``timing="line"``; an LRC with
    no word tags at all returns []."""
    if "<" not in (text or "") or not _LRC_WORD_TS.search(text):
        return []
    rows: list[tuple[float, str]] = []
    for raw in text.splitlines():
        m = _LRC_TS.match(raw.strip())
        if not m:
            continue
        try:
            t = int(m.group(1)) * 60 + float(m.group(2))
        except ValueError:
            continue
        rows.append((t, raw.strip()[m.end():]))
    rows.sort(key=lambda r: r[0])
    out: list[WordLine] = []
    for i, (t, body) in enumerate(rows):
        nxt = rows[i + 1][0] if i + 1 < len(rows) else None
        marks = list(_LRC_WORD_TS.finditer(body))
        if not marks:
            plain = " ".join(body.split())
            if plain:
                out.append(WordLine(t, _line_end(t, nxt, plain), plain))
            continue
        pieces: list[tuple[float, str]] = []
        for j, wm in enumerate(marks):
            try:
                ws = int(wm.group(1)) * 60 + float(wm.group(2))
            except ValueError:
                continue
            frag_end = marks[j + 1].start() if j + 1 < len(marks) else len(body)
            pieces.append((ws, body[wm.end():frag_end]))
        # a trailing tag with no text marks the end of the last word
        tail_end = None
        if pieces and not pieces[-1][1].strip():
            tail_end = pieces[-1][0]
            pieces.pop()
        timed: list[tuple[float, float, str]] = []
        for j, (ws, frag) in enumerate(pieces):
            we = pieces[j + 1][0] if j + 1 < len(pieces) else (
                tail_end if tail_end is not None else _line_end(ws, nxt, frag))
            timed.append((ws, max(ws + MIN_WORD_S, we), frag))
        words = _join_pieces(timed)
        text_line = " ".join("".join(f for _s, f in pieces).split())
        if not text_line:
            continue
        end = max([w.end for w in words] + [t])
        out.append(WordLine(t, end, text_line, tuple(words),
                            "source" if words else "line"))
    return out


def parse_lyricsfile(text: str) -> list[WordLine]:
    """LRClib's ``lyricsfile`` (YAML; lrcget's Lyricsfile 1.0): lines with
    start_ms / end_ms and optional ``words``. Needs PyYAML; without it,
    or on anything malformed, returns []."""
    if not text or "words" not in text:
        return []
    try:
        import yaml
        doc = yaml.safe_load(text)
    except Exception:
        return []
    if not isinstance(doc, dict):
        return []
    meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    try:
        offset = float(meta.get("offset_ms") or 0) / 1000.0
    except (TypeError, ValueError):
        offset = 0.0
    rows = doc.get("lines")
    if not isinstance(rows, list):
        return []
    out: list[WordLine] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        try:
            start = float(row.get("start_ms")) / 1000.0 + offset
        except (TypeError, ValueError):
            continue
        line_text = " ".join(str(row.get("text") or "").split())
        if not line_text:
            continue
        end_raw = row.get("end_ms")
        try:
            end = float(end_raw) / 1000.0 + offset if end_raw is not None else None
        except (TypeError, ValueError):
            end = None
        pieces: list[tuple[float, float, str]] = []
        raw_words = row.get("words") if isinstance(row.get("words"), list) else []
        for j, w in enumerate(raw_words):
            if not isinstance(w, dict):
                continue
            try:
                ws = float(w.get("start_ms")) / 1000.0 + offset
            except (TypeError, ValueError):
                continue
            we_raw = w.get("end_ms")
            if we_raw is None and j + 1 < len(raw_words) and isinstance(raw_words[j + 1], dict):
                we_raw = raw_words[j + 1].get("start_ms")
            try:
                we = float(we_raw) / 1000.0 + offset if we_raw is not None else ws + 0.4
            except (TypeError, ValueError):
                we = ws + 0.4
            pieces.append((ws, max(ws + MIN_WORD_S, we), str(w.get("text") or "")))
        words = _join_pieces(pieces)
        if end is None:
            end = max([w.end for w in words] + [start + 0.5])
        out.append(WordLine(start, end, line_text, tuple(words),
                            "source" if words else "line"))
    out.sort(key=lambda l: l.start)
    return out if any(l.words for l in out) else []


def lyricsfile_line_ends(text: str, timed: list[tuple[float, str]]) -> list[float | None]:
    """Per-line end times from a lyricsfile, matched onto ``timed`` (the
    synced LRC lines) by start time. [] without PyYAML or on a mismatch."""
    if not text or not timed or "end_ms" not in text:
        return []
    try:
        import yaml
        doc = yaml.safe_load(text)
    except Exception:
        return []
    rows = doc.get("lines") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        return []
    by_start: dict[int, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            s, e = float(row.get("start_ms")), float(row.get("end_ms"))
        except (TypeError, ValueError):
            continue
        if e > s:
            by_start[int(round(s / 10.0))] = e / 1000.0
    ends = [by_start.get(int(round(t * 100.0))) for t, _l in timed]
    return ends if any(e is not None for e in ends) else []


# ---------- line-mode fallback ----------

def _line_end(start: float, next_start: float | None, text: str) -> float:
    budget = max(1.2, SECONDS_PER_CHAR * max(2, len(norm(text))))
    cap = min(MAX_LINE_S, budget)
    if next_start is None:
        return start + cap
    return start + max(0.3, min(next_start - start, cap))


def spread_words(text: str, start: float, end: float) -> tuple[Word, ...]:
    """Length-weighted word times across [start, end] — what karaoke shows
    when nothing better is known."""
    words = split_words(text)
    if not words:
        return ()
    weights = [max(1, len(norm(w)) or len(w)) for w in words]
    total = float(sum(weights))
    span = max(0.2, end - start)
    out = []
    acc = 0.0
    for w, wt in zip(words, weights):
        s = start + span * acc / total
        acc += wt
        out.append(Word(s, start + span * acc / total, w))
    return tuple(out)


def line_sheet(timed_lines: list[tuple[float, str]],
               line_ends: list[float | None] | None = None) -> list[WordLine]:
    """Line-timed lyrics as a sheet with spread (line-mode) words."""
    out: list[WordLine] = []
    n = len(timed_lines)
    for i, (t, text) in enumerate(timed_lines):
        text = " ".join((text or "").split())
        nxt = timed_lines[i + 1][0] if i + 1 < n else None
        end = None
        if line_ends is not None and i < len(line_ends) and line_ends[i] is not None:
            end = float(line_ends[i])
            if nxt is not None:
                end = min(end, nxt)
            if end <= t:
                end = None
        if end is None:
            end = _line_end(t, nxt, text)
        out.append(WordLine(float(t), end, text, spread_words(text, t, end) if text else ()))
    return out


# ---------- merging source words onto the playing lines ----------

MATCH_SIM = 0.72        # bigram similarity that counts as "the same line"
MIN_MATCH_SHARE = 0.5   # of the sung lines, to trust a word source at all


def match_offset(base: list[WordLine], words: list[WordLine]) -> tuple[float, dict[int, int]] | None:
    """How far ``words`` (a word-synced sheet from another source) sits
    from ``base`` (the playing video's line timing), and which base line
    each word line lands on. Lines are matched by text in order, inside a
    ±12 s search; the offset is the median start difference. None when
    too few lines match — a different song or a different edit."""
    sung = [i for i, l in enumerate(base) if norm(l.text)]
    if not sung or not words:
        return None
    pairs: list[tuple[int, int]] = []
    j0 = 0
    for i in sung:
        best, best_j = 0.0, -1
        for j in range(j0, len(words)):
            if words[j].start - base[i].start > 30:
                break
            sim = similarity(base[i].text, words[j].text)
            if sim > best:
                best, best_j = sim, j
            if sim >= 0.999:
                break
        if best >= MATCH_SIM and best_j >= 0:
            pairs.append((i, best_j))
            j0 = best_j + 1
    if len(pairs) < max(2, MIN_MATCH_SHARE * len(sung)):
        return None
    diffs = [base[i].start - words[j].start for i, j in pairs]
    off = median(diffs)
    # the rough first pass can pair a repeated chorus line with the wrong
    # repeat; keep only pairs that agree with the median
    good = {i: j for i, j in pairs if abs(base[i].start - words[j].start - off) <= 1.5}
    if len(good) < max(2, MIN_MATCH_SHARE * len(sung)):
        return None
    off = median(base[i].start - words[j].start for i, j in good.items())
    return off, good


def shift(line: WordLine, by: float) -> WordLine:
    return replace(line, start=line.start + by, end=line.end + by,
                   words=tuple(Word(w.start + by, w.end + by, w.text) for w in line.words))


def adopt_source_words(base: list[WordLine], words: list[WordLine]) -> tuple[list[WordLine], float] | None:
    """Base lines with words taken from a word-synced source, shifted by
    the matched offset. Lines with no match keep their line-mode words.
    Returns (sheet, offset) or None when the source doesn't match."""
    matched = match_offset(base, words)
    if matched is None:
        return None
    off, pairs = matched
    flat = [Word(w.start + off, w.end + off, w.text) for l in words for w in l.words]
    out: list[WordLine] = []
    for i, line in enumerate(base):
        j = pairs.get(i)
        if j is not None and words[j].words:
            mapped = _map_words(line.text, shift(words[j], off).words)
        else:
            # split differently ("I'm the bad guy" + "Duh" vs one line):
            # the run of source words around this line that reads the same
            mapped = _run_for(line, flat) if norm(line.text) else None
        if mapped is None:
            out.append(line)
            continue
        end = max(line.end, mapped[-1].end)
        nxt = base[i + 1].start if i + 1 < len(base) else None
        if nxt is not None:
            end = min(end, max(nxt, mapped[-1].end))
        out.append(WordLine(min(line.start, mapped[0].start), end, line.text, mapped, "source"))
    return out, off


RUN_SIM = 0.8
RUN_REACH_S = 2.0


def _run_for(line: WordLine, flat: list[Word]) -> tuple[Word, ...] | None:
    """Best contiguous run of (offset-corrected) source words near
    ``line`` whose text matches it, mapped onto the line's words."""
    n = len(split_words(line.text))
    near = [k for k, w in enumerate(flat)
            if line.start - RUN_REACH_S <= w.start <= line.end + RUN_REACH_S]
    if not near or n == 0:
        return None
    lo, hi = near[0], near[-1] + 1
    best, best_run = 0.0, None
    for a in range(lo, hi):
        for b in range(a + max(1, n - 2), min(hi, a + n + 3) + 1):
            sim = similarity(" ".join(w.text for w in flat[a:b]), line.text)
            if sim > best:
                best, best_run = sim, flat[a:b]
    if best < RUN_SIM or not best_run:
        return None
    return _map_words(line.text, tuple(best_run))


def _map_words(text: str, src: tuple[Word, ...]) -> tuple[Word, ...] | None:
    """The base line's own words, timed from ``src``. Equal word counts
    map one to one; otherwise each base word takes the time of the source
    stretch covering the same share of the line's letters."""
    target = split_words(text)
    if not target or not src:
        return None
    if len(target) == len(src):
        return tuple(Word(s.start, s.end, t) for t, s in zip(target, src))
    src_len = [max(1, len(norm(w.text))) for w in src]
    tgt_len = [max(1, len(norm(w))) for w in target]
    st, tt = float(sum(src_len)), float(sum(tgt_len))
    if abs(st - tt) / max(st, tt) > 0.35:
        return None
    # cumulative letter position → time, along the source words
    knots_x = [0.0]
    knots_t = [src[0].start]
    acc = 0.0
    for w, n in zip(src, src_len):
        knots_x.append((acc + 0.001) / st)
        knots_t.append(w.start)
        acc += n
        knots_x.append(acc / st)
        knots_t.append(w.end)

    def at(x: float) -> float:
        k = max(1, min(len(knots_x) - 1, bisect_right(knots_x, x)))
        x0, x1 = knots_x[k - 1], knots_x[k]
        t0, t1 = knots_t[k - 1], knots_t[k]
        if x1 <= x0:
            return t1
        return t0 + (t1 - t0) * (x - x0) / (x1 - x0)

    out = []
    acc = 0.0
    for w, n in zip(target, tgt_len):
        s = at(acc / tt + 1e-4)
        acc += n
        e = at(acc / tt)
        out.append(Word(s, max(s + MIN_WORD_S, e), w))
    return tuple(out)


# ---------- playback lookup ----------

def active_line(sheet: list[WordLine], t: float, starts: list[float] | None = None) -> int:
    starts = starts if starts is not None else [l.start for l in sheet]
    return bisect_right(starts, t) - 1


def word_progress(line: WordLine, t: float) -> tuple[int, float]:
    """(index of the word being sung, 0..1 through it). Before the first
    word → (0, 0.0); between words → the previous word at 1.0; after the
    last → (last, 1.0)."""
    if not line.words:
        return -1, 0.0
    for i, w in enumerate(line.words):
        if t < w.start:
            if i == 0:
                return 0, 0.0
            return i - 1, 1.0
        if t < w.end:
            span = max(1e-3, w.end - w.start)
            return i, (t - w.start) / span
    return len(line.words) - 1, 1.0


def sheet_payload(sheet: list[WordLine]) -> str:
    return json.dumps([l.payload() for l in sheet], separators=(",", ":"))


def sheet_from_payload(raw) -> list[WordLine] | None:
    try:
        rows = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    out = []
    for row in rows:
        line = WordLine.from_payload(row)
        if line is None:
            return None
        out.append(line)
    return out
