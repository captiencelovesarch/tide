"""Plasma's own tools, for the accent colour and the wallpaper.

All of it goes through what Plasma ships for exactly this, so it works on
wayland: plasma-apply-colorscheme --accent-color for the accent,
kreadconfig6 / kwriteconfig6 for reading and putting back kdeglobals, and
plasmashell's evaluateScript (over qdbus6) for each screen's wallpaper.

``run`` is swappable so tests never touch a real desktop.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

ACCENT_KEYS = ("AccentColor", "LastUsedCustomAccentColor", "accentColorFromWallpaper")
TOOLS = ("plasma-apply-colorscheme", "kreadconfig6", "kwriteconfig6", "qdbus6")


def _run(cmd: list[str]) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        raise OSError(f"{Path(cmd[0]).name} failed: {(out.stderr or out.stdout).strip()[:120]}")
    return out.stdout


def missing() -> str:
    """why this can't run here, or "" when it can."""
    if "KDE" not in os.environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":"):
        return "this needs kde plasma"
    gone = [t for t in TOOLS if shutil.which(t) is None]
    return f"missing {', '.join(gone)}" if gone else ""


def rgb_text(hex_color: str) -> str:
    """"#7300ff" → "115,0,255", how kdeglobals writes a colour."""
    h = hex_color.lstrip("#")
    return ",".join(str(int(h[i:i + 2], 16)) for i in (0, 2, 4))


def hex_of(rgb: str) -> str:
    """"115,0,255" → "#7300ff"."""
    r, g, b = (int(x) for x in rgb.split(",")[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


class Desk:
    def __init__(self, run: Callable[[list[str]], str] = _run) -> None:
        self.run = run

    # ---- accent ----

    def _read(self, key: str, group: str = "General") -> str:
        return self.run(["kreadconfig6", "--file", "kdeglobals", "--group", group,
                         "--key", key]).strip()

    def read_accent(self) -> dict:
        saved = {k: self._read(k) for k in ACCENT_KEYS}
        saved["ColorScheme"] = self._read("ColorScheme")
        return saved

    def set_accent(self, hex_color: str) -> None:
        self.run(["plasma-apply-colorscheme", "--accent-color", hex_color])

    def _scheme_highlight(self, scheme: str) -> str | None:
        """the colour scheme's own selection colour, for putting back an
        accent that came from the scheme."""
        for root in (Path.home() / ".local/share/color-schemes", Path("/usr/share/color-schemes")):
            f = root / f"{scheme}.colors"
            if not f.is_file():
                continue
            group = None
            for line in f.read_text(errors="replace").splitlines():
                line = line.strip()
                if line.startswith("["):
                    group = line
                elif group == "[Colors:Selection]" and line.startswith("BackgroundNormal="):
                    return hex_of(line.split("=", 1)[1])
        return None

    def restore_accent(self, saved: dict) -> None:
        own = saved.get("AccentColor", "")
        if own:
            self.set_accent(hex_of(own))
        else:
            # the accent came from the scheme: paint the scheme's own colour
            # back, then forget the custom one so settings says "from scheme"
            hl = self._scheme_highlight(saved.get("ColorScheme", ""))
            if hl:
                self.set_accent(hl)
        for key in ACCENT_KEYS:
            value = saved.get(key, "")
            if value:
                self.run(["kwriteconfig6", "--file", "kdeglobals", "--group", "General",
                          "--key", key, value])
            else:
                self.run(["kwriteconfig6", "--file", "kdeglobals", "--group", "General",
                          "--key", key, "--delete"])

    # ---- wallpaper ----

    def _script(self, js: str) -> str:
        return self.run(["qdbus6", "org.kde.plasmashell", "/PlasmaShell",
                         "org.kde.PlasmaShell.evaluateScript", js])

    def read_wallpapers(self) -> list[dict]:
        """each screen's wallpaper: its plugin, and the image when it's an
        image wallpaper."""
        js = ('var out = []; desktops().forEach(function (d) {'
              ' d.currentConfigGroup = ["Wallpaper", "org.kde.image", "General"];'
              ' out.push({id: d.id, screen: d.screen, plugin: d.wallpaperPlugin,'
              ' image: d.readConfig("Image")}); });'
              ' print(JSON.stringify(out));')
        text = self._script(js).strip()
        return json.loads(text) if text else []

    def set_wallpaper(self, path: Path) -> None:
        """the image on every screen. a new file name each time, so plasma
        crossfades to it."""
        url = json.dumps(Path(path).resolve().as_uri())
        js = ('desktops().forEach(function (d) {'
              ' d.wallpaperPlugin = "org.kde.image";'
              ' d.currentConfigGroup = ["Wallpaper", "org.kde.image", "General"];'
              f' d.writeConfig("Image", {url}); }});')
        self._script(js)

    def restore_wallpapers(self, saved: list[dict]) -> None:
        data = json.dumps(saved)
        js = (f'var saved = {data}; desktops().forEach(function (d) {{'
              ' var was = null;'
              ' saved.forEach(function (s) { if (s.id === d.id) { was = s; } });'
              ' if (!was) { saved.forEach(function (s) { if (!was && s.screen === d.screen) { was = s; } }); }'
              ' if (!was) { return; }'
              # the image setting goes back whatever the plugin was, so no
              # reference to tide's files is left behind
              ' d.wallpaperPlugin = "org.kde.image";'
              ' d.currentConfigGroup = ["Wallpaper", "org.kde.image", "General"];'
              ' d.writeConfig("Image", was.image);'
              ' if (was.plugin !== "org.kde.image") { d.wallpaperPlugin = was.plugin; }'
              ' });')
        self._script(js)
