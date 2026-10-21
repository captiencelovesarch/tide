"""Starting OpenRGB's server when nothing answers, so lights works without
opening a second app (2026-10-21: "i hate having to open up a second app
and mess around with that").

Only for an address on this computer, only when OpenRGB isn't already
open (two copies would fight over the same usb devices), and only one
tide started itself gets stopped again.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

LOCAL = ("127.0.0.1", "localhost", "::1")


def answering(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def openrgb_running() -> bool:
    for comm in Path("/proc").glob("[0-9]*/comm"):
        try:
            if comm.read_text().strip().lower() == "openrgb":
                return True
        except OSError:
            continue
    return False


def start_server(host: str, port: int) -> tuple[subprocess.Popen | None, str]:
    """(the OpenRGB we started or None, what to tell the user)."""
    if host not in LOCAL:
        return None, ""
    if answering(host, port):
        return None, ""
    if openrgb_running():
        return None, ("openrgb is open but its sdk server is off. switch it on in "
                      "openrgb's sdk server tab, or close openrgb and tide will start it")
    exe = shutil.which("openrgb")
    if exe is None:
        return None, "openrgb isn't installed (on arch: pacman -S openrgb)"
    proc = subprocess.Popen(
        [exe, "--server", "--server-port", str(port)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env=os.environ.copy())
    return proc, "starting openrgb (finding your lights can take a minute)"


def stop_server(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
