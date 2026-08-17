"""Persistent app settings (theme, Discord, etc.).

Lives at ~/.config/tide/settings.toml. The settings dialog is the user-
facing surface; this module just handles read/write. We never require
the user to hand-edit this file.
"""
from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import config


@dataclass
class Settings:
    theme: str = "brutalist-mono"
    discord_enabled: bool = False
    discord_app_id: str = ""
    # Show the live synced-lyric line in the presence "state" field
    # (replaces artist · album while a line is active). Off by default —
    # lyrics get broadcast to everyone who can see the profile.
    discord_lyrics_enabled: bool = False
    # v1.4 — presence customization. Templates are format-style strings with
    # {title} {artists} {album} {source} placeholders; empty = the classic
    # defaults ("{title}" / "{artists} · {album}"). A live lyric still takes
    # over the state line while discord_lyrics_enabled is on.
    discord_details_template: str = ""
    discord_state_template: str = ""
    # Keep a "⏸ paused" presence up instead of clearing it entirely.
    discord_show_paused: bool = False
    # Include the elapsed/total progress-bar timestamps.
    discord_show_progress: bool = True
    # Presence verb: "listening" | "playing" | "watching".
    discord_activity_type: str = "listening"
    volume: int = 80
    sleep_preset_minutes: int = 30
    mini_mode_default: bool = False
    # v1.2.7 mini player redo — the dedicated frameless window (ui/mini.py).
    # Backdrop: "follow" = use adaptive_background_style; or a concrete
    # "field" | "band" | "vbeam" | "horizon" | "lightning" | "depths", or
    # "off" for a flat card.
    mini_backdrop_style: str = "follow"
    # "ring" = window border fills as the progress bar; "thin" = classic bar.
    mini_progress_style: str = "ring"
    # Live synced-lyric line under the artist.
    mini_ticker: bool = True
    # Controls fade out after the mouse leaves the mini alone for a beat.
    mini_zen: bool = True
    # Mini backdrop swells with the bass envelope (independent of the main
    # window's adaptive_pulse — it has to be; the whole mode is the backdrop).
    mini_pulse: bool = True
    # The mini's card physically swells from its center with the bass
    # (frame + ring breathe into a pre-reserved transparent gutter, so the
    # window geometry itself never changes — Wayland would anchor a growing
    # window at its top-left). Opt-in as a look preference.
    mini_pulse_resize: bool = False
    # Remembered state of the mini's lyrics panel.
    mini_lyrics_open: bool = False
    # Keep the mini above other windows. Wayland: applied by asking KWin
    # over D-Bus (no client-side hint exists); X11: WindowStaysOnTopHint.
    mini_pin: bool = False
    # Show a live visualizer canvas in place of the album-art tile. The
    # shared capture (consumer "mini") only runs while the vis page is on
    # screen and the player is actually playing.
    mini_show_visualizer: bool = False
    # "theme" = use theme default, "on" = always show, "off" = never show
    show_thumbnails: str = "theme"
    # Empty = auto-detect default sink monitor; otherwise PulseAudio source name.
    audio_device: str = ""
    listenbrainz_enabled: bool = False
    listenbrainz_token: str = ""
    layout: str = "classic"
    layout_overrides: dict = field(default_factory=dict)
    adaptive_accent: bool = False
    # Status-bar loading indicator: "off" | "numbers" | "blocks" | "dots" | "ascii".
    loading_indicator_style: str = "blocks"
    # Animation/motion intensity: "off" | "lite" | "full".
    motion: str = "lite"
    # UI scale preset: "compact" | "normal" | "large" | "huge".
    ui_scale: str = "normal"
    # Playback speed (1.0 = normal). Affects pitch unless preserve_pitch is on.
    playback_speed: float = 1.0
    # If True, mpv's scaletempo filter keeps pitch steady when speed changes.
    # Default off so the tide aesthetic is the slowed/sped-with-pitch one.
    preserve_pitch: bool = False
    # When True, the main app surface paints an album-palette backdrop (see
    # ui/central_bg.py).
    adaptive_background: bool = False
    # Adaptive backdrop style: "field" | "band" | "vbeam" | "horizon" |
    # "lightning" | "depths".
    adaptive_background_style: str = "field"
    # When True (and adaptive_background is on), that gradient also swells /
    # brightens on heavy bass, app-wide while playing. Needs the monitor
    # capture running, so it costs a little constant CPU during playback.
    adaptive_pulse: bool = False
    # Corner softness: "sharp" (0px), "soft" (6px), "rounded" (12px). Applied
    # via a persistent radius override on the theming manager so it doesn't
    # get cleared when the adaptive driver clears its dynamic overrides.
    corner_style: str = "sharp"
    # Nav-rail icon set: "off" | "brutalist" | "geometric" | "retro" |
    # "minimal". Picks a small unicode glyph rendered before each nav label.
    nav_icon_set: str = "off"
    # Font-family override. Empty = use the active theme's typography.family.
    # When set, the theming manager pushes this family on every theme apply.
    font_family_override: str = ""
    # Font-size override in points. 0 = use the active theme's size_pt.
    # The ui_scale preset still multiplies whichever base wins.
    font_size_override_pt: int = 0
    # Text-case override. Empty = use the active theme's typography.case;
    # otherwise "lower" | "upper" | "normal" | "leet" | "zalgo". Routed
    # through theming.styled_case, so it re-cases everything the theme would.
    text_case_override: str = ""
    # v1.3 — tide draws its own themed titlebar (frameless main window).
    # False = keep the compositor's native decoration.
    csd_titlebar: bool = True
    # Set to True the first time the OnboardingDialog reaches its final step
    # and the user clicks launch. False = wizard runs at next launch.
    first_launch_complete: bool = False
    # v1.2 multi-source
    active_source: str = "ytmusic"
    federated_search: bool = False
    # Per-source on/off. Keys are source slugs; values are bools.
    sources_enabled: dict = field(default_factory=lambda: {
        "ytmusic": True,
        "soundcloud": True,
        "bandcamp": True,
        "mixcloud": False,
        "local": True,
        "subsonic": False,
        "spotify": False,
    })
    local_music_dir: str = ""
    local_auto_index: bool = True
    # v1.2.1 — Spotify (Librespot backend)
    # Empty client_id falls through to the tide-shipped default. Power users
    # can paste their own dev-app client_id here (e.g. for higher rate
    # limits or to avoid tide's shared app). PKCE means no secret needed.
    spotify_client_id: str = ""
    # Audio quality: 96 / 160 / 320 kbps (320 requires Premium tier).
    spotify_bitrate: int = 320
    # Pulse/Pipe sink name passed to librespot. Empty = default sink.
    spotify_audio_device: str = ""
    # Show tide as a Spotify Connect device on the user's account. Off =
    # librespot launched with --disable-discovery so it's local-only.
    spotify_connect_enabled: bool = True
    # v1.2.1 — Subsonic / Navidrome (home music server)
    # Empty url means no server is configured; SubsonicSource registers
    # only when all three fields are populated.
    subsonic_url: str = ""
    subsonic_user: str = ""
    subsonic_pass: str = ""
    # API auth style: "salt" uses MD5(password + salt) per the Subsonic
    # spec (the safe-over-HTTP default); "plain" sends the password
    # directly via `p=` (HTTPS-only Navidrome installs).
    subsonic_auth_style: str = "salt"
    # v1.2.2 — Audio FX rack (10-band EQ + reverb + loudness norm + more).
    # ``audio_fx_state`` is the AudioFxState dataclass round-tripped as
    # JSON. Stored as a string field because the existing TOML serializer
    # only handles one level of nesting and the FX state has list-of-dict
    # slots inside it.
    audio_fx_state: str = ""
    # v1.2.3 — UI sounds (nav clicks, modal pops, toggle chirps). Auto-
    # muted while music is playing. Default off so a fresh install is
    # silent until the user opts in via Settings → appearance.
    ui_sounds_enabled: bool = False
    # v1.2.5 — instant play. Hover/press pre-resolve of stream URLs, and
    # how many top search results to warm in the background (0 = off).
    prefetch_hover: bool = True
    prefetch_warm_results: int = 3


def _to_toml(s: Settings) -> str:
    out: list[str] = []
    tables: list[str] = []
    for f in fields(s):
        val = getattr(s, f.name)
        if isinstance(val, bool):
            out.append(f"{f.name} = {'true' if val else 'false'}")
        elif isinstance(val, (int, float)):
            out.append(f"{f.name} = {val}")
        elif isinstance(val, dict):
            # Serialize as a [table] at the bottom.
            tables.append(f"\n[{f.name}]")
            for k, v in val.items():
                if isinstance(v, bool):
                    tables.append(f"{k} = {'true' if v else 'false'}")
                elif isinstance(v, (int, float)):
                    tables.append(f"{k} = {v}")
                else:
                    sv = str(v).replace("\\", "\\\\").replace('"', '\\"')
                    tables.append(f'{k} = "{sv}"')
        else:
            # naive string quoting — values are alphanumeric/punctuation only here
            escaped = str(val).replace("\\", "\\\\").replace('"', '\\"')
            out.append(f'{f.name} = "{escaped}"')
    return "\n".join(out) + "\n" + "\n".join(tables) + ("\n" if tables else "")


def _backup_path(path: Path) -> Path:
    # `settings.toml` -> `settings.toml.bak` (with_suffix would eat `.toml`).
    return path.with_name(path.name + ".bak")


def _atomic_write(path: Path, data: str, *, fsync: bool = True) -> None:
    """Write ``data`` to ``path`` via a unique same-directory temp file so a
    crash/power-loss mid-write can never leave a truncated target. The temp
    is fsync'd (when asked) before the rename and always 0o600 — settings
    can hold the subsonic password and the ListenBrainz token."""
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.chmod(tmp, 0o600)  # mkstemp default, but be explicit
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _try_parse(path: Path) -> dict | None:
    """Parse a TOML file; None if missing/unreadable/corrupt. A zero-key
    result (empty file — the classic power-loss artifact) counts as corrupt:
    ``save()`` always writes every field, so a legit file is never empty."""
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except Exception:
        return None
    return raw or None


def load() -> Settings:
    path = config.SETTINGS_FILE
    bak = _backup_path(path)
    raw = _try_parse(path)
    if raw is not None:
        # Tighten perms on files written by older versions, and refresh the
        # last-known-good backup with what we just parsed successfully.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        try:
            _atomic_write(bak, path.read_text(encoding="utf-8"), fsync=False)
        except Exception:
            pass
    else:
        raw = _try_parse(bak)
        if raw is None:
            # True first-ever launch (or both copies unreadable) — wizard runs.
            return Settings()
        # Main file is missing/corrupt but the backup parses: use it, and
        # heal the main file so nothing depends on the .bak sticking around.
        try:
            _atomic_write(path, bak.read_text(encoding="utf-8"))
        except Exception:
            pass
    known = {f.name for f in fields(Settings)}
    filtered = {k: v for k, v in raw.items() if k in known}
    # If a settings file exists at all, the user is past first launch —
    # the file only gets written by `save()` which only runs after the
    # wizard, in-app settings dialog, etc. Auto-stamp existing configs so
    # users upgrading from pre-wizard versions don't get re-onboarded.
    filtered.setdefault("first_launch_complete", True)
    return Settings(**filtered)


def save(s: Settings) -> None:
    path = config.SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _to_toml(s)
    _atomic_write(path, payload)
    # Mirror the freshly-committed content into the backup. Writing the
    # payload (rather than rotating the old file) guarantees the .bak is
    # always parseable — rotating could immortalize an already-corrupt
    # main file. Best-effort and un-fsync'd: if it tears, the next
    # successful load() rewrites it from the good main file.
    try:
        _atomic_write(_backup_path(path), payload, fsync=False)
    except Exception:
        pass
