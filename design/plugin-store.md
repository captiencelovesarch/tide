# plugin store

> **status: phases a, b and c built (2026-10-21). b and c are on branch
> `claude/plugin-store-ca174f`, not committed.** written 2026-10-21 against
> local main (`730ecd3`). builds on design/plugins.md (the host, the ctx,
> phase 3). phase d's first part (discord out, listenbrainz dropped) is built; the rest waits for your ok.

the goal: a fresh tide has no plugins. you open the store, look at a
plugin's previews, click install, and it's running. click uninstall and
it's gone. every new "wouldn't it be cool if" feature starts life as a
store plugin, not in core.

the first store opens with three plugins: **steamvr, lights, kde desktop**.
the stream overlay stays out until it's tested to your standards.

---

## the short version

- the plugins leave tide's tree and move to a new repo,
  `captiencelovesarch/tide-plugins`. tide ships the host, the api and the
  store, and nothing else.
- that repo publishes a signed **index**: every plugin's name, blurb,
  version, size, checksum and preview images. tide reads it, and the store
  page draws itself from it.
- **install** = download the zip, check it against the signed index, unpack
  it, start it. no restart, no save button.
- **uninstall** = stop it, delete its folder. its settings are kept in case
  you come back, unless you tick "forget my settings".
- a plugin carries its own python dependencies in a `vendor/` folder.
  steamvr ships pyopenvr inside its zip, so it really is one click. we no
  longer need to publish `python-openvr` to the aur.
- things a plugin can't carry (openrgb, plasma, steamvr itself) are listed
  on its store page **before** you install, with the exact command to get
  them.
- discord and listenbrainz move out of core and into the store in this
  update. people who already use them get them installed for them on
  upgrade, so nothing they set up breaks.

### an honest note on bloat

the plugins themselves are small (~800 kb all together). moving discord,
listenbrainz and the small sources out takes about 1.5k lines off a 66k
line core. so the store doesn't make tide much smaller **today**. what it
does is stop the growth: from now on, the next ten features don't go in
core. the big core pieces (window.py at 5.8k lines, the visualizer, the
backdrops) are core music player stuff and stay.

---

## decisions for you

my pick is in bold.

| # | question | options | why my pick |
|---|---|---|---|
| 1 | where does the store live? | **its own page in the main window, with a nav button** · a tab in settings · its own window | settings only applies on save and is a small modal box. a store with previews wants room, and install should act right away |
| 2 | is "installed" the same as "on"? | **yes, no separate switch** · install, then switch on | that's your vision: install and it works. a plugin you don't want running, you uninstall |
| 3 | where do a plugin's settings go? | **on its store page, under the install button, applied live** · stay in the settings dialog | one place per plugin. the settings dialog's plugins tab goes away (it gets one line: "plugins live in the store") |
| 4 | plugin updates | **an "update" button and a dot on the store's nav button. no auto-update** · update on their own | you decide when code on your machine changes. auto can come later |
| 5 | discord / listenbrainz users on upgrade | **tide installs the plugin for them once, with a toast saying so** · a toast asking them to install it | not breaking someone's discord status for an update they didn't ask for |
| 6 | the update check (design/plugins.md said move it out first) | **keep it in core, give it an off switch** · make it a plugin | a fresh tide that can't tell you it's out of date is worse. and the store already checks the network once a day, so the two share that check |
| 7 | what else moves out this update? | **discord, listenbrainz** · also bandcamp/mixcloud/soundcloud · also the extra visualizer tiles | the sources fit (tide is yt first) but need a `ctx.sources` hook. the tiles need the tile registry. both are good for the update after |
| 8 | previews | **up to 4 images plus one short looping clip (animated webp)** · images only | you're filming these plugins for tiktok anyway. a 5 second cut of that clip is the preview. qt plays animated webp on its own, no new deps |
| 9 | unsigned plugins (your own, someone's from github) | **still work from `~/.config/tide/plugins/`, marked "not from the store"** · store only | keeps hacking on plugins easy, which the whole idea depends on |

---

## the index

one json file, signed, at a fixed address in the tide-plugins repo
(github release asset, so it's cached by github's cdn).

```json
{
  "format": 1,
  "generated": "2026-11-02T18:00:00Z",
  "plugins": [
    {
      "slug": "steamvr",
      "name": "steamvr",
      "blurb": "your music, floating in vr. point at it, poke it, grab it.",
      "about": "longer text for the plugin's page...",
      "version": "1.0.0",
      "api": 1,
      "min_tide": "2.3.0",
      "size": 1843200,
      "sha256": "...",
      "url": "https://github.com/captiencelovesarch/tide-plugins/releases/download/steamvr-1.0.0/steamvr.zip",
      "uses": ["playback", "queue", "lyrics", "vr", "ui"],
      "needs": [
        {"what": "steamvr and a headset", "check": "steamvr", "how": "install steamvr from steam"}
      ],
      "author": "captience",
      "official": true,
      "repo": "https://github.com/captiencelovesarch/tide-plugins/tree/main/steamvr",
      "icon": ".../steamvr-icon.svg",
      "previews": [
        {"kind": "clip",  "url": ".../steamvr-clip.webp"},
        {"kind": "image", "url": ".../steamvr-1.png"}
      ]
    }
  ]
}
```

next to it, `index.json.sig`: an ed25519 signature of the exact bytes.

- tide has the public key built in. `python-cryptography` is already a
  dependency, nothing new.
- the private key lives with you, offline. losing it means a new key and a
  tide update, so keep a backup.
- tide refuses an index whose signature doesn't check, and a zip whose
  sha256 doesn't match the index. a checksum in a signed file covers the
  zips and images too, so they don't each need a signature.
- `api` and `min_tide` decide what shows up. a plugin built for a newer
  tide shows as "needs tide 2.4". an installed plugin whose api is older
  than tide's shows "needs an update" and stops, same as the host's
  "built for a different tide" today.

## installing, step by step

1. click install. the button becomes a progress bar (size is known from
   the index).
2. download the zip to `<cache>/plugins/downloads/`, check the sha256.
3. unpack into `<data>/plugins/installed/<slug>/`. refuse any path in the
   zip that escapes that folder (`..`, absolute paths, symlinks).
4. the host discovers it (a new root, origin `store`) and starts it.
5. the button says **uninstall**. the plugin's settings appear under it.

if anything fails, the folder is removed and the button says what went
wrong in one line ("couldn't reach the store", "the download was damaged,
try again").

uninstall: stop, remove the folder, keep `<config>/plugin-settings/<slug>`
unless "forget my settings" is ticked. `<data>/plugins/<slug>/errors.log`
goes with it.

## dependencies

**python modules** go in the zip under `vendor/`. the host puts that folder
first on the plugin's import path when it starts, and takes it off when it
stops.

- pyopenvr is a `py3-none-any` wheel that carries its own `libopenvr_api`,
  so steamvr's zip just includes it.
- rule: a plugin vendors only things tide itself doesn't import. two
  plugins vendoring two versions of the same module is the case to avoid,
  so the publish script warns if two plugins in the index vendor the same
  name.
- `requires` in plugin.toml still works for things the system provides
  (`pypresence` for discord, which stays an aur optdepend, or gets
  vendored. it's pure python, so vendoring is my pick).

**programs and hardware** go in the index's `needs` list. each has a
`check` (a program on PATH, a running process, a desktop session) and a
`how` line. the store page shows them above the install button:

- lights: "openrgb, running with its sdk server on" · `pacman -S openrgb`,
  then `openrgb --server --startminimized`
- kde desktop: "kde plasma 6" · the store greys it out on other desktops
  with "only works on kde plasma"
- steamvr: "steamvr and a headset"

installing still works with a need missing. the plugin's status says what
it's waiting for, like the host does today.

## the store page

- a `plugins` button at the bottom of the nav rail, above settings, with a
  dot when updates are waiting.
- filter chips: all · installed · updates, then categories (sources,
  lyrics, visuals, hardware, tools). search top right.
- **plugin of the week** on top: one big tile, its clip playing, and a line
  from you on why it's picked. it's a field in the signed index, so you
  change it by publishing, no tide release:
  `"featured": {"slug": "steamvr", "note": "...", "until": "2026-11-09"}`.
  past `until` (you missed a week), the tile shows the newest plugin
  instead, labelled "new", never a stale pick. it's how community plugins
  get seen.
- a grid of cards: the plugin's **icon**, name, author, one line, and the
  install button right on the card. installing turns the button into a
  progress bar, then "✓ installed". installed ones show the mark instead of
  the button.
- icons: every plugin ships `icon.svg`, a 24 px **outline** drawn in
  `currentColor`, the same rules as tide's own nav icons. that way it
  follows the active icon set (thin, regular, bold get the stroke weight
  derived like ours do) and the theme's ink. an optional `icon-filled.svg`
  is used by the filled and duotone sets. no icon: a tile with the first
  letter. themes with `show_thumbnails = false` (blackwater,
  terminal-green) drop icons and previews like they drop covers, which is
  the point of those themes.
- clips play on hover only, never a grid of loops.

### banners (picked 2026-10-21)

every card in the grid, the top of every plugin page, and the website and
socials use the plugin's **banner**: part youtube thumbnail, part ad.

- 16:9, exported 1280×720 webp. two cuts of one design:
  - **full**: the logo, "<plugin> × tide", a real capture. plugin page
    header, website, socials.
  - **grid**: background, logo, capture, no text. the card already prints
    the name under it, and text at grid size is unreadable anyway.
- **background = the plugin's own world**: steamvr gets the aurora, kde a
  plasma-style wallpaper, lights a dark room with rgb. one rule across all
  of them: dark or saturated, so the white logo and text always read.
- **the capture is real**, from the actual software. at grid size it has to
  read as one simple subject (the panel, the lit keyboard, the taskbar),
  not a busy scene.
- **logos**: a mix of the real brand mark (where its guidelines allow it,
  check each one before a banner ships), tide's own mark, and a custom
  mark per plugin. "× tide" stays plain type, never merged into someone
  else's logo.
- **icons still matter**: themes with `show_thumbnails = false` show the
  icon tile instead of the banner, and the icon is what shows in small
  places (update toasts, the installed list, the settings line).
- **a banner is required to be listed.** no banner, no store entry.
- **how they're made**: together. we both gather the material (real
  captures from the software, logos, wallpapers), claude builds the
  banner, and we iterate until it's good. community plugins get the same
  treatment when they're submitted.

### moving banners

the banner can move, but it must never be disorienting:

- only the **capture frame** moves. background, logo and text stay still,
  so the card never shifts as a whole.
- in the grid it plays on **hover only**. the plugin of the week tile may
  play on its own, as the only moving thing on the page.
- 4 to 8 seconds, looping with a crossfade at the seam, no hard jump.
- no camera whips, no fast cuts. the flash cap holds (3 flashes a second at
  most), same as the plugins themselves.
- reduced motion (tide's own setting or the system's) shows the still.
- format: a short muted webm or animated webp next to the still. the still
  is always there and shows first.
- click a card: the plugin's page. a link to the plugin's github (or
  wherever its code lives: `repo` in the index, required, so anyone can
  read what they're installing). previews on top: any mix of clips and
  screenshots, in the order the author lists them, one big viewer with a
  thumbnail strip under it. clips play muted and loop while they're the
  big one; a clip's thumbnail shows a small play mark. plugin author name, then the blurb, what it needs, what it
  uses (the trust line), size, version, and the button.
- installed: the button reads uninstall (or update, when there's one), and
  the plugin's settings sit below it, applied as you change them.
- lowercase, tide's widgets, works under both personalities, same rules as
  any plugin ui.
- offline: the page shows your installed plugins from their own manifests
  and one line saying the store can't be reached. installed plugins keep
  working.
- the index is fetched once a day (shared with the update check) and when
  you open the page if the copy is older than an hour. previews are cached
  in `<cache>/plugins/previews/`.

## publishing (your side)

in tide-plugins: `tools/publish.py <slug>`

1. reads `<slug>/plugin.toml` and `<slug>/store.toml` (blurb, about, needs,
   preview files).
2. builds the zip, vendoring what `store.toml` lists.
3. uploads the zip and previews to a github release `<slug>-<version>`.
4. rebuilds `index.json`, signs it with your key, uploads both to the
   `index` release.

the stream overlay lives in the repo but isn't in the index until you say
so.

for the flatpak (plan b): downloaded plugins live in the data dir, which the
sandbox can write. network is already allowed. nothing extra.

## what moves out of core (picked 2026-10-21)

| plugin | what's in it | size today | tangle |
|---|---|---|---|
| discord presence | `discord_rpc.py` | 830 | 16 window refs, wiring listed in design/plugins.md |
| listenbrainz | `listenbrainz.py` | 186 | 5 refs |
| spotify | `sources/spotify.py`, `auth_spotify.py`, `playback/librespot_backend.py`, `ui/spotify_signin.py` | ~2.1k | 3 refs. `python-spotipy` leaves the aur depends |
| subsonic | `sources/subsonic.py`, `ui/subsonic_signin.py` | ~850 | 2 refs |
| bandcamp, mixcloud, soundcloud | `sources/*.py` | ~300 | none. one plugin or three: open |
| local files | `sources/local.py` | 561 | low. `python-watchdog` goes with it |
| precise lyric timing | `lyric_align.py`, onnxruntime, the ~95 MB model | 628 | none in the window. lives in `lyric_timing.py`'s choice |
| voice | `speech_whisper.py` | 271 | only steamvr uses it, so it moves into steamvr's zip |
| more visualizer tiles | river, spectrogram, halo, tapper from `vis_play.py` | ~700 | needs the tile registry |
| instrumental finder | `instrumental.py` + the "mute lyrics" toggle | 329 | 49 window refs. needs a lyrics view hook |
| tide powertools | `ui/theme_editor.py`, `ui/glyph_editor.py`, `ui/keymap_editor.py` | ~1.1k | glyph editor has 47 refs. the settings dialog's buttons for them become plugin hooks |

stays in core: speed and the fx rack, mini player, fullscreen, lyrics,
queue, history, mpris, the main visualizer, sleep timer, ui sounds, art and
text transitions, the bar customizer.

new hooks this needs: `ctx.sources.register` (sources), the tile registry
(tiles), a lyrics view hook (instrumental), a settings-dialog button hook
(powertools).

**local files moving out means a fresh tide plays youtube music and nothing
else.** that's on brand (yt first), but onboarding should offer it: one
"play files from your computer too?" row with an install button.

every one of these that a user already had set up gets the auto-install on
upgrade (below). spotify and subsonic sign-ins need the most care: the
tokens move to the plugin's settings and must still work after.

## discord and listenbrainz moving out

design/plugins.md phase 3 has the wiring to remove for both (file and line
list). what's new here:

- they're published to the store like any other plugin.
- on first launch after the upgrade, a one-time migration: if
  `discord_enabled` or a listenbrainz token was set, tide installs that
  plugin from the store, copies the old settings into the plugin's
  settings, and shows a toast: "discord presence is a plugin now. we
  installed it for you."
- offline at that moment: it tries again next launch. the old settings
  stay in settings.json until the plugin is installed, then they're
  removed.

## build order

each phase leaves tide working.

**phase a · the repo and the index** (1–2 days)
- [ ] create `tide-plugins`, move steamvr, lights, kde desktop and the
      overlay into it with their history (`git filter-repo` on the paths)
- [ ] `store.toml` for the three, `tools/publish.py`, the signing key
- [ ] a first index with the three, published
- done when: a signed index.json is online and the zips match it.

**phase b · install and uninstall in the host** (2 days)
- [ ] `src/tide/store.py`: fetch + verify the index, download + verify a
      zip, safe unpack, uninstall
- [ ] the `store` root in `plugin_roots()`, drop the bundled root
- [ ] `vendor/` on the import path per plugin
- [ ] `needs` checks
- [ ] tests: bad signature, bad checksum, zip escaping its folder,
      uninstall keeps or forgets settings, vendor path is removed on stop
- done when: a test installs a plugin from a local fake index, it runs,
  and uninstalling leaves nothing behind.

**phase c · the store page** (3 days)
- [ ] the page, the nav button, cards, plugin page with previews and the
      button states (install / installing / uninstall / update / can't)
- [ ] plugin settings on the plugin page, live
- [ ] settings dialog's plugins tab replaced by the one line
- [ ] update dot
- done when: from a fresh config you install steamvr from the store, use
  it in vr, and uninstall it, without touching a terminal.

**phase d · discord and listenbrainz out** (2–3 days)
- [ ] design/plugins.md phase 3 (minus the update check)
- [ ] publish both, the upgrade migration
- [ ] update check gets its off switch
- done when: your own discord setup survives the upgrade untouched.

**phase e · release**
- [ ] the previews: clips cut from the tiktok footage
- [ ] readme + website: the store, how to write a plugin
- [ ] SECURITY.md: the signing key, what the store checks

roughly two weeks of work before the release. phases a and b don't touch
the ui at all, so they can go first while the previews get filmed.

## later

- `ctx.sources.register`, then bandcamp, mixcloud, soundcloud as plugins
- the tile registry, then the extra visualizer tiles as a plugin
- plugins from other people in the index (a review step, then you sign)
- auto-update as an opt-in
- ratings, install counts. needs a server. probably never.

---

## built (2026-10-21)

**phase b**: `src/tide/store.py` (index fetch + ed25519 check, cached copy
re-checked on read, zip download checked against size and sha256, unpack
that refuses `..`, absolute paths, backslashes, symlinks, devices and zip
bombs, install that swaps an update in only once it's whole, uninstall to a
trash folder, undo, `needs` checks, version and `min_tide` checks, the
plugin-of-the-week pick). the host has a `store` root
(`<data>/plugins/installed/`) in place of the bundled one, `rescan()`,
`installed()` / `release()` / `uninstalled()`, store plugins on by default,
and `vendor/` on `sys.path` only while the plugin runs (its modules purged
on stop). tests: `tests/test_store.py`.

**phase c**: `src/tide/ui/store_view.py`: the page (stack index 12), the
`plugins` nav button above settings with the update dot, chips, search,
the week tile, cards with banners (motion on hover only, reduced motion
shows the still, icon row when the theme drops pictures), the plugin page
(banner + previews viewer with a thumbnail strip, author, needs with live
✓ / ✗ and the command, the "can" line, size, version, read the code, the
button, forget my settings, settings applied live), and the undo toast.
the settings dialog's plugins tab is one line. `ui/plugins_tab.py` is
gone. tests: `tests/test_store_view.py`.

**phase a** (2026-10-21, its own session): `captiencelovesarch/tide-plugins`
is live with steamvr, lights and kde published and signed with your key.
tide's side: `store.PUBLIC_KEY` is that key, the version is 3.0.0 (a source
checkout now reports its own version, not the installed package's), and
the plugins, their tests and the bundled root left tide's tree. for a local
store, `tools/publish.py --local DIR` in tide-plugins.

also: the store page lists plugin folders whose plugin.toml doesn't parse,
opening it picks up new or edited folders in `~/.config/tide/plugins/`, and
your own plugins get a reload button on their page.

### index fields added while building

| field | why |
|---|---|
| `category` | the chips. one of sources, lyrics, visuals, hardware, tools; anything else reads as tools |
| `banner: {grid, full, motion}` | each `{url, sha256}`. `grid` required (no banner, no entry), the other two optional |
| `sha256` on `icon`, `icon_filled` and every preview | so the one signature covers every picture too |
| `needs[].hard` + `hard_note` | a need that greys install out (kde: "only works on kde plasma") |
| `added` | yyyy-mm-dd, picks "new" when the week's pick is stale |

the zip's root is the plugin folder (plugin.toml at the top). its
plugin.toml must match the index's slug, version and api or it isn't
installed. the slug `installed` is reserved.

checks a `needs` entry can name: `steamvr` (steamvr installed under any of
steam's usual folders), `openrgb` (on PATH), `plasma` (a kde session with
plasma's tools), plus `program:<name>`, `process:<name>` and
`desktop:<name>`. an unknown check shows the line with a `·` and no verdict.

## what's left

**phase d, first part: built 2026-10-21** (uncommitted on the branch)
- discord presence and listenbrainz left core: `discord_rpc.py`,
  `listenbrainz.py`, their 15 settings, descriptors, dialog special cases,
  window appliers, the tray's discord item and the headless lyric tracker.
  listenbrainz is dropped for good, not a plugin (your call, 2026-10-21):
  a token left in settings.toml goes with the next save, nothing installs.
  `python-pypresence` left the PKGBUILD optdepends and the pyproject extra.
- discord is a plugin in tide-plugins (`discord/`), with store.toml, an
  icon and tests (core's discord test moved there whole). it vendors
  pypresence. not published yet: it needs banners.
- `ctx.ui.add_tray_action` (discord's "hide from discord") and
  `TrackInfo.artist_link` / `album_link`.
- `plugin_migrate.py`: at launch, before settings.toml can lose the old
  keys, a user who had discord on gets their settings
  copied into the plugin's file and an install queued
  (`plugins-pending.toml`). the store installs it once it reaches the
  index, with the toast; offline it waits for next launch. each feature is
  looked at once, so uninstalling later sticks. onboarding's integrations
  step (discord only now) queues the install the same way.
- the update check has its off switch (`check_updates`, integrations →
  updates). it doesn't share the store's daily fetch yet: it's still one
  check at launch.

**phase d, the rest** (needs your ok before each leaves core): spotify,
subsonic, the small sources, local files (with the onboarding row), precise
lyric timing, voice (into steamvr), more visualizer tiles, instrumental
finder, powertools. each needs its hook first (see the table above).

**small things noticed while building**
- `tide --no-plugins` still lets the store install and start a plugin.
- a plugin of yours with the same slug as a store one overrides it, so
  installing the store one does nothing visible. the page could say so.
