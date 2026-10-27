# PyInstaller spec for the Windows build. From the repo root:
#   pyinstaller --noconfirm --distpath build/windows/dist --workpath build/windows/work windows/tide.spec
# Expects build/windows/bin (fetch-bin.ps1) and windows/tide.ico (make_ico.py).
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
BIN = ROOT / "build" / "windows" / "bin"

# tide.playback imports mpv, which loads libmpv-2.dll from PATH; without
# it on PATH here, collect_submodules can't import (and so skips) it.
os.environ["PATH"] = str(BIN) + os.pathsep + os.environ.get("PATH", "")

# themes/layouts/fonts are read next to the modules, so the whole package
# ships as files on disk instead of inside the archive: compiled .pyc
# ("pyc" mode), so the first launch doesn't compile all of tide first. mpris
# needs QtDBus, which Windows PySide6 doesn't have.
hidden = [m for m in collect_submodules("tide") if m != "tide.mpris"]
hidden += collect_submodules("yt_dlp")  # extractors load lazily
hidden += ["watchdog.observers", "yaml"]
# Since 3.0 plugins come from the store at runtime, so PyInstaller never
# sees what they import, and a module tide itself doesn't use isn't in the
# bundle. A store plugin can vendor pure-python packages (discord ships
# pypresence), but not the standard library or Qt. This list is what the
# store's plugins import (tide-plugins, scanned 2026-10-27); add to it when
# a plugin needs more. mutagen is a tide dependency only local files uses.
hidden += [
    "asyncio", "atexit", "base64", "bisect", "colorsys", "ctypes", "hashlib",
    "http.server", "importlib.resources", "inspect", "logging", "platform",
    "queue", "select", "socket", "sqlite3", "struct", "textwrap",
    "unicodedata", "urllib.parse", "urllib.request", "uuid", "weakref",
    "PySide6.QtOpenGL",
]
hidden += collect_submodules("mutagen")

datas = collect_data_files("tide")
datas += collect_data_files("ytmusicapi")
datas += collect_data_files("yt_dlp_ejs")
datas += copy_metadata("tide") + copy_metadata("yt-dlp") + copy_metadata("ytmusicapi")
datas += [(str(ROOT / "assets" / "icon-256.png"), "tide/assets")]

binaries = [(str(p), "bin") for p in BIN.iterdir() if p.is_file()]

a = Analysis(
    [str(ROOT / "windows" / "launch.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tide.mpris", "secretstorage", "tkinter"],
    module_collection_mode={"tide": "pyc"},
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="tide",
    icon=str(ROOT / "windows" / "tide.ico"),
    console=False,
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name="tide", upx=False)
