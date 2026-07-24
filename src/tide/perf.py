"""Lightweight play-latency diagnostics.

The window prints one always-on summary line per play (see
``MainWindow._play_track`` / ``_on_state``); the finer-grained marks inside
the resolve pipeline are gated behind ``TIDE_PERF=1`` so normal runs stay
quiet. Developer plumbing only — nothing here is user configuration, so an
env var (not a settings field) is the right switch.
"""
from __future__ import annotations

import os
import sys
import time
from functools import lru_cache


@lru_cache(maxsize=1)
def enabled() -> bool:
    return os.environ.get("TIDE_PERF") == "1"


def mark(msg: str) -> None:
    """Print a timestamped diagnostic line to stderr when TIDE_PERF=1.

    Safe from any thread — a single print call, no shared state.
    """
    if enabled():
        print(f"tide-perf [{time.monotonic():10.3f}] {msg}", file=sys.stderr)
