"""Persistent app settings (theme, Discord, etc.).

Lives at ~/.config/tide/settings.toml. The settings dialog is the user-
facing surface; this module just handles read/write. We never require
the user to hand-edit this file.
"""
from __future__ import annotations

import os
import re
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
    # What the member list shows after the verb: "song" (line 1) | "app".
    discord_status_display: str = "song"
    # Link title / artist / cover to the track's page, with a listen button.
    discord_links: bool = True
    # The tray's "hide from discord" switch. Kept across restarts, so a
    # hidden presence doesn't come back on its own.
    discord_hidden: bool = False
    # Never share a local file.
    discord_hide_local: bool = False
    volume: int = 80
    sleep_preset_minutes: int = 30
    mini_mode_default: bool = False
    # v1.2.7 mini player redo — the dedicated frameless window (ui/mini.py).
    # Backdrop: "follow" = use adaptive_background_style; or a concrete
    # style slug from backdrops.SLUGS, or "off" for a flat card.
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
    # Adaptive backdrop style: one of backdrops.SLUGS.
    adaptive_background_style: str = "field"
    # When True (and adaptive_background is on), that gradient also swells /
    # brightens on heavy bass, app-wide while playing. Needs the monitor
    # capture running, so it costs a little constant CPU during playback.
    adaptive_pulse: bool = False
    # When True, fg/dim are held above a WCAG contrast floor against the
    # surfaces behind them (see tide/contrast.py). Default ON: the light
    # themes ship greys that wash out on their own background, and text
    # you can't read is never the aesthetic anyone chose.
    adaptive_text_contrast: bool = True
    # Corner softness: "sharp" (0px), "soft" (6px), "rounded" (12px). Applied
    # via a persistent radius override on the theming manager so it doesn't
    # get cleared when the adaptive driver clears its dynamic overrides.
    corner_style: str = "sharp"
    # Nav-rail icon set: "off" | "brutalist" | "geometric" | "retro" |
    # "minimal". Picks a small unicode glyph rendered before each nav label.
    nav_icon_set: str = "off"
    # The rail folded to icons (Ctrl+B / the chevron at its foot). Only
    # possible with an icon set on; otherwise the toggle hides itself.
    nav_rail_collapsed: bool = False
    # How the title changes when the track does: "scramble" | "sweep" |
    # "rise" | "off" (see ui/text_fx.py). Motion "off" makes any of them
    # instant.
    text_transition: str = "scramble"
    # How the album art changes when the track does: "flip" | "slide" |
    # "pop" | "blocks" | "fade" | "off" (see ui/art_fx.py). Motion "off"
    # makes any of them instant.
    art_transition: str = "flip"
    # Lyric type, relative to the body text: "small" | "medium" | "large"
    # | "huge" (see ui/lyrics.py SIZES). The ui scale multiplies it too.
    lyrics_size: str = "large"
    # Window + tray icon: "theme" draws it in the active theme's colours
    # (see ui/app_icon.py), "classic" keeps the launcher's night-blue one.
    app_icon: str = "theme"
    # Tray icon: "mono" is a one-colour glyph (white on a dark panel, dark
    # on a light one, from the system's light/dark setting), "app" is the
    # window icon.
    tray_icon: str = "mono"
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
    # (spotify_client_id lived here through v1.x, unread; deleted in 2.0
    # — load()'s unknown-key filter drops the stale key from old files)
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
    # JSON. Stored as a string field because at the time the TOML
    # serializer only handled one level of nesting (it learned sub-tables
    # in 2.0); it stays a JSON string so 1.x files keep round-tripping.
    audio_fx_state: str = ""
    # v1.2.3 — UI sounds (nav clicks, modal pops, toggle chirps). Auto-
    # muted while music is playing. Default off so a fresh install is
    # silent until the user opts in via Settings → appearance.
    ui_sounds_enabled: bool = False
    # v1.2.5 — instant play. Hover/press pre-resolve of stream URLs, and
    # how many top search results to warm in the background (0 = off).
    prefetch_hover: bool = True
    prefetch_warm_results: int = 3
    # What radio refills read (sources.ytmusic._RADIO_MIXES):
    # "balanced" alternates YouTube's Familiar and All tuners, "familiar"
    # / "discover" read one tuner, "all" is the plain radio tide used to
    # play. Read at refill time.
    radio_mix: str = "balanced"
    # v1.5 — report plays back to the active source's own history (YouTube
    # Music: the same videostats ping the web player sends), so the
    # account's recommendations learn from what plays in tide. Privacy
    # decision, so it is CHOSEN in the onboarding wizard, not defaulted on;
    # upgraders who never see the wizard stay off until they opt in via
    # Settings → integrations.
    report_plays: bool = False
    # Stamped True once the wizard's play-reporting step (or the settings
    # toggle) has been answered — so upgraders get a one-time settings
    # pointer instead of silently never learning the feature exists.
    report_plays_answered: bool = False
    # v1.5 home engine. "patterns" = the block/pattern home (hero, grids,
    # mosaics, charts, moods); "shelves" = the v1.4 plain shelf rows.
    home_layout: str = "patterns"
    # v1.6 fullscreen mode — the lean-back now-playing window (ui/fullscreen.py),
    # opened with F11 or the [⤢] strip button. Backdrop: "follow" = use
    # adaptive_background_style; or a concrete style slug, or "off" for flat.
    # Same choices as the mini player.
    fullscreen_backdrop_style: str = "follow"
    # Remembered side pane on the right half: "lyrics" | "queue" | "off".
    fullscreen_pane: str = "lyrics"
    # Fullscreen backdrop swells with the bass envelope (shares the mini's
    # capture consumer; only runs while the window is up).
    fullscreen_pulse: bool = True
    # v2.0 keymap editor bindings: action id -> key sequence; missing =
    # the default binding. Global — muscle memory doesn't flip with the
    # personality, so NOT a preset stash field.
    keymap: dict = field(default_factory=dict)
    # v2.0 glyph editor overrides: glyph key (tide.glyphs.KEYS) -> 1-3
    # char string. Chrome flips, so this IS in presets.STASH_FIELDS.
    glyph_overrides: dict = field(default_factory=dict)
    # v2.0 personality presets. ``preset`` is the active id: "" (pre-2.0
    # config never adopted), "brutalist", or "modern". ``preset_chosen``
    # flips True only on an EXPLICIT pick — silent adoption keeps it
    # False so the chooser can still offer itself later.
    preset: str = ""
    preset_chosen: bool = False
    # Per-personality stash (presets.STASH_FIELDS): preset id ->
    # {settings field -> stashed value}. Flips round-trip through here.
    preset_state: dict = field(default_factory=dict)
    # v2.0 theme picker escape hatch: list the whole catalog, not just
    # the personality's aesthetic. A cross-line pick still flips the tide
    # (window._reconcile_preset_after_dialog). NOT a stash field — about
    # the picker, not part of either look.
    theme_picker_show_all: bool = False
    # Remembered main-window size per layout: layout slug -> [w, h].
    window_sizes: dict = field(default_factory=dict)


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(k: str) -> str:
    k = str(k)
    if _BARE_KEY.match(k):
        return k
    return '"' + k.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_value(v) -> str:
    """One value → toml source. Handles the shapes settings actually hold:
    scalars, lists of scalars, and dicts of scalars (rendered inline)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        inner = ", ".join(f"{_toml_key(k)} = {_toml_value(x)}" for k, x in v.items())
        return "{ " + inner + " }"
    # naive string quoting — values are alphanumeric/punctuation only here
    sv = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{sv}"'


def _to_toml(s: Settings) -> str:
    out: list[str] = []
    tables: list[str] = []
    for f in fields(s):
        val = getattr(s, f.name)
        if isinstance(val, dict):
            # Serialize as a [table] at the bottom; dict values (preset
            # stashes) become [field.sub] sub-tables, which must trail the
            # scalars — toml assigns everything after a sub-header to it.
            tables.append(f"\n[{f.name}]")
            subs: list[str] = []
            for k, v in val.items():
                if isinstance(v, dict):
                    subs.append(f"\n[{f.name}.{_toml_key(k)}]")
                    subs.extend(
                        f"{_toml_key(sk)} = {_toml_value(sv)}"
                        for sk, sv in v.items()
                    )
                else:
                    tables.append(f"{_toml_key(k)} = {_toml_value(v)}")
            tables.extend(subs)
        else:
            out.append(f"{f.name} = {_toml_value(val)}")
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
    _retire_backdrops(filtered)
    # If a settings file exists at all, the user is past first launch —
    # the file only gets written by `save()` which only runs after the
    # wizard, in-app settings dialog, etc. Auto-stamp existing configs so
    # users upgrading from pre-wizard versions don't get re-onboarded.
    filtered.setdefault("first_launch_complete", True)
    return Settings(**filtered)


_BACKDROP_FIELDS = (
    "adaptive_background_style",
    "mini_backdrop_style",
    "fullscreen_backdrop_style",
)


def _retire_backdrops(raw: dict) -> None:
    """Rewrite backdrop picks that name a style tide no longer ships,
    top level and every personality's stash, to the style that replaced
    it."""
    from .backdrops import RETIRED

    def fix(d) -> None:
        if not isinstance(d, dict):
            return
        for key in _BACKDROP_FIELDS:
            value = d.get(key)
            if isinstance(value, str) and value in RETIRED:
                d[key] = RETIRED[value]

    fix(raw)
    stash = raw.get("preset_state")
    if isinstance(stash, dict):
        for state in stash.values():
            fix(state)


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


def save_fields(s: Settings, *names: str) -> None:
    """Load-modify-save: re-parse the disk file and copy ONLY the named
    fields from ``s`` onto it. Concurrent savers hold stale ``Settings``
    objects — a whole-object ``save()`` from one reverts what another
    wrote in between. Full ``save(s)`` when nothing on disk parses.
    """
    known = {f.name for f in fields(Settings)}
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ValueError(f"unknown settings field(s): {', '.join(unknown)}")
    raw = _try_parse(config.SETTINGS_FILE)
    if raw is None:
        # Same fallback chain as load(): the .bak is the last known good.
        raw = _try_parse(_backup_path(config.SETTINGS_FILE))
    if raw is None:
        save(s)
        return
    filtered = {k: v for k, v in raw.items() if k in known}
    # Match load()'s upgrade stamp so a merge over a pre-wizard file can't
    # write first_launch_complete = false and re-onboard the user.
    filtered.setdefault("first_launch_complete", True)
    disk = Settings(**filtered)
    for name in names:
        setattr(disk, name, getattr(s, name))
    save(disk)


def ensure_v1_backup() -> None:
    """One-shot downgrade shield: archive the last 1.x settings file as
    settings.toml.v1.bak before preset fields land in it — a 1.x run
    after a 2.0 one drops unknown keys on its next save(), silently
    shedding the preset state. Best-effort — never raises."""
    try:
        path = config.SETTINGS_FILE
        v1 = path.with_name(path.name + ".v1.bak")
        if v1.exists():
            return
        raw = _try_parse(path)
        if raw is None or "preset" in raw:
            # Missing/corrupt, or already written by a 2.0 build.
            return
        _atomic_write(v1, path.read_text(encoding="utf-8"), fsync=False)
    except Exception:
        pass
