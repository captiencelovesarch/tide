"""The light show: a thread of its own, 60 frames a second, so a slow
device or a dropped wifi packet never holds up tide.

Each frame takes the highest pulse since the last one (like tide's
backdrop), shapes it with tide.plugin_api's flash cap and envelope, and
paints every device: the cover's two colours as a slow-moving gradient
along its leds, at the beat's brightness. A device that fails is retried
every few seconds; the others carry on.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Callable

from tide.plugin_api import Envelope, FlashLimiter, STRENGTH

FPS = 60
RETRY_S = 5.0
DRIFT_S = 24.0             # one trip of the gradient along the leds
REST = 0.18                # brightness while paused, with "glow dimly"


def brightness(mode: str, pulse: float) -> float:
    """how bright the lights are for a pulse, before strength. a room
    needs more range than a glow at the edge of your view."""
    pulse = max(0.0, min(1.0, pulse))
    if mode == "breathing":
        return 0.35 + 0.65 * pulse
    return 0.12 + 0.88 * pulse


def gradient(n: int, colors: tuple, level: float, phase: float) -> list[tuple[int, int, int]]:
    """``n`` leds from colour a to b and back, shifted by ``phase`` (0..1),
    at ``level`` (0..1)."""
    (r1, g1, b1), (r2, g2, b2) = colors
    level = max(0.0, min(1.0, level))
    out = []
    for i in range(n):
        x = (i / max(1, n) + phase) % 1.0
        t = 0.5 - 0.5 * math.cos(2 * math.pi * x)
        out.append((int(255 * level * (r1 + (r2 - r1) * t)),
                    int(255 * level * (g1 + (g2 - g1) * t)),
                    int(255 * level * (b1 + (b2 - b1) * t))))
    return out


class Show(threading.Thread):
    def __init__(self, outputs: list, on_status: Callable[[str], None]) -> None:
        super().__init__(name="tide-lights", daemon=True)
        self.outputs = outputs
        self.on_status = on_status
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.pulse = 0.0
        self.pulse_peak = 0.0
        self.playing = False
        self.colors: tuple = ((1.0, 0.83, 0.0), (1.0, 0.45, 0.2))
        self.mode = "kick"
        self.strength = "normal"
        self.paused = "dim"
        self.sweep_until = 0.0
        self._live: dict[int, bool] = {}
        self._retry_at: dict[int, float] = {}
        self._connecting: set[int] = set()
        self._said = ""

    # ---- from the gui thread ----

    def set_pulse(self, level: float) -> None:
        with self._lock:
            self.pulse = float(level)
            self.pulse_peak = max(self.pulse_peak, self.pulse)

    def configure(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def sweep(self, seconds: float = 1.6) -> None:
        """the test button: a fast run through the colours."""
        with self._lock:
            self.sweep_until = time.monotonic() + seconds

    def stop(self) -> None:
        self._stop.set()

    # ---- the thread ----

    def run(self) -> None:
        flash, env = FlashLimiter(), Envelope()
        frame = 1.0 / FPS
        next_t = time.monotonic()
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                with self._lock:
                    beat = max(self.pulse, self.pulse_peak)
                    self.pulse_peak = 0.0
                    playing, colors = self.playing, self.colors
                    mode, strength, paused = self.mode, self.strength, self.paused
                    sweep = now < self.sweep_until
                if sweep:
                    level = 1.0
                    phase = (now * 2.0) % 1.0
                elif playing:
                    target = brightness(mode, beat)
                    level = env.update(now, flash.update(now, target))
                    level *= STRENGTH.get(strength, 0.7)
                    phase = (now / DRIFT_S) % 1.0
                else:
                    flash.update(now, 0.0)
                    env.update(now, 0.0)
                    level = REST * STRENGTH.get(strength, 0.7) if paused == "dim" else 0.0
                    phase = (now / DRIFT_S) % 1.0
                self._paint(now, colors, level, phase)
                next_t += frame
                delay = next_t - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
                else:
                    next_t = time.monotonic()
        finally:
            for i, out in enumerate(self.outputs):
                if self._live.get(i):
                    try:
                        out.close()
                    except OSError:
                        pass

    def _connect(self, i: int, out) -> None:
        """off the show's thread: a strip that doesn't answer takes two
        seconds to time out, and the other lights shouldn't stall for it."""
        try:
            out.connect()
        except (OSError, ValueError) as exc:
            with self._lock:
                self._connecting.discard(i)
                self._retry_at[i] = time.monotonic() + RETRY_S
            self._report(f"{out.kind} {out.host}: {exc}"[:110])
            return
        with self._lock:
            self._connecting.discard(i)
            stopped = self._stop.is_set()
            if not stopped:
                self._live[i] = True
        if stopped:
            # the show ended while this one was connecting: let it go
            try:
                out.close()
            except OSError:
                pass
            return
        self._report_live()

    def _paint(self, now: float, colors: tuple, level: float, phase: float) -> None:
        for i, out in enumerate(self.outputs):
            with self._lock:
                live = self._live.get(i, False)
                start = (not live and i not in self._connecting
                         and now >= self._retry_at.get(i, 0.0) and not self._stop.is_set())
                if start:
                    self._connecting.add(i)
            if start:
                threading.Thread(target=self._connect, args=(i, out), daemon=True,
                                 name=f"tide-lights-{out.kind}").start()
            if not live:
                continue
            try:
                out.send([gradient(n, colors, level, phase) for n in out.leds])
            except (OSError, ValueError) as exc:
                with self._lock:
                    self._live[i] = False
                    self._retry_at[i] = now + RETRY_S
                try:
                    out.close()
                except OSError:
                    pass
                self._report(f"lost {out.name}: {exc}"[:110])
                self._report_live()

    def _report_live(self) -> None:
        with self._lock:
            names = [o.name for i, o in enumerate(self.outputs) if self._live.get(i)]
        self._report("lighting " + ", ".join(names) if names else "no lights connected")

    def _report(self, text: str) -> None:
        with self._lock:
            if text == self._said or self._stop.is_set():
                # stopped: the plugin's status line may be gone already (a
                # slow device answering after tide let go)
                return
            self._said = text
        try:
            self.on_status(text)
        except RuntimeError:
            pass
