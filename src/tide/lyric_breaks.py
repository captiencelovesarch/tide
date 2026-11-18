"""Where a timed sheet's breaks are: the intro, an instrumental, a long
blank line, or a long wait after a line's singing ends.

The lyrics views draw their three dots over these. LyricTracker reads
the same answer for the mini player's ticker and the plugins' lyric
feed, so every surface agrees on when a break starts and ends. No Qt
here: it's all floats and strings.
"""
from __future__ import annotations

# Breaks this long get the dots where the next line will land.
GAP_MIN_S = 4.0
# A line stays lit this long past the end of its singing (when the source
# says where that is), and only fades back if the next line is at least
# FADE_MIN_S away: a quick handoff just moves on.
TAIL_GRACE_S = 0.35
FADE_MIN_S = 2.5

_NOTE_CHARS = frozenset("♩♪♫♬🎵🎶🎼")


def is_blank(text: str) -> bool:
    """No words to sing: empty, or only ♪ and punctuation."""
    return not any(ch.isalnum() for ch in (text or ""))


def is_note(text: str) -> bool:
    """A source's instrumental mark (a line of just ♪): drawn as the
    break's three dots instead."""
    t = (text or "").strip()
    return bool(t) and is_blank(t) and any(ch in _NOTE_CHARS for ch in t)


def next_sung(blank: list[bool]) -> list:
    """``out[i + 1]`` is the first line at or after ``i`` with words (None
    past the last one). One longer than the sheet so the intro (i = -1)
    has a slot."""
    out, upcoming = [None] * (len(blank) + 1), None
    for i in range(len(blank) - 1, -2, -1):
        out[i + 1] = upcoming
        if i >= 0 and not blank[i]:
            upcoming = i
    return out


def real_line_ends(sheet, result) -> list:
    """When each line's singing stops, only where something knows it:
    a word-timed line's last word, or the source's own line end (YT and
    lrclib's lyricsfile say). None where it would only be a guess, so a
    line never fades back while it's still being sung."""
    src = list(getattr(result, "line_ends", []) or [])
    known = len(src) == len(sheet)
    out: list = []
    for i, line in enumerate(sheet):
        end = None
        if line.words and line.timing in ("source", "aligned"):
            end = float(line.words[-1].end)
        elif known and src[i] is not None:
            end = float(line.end)        # the source's end, capped at the next line
        out.append(end)
    return out


def visual(idx: int, secs: float, starts: list, ends: list, blank: list,
           nxt: list) -> tuple[int, tuple | None]:
    """(the line drawn as sung, the break the dots are up for) at
    ``secs``, where ``idx`` is the latest line started by then. A line
    is sung until its singing ends (when that's known) and the next line
    is a while off; a break long enough gets dots, from where it starts
    to the next line with words. A break is ``(next line, begin, end)``."""
    n = len(starts)
    if not n or len(nxt) != n + 1:
        return idx, None
    if idx >= 0 and not blank[idx]:
        end = ends[idx] if idx < len(ends) else None
        if end is None or secs < end + TAIL_GRACE_S:
            return idx, None
        j = nxt[idx + 1]
        if j is None:
            return -1, None                  # the last line, sung
        if starts[j] - end < FADE_MIN_S:
            return idx, None                 # the next one is right there
        begin = end + TAIL_GRACE_S
        return -1, ((j, begin, starts[j]) if starts[j] - begin >= GAP_MIN_S else None)
    # the intro, or a blank line
    j = nxt[idx + 1]
    if j is None:
        return -1, None
    if idx < 0:
        begin = 0.0
    else:
        k = idx
        while k > 0 and blank[k - 1]:
            k -= 1
        begin = starts[k]
    return -1, ((j, begin, starts[j]) if starts[j] - begin >= GAP_MIN_S else None)
