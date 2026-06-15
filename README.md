<div align="center">

<img src="assets/icon-256.png" alt="tide" width="160" height="160" />

# tide

**a brutalist youtube music client**

native Qt6 · 11 themes · 9 visualizers · MPRIS2 · adaptive accent · zero config-file editing

[![release](https://img.shields.io/github/v/release/milkkjello-del/tide?style=flat-square&color=d4b95e&labelColor=0b0b0b)](https://github.com/milkkjello-del/tide/releases/latest)
[![license](https://img.shields.io/badge/license-GPL--3.0-d4b95e?style=flat-square&labelColor=0b0b0b)](LICENSE)
[![arch](https://img.shields.io/badge/distro-arch_linux-d4b95e?style=flat-square&labelColor=0b0b0b)](https://archlinux.org)
[![qt6](https://img.shields.io/badge/qt-6-d4b95e?style=flat-square&labelColor=0b0b0b)](https://www.qt.io)

</div>

---

<div align="center">

<img src="assets/screenshots/search.png" alt="tide — search view" width="780" />

</div>

```
yay -S tide        # release (when AUR registrations reopen)
# or right now:
git clone https://github.com/milkkjello-del/tide.git && cd tide && makepkg -si
```

then launch `tide`, click **`[import]`**, you're listening. that's the whole setup.

---

## what it is

tide is a **standalone desktop client for youtube music**. native Qt6 — not an Electron skin. it sits on top of `mpv`, talks to YouTube's catalog via `ytmusicapi`, resolves streams via `yt-dlp`, and renders everything in IBM Plex with monochrome + an accent color you can let the album cover dictate.

it was designed for one thing: to be **the music app that has a sense of itself**. no shipped defaults are middle-of-the-road. brutalist-mono ships as the default; if you don't like brutalism, swap themes from a dropdown — same app, completely different personality.

## features

### playback

- search the YouTube Music catalog (your account or anonymous)
- your **library + playlists** (liked songs first)
- **queue + radio autoplay** — when the queue ends, tide pulls a continuous radio seeded from the last track
- **lyrics** — plain from YT Music, **synced** via LRClib fallback (active line bolds + auto-scrolls)
- **drag-to-reorder queue**, sleep timer (Ctrl+I), like button (Ctrl+H), history view, mini-mode (Ctrl+M)
- **resume on launch** — quit mid-song, relaunch, picks up paused at the same position

### discovery (Ctrl+6)

- YT Music's home shelves rendered as horizontal card rows
- artist detail (top songs + albums + singles + related)
- album detail (cover + tracklist + play all / shuffle)
- search filter tabs: `[songs] [videos] [albums] [artists]`

<div align="center">

<img src="assets/screenshots/explore.png" alt="explore — home shelves" width="780" />

</div>

### look & feel

- **11 themes**, each with its own personality:

  | | font | case | controls | accent |
  |---|---|---|---|---|
  | **brutalist-mono** (default) | IBM Plex Mono | lowercase | `[play]` | amber `#d4b95e` |
  | gruvbox | IBM Plex Mono | lowercase | `[play]` | mustard |
  | terminal-green | IBM Plex Mono | UPPERCASE | `[play]` | CRT green |
  | solarized-light | IBM Plex Mono | normal | text | blue |
  | paper | IBM Plex Sans | normal | text | crimson |
  | nord | IBM Plex Sans | normal | ▶ | frost |
  | catppuccin | IBM Plex Sans | normal | ▶ | pink |
  | rose-pine | IBM Plex Sans | normal | ▶ | rose |
  | ambient | IBM Plex Sans | normal | ▶ | lavender |
  | synthwave | IBM Plex Sans | **`L33T`** | ▶ | neon magenta + cyan |
  | adaptive | IBM Plex Sans | normal | ▶ | follows current album art |

<div align="center">

<table>
<tr>
<td><img src="assets/screenshots/theme1.png" alt="theme variant 1" width="300" /></td>
<td><img src="assets/screenshots/theme2.png" alt="theme variant 2" width="300" /></td>
<td><img src="assets/screenshots/theme3.png" alt="theme variant 3" width="300" /></td>
</tr>
</table>

<sub>same app, three themes</sub>

</div>

- **4 layout presets**: `classic`, `focused`, `dj-deck`, `walkman` (portrait phone-shape) — each swaps widget variants (progress style, volume style, album-art shape, controls size, label arrangement)
- **adaptive accent** — opt-in toggle that animates the theme accent toward the current cover's dominant color
- **per-theme text case** — synthwave renders `H3110 W0R1D`, terminal-green renders `ALL CAPS`, brutalist stays lowercase

### audio visualizer (Ctrl+7, F11 for fullscreen)

9 theme-aware renderers driven by a PipeWire monitor capture + numpy FFT pipeline:

- `bars-mono` `▁▂▃▅▆▇█` — for mono themes
- `bars-filled` — gradient rectangles, sans themes
- `oscilloscope` — waveform line + halo (ambient)
- `neon-grid` — synthwave perspective grid + spectrum bars
- `circle-burst` — radial 360° spectrum
- `mirror-bars` — symmetric VU-style EQ
- `dot-matrix` — pixelated reactive grid (brutalist)
- `starfield` — particles flying toward camera, bass-driven speed
- `matrix-rain` — cascading characters

in-canvas `⚙` cog overrides renderer + audio source on the fly.

### system integration

- **MPRIS2** over QtDBus — media keys, KDE Plasma & GNOME panel controls, lockscreen art
- **Discord rich presence** — opt-in, shows `0:34 / 3:42` progress with current track + album cover (you bring your own Discord app ID)
- **ListenBrainz scrobbling** — opt-in, paste your user token
- **system tray** (KDE/GNOME) — hide-to-tray on close, full controls in the tray menu
- **daily update check** — toast when a newer release lands on GitHub

## install

### arch linux (everything is in `extra`)

```sh
git clone https://github.com/milkkjello-del/tide.git
cd tide
makepkg -si
```

tide ends up at `/usr/bin/tide`. desktop launcher + icon get installed for KDE/GNOME menus. when AUR registrations reopen, `yay -S tide` will work too.

### sign in

on first launch, tide opens a small dialog: open YT Music in your browser, sign in normally, click **`[import]`** in tide. tide reads the cookies straight out of your chromium-family browser (decrypting via your kwallet/libsecret key) and you're in.

supported browsers: Chromium, Chrome, Brave, Vivaldi, Microsoft Edge. **OAuth doesn't work** for YT Music as of 2024 — Google blocks WEB_REMIX endpoints for OAuth-bearer tokens — so cookie import is the only working path.

### other linux distros

not officially supported, but doable:

```sh
sudo apt install python3 mpv libmpv-dev fonts-ibm-plex   # debian/ubuntu equivalent
pip install --user pyside6 ytmusicapi yt-dlp python-mpv cryptography pypresence numpy sounddevice secretstorage
git clone https://github.com/milkkjello-del/tide.git
cd tide && PYTHONPATH=src python -m tide
```

no desktop launcher, no auto-icons. tested only on arch.

### macOS / Windows

probably possible, untested, doesn't make sense without MPRIS / kwallet / parec. you'd be in port-the-app territory.

## keyboard shortcuts

| key | action |
|---|---|
| `Ctrl+1` | search |
| `Ctrl+2` | library |
| `Ctrl+3` | queue |
| `Ctrl+4` | lyrics |
| `Ctrl+5` | history |
| `Ctrl+6` | explore |
| `Ctrl+7` | visualizer |
| `Ctrl+,` | settings |
| `Ctrl+F` / `Ctrl+L` | focus search bar |
| `Space` | play / pause |
| `Ctrl+→` / `Ctrl+←` | next / previous track |
| `Ctrl+↑` / `Ctrl+↓` | volume +/− 5 |
| `Ctrl+H` | like / unlike current track |
| `Ctrl+I` | sleep timer dialog |
| `Ctrl+M` | toggle mini-mode |
| `F11` | visualizer fullscreen |

right-click any track row for: play now / play next / add to queue / start radio from here / view artist.

## file locations

| path | what |
|---|---|
| `~/.config/tide/settings.toml` | theme, layout, discord, scrobbling, volume, etc. |
| `~/.config/tide/browser.json` | imported YT cookies (chmod 0600) |
| `~/.config/tide/themes/` | drop your own themes here |
| `~/.config/tide/layouts/` | drop your own layouts here |
| `~/.cache/tide/streams/` | stream URL cache (per-source, capped) |
| `~/.cache/tide/art/` | thumbnail cache (auto-pruned to 1000 newest) |
| `~/.cache/tide/lyrics/` | LRClib lyric cache |
| `~/.cache/tide/session.json` | resume-on-launch state |
| `~/.cache/tide/history.jsonl` | play history |
| `~/.local/share/tide/webview/` | leftover from old webview wizard (safe to delete) |

every settable knob is reachable from the **Settings** dialog. no config-file editing is required for anything tide ships. ever.

## tech

```
python 3.12+
PySide6 (Qt6, LGPL)
mpv + python-mpv
ytmusicapi + yt-dlp
python-cryptography      (chromium cookie decryption)
python-numpy             (visualizer FFT)
python-sounddevice       (audio capture)
parec                    (PipeWire monitor capture)
ttf-ibm-plex             (bundled font)

optional:
python-pypresence        (Discord rich presence)
python-secretstorage     (GNOME/libsecret cookie key)
kwallet                  (KDE wallet cookie key)
```

all deps live in Arch's `extra` repo. zero AUR-only python packages. the PKGBUILD is the entire dependency manifest.

## roadmap

- [x] **v1.0** — initial release (search, library, playlists, queue, lyrics, MPRIS, 10 themes)
- [x] **v1.1** — QOL kitchen sink (visualizer, scrobbling, layouts, adaptive accent, tray, history, sleep timer, mini-mode, 11 themes)
- [ ] **v1.2.0** — multi-source: + SoundCloud, Bandcamp, Mixcloud, Local files (in planning)
- [ ] **v1.2.1** — Spotify (Premium via librespot)
- [ ] **v1.2.2** — Apple Music (MusicKit JS via embedded webview)

## license

[GPL-3.0-or-later](LICENSE).

## not affiliated

not affiliated with YouTube or Google. tide uses public YT Music endpoints via [`ytmusicapi`](https://github.com/sigma67/ytmusicapi) and resolves audio streams via [`yt-dlp`](https://github.com/yt-dlp/yt-dlp). YouTube cookies you import are stored only locally.

---

<div align="center">

made with care, claude, and a lot of "lol let's just add that too"

</div>
