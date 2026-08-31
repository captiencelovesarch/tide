"""Playback-speed law — one source of truth at package root (non-ui
modules must not import ui). ui/speed.py imports and re-exports so
existing ``from .speed import SPEED_STEP`` call sites keep working.

Two ranges on purpose: the UI range [SPEED_MIN, SPEED_MAX] is what the
popover offers (outside 0.5–2.0 is novelty, not audible); the ENGINE
range [ENGINE_MIN, ENGINE_MAX] is the hard bound clamped before touching
mpv — wider so remote callers (MPRIS Rate, scripts) get headroom, but
still intelligible.
"""
from __future__ import annotations


# UI law — shared by the speed popover, window shortcuts and MPRIS bounds.
SPEED_PRESETS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
SPEED_MIN = 0.5
SPEED_MAX = 2.0
SPEED_STEP = 0.05

# engine law — clamped by player.py / the router right before a backend
ENGINE_MIN = 0.25
ENGINE_MAX = 4.0


def format_speed(speed: float) -> str:
    """Render for the UI: ``"1.0×"`` (not ``"1.00×"`` or ``"1×"``) for
    round values; two decimals for in-between values like 1.25 so similar
    nudges stay distinguishable."""
    if abs(speed * 10 - round(speed * 10)) < 1e-3:
        return f"{speed:.1f}×"
    return f"{speed:.2f}×"


def clamp(value: float) -> float:
    """UI clamp: quantize to the step grid first — chained −0.05 nudges
    must not drift to 1.0500000004× — then bound to [SPEED_MIN, SPEED_MAX]."""
    snapped = round(float(value) / SPEED_STEP) * SPEED_STEP
    return max(SPEED_MIN, min(SPEED_MAX, round(snapped, 2)))


def engine_clamp(value: float) -> float:
    """Plain bound to [ENGINE_MIN, ENGINE_MAX] — no step quantization."""
    return max(ENGINE_MIN, min(ENGINE_MAX, float(value)))
