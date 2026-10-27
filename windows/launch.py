"""Entry point for the Windows build (PyInstaller freezes this file).

Does the few things a frozen, windowed tide.exe needs before tide itself
loads, then hands over to tide.app.run.

``tide.exe --selftest`` imports everything, checks the bundled tools and
exits 0 or 1 without opening a window. CI runs it on every build.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _base() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _log_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    return root / "tide" / "cache" / "tide.log"


def _redirect_output() -> None:
    """A windowed exe has no stdout/stderr (both are None), so anything that
    writes to them directly would crash. Send both to a log file instead,
    which is also what we ask for when something goes wrong."""
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        log = open(path, "w", encoding="utf-8", buffering=1, errors="replace")
    except OSError:
        log = open(os.devnull, "w")
    sys.stdout = sys.stderr = log


def _bundled_tools() -> None:
    """ffmpeg, deno (yt-dlp's JS runtime) and libmpv-2.dll live in bin/."""
    bin_dir = _base() / "bin"
    if bin_dir.is_dir():
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):  # Windows only
            os.add_dll_directory(str(bin_dir))


def _no_console_windows() -> None:
    """Every child process (ffmpeg, deno, librespot) would otherwise flash
    a black console window. One patch here covers all of them."""
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if not flag:
        return
    original = subprocess.Popen.__init__

    def init(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | flag
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = init


def _taskbar_identity() -> None:
    """Group tide's windows under its own taskbar button and icon."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("tide.player")
    except Exception:
        pass


def _selftest() -> int:
    problems = []
    for tool in ("ffmpeg", "deno"):
        if shutil.which(tool) is None:
            problems.append(f"{tool} missing from bin/")
    try:
        import mpv  # noqa: F401  (loads libmpv-2.dll)
        import tide.app  # noqa: F401
        import importlib.util
        from tide import config, theming, layout
        dirs = [theming.BUNDLED_THEMES_DIR, layout.BUNDLED_LAYOUTS_DIR]
        if importlib.util.find_spec("tide.store"):  # 3.0+: plugins from the store
            import tide.plugin_host  # noqa: F401
            import tide.store  # noqa: F401
            # store plugins load at runtime, so what they import has to be
            # in the bundle already (see tide.spec)
            for mod in ("asyncio", "sqlite3", "http.server", "mutagen"):
                if importlib.util.find_spec(mod) is None:
                    problems.append(f"{mod} missing (store plugins need it)")
        for d in dirs:
            if not Path(d).is_dir():
                problems.append(f"bundled dir missing: {d}")
        print("config dir:", config.CONFIG_DIR)
        import tide
        print("version:", tide.__version__)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        problems.append(f"import failed: {exc!r}")
    for p in problems:
        print("selftest:", p)
    print("selftest:", "FAILED" if problems else "ok")
    return 1 if problems else 0


def main() -> int:
    _redirect_output()
    _bundled_tools()
    _no_console_windows()
    _taskbar_identity()
    if "--selftest" in sys.argv[1:]:
        return _selftest()
    from tide.app import run
    return run()


if __name__ == "__main__":
    sys.exit(main())
