"""Test isolation: NO test may touch the real ~/.config/tide or ~/.cache/tide.

This existed as a polite convention and it failed silently for a long
time: every Qt test that called ``_play_track`` with fixture tracks
("t0" by "a") appended them to the REAL history.jsonl — by v1.5 that was
964 junk entries, and the home hero greeted the user with
"keep listening: a — t0". (Settings/session escaped only by luck of
which code paths the tests exercised.)

tide.config derives every path from XDG env vars at import time, so the
redirect must happen before anything imports tide — which is exactly
what a root conftest's module body guarantees under pytest. One temp
tree per test run; the OS reaps /tmp.
"""
import os
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="tide-tests-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_SANDBOX, "config")
os.environ["XDG_CACHE_HOME"] = os.path.join(_SANDBOX, "cache")

# Import AFTER the redirect and pin the sandbox, so a stray earlier import
# of tide.config in the same interpreter can't leave real paths behind.
from tide import config as _config  # noqa: E402

assert str(_config.CACHE_DIR).startswith(_SANDBOX), (
    "tide.config was imported before the test sandbox took effect — "
    "real user data is at risk; refusing to run"
)
