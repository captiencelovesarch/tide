"""Playback-speed law.

The speed constants and clamp grew up inside ui/speed.py, which left
player.py and the playback router repeating the 0.25/4.0 engine bounds as
literals — non-ui modules must not import ui, so they couldn't share the
constants even if they wanted to. This module is the one source of truth
at package root; ui/speed.py imports and re-exports so every existing
``from .speed import SPEED_STEP`` style call site keeps working.

Two ranges on purpose:

- the UI range [SPEED_MIN, SPEED_MAX] is what the popover exposes — mpv
  accepts wider but anything outside 0.5–2.0 is more "novelty" than
  "audible," so the UI doesn't offer it.
- the ENGINE range [ENGINE_MIN, ENGINE_MAX] is the hard bound the player
  and router clamp to before touching mpv — wider than the UI so remote
  callers (MPRIS Rate, scripts) get some headroom, but still inside what
  stays intelligible.
"""
from __future__ import annotations


# UI law — shared by the speed popover, window shortcuts and MPRIS bounds.
SPEED_PRESETS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
SPEED_MIN = 0.5
SPEED_MAX = 2.0
SPEED_STEP = 0.05

# Engine law — the clamp player.py / playback router apply right before
# handing the rate to a backend.
ENGINE_MIN = 0.25
ENGINE_MAX = 4.0


def format_speed(speed: float) -> str:
    """Render a speed value for the UI. Prefers a single decimal when the
    value is "round" so we get ``"1.0×"`` not ``"1.00×"`` or ``"1×"``; for
    in-between values like 1.25 we keep the two decimals so the user can
    tell the difference between similar nudges."""
    if abs(speed * 10 - round(speed * 10)) < 1e-3:
        return f"{speed:.1f}×"
    return f"{speed:.2f}×"


def clamp(value: float) -> float:
    """UI clamp: quantize to the step grid so floating math doesn't
    accumulate (e.g. a chain of −0.05 nudges shouldn't drift off to
    1.0500000004×), then bound to [SPEED_MIN, SPEED_MAX]."""
    snapped = round(float(value) / SPEED_STEP) * SPEED_STEP
    return max(SPEED_MIN, min(SPEED_MAX, round(snapped, 2)))


def engine_clamp(value: float) -> float:
    """Engine clamp: plain bound to [ENGINE_MIN, ENGINE_MAX], no step
    quantization — the engine takes whatever rate it's given as long as
    it stays intelligible."""
    return max(ENGINE_MIN, min(ENGINE_MAX, float(value)))
