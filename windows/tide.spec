# PyInstaller spec for the Windows build. From the repo root:
#   pyinstaller --noconfirm --distpath build/windows/dist --workpath build/windows/work windows/tide.spec
# Expects build/windows/bin (fetch-bin.ps1) and windows/tide.ico (make_ico.py).
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
BIN = ROOT / "build" / "windows" / "bin"

# tide's bundled plugins are found by scanning plugins/ on disk and
# imported by name, and themes/layouts/fonts are read next to the modules,
# so the whole package ships as plain files ("py" mode) instead of inside
# the archive. mpris needs QtDBus, which Windows PySide6 doesn't have.
hidden = [m for m in collect_submodules("tide") if m != "tide.mpris"]
hidden += collect_submodules("yt_dlp")  # extractors load lazily
hidden += ["pypresence", "watchdog.observers", "yaml"]
# Bundled plugins aren't packages (no __init__.py), so PyInstaller never
# sees what they import. These are the ones nothing else pulls in.
hidden += ["http.server", "colorsys", "select", "urllib.request", "PySide6.QtOpenGL"]

datas = collect_data_files("tide")
datas += [
    (str(p), str(Path("tide") / p.parent.relative_to(ROOT / "src" / "tide")))
    for p in (ROOT / "src" / "tide" / "plugins").rglob("*")
    if p.is_file() and "__pycache__" not in p.parts
]
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
    module_collection_mode={"tide": "py"},
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
