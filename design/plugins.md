# plugins

> **status: building.** written 2026-10-18 against 2.3.0 (`8bdf4a3`);
> line references below are from that tree. built so far (2026-10-19):
> the host (`plugin_host.py`, `plugin_api.py`, phase 1), the plugins tab in
> settings (`ui/plugins_tab.py`, phase 2), and the steamvr plugin's first
> slice (`plugins/steamvr/`, see design/steamvr.md). not yet: the ui hooks
> (views, tiles, tray, bar items, shortcuts), `ctx.lyrics`, `ctx.beats`,
> `ctx.audio`, and moving features out of core (phase 3).

the goal: tide keeps growing sideways (presence, scrobbling, a rhythm game
in the visualizer, an fx rack, an update nag). plugins let those live
outside the core, switched on or off from settings, without a restart. the
first big new one is a steamvr overlay: a floating now-playing panel you
can point at, poke and grab.

---

## the short version

- a plugin is a folder with a `plugin.toml` and some python. tide finds them
  in three places, same as themes.
- it gets one object, `ctx`, and talks to tide only through it. everything
  it registers through `ctx` is undone automatically when it's switched off,
  so on/off is live, no restart.
- its settings are declared in `plugin.toml`, so they always show up in the
  settings dialog (even while the plugin is off), and it can't read a
  setting it didn't declare. the "every setting is in the gui" rule holds by
  construction.
- trust: plugins are normal python with your permissions. no sandbox. tide
  says so plainly, only loads plugins you switched on, and has a safe mode
  for when one crashes tide.
- extract first: update check, listenbrainz, discord (in that order, each
  easier than it sounds). then the extra visualizer tiles. leave the mini
  player in core. the fx rack is possible but later.
- steamvr: doable with pyopenvr. a 1-evening spike decides whether it's
  real on your machine before any of this gets built.

## decisions for you

my pick is in bold. say the word and the build plan below follows it.

| # | question | options | why my pick |
|---|---|---|---|
| 1 | where does the steamvr plugin live? | **in tide's tree, off by default** · separate repo + AUR package | the api will churn while it's young. one repo means one commit when it does |
| 2 | does a plugin's on/off replace its old "enable" setting? (e.g. `discord_enabled`) | **yes, the plugin switch is the feature switch** · keep both | two switches for one thing is confusing |
| 3 | can a user plugin override a bundled one with the same slug? | **yes, marked "overrides bundled" in the list** · no | matches how themes work, handy for hacking on a bundled plugin |
| 4 | update check: on or off by default? | **on, except AUR/flatpak builds where the package manager already tells you** · always on | pacman users get the news from their aur helper |
| 5 | which controllers do you have? | index · vive wands · quest via steam link · other | decides whether "poke" can use real finger tracking or has to fake a fingertip |
| 6 | per-personality plugin settings? | **not in v1** · yes | nothing that's moving out today is per-preset |

---

## what a plugin looks like

```
~/.config/tide/plugins/now-playing-file/
├── plugin.toml
└── plugin.py
```

```toml
# plugin.toml
[plugin]
slug        = "now-playing-file"
name        = "now playing file"
description = "writes the current song to a text file. for obs."
version     = "1.0.0"
api         = 1                 # the tide plugin api it was written for
entry       = "plugin:NowPlayingFile"
requires    = []                # python modules it needs, e.g. ["pypresence"]
uses        = ["playback"]      # shown in the plugin list, see "trust"

[[setting]]
key     = "path"
label   = "write to"
kind    = "str"
default = "~/now-playing.txt"
tooltip = "obs: add a text source, read from file."

[[setting]]
key     = "template"
label   = "format"
kind    = "str"
default = "{artists} · {title}"
```

```python
# plugin.py
from pathlib import Path


class NowPlayingFile:
    def start(self, ctx):
        self.ctx = ctx
        ctx.playback.track_changed.connect(self.write)
        ctx.settings.changed.connect(lambda _keys: self.write(ctx.track.current()))
        self.write(ctx.track.current())

    def write(self, track):
        path = Path(self.ctx.settings["path"]).expanduser()
        text = self.ctx.settings["template"].format_map(track.as_dict()) if track else ""
        path.write_text(text + "\n", encoding="utf-8")

    def stop(self):
        pass   # nothing to undo: the connections above go away with ctx
```

that's the whole thing. flip it on in settings → plugins, it runs. flip it
off, it's gone.

---

## 1. the api

### where plugins live

same three roots and override order as `theming._theme_dirs()`
(`src/tide/theming.py:129`) and `layout.py:84`:

| root | who puts things there | default state |
|---|---|---|
| `src/tide/plugins/<slug>/` | tide itself (bundled) | whatever the manifest's `default` says |
| `/usr/share/tide/plugins/<slug>/` | distro / AUR packages | off |
| `~/.config/tide/plugins/<slug>/` | you | off |

a plugin nobody switched on never gets imported. tide only reads its
`plugin.toml` to list it. new constants go in `config.py` next to
`USER_THEMES_DIR`: `USER_PLUGINS_DIR`, `PLUGIN_SETTINGS_DIR`
(`~/.config/tide/plugin-settings/`), `PLUGIN_DATA_DIR`
(`~/.local/share/tide/plugins/`).

### plugin.toml

| key | required | meaning |
|---|---|---|
| `slug` | yes | `[a-z0-9-]+`, unique. folder name if missing |
| `name` | yes | shown in settings, lowercase |
| `description` | yes | one line, tide's voice |
| `version` | yes | the plugin's own version |
| `api` | yes | major version of `tide.plugin_api`. a mismatch lists the plugin as "built for a different tide" and won't load it |
| `entry` | yes | `module:Class` inside the folder |
| `requires` | no | python import names. if one won't import, the row says "needs pypresence" and the switch is greyed |
| `uses` | no | what it touches, from a fixed list (`playback`, `queue`, `lyrics`, `audio`, `network`, `files`, `ui`). shown in the list. a label, not a lock (see trust) |
| `default` | no | `true`/`false`, bundled plugins only. user plugins always start off |
| `[[setting]]` | no | one table per setting, see below |

### lifecycle

```
           found ──(requires missing / api mismatch)──► listed, can't enable
             │
       user switches on
             ▼
   import ─► Class() ─► start(ctx) ──(raises)──► "crashed: <error>", ctx undone, off
             │
          running ◄──── settings changed ──── ctx.settings.changed(keys)
             │
       user switches off / tide quits
             ▼
   stop() ─► ctx.dispose() ─► drop tide_plugins.<slug>.* from sys.modules
```

rules:

1. **switching applies on save**, like every other setting: the dialog
   diffs, then the host starts or stops plugins live. no restart, ever
   (CONTRIBUTING's hot-swap rule).
2. **ctx undoes everything.** each plugin gets its own `ctx` with its own
   signal relay object. plugins connect to *that*, never to `Player` or
   `Queue` directly, so deleting the relay drops every connection even if
   `stop()` forgot one. views, tiles, tray entries, bar items, shortcuts,
   audio filters, lyric/beat subscriptions and background jobs are all
   registered through ctx and removed by `dispose()`.
3. **errors don't take tide down.** every call into plugin code is wrapped.
   first error of a kind is logged (same idea as `vis_deck._log_once`).
   five errors inside ten seconds and the host switches the plugin off with
   a toast: `discord plugin kept failing. turned it off`.
4. **safe mode.** before `start()` the host writes
   `~/.cache/tide/plugin-starting` with the slug, and deletes it after. if
   tide launches and finds the file, that plugin starts off and a toast says
   `steamvr plugin crashed tide last time. it's off for now`. this is the
   only defense against a native segfault (pyopenvr is a C library).
   `tide --no-plugins` skips all of them for one run.
5. **reload** button on user plugins (dev nicety): stop, purge modules,
   start. pure python reloads clean. native extensions stay loaded until
   tide restarts; that's python, not something to fix.
6. **load order.** the host boots in `app.run()` right where discord is
   wired today (`app.py:647`), after the window, tray, adaptive and ambient
   drivers exist. shutdown goes before `qthreads.join` (`app.py:922`).

### the ctx

everything a plugin gets. all of it is GUI-thread only; signals arrive on
the GUI thread. right column is what backs it in core today, so the host is
mostly thin wrappers.

| `ctx.` | what you get | backed by |
|---|---|---|
| `playback` | signals `track_changed(TrackInfo\|None)`, `state_changed("playing"\|"paused"\|"stopped")`, `position_changed(s)`, `duration_changed(s)`, `speed_changed(x)`, `ended()`. calls `play() pause() toggle() next() previous() seek(s)`, `volume` get/set, `play(track)`, `like(bool)` | `Player` signals (`player.py:54`), `Queue.current_changed`, `window._on_next_clicked` / `_on_prev_clicked` / `_play_track` / `_on_like_clicked` |
| `queue` | `tracks()`, `current_index()`, `add(t)`, `add_next(t)`, `shuffle` / `repeat` get/set, signal `changed()` | `Queue` (`queue.py:89`) |
| `track` | `current() → TrackInfo` (frozen: title, artists, album, duration, source, id, link, art_url, `as_dict()`), `art(size, callback)` → QImage, signal `art_changed(QImage)` | `sources.base.Track`, `ui/art_cache.py`, `AdaptiveDriver.art_ready` |
| `lyrics` | `subscribe()` / `unsubscribe()`. while subscribed: `timeline_changed(list[(t, line)])`, `line_changed(str\|None)`, `words_changed(...)` when word timing exists | one shared `LyricTracker` (`lyric_tracker.py`), refcounted. today discord has one and the mini has its own private one; this merges them |
| `beats` | `watch()` / `unwatch()`, `clock() → BeatClock` (map, position, speed) to read each frame, `map_changed()`, `on_pulse(fn(level))` for the bass envelope | `AmbientController.set_watching` / `beat_clock` / `add_target` (`ui/ambient.py:129-165`). the refcount already exists |
| `audio` | `spectrum()` consumer of the shared capture, `set_filter(chain)`: this plugin's slot in the mpv `af` chain | `audio_capture.feed()`, `Player.set_audio_filter_chain` (`player.py:218`). the host joins every plugin's chain in a fixed order |
| `settings` | `ctx.settings["key"]`, `set(key, value)`, signal `changed(set[str])`. undeclared key → `KeyError` | new `PluginSettings`, see below |
| `ui` | `add_view(...)`, `add_visualizer_tile(...)`, `add_tray_action(...)`, `add_bar_item(...)`, `add_shortcut(...)`, `toast(text, action_label, on_action)` | see "ui hooks" |
| `theme` | `current()`, `token(name, default)`, `aesthetic()`, `font()`, `fonts()` (the type roles: `ui`, `title`, `numbers`, each family / weight / size % / spacing %), signal `changed()` | `theming.manager()`, `ui/typefaces.py` |
| `motion` | `dur("short")`, `ease("out")`, `intensity()`, `profile()` | `ui/motion.py`. plugins animate through this like core does, so intensity off still means nothing moves |
| `jobs` | `run(fn, on_done)` off the GUI thread, joined at stop/quit | `qthreads.py`. a plugin owning a raw QThread is how you get the "QThread destroyed while running" abort |
| `paths` | `data_dir`, `cache_dir` (per plugin, created 0700) | `config.py` |
| `music` | `search`, `library`, `open`, `play_now(track, radio=None)` (None: radio as tide's search does), `radio(track)`, `play_next`, `play(tracks, start, shuffle)` | `window._play_now` / `_start_radio` / `_queue_next` |
| `log` | `logging.getLogger("tide.plugin.<slug>")` | |
| `tide_version`, `manifest.version` | tide's version and the plugin's own, for an about line | `tide.__version__` |

the only module plugins import from tide is `tide.plugin_api` (the
`Plugin` protocol, `TrackInfo`, `BeatClock`, and for tiles the base classes
in `tide.plugin_api.vis`). anything else under `tide.*` is internal and can
break between releases. that's written in the module docstring and in the
plugin docs.

### ui hooks

| hook | what appears | core change needed |
|---|---|---|
| `add_view(id, label, factory, glyph=None, shortcut=None)` | a page in the main stack plus a nav rail button after the core ones | the stack is index-numbered by hand (`window.py:1051-1062`, map at `window.py:1829`). needs a name→widget registry. nav rail order for plugin views: after core, before settings |
| `add_visualizer_tile(kind, label, factory, knobs=())` | a choice in each tile slot | `vis_play.TILE_KINDS` and `make_tile` (`vis_play.py:45`, `:1050`) become a registry. a slot naming a kind whose plugin is off shows a "plugin off" tile instead of being rewritten, so switching it back restores the grid. `_slot_menu` (`visualizer.py:886`) reads knobs from the plugin store |
| `add_tray_action(label, fn, checkable=False, checked=None)` | an item in a plugin block of the tray menu | `TideTray._build_menu` (`ui/tray.py:61`) grows a section the host fills. replaces the discord-only `sync_discord` |
| `add_bar_item(id, label, factory)` | a control the bar customizer can place | `bar_config.normalize` drops unknown ids. it must keep `plugin:` ids so a disabled plugin doesn't erase your bar layout |
| `add_shortcut(id, label, default, fn)` | a row in the keymap editor under "plugins" | `ShortcutAction` list (`window.py:377`) becomes extendable |
| `toast(...)` | a toast | `ui/toast.show_toast(window.toast_host(), ...)` |

rule for any plugin ui: no tooltips-as-docs, lowercase, tide's widgets
(`BracketButton`, `Heading`) so it looks like tide under both personalities.

### settings

this is the part with the most rules behind it, so it gets its own section.

**declare.** settings are `[[setting]]` tables in `plugin.toml`, same fields
as `OptionDesc` (`ui/settings_schema.py:35`): `key label kind default
tooltip choices advanced`, plus three new optional ones:

- `secret = true`: masked line edit (the listenbrainz token gets this today
  through a special case in `ui/settings.py:158`)
- `help_url`: a `get one →` button beside it (discord app id and the lb
  token both have hand-built ones now, `ui/settings.py:495-506`)
- `tab`: defaults to `"plugins"`. discord and listenbrainz can say
  `"integrations"` so nothing moves on screen for existing users

`choices` is an inline list, or the name of a function in the plugin's
module (resolved only while the plugin is on; while it's off the combo
shows the saved value only).

**why the manifest and not python:** the dialog has to show discord's app id
field before discord is on. reading toml doesn't run anyone's code.

**store.** `~/.config/tide/plugin-settings/<slug>.toml`, 0600, one file per
plugin, written atomically with the same `_atomic_write` as
`settings.toml`. which plugins are on lives in
`~/.config/tide/plugins.toml` (`[enabled] discord = true`). they can't go
inside `settings.toml`: `settings.load()` drops every key that isn't a
`Settings` field (`settings.py:454`) and `save()` rewrites the whole file,
so a `[plugins]` table would vanish on the first save.

**show.** `settings_schema` gains `plugin_descriptors()`, which turns every
discovered manifest's settings into `OptionDesc`s with key
`plugin:<slug>:<key>` and a `store` pointing at that plugin's file. the
dialog (`ui/settings.py`) reads and writes values through a small store
adapter instead of `getattr(settings, key)`. the plugins tab is one section
per plugin: the on/off switch first, its settings below it, greyed while
off (the existing `ENABLE_RULES` mechanism, `ui/settings.py:169`).

**apply.** on accept the dialog splits changed keys: core keys go to
`window.run_live_appliers` like today, plugin keys go to that plugin's
`ctx.settings.changed`.

**test.** `tests/test_plugins.py`:

- every bundled `plugin.toml` parses, has a known `api`, valid kinds, tabs
  and non-empty labels (mirrors `test_settings_schema.py:99-104`)
- every declared default type-checks against its kind
- each bundled plugin starts and stops under a fake ctx, offscreen, and the
  fake store raises on any undeclared key. that's the meta-test's job for
  plugins: a plugin can't have a hidden setting because it can't read one
- `coverage_report()` stays as is: when a feature moves out, its fields
  leave `Settings` and its descriptors leave `REGISTRY` in the same commit

**migrate.** when a feature moves out, the host copies its old keys out of
the raw `settings.toml` into the plugin file once, before anything calls
`settings.save()`, and maps the old `*_enabled` to the plugin switch. same
shape as `_retire_backdrops` (`settings.py:494`). downgrading after that
loses those settings; `settings.toml.bak` still has them.

---

## 2. what moves out of core

| feature | size | tangle | cost | verdict |
|---|---|---|---|---|
| update check | `updates.py` 118 + ~40 lines in `app.py` | none | ½ day | **first.** the hello-world plugin |
| listenbrainz | `listenbrainz.py` 186 | low | ½ day | **second** |
| discord presence | `discord_rpc.py` 830 | medium, but all of it is wiring | 1–2 days | **third.** proves lyrics, tray and settings hooks |
| extra visualizer tiles | river, spectrogram, halo, tapper in `vis_play.py` (~700 of 1194) | medium | 2 days | **fourth** |
| fx rack | `audio_fx.py` 599, `audio_fx_view.py` 918, `audio_fx_popover.py` 538, `fx_ir.py` 235 | high | 3–4 days | later, maybe |
| mini player | `ui/mini.py` 1289 | high | a week+ | **keep in core** |

### update check · trivial

- today: `app.py:749-785` builds a signal bridge and calls
  `updates.check_in_background` unless flatpak. there is **no setting** for
  it at all, which already breaks the "every setting in the gui" spirit.
- as a plugin: `plugins/update-check/` with `updates.py` moved in, the toast
  through `ctx.toast`. the plugin switch becomes the missing setting.
- touches: `app.py` (delete the block), `config.in_flatpak()` read in the
  manifest's default or at start. tests: 5 files mention updates; most are
  the semver parser, they move with it.

### listenbrainz · easy

- depends only on `Player` + `Queue` signals and two settings.
- wiring to remove: `app.py:664-668`, `window.apply_listenbrainz_setting`
  (`window.py:5492`) and its `LIVE_APPLY_ORDER` entry, two `Settings`
  fields, two `OptionDesc`s (`settings_schema.py:902-915`), the widget-attr,
  placeholder and help-link special cases in `ui/settings.py`
  (`:128`, `:158`, `:501`), and the onboarding integrations step
  (`ui/onboarding.py:973-1033`).
- onboarding keeps its integrations page but writes to the plugin store via
  a small `plugins.settings_for(slug)` helper.
- manifest: `secret = true` token, `help_url`, `tab = "integrations"`,
  `uses = ["playback", "network"]`.

### discord presence · medium, mostly mechanical

- `DiscordPresence` is already self-contained: it listens to the queue and
  player (`discord_rpc.py:254-260`), takes options from
  `presence_options(settings)` and a lyric timeline. it never reaches into
  the window. the class moves as is.
- wiring to remove:
  - `app.py:40`, `:647-662`, `:787-788`, `:909`
  - `window.apply_discord_setting` + `set_discord_hidden`
    (`window.py:5460-5490`)
  - tray: `discord_action`, `sync_discord`, `_on_discord_toggled`
    (`ui/tray.py:81-104`) → one `ctx.ui.add_tray_action("hide from
    discord", checkable=True)`
  - 12 `discord_*` fields in `Settings`, 12 descriptors, the attr map,
    placeholders, enable rule and help button in `ui/settings.py`
    (`:116-127`, `:155-157`, `:169-180`, `:495`)
  - onboarding's discord half
- the headless `LyricTracker` exists only for discord today. it becomes the
  shared backend of `ctx.lyrics`.
- `python-pypresence` stays an optdepend; the manifest says
  `requires = ["pypresence"]` and the row shows "needs pypresence" without it.
- tests: 3 files.

### extra visualizer tiles · medium

- the tile interface is already plugin-shaped: `TileMixin`
  (`vis_deck.py:1536`) with `on_frame / settings_changed / theme_changed /
  track_changed / release` and a `KNOBS` tuple. `make_tile` is a switch on
  `kind`.
- keep in core: spectrum, mirror, scope, deck, lyrics, classic. move into
  one bundled plugin `more-tiles` (default on): river, spectrogram, halo,
  tapper. the tapper is a rhythm game living inside a music player's
  visualizer; that's the feature creep you mean.
- work: tile registry (see ui hooks), export `CanvasTile`, `Frame`,
  `Palette`, `bloom`, `paint_outline` through `tide.plugin_api.vis`,
  move ~10 `visualizer_<tile>_*` fields to the plugin store (with
  migration), teach `_slot_menu` to read plugin knobs. `RETIRED_VISUALIZER_TILES`
  (`settings.py:467`) shows the slot fallback already has a pattern.
- tests: 6 files.

### fx rack · hard, later

- it's a view (stack index 10, nav button `fx`, Ctrl+8), a bar button
  (`fx` in `bar_config.TOOLS`), a settings-dialog button, a nav glyph in
  every icon set, and ~90 lines of debounced push/save logic inside
  `MainWindow` (`window.py:4167-4257`). plus boot-time chain push in
  `app.py:835-845`. 49 references in `window.py`, 9 test files.
- doable once `add_view`, `add_bar_item` and `ctx.audio.set_filter` exist,
  and it's a good test of them. but it's a core music feature for a lot of
  people. do it after the others, if the core still feels heavy.

### mini player · keep in core

- it's a window mode, a peer of fullscreen, not an add-on. it reaches into
  `window.player`, `volume`, `prev_btn`, `next_btn`, `like_btn`, `api`,
  `_last_position`; `set_mini_mode` coordinates adaptive, ambient and the
  fullscreen hand-off (`window.py:4981-5060`); its 9 settings are stashed per
  personality; 35 test files mention it.
- extracting it means a window-mode api that nothing else needs. not worth
  it. it does get one cleanup for free: its private lyric tracker can use
  the shared one.

### also worth a look later

sources already have a plugin-shaped interface (`MusicSource`,
`sources/base.py:66`, registered in `app.py:502-550`). a
`ctx.sources.register(source)` hook would let bandcamp, mixcloud and
soundcloud (80–111 lines each) move out too. ui sounds, the sleep timer and
the lyric timing editor are other candidates. none of these need doing now.

---

## 3. trust

plugins are python running inside tide, with your user's permissions. a
plugin can read your youtube music cookies, your spotify token, any file you
can. no in-process trick changes that, and pretending otherwise would be
worse than saying it.

### what SECURITY.md should say

new section, after "reporting a vulnerability" (not editing SECURITY.md in
this session, this is the proposed text):

> ## plugins
>
> a plugin is python code that runs inside tide with your permissions. it
> can do anything tide can: read your sign-in cookies and tokens, read and
> write your files, talk to the network. tide doesn't sandbox plugins.
>
> - bundled plugins (the ones in tide's own tree) are reviewed like the rest
>   of tide and covered by this policy.
> - plugins you install yourself are as safe as their author. read the code
>   or trust the person, same as an aur package.
> - tide never turns on a plugin you added. new ones show up in settings →
>   plugins switched off.
> - the "uses" line in the plugin list is what the author says it touches.
>   tide shows it, it doesn't enforce it.
>
> worth reporting: a way to get a plugin loaded without switching it on, a
> bundled plugin leaking credentials, or tide's plugin api handing out
> credentials it shouldn't. not a vulnerability: a third-party plugin doing
> something bad after you turned it on.

### cheap things worth doing

| do | why |
|---|---|
| off until switched on, for anything not bundled | a folder something else dropped in doesn't run on next launch. not a security boundary (anything that can write there can write `~/.bashrc`), but no surprises |
| `ctx` hands out no credentials | no cookie jar, no tokens, no source objects. stops accidental leaks and makes "this plugin asks for your session" a visible code smell |
| plugin settings files 0600, plugin dirs 0700 | same treatment as `config.ensure_dirs()` gives the rest |
| `uses` shown in the list | honest labelling. costs nothing |
| safe mode + `--no-plugins` | a crash loop from a native lib is the most likely real problem |

### not worth doing

| skip | why |
|---|---|
| running plugins in a subprocess | only works for headless plugins (no views, no tiles, no vr panel). it'd split the api in two for a fraction of plugins |
| permission enforcement (blocking imports, `sys.addaudithook`) | python can't sandbox python. any of it is bypassed in one line. it would only make people think they're safe |
| code signing / hash pinning | solo project, no store, no signing infra. whoever can change the files can change the pins |

flatpak note: the flatpak build already sandboxes the whole app, so plugins
inherit that. it also means the steamvr plugin can't reach steamvr from the
flatpak (no access to `~/.steam` or the vrcompositor ipc). steamvr is
aur/source only.

---

## the first update's plugins (2026-10-19)

steamvr, lights and kde desktop ship as the first update's three, all
off by default (2026-10-21: kde desktop took the overlay's place, since
arch's obs-studio is built without the browser source and the overlay
couldn't be tried; the overlay stays in the tree as an extra). more come
later, each one optional. none of them needed new core hooks.

### the beat, shared

`tide.plugin_api` has what turns `ctx.beats.pulse` into what tide's
backdrop shows, so a beat looks the same in vr, on your lights and on
stream: `Envelope` (straight up, 30 ms down), `FlashLimiter` (three full
flashes a second; past that a hit still shows, as a step just under a
flash), `Kicks` (a rise over a trough, one per attack), `glow_level`,
`STRENGTH`, `PULSE_MODES`, `cover_colors(img)`, `cover_palette(img)`.
`cover_colors` goes by area: the colour most of the cover is, and a
second only if it covers a real part of it (a small blue painting on a
red cover used to light half of everything blue). anything that lights a
room or fills a view should go through `FlashLimiter`.

### lights

- `plugins/lights/`: OpenRGB over its sdk protocol (tcp 6742, protocol
  version 0 so every server answers in the simplest layout; no library)
  and WLED over its realtime udp protocol (21324; DRGB, DNRGB past 490
  leds). the cover's two colours as a slow gradient along each device,
  at the beat's brightness.
- the show runs on a thread of its own at 60 fps; devices connect on
  short threads of their own, so one that doesn't answer never stalls
  the rest. failed ones retry every 5 s.
- letting go: OpenRGB devices get the colours they had back (their mode
  stays direct); WLED takes its own effect back two seconds after the
  last packet.
- no second app to babysit (2026-10-21): if nothing answers on a local
  OpenRGB address and OpenRGB isn't open, tide starts `openrgb --server`
  itself (no window) and stops that one when lights goes off. If OpenRGB
  is open with its server off, the status says so instead of starting a
  second copy (two would fight over the same usb devices). An
  addressable header OpenRGB reports at 0 leds gets 24 (more than are
  plugged in is harmless; OpenRGB keeps the size), so a cooler or fans
  on it light up. Both are settings, on by default.
- settings: pulse (kick / breathing), brightness, colours (cover /
  accent), when paused (dim / dark), openrgb on + address, wled
  addresses, "flash them once".
- colours for leds (2026-10-21): leds can't do pastel, a soft colour is
  mostly white and comes out washed out (igor's pink came out salmon).
  `led_color` keeps the hue at full strength, pastel pinks lean to
  magenta (how leds make pink), and the gradient travels the colour
  wheel the short way instead of blending through grey.
- tests: a fake OpenRGB server speaking protocol 0, a fake WLED (http
  info + udp).

### kde desktop

- `plugins/kde/`: the plasma accent colour is tide's own adaptive accent
  (the colour its backdrop pulses in), plain, read from `ctx.theme` when
  `theme.changed` fires; white when the adaptive driver flags the cover
  as neutral (`ambient_neutral`). it used to pick from the cover itself,
  which lagged the song and sometimes kept the song before's colour
  (2026-10-21). plasma flips selection text to black on light accents, so
  no contrast fix is needed. with the wallpaper level
  every screen's wallpaper becomes a soft blur of the cover (a new file
  name each song, so plasma crossfades). a song has to stay 1.2 s before
  the desktop changes.
- only plasma's own tools: `plasma-apply-colorscheme --accent-color`
  (it bakes the accent into the colour groups and leaves
  `[General] AccentColor` alone, so read the groups, not that key, to see
  it), kreadconfig6 / kwriteconfig6, and plasmashell's evaluateScript
  over qdbus6 for wallpapers. all of it works on wayland.
- your look is saved to restore.json before the first change and put
  back when the plugin goes off or tide quits; a leftover restore.json
  at start (a crash) is put back first. every desktop change runs in
  order on one worker thread. checked on the real desktop: kdeglobals
  came back byte for byte, three screens' wallpapers too.
- the rules (2026-10-21, the user's own; it used to flip back mid-song):
  on play the song's colour goes on and stays while it plays; paused or
  out of music, it stays 3 s and then yours comes back; a new song gets
  its colour half a second in. loading counts as playing, because a song
  change goes idle → loading → playing, and a slow stream used to make
  that gap look like a pause (yours came back, then the old song's
  colour). no setting for it any more.
- next: a live wallpaper (tide's own plasma wallpaper plugin, beating
  with the music) as the "all out" level.
- tests fake plasma's tools as scripts first on PATH, with tide's data
  folder pointed at a temp one: nothing a test runs can reach the real
  desktop.

### stream overlay

- `plugins/overlay/`: a page for an obs browser source on
  `http://127.0.0.1:7311/`, three layouts (card, lyrics, slim bar; pick
  per source with `?layout=`), live through server-sent events: the song,
  the cover, tide's colours and type roles, the lyric lines, the beat
  (flash-capped before it leaves, ~30 a second).
- 127.0.0.1 only, read only, and requests naming another host are
  refused (a page elsewhere can't read what you play through a name
  pointed at 127.0.0.1). `?preview` puts a checkerboard behind it for
  looking at it in a browser.
- the page runs its own position clock; tide corrects it on seeks and
  every 5 s.

## 4. steamvr plugin sketch

> how it should feel (summoning, attention, poke, pin) and its build order
> now live in `design/steamvr.md`. this section is the transport layer
> underneath: runtime, textures, placement, laser, deps, risks. where the two
> disagree, steamvr.md wins (it replaces the single wrist/hand/world panel
> below with a bubble → panel that follows your attention).

### what it does

a tide panel floating in vr while you're in a game: cover, title, artist,
prev / play / next / like, a seek bar, volume, and the current lyric line
(karaoke sweep when word timing exists). three ways to place it:

- **world**: hangs where you left it. grab the edge and move it.
- **hand**: rides on top of a controller like a phone in your hand.
- **wrist**: on the inside of your wrist, only shows when you look at it.

and two ways to use it: point the steamvr laser at it and pull the trigger,
or reach out and poke the buttons with your finger.

### how

**runtime.** `openvr.init(openvr.VRApplication_Overlay)` from pyopenvr.
overlay apps run next to whatever game is open, no scene of their own. if
steamvr isn't running, init fails; retry every 10 s while the plugin is on,
row status says `waiting for steamvr`. on `VREvent_Quit` call
`acknowledgeQuit_Exiting()` and shut down cleanly, or steamvr hangs on exit
waiting for tide.

**the panel.** a dedicated `VrPanel(QWidget)`, never shown on the desktop
(`WA_DontShowOnScreen`), built from tide's own widgets so it themes and
switches personality like everything else. big targets: ~4 cm buttons at
0.35 m wide / 800 px is ~90 px per button. no popups, menus, combo boxes or
tooltips: those open real desktop windows, invisible in vr.

**drawing it.** `panel.render(qimage)` on the GUI thread, then hand it to
steamvr:

1. **not `setOverlayRaw`.** the spike (2026-10-18) crashed steamvr's
   compositor with it: 800×480 frames a few times a second hit
   `ASSERT: "Vulkan transfer ring buffer overflow"` and vrcompositor
   aborted. our side reported success the whole time.
2. *the plan:* a `QOpenGLContext` + `QOffscreenSurface`, upload to a gl
   texture, `setOverlayTexture` with `TextureType_OpenGL`. ~1 ms per frame
   on our side (wayland, gl 4.6). whether steamvr takes a texture from a
   wayland (egl) context is the spike's next question.
3. *fallback:* `setOverlayFromFile` with a png per change, ~8 ms per frame
   on our side, capped at 10 fps. proven safe for a static card.

**placing it.**

- size: `setOverlayWidthInMeters(h, 0.35)`
- world: `setOverlayTransformAbsolute(h, TrackingUniverseStanding, m34)`.
  first open puts it 0.6 m in front of the hmd at chest height; the pose is
  saved in the plugin's settings.
- hand / wrist: `getTrackedDeviceIndexForControllerRole(LeftHand / RightHand)`
  then `setOverlayTransformTrackedDeviceRelative(h, index, offset)`.
- wrist show/hide: each frame, the angle between the hmd's forward vector and
  the panel's normal. under ~35° for 200 ms shows it, over ~50° hides it.

**laser input (phase 1).** steamvr does the hard part.

- `setOverlayInputMethod(h, VROverlayInputMethod_Mouse)`,
  `setOverlayMouseScale(h, (w, h))`,
  `setOverlayFlag(h, VROverlayFlags_MakeOverlaysInteractiveIfVisible, True)`
  so steamvr draws its laser when you point at the panel outside the
  dashboard. `SendVRSmoothScrollEvents` for scroll on the volume.
- a 90 Hz `QTimer` (only while visible) drains `pollNextOverlayEvent`:
  `VREvent_MouseMove / MouseButtonDown / MouseButtonUp / ScrollSmooth`.
- mouse y comes in gl space (origin bottom left). flip it, find
  `panel.childAt(p)`, send `Enter / Leave / MouseMove / Press / Release` to
  that child with `QApplication.sendEvent`. track the hovered child to send
  leave events and the pressed child so a drag on the seek bar keeps going
  after the laser drifts off it.

**poke and grab (phase 2).** this is the "physically interactive" part and
it's all on us; steamvr gives poses, not touches.

- fingertip: with index controllers, an action manifest
  (`setActionManifestPath`) with a skeleton action per hand, then
  `getSkeletalBoneData` for the index fingertip bone. on wands or quest
  controllers, fake it: a fixed point ~3 cm ahead of the controller tip.
- each frame: move the fingertip into the panel's space (inverse of the
  panel transform). `x, y` → panel pixels, `z` = distance from the surface.
  - `|z| < 4 cm` and inside the panel: hover, draw a small dot where the
    finger is.
  - `z` crosses 0 going in: press. a 1 cm dead zone before release stops
    jitter from double-tapping. buttons draw pushed in.
  - moving while pressed: drag (seek bar scrub, volume).
  - `triggerHapticVibrationAction` for a short tick on press and on release.
- grab: grip within 3 cm of the panel edge latches the panel to the hand
  (store the hand→panel offset), let go drops it there. saved.
- touches feed the same event path as the laser, so the panel code doesn't
  know which one you used.
- polish once it works: the panel border pulses on the kick via
  `ctx.beats.on_pulse`, which is very tide.

### settings it declares

`placement` (world / left hand / right hand / left wrist / right wrist),
`width` (cm), `opacity`, `lyrics` (on/off), `poke` (on/off),
`haptics` (on/off), `world_pose` (internal: saved by grab, shown as a
`reset position` button via a `kind = "action"` setting, which the settings
loader would need).

### ctx hooks it needs

playback (all of it), track + art, lyrics with word timing, beats pulse,
theme, motion, settings, a tray toggle `vr panel`, toast for errors. no
views, no tiles. so it can be built right after the discord extraction
proves those hooks, without waiting for the ui-hook work.

### deps and packaging

- `openvr` on pypi (pyopenvr, by cmbruns): 2.12.1401, aug 2025, bsd,
  `py3-none-any` wheel that loads its own `libopenvr_api`. upstream says
  the openvr sdk isn't being updated anymore.
- arch: `openvr` 2.15.6 is in `extra` (the c library), but there's **no
  `python-openvr` package in the aur** as of today. you'd publish one
  (a short pkgbuild around the pypi wheel), then tide's PKGBUILD gains:
  `'python-openvr: steamvr overlay plugin'` in `optdepends`.
- steamvr itself, a headset. nothing for the flatpak.
- the plugin ships in tide's tree (decision 1), `default = false`,
  `requires = ["openvr", "numpy"]` (numpy is already a tide dep for the
  visualizer), so without pyopenvr the row says `needs python-openvr`.

### risks

| risk | how bad | what to do |
|---|---|---|
| openvr is frozen and valve's future is openxr, which has no overlay support in steamvr | long term | fine for years: steamvr still runs openvr apps. if it ever goes, the panel code stays and only the transport changes |
| a native crash in libopenvr_api kills tide, music stops | medium | safe mode (above). keep all openvr calls on one thread, check every return code |
| linux steamvr is flaky (async reprojection, overlays vanishing after a steamvr restart) | medium | recreate overlays on reconnect, don't cache handles across a `VREvent_Quit` |
| monado / wivrn users through opencomposite or xrizer: overlay support is partial | low | say "steamvr only" in the description |
| rendering a widget each frame competes with tide's own ui | low | redraw on change only. the main window is usually hidden while you're in vr anyway |
| skeletal input needs an action manifest and shows up in steamvr's binding ui as "tide" | low | ship sane default bindings with the plugin |
| synthesized qt mouse events miss some widget behaviors (hover styles, drag thresholds) | low | the panel is purpose-built, keep its widgets simple. test each control with both inputs |

---

## build order

each phase ends with something that works and ships on its own.

**phase 0 · steamvr spike** (1–2 evenings, separate script, no tide changes. full checklist: `design/steamvr.md`, vr 0)
- [ ] `pip install --user openvr` in a venv, a 50-line script: init as
      overlay, `setOverlayRaw` a png 0.6 m in front of you
- [ ] same, but a rendered `QWidget` with a button
- [ ] laser click reaches the button
- done when: you clicked a qt button in vr. if this fails on your setup,
  the plugin system still makes sense; vr waits.

**phase 1 · the host** (2–3 days)
- [ ] `src/tide/plugin_api.py`: `Plugin` protocol, `TrackInfo`, ctx types,
      `API_VERSION = 1`
- [ ] `src/tide/plugins/__init__.py`: discover (three roots), manifest
      parsing, enable state in `plugins.toml`, start/stop, error counting,
      safe-mode stamp, `--no-plugins`
- [ ] ctx: `playback`, `queue`, `track`, `settings`, `toast`, `jobs`,
      `paths`, `log`, `theme`, `motion`
- [ ] boot in `app.py` where discord is wired today, shutdown before
      `qthreads.join`
- [ ] `tests/test_plugins.py`: manifest validation, start/stop under a fake
      ctx, undeclared-key error, ctx dispose really disconnects
- done when: the `now-playing-file` example above runs from
  `~/.config/tide/plugins/` and turning it off stops it.

**phase 2 · plugins in the settings dialog** (2 days)
- [ ] `plugin_descriptors()` in `settings_schema.py`, store adapter in
      `ui/settings.py`, `secret` / `help_url` / `tab`
- [ ] plugins tab: switch, name, description, `uses`, origin, status
      (`needs X`, `crashed: ...`, `built for a different tide`), reload for
      user plugins
- [ ] plugin switches and settings apply on save, live
- done when: you can install, configure, enable and disable a plugin
  without touching a file or restarting.

**phase 3 · first extractions** (2–3 days)
- [ ] `update-check` (and add the off switch it never had)
- [ ] `listenbrainz` with migration
- [ ] `ctx.lyrics` + `ctx.ui.add_tray_action`, then `discord` with migration
- [ ] remove the moved `Settings` fields and descriptors; meta-test passes
- done when: an existing user's discord and listenbrainz setup survives the
  upgrade untouched, and both can be switched off and on live.

**phase 4 · steamvr plugin** (see `design/steamvr.md`, vr 1 to vr 5)
- [ ] `ctx.beats`, plus the extra hooks steamvr.md lists
- [ ] publish `python-openvr` to the aur, add the optdepend
- [ ] vr 1 (it shows up), vr 2 (physical), vr 3 (the ring), vr 4 (voice), vr 5 (it lives here)
- done when: in vivecraft you touch your headphones, skip a song, look
  back at the game and it's gone.

**phase 5** folded into phase 4.

**phase 6 · ui hooks + tiles** (3 days)
- [ ] view registry, tile registry, bar items that survive the plugin
      being off, plugin shortcuts in the keymap editor
- [ ] `tide.plugin_api.vis`, then `more-tiles` with migration

**phase 7 · maybe**
- [ ] fx rack as a plugin
- [ ] `ctx.sources.register` and the small sources
- [ ] plugin docs page on the website

## open questions

- the decisions table above, especially 1 and 5.
- should the plugins tab live in settings, or get its own small window like
  the theme editor? settings is simpler; a window would leave room for a
  future "get plugins" list.
- `tide.plugin_api` stability promise: "api 1 doesn't break until tide 3"?
  needs a line in CONTRIBUTING once the host lands.
