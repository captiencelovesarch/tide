"""Persistent app settings (theme, Discord, etc.).

Lives at ~/.config/tide/settings.toml. The settings dialog is the user-
facing surface; this module just handles read/write. We never require
the user to hand-edit this file.
"""
from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import config


@dataclass
class Settings:
    theme: str = "brutalist-mono"
    discord_enabled: bool = False
    discord_app_id: str = ""
    volume: int = 80


def _to_toml(s: Settings) -> str:
    out: list[str] = []
    for f in fields(s):
        val = getattr(s, f.name)
        if isinstance(val, bool):
            out.append(f"{f.name} = {'true' if val else 'false'}")
        elif isinstance(val, (int, float)):
            out.append(f"{f.name} = {val}")
        else:
            # naive string quoting — values are alphanumeric/punctuation only here
            escaped = str(val).replace("\\", "\\\\").replace('"', '\\"')
            out.append(f'{f.name} = "{escaped}"')
    return "\n".join(out) + "\n"


def load() -> Settings:
    path = config.SETTINGS_FILE
    if not path.is_file():
        return Settings()
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except Exception:
        return Settings()
    known = {f.name for f in fields(Settings)}
    filtered = {k: v for k, v in raw.items() if k in known}
    return Settings(**filtered)


def save(s: Settings) -> None:
    path = config.SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(_to_toml(s))
    tmp.replace(path)
