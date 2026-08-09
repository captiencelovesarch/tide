# Changelog

All notable changes to **tide** land here. Format roughly follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are [semver-ish](https://semver.org/) with a 4-segment patch when a release ships polish on top of an already-tagged feature drop.

The canonical source of truth for the diff lives in the [GitHub Releases](https://github.com/captiencelovesarch/tide/releases) — this file is for browsing history at a glance.

## [Unreleased]

### Changed
- **The setup wizard stopped pre-checking sources.** SoundCloud and Bandcamp arrived already ticked — a checked box the user never touched isn't a choice. Everything now starts off, and picking zero sources is fine (the app binds to a fallback until you enable one in Settings).
- **The living background is offered during setup.** The feel step only pitched the adaptive accent; the album-lit gradient that breathes with the music — arguably the app's whole look — was undiscoverable until you dug through Settings → appearance. One new toggle (on by default) enables the backdrop and the bass pulse together; the style stays "living fields" and is refinable later.

### Fixed
- **The bass arch is one hill again.** As bass pushed the arch taller, the crest bloom (drawn in the album's secondary color) climbed away from the mound while the arch body between them thinned out — brightness and hue both dipped, and the hill split into two disconnected lights. A spine glow now fills the arch in a blended hue, so the light runs continuously from the orange base to the crest.
- **tide shows up when you launch it.** With the tide titlebar enabled, the window mapped and then vanished into the tray — `setWindowFlag` unmaps a shown window, and the re-show guard read `isVisible()` *after* the flip, which is always false. The frame flags now apply before the first show (so launch maps once, correctly), and live toggles from Settings sample visibility before flipping so the re-show actually fires.
- **A disabled Spotify keeps its expired tokens to itself.** The sources panel probed Spotify's profile on every launch whenever tokens existed on disk — enabled or not — and a revoked grant raised the "spotify: token expired" toast before the window even settled. The probe now skips disabled sources, and the expiry toast is gated the same way (the Sources tab still shows the honest status; re-enabling restores the right to shout).
- **The setup wizard's [next] notices you signed in.** Completing YouTube Music sign-in (or Spotify / Subsonic / local-folder setup) inside the sources step updated the state but never told the button, so [next] stayed dead until you toggled the source off and back on. Setup outcomes now re-emit the state change.
- **The pause glyph sits on the baseline with everybody else.** `⏸` (U+23F8) only exists in symbol/emoji fallback fonts whose metrics ride high, so the pause button floated above its neighbors. It's now `▮▮` from Geometric Shapes — the same block as `▶` — so whatever font draws the play triangle draws the pause bars, at the same baseline.

## [1.3.1] — 2026-08-03 — shuffle and repeat take their seats, and the chrome gets straightened

### Added
- **Shuffle and repeat, on the control bar, where they always belonged.** `[shuffle]` draws randomly from what hasn't played this cycle (tracked by id, so queue edits can't confuse it) and `[prev]` walks back through the actual random order you heard. `[repeat]` cycles off → all → one: *all* wraps the queue (shuffled, it deals a fresh cycle instead), *one* replays the finished track — but a manual `[next]` always moves on, because a next button that refuses to go next is a prank, not a mode. Both latch in the theme's accent when on (repeat-one wears a small ¹), live in every controls variant plus the mini player, persist with the session, sync over MPRIS (`Shuffle`/`LoopStatus`, writable — KDE's media widget can drive them), and answer to Ctrl+S / Ctrl+R. The up-next line and the stream prefetcher both honor the modes, so "next:" never lies and shuffled skips stay instant.

### Changed
- **The "large" controls variant stopped dressing like a form.** Bordered 16pt boxes with an accent flood on hover are now borderless oversized glyphs that color on hover — walkman and focused finally get the "soft controls" they were promised, and six controls fit walkman's 480px window with room to breathe.
- **The nav-rail SVG icons got redrawn** on one consistent grid (uniform stroke, round caps, actual negative space). The house's roof meets its walls and it gained a door, the library is book spines (one leaning) instead of a barcode blob, the queue's play triangle is filled and aligned, the mic stands on a centered base, the settings gear has teeth attached to the ring instead of sun rays, and source is a proper swap-arrows pair. Sharper at 16px, calmer at any size.

### Fixed
- **The window buttons finally sit on one line.** The minimize dash rendered visibly lower than □ and ✕ — font glyphs never share an optical center (measured 1.5px of disagreement in IBM Plex). Modern themes now *paint* the three glyphs on a single geometric center; brutalist themes keep their `[_] [□] [✕]` text personality untouched.
- **The little black box in the bottom-right corner is gone.** It was the status bar's QSizeGrip: themes paint every bare QWidget with `background: @bg`, so the grip sat as an opaque square on the adaptive gradient. The grip is retired (edge-drag resize already covers the corner in CSD, native borders cover it otherwise) and a QSS guard keeps any future grip transparent.
- Layout mode switches now explicitly retain the audio-fx and speed buttons across the now-playing strip rebuild instead of relying on event-loop timing to rescue them from the discarded layout.

## [1.3.0] — 2026-07-30 — the mini player deserves to exist, and the window is all tide now

Mini mode used to be the main window with everything hidden, shrunk to 340×360 with its title bar still on. Full redo: it is now a **dedicated frameless window** (`ui/mini.py`) with the adaptive gradient painted edge to edge, and the old shrink-in-place behavior is gone.

### Added
- **Click the album art to enter it.** Single click on the now-playing art (any variant — square, circle, polaroid) opens the mini; clicking the mini's art brings the full window back. Ctrl+M still toggles both ways, Esc and `[⤢ expand]` exit, and drag-anywhere moves it (press-and-drag on the art works too).
- **The backdrop is alive.** The mini paints the same album-palette gradient as the adaptive theme — and it keeps working *even when both main adaptive toggles are off* (`AdaptiveDriver.set_mini_active` counts the mini as a palette consumer while it's on screen). With "bass pulse" on (default), the gradient breathes with the actual low-end envelope from the audio capture. Style is changeable from the mini's right-click menu or Settings → appearance → mini player: follow main / living fields / diagonal band / bass arch / off.
- **The window border is the progress bar.** A thin accent ring traced around the frameless edge fills clockwise with playback, and clicking anywhere along the border seeks there. Prefer the classic look? `progress: thin bar` swaps it.
- **Live lyric ticker** — one always-visible synced-lyric line under the artist, scramble-animated, fed by its own `LyricTracker` (the Discord one keeps its own enable state). Long lines don't get chopped to "…": the ticker wraps up to three lines and the window smoothly grows *down* to make room (width can't budge — Wayland anchors a widening window at its left edge — and the label's height is pinned per line, never per scramble frame, so the anti-spazz guarantee holds). The `[lyrics]` button expands a full synced-lyrics panel in place; its open/closed state is remembered.
- **`[pin]`** — keep the mini above other windows, from the button, the right-click menu, or Settings. On Wayland no client-side hint can do this (xdg-shell has no keep-above), so tide asks **KWin directly over its scripting D-Bus**; on X11 it's the plain `WindowStaysOnTopHint`. Re-applied on every show (a remap loses the flag), and on non-KDE Wayland compositors it degrades to a toast pointing at window rules.
- **Art ⇄ visualizer flip** — the album-art tile can swap for a live visualizer canvas (right-click → "visualizer tile", or the checkbox in Settings → mini player). It reuses the main visualizer's renderers (theme-picked) painting straight over the breathing backdrop, and the audio capture is only held while the mini is on screen with the vis tile showing and music actually playing.
- **Zen** — leave the mouse alone for a beat and the labels and controls fade out while the window itself draws up in one linear motion, until only the art card (gradient frame, ring and all) is left sitting on the desk. Any hover wakes it and the window grows back. Off-switchable ("auto-hide controls").
- **Lyrics always get room.** The same collapse serves the lyrics panel: if opening it would push the window past the bottom of the screen, the controls fold away immediately so the panel fits — and idle re-collapses them while the squeeze is needed, whether or not zen is enabled.
- **Bass resize (opt-in)** — with "bass resize" on, the card *physically* swells with the low-end envelope, up to 8px a side with the border ring riding along, quantized to whole pixels. It grows **from its center**: the window pre-reserves the full pad as a transparent gutter and the card breathes into it, so the window geometry never changes — necessary on Wayland, where a genuinely growing window would stay pinned at its top-left and only ever expand right/down. Flip it in the right-click menu or Settings → mini player.
- **Scroll anywhere = volume**, with a quick glow kick in the backdrop as feedback when it isn't already pulsing.
- **"start in mini player"** — the `mini_mode_default` setting existed since v1.1 and was never read; it now works and has a checkbox.
- Theme personality carries over wholesale: bracket buttons, per-theme text case (l33t in synthwave, UPPER in terminal-green), theme fonts, adaptive accent — the mini is unmistakably tide in every theme.
- **Five new themes, all original.** No ports this round — each one is tide's own: **abyss** (deep-ocean dark, bioluminescent cyan over near-black blue), **blackwater** (true #000000 for OLED panels, hairline borders, thin progress + slider, thumbnails off — nothing glows that doesn't have to), **golden hour** (warm sunset light theme — sand, espresso, burnt orange + dusty rose, polaroid album art), **storm** (slate greys, hard edges, IBM Plex Mono in UPPERCASE, `// ` list markers, one electric-yellow lightning accent, oscilloscope), and **undertow** (moody indigo depths with a single blood-red accent and a red wedge for volume). Every theme picks its own visualizer, controls, text case and widget variants — they're personalities, not palettes. (A sixth, the translucent **seaglass**, is built but shelved at `design/shelved-themes/` until its glass renders right everywhere — the `window_translucent` plumbing ships regardless.)
- **tide draws its own titlebar now.** The main window goes frameless and gets themed chrome: the bar, title and window buttons come from the active theme's palette — no more grey stranger's bar over golden hour. Full personality carry-over: brutalist themes get `[_] [□] [✕]` bracket buttons and the title runs through the theme's text case (**TIDE** in storm). Drag the bar to move (compositor-native, KDE snapping keeps working), double-click to maximize, grab any edge to resize. Prefer the system decoration? Settings → appearance → untick "tide titlebar" — it flips live.
- **Fonts are a real setting now.** The font picker lists **every** installed family (not just monospace), each row rendered in its own face so you pick by look, with tide's bundled three pinned on top. Picking previews **live** across the whole app; cancel puts everything back. And there's a font-size override (`theme default` by default) that fine-tunes on top of the ui-scale preset.

### Changed
- Compact layouts (walkman) no longer piggyback on mini mode — `apply_layout` handles chrome via its visibility flags alone, which also fixes Ctrl+M inside walkman force-showing the nav rail against the layout's wishes.
- Toasts, the tray toggle, and MPRIS `Raise` all target whichever window is active, so nothing renders into (or raises) a hidden main window while the mini is up.
- The mini's album art drops the 1px tile border (`AlbumArt.set_framed(False)`) — big art reads better bare over the gradient; list/strip tiles keep their frame.

### Fixed
- Quitting from the tray while the mini is open closes it cleanly instead of leaving a headless window keeping a dead player company; closing the mini from the compositor exits to the full window instead of stranding the app with no windows at all.
- **The mini no longer spazzes when a new lyric line lands.** The title/artist/ticker labels are pinned to the art's width: without the pin, every scramble frame re-measured the label (non-mono theme fonts give each random-glyph frame a different pixel width) and the fixed-size layout re-negotiated the *window* per frame — a visible shudder on every ticker update, zen included. Scrambles are also skipped entirely while zen has the ticker invisible.

## [1.2.7] — 2026-07-29 — the prev button works, expiry stops being a surprise, and a freeze-crash dies

Four annoyances from the first AUR release, all small on the surface and none small underneath.

### Fixed
- **The random mid-listen freeze-crash is gone — and it was a proper whodunit.** Every background worker (stream resolve, like, radio, lyrics, prefetch…) tore itself down with `thread.finished → worker.deleteLater`, which destroys the worker's Python wrapper **on the worker's own thread**: Shiboken takes Qt's pooled connection mutex inside `~QObject`, then blocks waiting for the GIL. The GUI thread holds the GIL whenever Python runs, and during any stylesheet repolish it makes hundreds of `QObject::connect` calls that want those same pooled mutexes — Qt hashes unrelated objects onto a shared mutex pool, so the two threads don't even have to touch the same object to collide. Lock inversion, permanent freeze, compositor kill. Worker destruction now happens exclusively on the GUI thread via the `qthreads` retain registry (the `worker.deleteLater` connections are gone, and the rule is documented in `qthreads.py`). Found by reproducing the deadlock *inside the test suite* — the coredump's two stacks matched the live crash exactly.
- **Full-app restyles are deferred and coalesced.** The adaptive driver re-applied the entire application stylesheet synchronously inside whatever signal was emitting, once per palette event — the exact GUI-side half of the deadlock above, and a full-app repolish per track even when nothing changed. `ThemeManager` now batches every restyle onto the next event-loop turn: a burst of palette nudges costs one repolish, an unchanged stylesheet costs zero, and `setStyleSheet` never runs inside an emission. tide also pins the **Fusion** widget style at startup, so themes render identically on every desktop instead of inheriting whatever style plugin the platform injects underneath the QSS (KDE's Breeze runs its own connect-heavy machinery during every repolish — pure trigger surface).
- **The previous-track button did something for the first time.** `queue.back()` only ever walked backwards inside the *current* queue array — but `_play_now()` clears that array on every pick, so anyone playing songs one at a time sat at row 0 forever with nothing behind them. Every press fell through to the "restart the current track" branch, which is why the button looked dead. Playback now keeps a cross-queue history (capped at 100) that survives queue replacement, and `[prev]` walks it once the queue itself runs out. Going back doesn't re-record history, so prev/prev no longer ping-pongs between the same two songs, and the restored track is inserted *before* the current one (`Queue.add_prev`) so the next press keeps moving backwards rather than turning around.
- **Double-pressing prev goes back instead of restarting twice.** The >3s "restart the song" branch left `_last_position` at its pre-seek value until mpv's next position tick, so a quick second press re-read the stale position and restarted the track again.
- **The session-expired toast is no longer clipped.** Its ✕ was `setFixedWidth(24)` while the shared button rule applied `padding: 3px 10px` — 20px of padding against 24px of width left 4px for the glyph, which rendered as a bare dot. The dismiss button is now square and unpadded, and the toast measures wrapped text with `heightForWidth` instead of the single-line size hint.
- **A dead YouTube Music session is detected even when YouTube doesn't say so.** This is the one that made playback feel haunted. An expired cookie jar doesn't reliably 401 — YouTube also just answers **HTTP 200 with the signed-out payload**, and the account menu comes back with no `activeAccountHeaderRenderer`. Nothing in that response looks like a failure, so the 401-shaped sentinel never fired: the app went on reporting "signed in" while quietly serving anonymous results, which is exactly what "randomly getting stuff you wouldn't normally get, mid-session" was. `probe_auth()` now treats an anonymous account payload as expiry, matched narrowly on the account-header path so an unrelated YouTube layout change can't turn into a bogus sign-in prompt.

### Added
- **One-click `[refresh token]`.** The expiry toast's action used to open the sign-in wizard and make you redo the whole browser dance. In practice the browser is still signed in and only *tide's copy* of the cookies went stale, so the action now re-imports them silently from whichever Chromium-family profile still holds a live session — no dialog, no browser round-trip — and rebuilds the live client in place via `YTMusicSource.reload_client()`, so nothing needs a restart. The wizard remains the fallback for when no browser has a session, which is the only case where you genuinely have to go log in again.
- **Auth heartbeat.** A cheap authenticated round-trip every 10 minutes, so a session that dies mid-listen announces itself within minutes instead of waiting for you to notice the results going wrong. Network errors are never classified as expiry, and a known-dead source isn't re-probed.
- **Cookie expiry is recorded and surfaced.** The importer already read `expires_utc` out of the browser's cookie DB and threw it away; it's now converted from Chromium's 1601 epoch and stored in a sidecar `browser_meta.json` — kept *out* of `browser.json`, which ytmusicapi reads as a literal headers dict and would put on the wire. The Sources row leads with when the session was last **verified** rather than the cookie deadline: Google invalidates sessions server-side long before the cookie lapses, so a jar stamped "expires in 394 days" can be dead today, and quoting that number would be false reassurance. The deadline only appears once it's within 14 days, and is warned about within 3.

### Notes
- Sessions imported before this release have no recorded expiry. That reads as *unknown*, never as *expired* — the countdown starts after the next import or token refresh.

## [1.2.6] — 2026-07-26 — instant play + worker-thread crash fix — first AUR release

Supersedes the never-released 1.2.5. Rolls the instant-play work up with the use-after-free fix that was tearing down launch and playback, and ships tide to the Arch User Repository for the first time — `yay -S tide`.

### Added
- **Instant play** — clicking a track used to pay the full ~4.5 s yt-dlp resolve every time, because the prefetch that was supposed to have already done the work never ran (see the worker-lifetime fix below). With prefetch actually alive, resolution now starts before you commit: hovering a row begins a background resolve, and `attach_press` starts one on *mouse-down*, before activation even fires. `_play_track` joins an in-flight resolve instead of racing a duplicate one (10 s fallback timer, one retry on failure).
- **Result warming** — staggered pre-resolve of the top N search results, restricted to slow-resolve sources and replaced wholesale on each new search.
- **Playback settings section** — hover-prefetch toggle and a warm-results picker (off/2/3/5), applied live.
- **Shutdown thread join** — `qthreads.join()` asks retained worker threads to quit and waits briefly (capped per thread), so an in-flight network resolve can't have its thread ripped out from under it at exit.

### Fixed
- **Worker threads are no longer freed while still running.** PySide6 gives *Python* ownership of a QObject built without a parent, so the C++ object is deleted as soon as the last Python reference to its wrapper drops. A worker moved onto a QThread that lost its last reference got freed from the GUI thread while its own thread was still dispatching events to it — a use-after-free surfacing as SIGSEGV inside `QObject::~QObject`, preceded by Qt's *"shared QObject was deleted directly. The program is malformed and may crash."* Signal connections don't help: PySide6 holds only a **weak** reference to a bound-method slot, so `thread.started.connect(worker.run)` keeps nothing alive. Per-instance attributes don't either — the next call overwrites them and drops the previous worker mid-flight. Every `(thread, worker)` pair now lives in a registry until the QThread emits `destroyed`, and worker threads are deliberately left unparented so a widget teardown can't abort on a still-running thread. This is what crashed tide on a fast track change, on overlapping prefetches, and on a quick second click into a library/album/artist page.
- **Prefetch workers stopped being no-ops.** `StreamPrefetch`'s resolver workers were silently garbage-collected the moment `request()` returned — the module deliberately kept no Python references, as an earlier attempt to dodge a segfault. Every hover and neighborhood prefetch in 1.2.4 therefore did nothing at all.
- **Doomed yt-dlp double round-trips are skipped.** After the authenticated pass fails, it's skipped for 30 minutes per cookiefile mtime rather than paying the failing round-trip on every resolve.

### Packaging
- First AUR release. `sha256sums` now carries a real checksum instead of `SKIP`, and the maintainer line is populated.

## [1.2.4] — 2026-07-02 — security hardening + live lyrics + bug fixes

Supersedes the never-released 1.2.3.2. A full security pass (world-readable credential fix, art-fetch SSRF/local-file-read guard, mpv protocol allowlist, Subsonic cleartext-password enforcement, and remote-metadata UI-injection hardening) lands alongside live synced lyrics in Discord Rich Presence, the app-wide adaptive backdrop, browser-free YouTube Music playback, and the rest of the accuracy/robustness fixes below.

### Added
- **Living adaptive backdrop** — the main app surface can now breathe with album-derived color: local-painted adaptive styles drift behind transparent structural views and swell on bass without driving per-frame `setStyleSheet()` calls. Styles include layered fields, the classic diagonal sweep, and a bottom-edge hill that swells into a filled arch on bass.
- **Bass pulse feed sharing** — the visualizer and ambient backdrop share the same reference-counted audio capture feed, so opening/closing the visualizer no longer tears down the app-wide pulse.
- **Live lyric in Discord presence** — opt-in toggle under Settings → discord: when the playing track has synced lyrics, the presence state line swaps artist · album for the line under the playhead (`♪ like this`), reverting during intros, instrumental gaps, and untimed tracks. A headless tracker (not the lyrics panel) drives it, so it keeps working with the panel closed, and it reuses the panel's provider chain + on-disk cache — one fetch per track, not two. Lyric pushes get their own wider throttle spacing (4.5 s) so sustained line traffic stays inside Discord's ~5-writes-per-20 s budget — dense verses skip ahead to the newest line rather than queueing a backlog — and an urgent pause/skip write still preempts a pending lyric push. Off by default: lyrics are broadcast to everyone who can see your profile.

### Security
- **Cache & data directories are now owner-only (0700).** `ensure_dirs()` hardened only the config dir; the cache and data trees were left world-traversable at 0755. The librespot subprocess writes a reusable Spotify session credential (`credentials.json`) under the cache dir at its own 0644, so on any system with a group-readable home a co-user could lift it. All three top-level state roots are now chmod 0700 — a directory boundary that holds regardless of whether an individual writer remembers to tighten its own files.
- **Album-art fetches are restricted to http(s) and size-bounded.** Thumbnail URLs come straight from third-party server JSON, and both art-fetch paths handed them to Qt's `QNetworkAccessManager` unchecked — which natively speaks `file://`, `data:`, and `ftp://`. A malicious/MITM'd source could return `file:///home/you/.ssh/id_rsa` (local-file read) or `http://169.254.169.254/…` (blind SSRF to internal hosts). Fetches now reject any non-http(s) scheme before touching the network, enforce a transfer timeout, and abort mid-stream past an 8 MB ceiling so a giant/slow body can't exhaust memory. The disk write also moved to a temp-plus-atomic-replace so a pre-planted symlink at the cache path can't redirect the write.
- **Media playback rejects mpv meta-protocols.** Resolved stream targets now pass a scheme allowlist (http/https/file/bare local path) before reaching libmpv, so a hostile source or extractor can't hand back `edl://`, `lavfi://`, `memory://`, `av://`, etc. to make the player read or compose local files.
- **Subsonic never sends your password in cleartext over HTTP.** "Plain password (https only)" was only a label — nothing enforced it, so `plain` auth against an `http://` server transmitted the password in the query string. The transmission point now silently falls back to the salt+token scheme whenever the connection isn't HTTPS, so the cleartext password never crosses an unencrypted link (and never lands in server access logs).
- **Update-check links are constrained to real GitHub URLs.** The release `html_url` from the GitHub API fed straight into `QDesktopServices.openUrl`; it's now allowlisted to `https://github.com/…` at both the parse site and the click site, so an unexpected `javascript:`/off-platform value can't be opened.
- **YT auth files no longer flicker world-readable.** `browser.json` and the derived `yt_cookies.txt` were written at the umask default and chmod'd to 0600 afterward, leaving a brief 0644 window. They're now created 0600 up front via a temp-plus-atomic-replace helper.
- **Remote metadata can't inject markup into the UI.** `QLabel` defaults to AutoText, which renders anything tag-like as HTML — so a crafted album/artist description, lyric line, track title, or server error message could inject rich text or a `file://` image probe into the app. Every label that displays remote strings (album/artist headers and descriptions, the timed- and plain-lyrics views, the lyrics/up-next/toast text) is now forced to `PlainText`, so remote content always shows literally. (The karaoke view already escaped and was unaffected.)
- **Hardened response parsing against hostile/malformed servers.** Duration/count fields are coerced through a non-raising `safe_int` (a non-numeric or >4300-digit value previously raised `ValueError` and tore down the worker thread), Subsonic responses of the wrong JSON *shape* are rejected instead of crashing on `.get()`, `parse_lrc` skips absurd timestamps rather than aborting the whole parse, poisoned lyric caches degrade to a miss, and every remote JSON read is byte-capped so a multi-GB body can't OOM the app.

### Fixed
- **Expired YouTube Music cookies now announce themselves** — imported cookies die quietly: YouTube starts answering 401 and every layer above either showed a generic error or swallowed it and rendered empty (home → generic shelves, library → nothing), so expiry looked like random breakage until you guessed "re-import cookies". Every ytmusicapi call now runs through an auth sentinel that recognizes auth-shaped failures (HTTP 401 / "authentication credential"), flips the source to *session expired* (warn dot + honest status in Sources), and raises one sticky toast with a **[sign in]** action that opens the import wizard in place — sign back in and home/library reload on the spot. A cheap background probe at launch means a dead jar is reported seconds after startup, not whenever you next touch search. Network blips are never misread as expiry, and playback keeps its anonymous fallback while you decide.
- **Discord presence tracks slowed/sped playback** — the progress bar is drawn by Discord in real wall-clock time, but the `end` timestamp was always `start + duration`, assuming 1× speed. Play a track at 0.5× and the bar raced to the end in half the real time (and 2× overshot). The player now publishes a `speed_changed` signal; presence scales `end` to `start + duration / speed` and re-anchors `start` to the live position on every speed change, so a 4:00 song at 0.5× correctly reads as an 8:00 bar that stays in lockstep with the audio.
- **Presence reliably stops on pause / skip** — Discord silently drops presence writes past ~5 per 20 s, so a quick play→pause or a rapid skip could lose the *clear*, stranding a stale "playing …" on your profile. All pushes now funnel through a trailing-edge throttle (`MIN_PUSH_INTERVAL_S`) that always sends the **latest** desired state, so the final pause/clear can never be the write that gets swallowed.
- **YouTube Music plays without a browser open** — stream resolution ran yt-dlp **anonymously**, which is what trips "sign in to confirm you're not a bot" / age-gates and made playback feel like it needed a logged-in `music.youtube.com` tab. yt-dlp now authenticates from the cookies already harvested at sign-in (a Netscape cookie jar derived from `browser.json`, regenerated only when that file changes), so playback is self-sufficient with no browser running — and unlocks higher-bitrate formats. Falls back to an anonymous resolve if the cookies are stale, so a bad jar is never worse than none.
- **Adaptive colors stay faithful to album art** — palette picking now chooses dominant hue families by real pixel mass before boosting readability, so green/grey covers do not turn yellow/pink because of tiny bright details.
- **UI sounds no longer crash startup on fragile QtMultimedia stacks** — sound effects now dispatch through the system WAV player (`pw-play` / `paplay` / `aplay`) instead of constructing `QSoundEffect`, avoiding a native PipeWire/QtMultimedia segfault seen before the main window opened.
- **UI sounds actually ship in installed builds** — the six WAVs lived in repo-root `assets/sounds/`, which the wheel never packaged, so every pacman/pip install had silently dead UI sounds. They now live inside the package (`tide/sounds/`) and are included in the build; the sdist also carries the desktop file and icons it needs so a wheel built from the published tarball keeps its desktop integration.
- **Radio can't dead-end anymore** — answering a refill request with `disable_radio()` (which the app does when the active source has no radio capability) stranded the one-shot refill guard, so radio never refilled again for the rest of the session even after switching back to a capable source. The guard now always clears.
- **Removing the playing track behaves like skip** — deleting the current queue row left the audio playing the removed track while the highlight moved on, and the next advance skipped a song. Removal now plays the track that took the slot (or stops when there's nothing after it), keeping audio, highlight, and advance in agreement.
- **Slow responses can't overwrite fast ones** — searching twice quickly could interleave both result sets into one list (the slower query appended after the faster one landed), and opening playlist B while playlist A was still loading let A repaint the pane afterwards. Search and playlist-detail responses now carry a request generation and stragglers from abandoned requests are dropped.
- **Spotify no longer stalls boot or vanishes offline** — startup did a blocking token refresh on the GUI thread (up to 15 s on a dead network) and dropped the source entirely if it failed. The source now registers from the stored grant with zero network and refreshes lazily on first use. Token refreshes are serialized under a lock (Spotify rotates refresh tokens, so concurrent refreshes burned them), network failures back off instead of hammering, and a revoked grant (`invalid_grant`) flips the row to *session expired* with the same sticky sign-in toast YT Music uses.
- **Spotify library shows more than 10 playlists** — the dev-mode result cap was applied as a total instead of a page size; the library list now paginates through your whole playlist collection in capped pages.
- **Source panel can't freeze on Spotify status** — the status line fetched your Spotify profile over the network on every repaint of the sources view, on the GUI thread, and a failed fetch retried on each call. Profile lookups now run in the panel's background prober (like Subsonic's ping), failures are negative-cached, and the repaint path never touches the network.
- **Subsonic failures are honest** — search/library/playlist errors used to be swallowed into "no results" / empty views while the panel kept claiming "signed in". Errors now surface in the UI, the row's health flips on transport/credential failures (and heals on the next successful call), and a per-request error like *not found* no longer poisons the whole source.
- **Stale prefetched stream URLs don't replay failures** — when mpv errored on a pre-resolved URL (expired CDN signature), the dead URL stayed in the prefetch cache and each retry replayed it. Player errors now evict the current track's prefetch entry so the next attempt resolves fresh.
- **Visualizer/ambient capture survives audio-server hiccups** — if `parec` exited (sink unplugged, PipeWire restart), the FFT loop spun forever on a dead pipe with the child unreaped, leaving the visualizer permanently dark. The capture now detects the death, respawns up to three times, and surfaces an error if it keeps dying. The shared capture is also force-stopped at quit, so an orphaned `parec` can no longer outlive tide and hold your monitor stream open.
- **Local search accepts quotes** — a query containing `"` (e.g. `12" remix`) broke the FTS5 MATCH string and errored the whole search; embedded quotes are now escaped.
- **Explore rows and onboarding cards stop leaking theme connections** — two widgets connected lambdas to the app-lifetime theme signal, which never disconnects; every discarded row kept a callback firing into freed Qt objects on later theme changes. Both use bound methods now, which auto-disconnect on destruction.
- **Karaoke / synced lyrics finally match the audio** — timed lyrics always came from LRClib, matched by title/artist text, so the sync frequently belonged to a *different recording* (album cut vs music video vs remaster) and the highlight drifted seconds off the song. YouTube Music's own line-synced lyrics — authored against the exact video being played — are now fetched first and win whenever they exist; LRClib stays as the fallback for tracks YT hasn't synced. (Implementation quirk: YT only serves timestamps to mobile clients and 400s the mobile context under signed-in browser cookies, so a dedicated anonymous client does the timed fetch.)

### Security
- **Subsonic credentials no longer persist to disk in URLs** — stream URLs embed the auth token (or, in *plain* mode, the actual password) in the query string, and the stream cache wrote them world-readable to `~/.cache/tide/streams/subsonic.json`, where they survived sign-out — a standing credential for any local reader, since Subsonic tokens never expire. Subsonic URLs are now built fresh per request and never cached; the stream-cache directory/files are 0600/0700; any previously persisted file is purged on startup, sign-out, and reconfiguration; and settings/session/history writes were tightened to 0600 with atomic, crash-safe replacement (settings additionally keep a last-known-good `.bak` so power loss can't reset you to onboarding).

### Changed
- **Visualizer rendering is capped and cheaper** — oscilloscope repaints are timer-capped, rendered through a bounded offscreen buffer, and decimated to roughly one point per pixel. Added a fast envelope renderer for large/HiDPI windows.

## [1.2.3.1] — 2026-06-30 — bug fixes

Pure bug-fix patch on top of 1.2.3 — no new surface, just things that were quietly wrong. The headline is YouTube Music personalization: the home feed served the generic logged-out shelves and the library came up empty even while signed in.

### Fixed
- **YouTube Music home is personalized again** — `browser_import` was merging `.google.com` *and* `.youtube.com` cookies into one flat header and deduping by name across domains. The auth cookies (`SID`, `__Secure-3PSID`, `SAPISID`, `__Secure-3PAPISID`) exist on **both** domains with **different** values, so wrong-domain values reached `music.youtube.com` and YouTube treated every request as logged-out (`LOGGED_IN:false`) — search still worked only because it needs no auth, which masked the bug. Now imports only `youtube.com`-scoped cookies, exactly what a browser sends to `music.youtube.com`. Personalized shelves (Quick picks, Listen again, Mixed for you, …) and library are restored.
- **YouTube Music sign-out actually signs out** — `YTMusicSource` inherited the base no-op `sign_out()`, so the Sources-tab button never cleared the saved cookie and the row kept saying "signed in". It now clears the cookie and flips the row state.
- **In-app re-sign-in** — the source gear dialog showed no button at all for a signed-out auth source, so signing out stranded YouTube Music until a restart. Added a `[sign in]` button (and `begin_auth()`) that re-runs the import wizard and refreshes the live client in place — no restart.
- **Auth no longer self-destructs on a network blip** — `ensure_signed_in()` deleted the saved cookies on *any* exception while building the client (which makes a network call), silently signing the user out. Transient failures now leave the saved session intact.
- **Album art honors rounded corners** — QSS `border-radius` never clips a `QLabel` pixmap, and `AlbumArt` read the theme's base `layout.radius_px` instead of the effective corner-style radius, so the art stayed square inside a rounded border. The pixmap is now masked to `theming.effective_radius_px()`. Sharp themes and the circle/polaroid variants are unchanged.
- **Settings stop resetting on save** — the settings dialog rebuilt its working copy field-by-field and silently dropped 16 fields (`ui_sounds_enabled`, `sources_enabled`, Spotify/Subsonic credentials, `audio_fx_state`, `first_launch_complete`, …), resetting them to defaults every time you hit save. It now deep-copies the whole settings object.

## [1.2.3] — 2026-06-16 — ui sounds + crossfading views

Tide gets a sense of touch. Short percussive sounds when switching nav views, soft pops on modal open/close, chirps on checkbox/radio toggles — all six WAVs hand-authored, all ship in `assets/sounds/`. The sounds auto-mute the second music starts playing so they never compete with the audio you're actually listening to. View switches also crossfade now using the same motion helper the onboarding wizard already used. Defaults to **off** on first launch — opt in via Settings → appearance → "ui sounds".

### Added
- **UI sound dispatcher** — `tide.ui_sounds.UiSoundPlayer`: one `QSoundEffect` per WAV key, played via `play("nav")` / `play("modal_open")` etc. Volume capped at 20 % of system so clicks never startle.
- **Six bundled sounds** — `nav` / `back` (view switch + back), `modal_open` / `modal_close` (dialogs), `toggle_on` / `toggle_off` (checkbox + radio flips).
- **Auto-mute during playback** — listens to the playback router's `state_changed`; flips muted on `PlayState.PLAYING` and back off the instant the player pauses or stops. Reseeds from the live player state on app start so resume-from-paused-session stays silent or not as appropriate.
- **Crossfading view transitions** — `_switch_view`, `_push_view`, and `_go_back` route through `tide.ui.motion.crossfade_stack` with `DUR_SHORT` (200 ms). Motion intensity `off` falls through to a direct `setCurrentIndex` automatically — same path the onboarding wizard already used between steps.
- **Settings toggle** — new "ui sounds" row under "motion:" in the appearance form. Hot-applied on save (no restart). Default `False` so a fresh install is silent.

### Technical
- `assets/sounds/` directory + `README.md` author spec — WAV format target documented for future contributors.
- Missing WAVs degrade cleanly — the loader treats every key as optional, so a fresh checkout without files just plays silence per key.
- Toggle hookups live in `_on_master_toggle` / `_on_loudness` / `_on_compressor` / `_on_mono` (audio fx view) and `_on_row_enable` / `_on_federate_toggle` (source panel). Future toggles add one `self._ui_sound(...)` line to the same handler.

## [1.2.2] — 2026-06-16 — audio fx rack

The "slowed + reverb" aesthetic finally has the reverb. v1.2.2 ships a full audio-FX rack riding on top of the existing mpv playback path — a 10-band graphic EQ, a five-preset reverb (off / room / hall / cathedral / signature **slowed**), bass + treble shelves, loudness normalization for queue sanity, stereo width, a level-boost compressor, and a mono fold. Everything composes with the existing speed + pitch-correction controls because mpv layers its scaletempo around our user filter graph. Works across every source — YT Music, SoundCloud, Bandcamp, Mixcloud, Local, Subsonic — anything that resolves to a stream mpv plays.

### Added
- **`[audio fx]` panel** — new top-level view at `Ctrl+9`. 10 vertical EQ sliders (32 Hz → 16 kHz, ±12 dB, half-octave width). EQ preset cards (flat / bass boost / treble boost / vocal boost / v-shape / soft warmth). Reverb preset picker + wet slider. Bass + treble shelves. Loudness-norm / compressor / mono toggles. Stereo-width slider. Three user-saved EQ slots (`[save] [load] [clear]`).
- **Quick `[fx]` popover** — new now-playing-strip button (next to `[speed]`). Master enable, preset chooser, reverb chooser, bass + treble shelves. Right-click the button toggles the whole rack on/off without opening the popover.
- **Signature "slowed" reverb preset** — three-tap `aecho` chain tuned for the slowed-and-reverb vibe. Pair with playback speed 0.85× + pitch-correction off for the canonical tide sound.
- **Loudness normalization** — `loudnorm=I=-14:LRA=11:tp=-1.5` (the streaming-platform target) gives consistent perceived volume across queue tracks regardless of source mastering.
- **Persisted state** — full rack (master + EQ bands + every knob + custom slots) round-trips through `~/.config/tide/settings.toml` as a single `audio_fx_state` JSON string. Debounced saves (250 ms) so EQ-slider drag doesn't write the file 60 times a second.
- **Settings dialog `audio fx` section** — short explainer + `[open audio fx panel →]` jump.

### Technical
- New `tide/audio_fx.py` module — pure-Python `AudioFxState` dataclass + `build_filter_chain(state) → str` (renders mpv `af` chain in fixed order: EQ → bass → treble → stereo → compressor → reverb → loudnorm → mono). No Qt dependency.
- New `Player.set_audio_filter_chain(chain)` writes the mpv `af` property; rejection (unparseable filter string) leaves the previous chain intact and logs to stderr instead of crashing.
- `PlaybackBackend` default no-op + `PlaybackRouter` fan-out — librespot / MusicKit ignore the chain since they own their audio rendering path.

## [1.2.1] — 2026-06-16 — spotify (shelved) + subsonic + wizard fix

The architecture for Spotify lands — six sources can register, the library and search surfaces work end-to-end, librespot runs as a Spotify Connect device, OAuth and credentials caching are wired. **But audio playback is broken upstream** and there's no client-side fix. Spotify rolled out a platform-security update on 2026-02-06 that refuses audio-decryption keys to librespot regardless of how it authenticates (`--access-token`, librespot's own `--enable-oauth`, cached credentials — all fail with `audio key error 0 1`). Tide ships the source so the code path is ready when (if) Spotify reopens or librespot upstream patches it, but enabling Spotify surfaces an explicit warning that playback won't work.

Subsonic is the silver lining of the same release. Self-hosted music — Navidrome, Airsonic, gonic, Funkwhale, the reference Subsonic server — all speak the same REST surface, and tide now streams audio straight from your server. Search + library + albums + artists + home shelves + starred-as-liked all wired through `search3` / `getAlbumList2` / `getArtist` / `getSimilarSongs2`. No platform broker, no rate limits, no shelf state — your server, your music.

### Added (functional)
- **Spotify source** — search (10-result cap, see below), library + playlists (Liked Songs first), like/save, lyrics via LRClib fallback. Registered after auth completes; appears last in the source panel.
- **Subsonic / Navidrome source** — full Subsonic API 1.16.1 client: search3 for songs/albums/artists, getPlaylists + getPlaylist for library, getAlbum / getArtist / getArtistInfo2 for detail pages, getAlbumList2 (newest / frequent / starred / random) for home shelves, getSimilarSongs2 → getRandomSongs as the radio fallback, star/unstar for likes. MD5(password + salt) auth by default (safe over HTTP); plain-password mode for HTTPS-only Navidrome installs. Audio streams via the `stream` endpoint straight into mpv — same playback path as YT Music / SoundCloud / Bandcamp.
- **Subsonic connect dialog** — onboarding wizard ships a [set up] card for Subsonic that opens a URL + user + password + auth-style form with a `[test connection]` button (ping runs on a background thread, returns "signed in as X" or "can't reach …"). Same dialog reachable from the source panel's gear icon for re-configure or sign-out.
- **Shelved-state warning** — toggling Spotify on (in the onboarding wizard or the source panel) surfaces a confirmation dialog explaining what works, what doesn't, and why. Defaults to "No".
- **OAuth-PKCE sign-in for Web API** — onboarding's source step shows Spotify; clicking it opens the browser to Spotify's auth page and tide listens on `127.0.0.1:8898` for the redirect. Refresh tokens AES-encrypted at `~/.config/tide/spotify.json`.
- **Librespot subprocess + Web API control plane** — librespot runs as a Spotify Connect device named `tide` with `--system-cache ~/.cache/tide/librespot/`. tide drives play/pause/seek/queue via Web API. The router auto-switches between MPV (everywhere else) and librespot mid-queue.

### Spotify Dev Mode caveats (all the new February 2026 walls)
Spotify rolled out a tightened "Development Mode" tier on 2026-02-06. Apps that haven't been granted Extended Quota Mode (an explicit app review at developer.spotify.com) hit four new walls:
- **Audio decryption refused** — `audio_key error 0 1` for every track, regardless of auth method. This is the blocker that shelved playback.
- **Search results capped at 10** per query — anything higher returns 400 "Invalid limit". tide caps at 10 so the UI never sees the error.
- **`/browse/*` endpoints are 403** (featured-playlists, new-releases, categories) — home shelves can't render. Spotify's source registers without the `home` capability.
- **Artist top-tracks + related-artists are 403** — artist detail and track-station radio can't fill. Spotify's source registers without the `radio` capability.

### Fixed
- **First-launch wizard segfault** — clicking the `[set up]` button on a source card or canceling a YT cookie / Local-files dialog was opening a modal QDialog directly inside the click emission. On PySide6 + Python 3.14, returning to the Python frame ref-dropped the dialog while the SignalManager frames were still live, and the `deleteChildren` walk corrupted the heap. The setup dialog is now deferred past the emission via `QTimer.singleShot(0)`, and the dialog instance is explicitly `deleteLater()`'d instead of relying on Python ref-drop. Same blockSignals guard on the cancel-path uncheck so a second toggled emission can't re-enter mid-cleanup.
- **Radio refill against unsupported sources** — `_on_radio_refill_requested` checked the active source's `radio` capability; if the source declares no radio (now true for Spotify), tide auto-disables the queue's radio flag instead of spinning a worker that 403s in a loop.
- **librespot stderr was swallowed** — subprocess used to `stderr=subprocess.DEVNULL`, which hid the `Unrecognized option` / `audio key error` / Connect-handshake details that took most of v1.2.1's debugging time to surface. Now stderr inherits tide's so librespot's logs land in the same stream.

## [1.2.0.1] — 2026-06-16 — pre-spotify glow-up

### Added
- **Animations** — a motion system (`off` / `lite` / `full`) gating the new track-change signature: title scramble-decodes left-to-right while the album art crossfades between covers, layered on top of the existing adaptive accent fade. Reduced-motion env hint clamps `full` to `lite`.
- **Playback speed** — pitch-shifting speed control from 0.5× to 2.0× with a popover (`−0.05` / `+0.05` / presets / reset) and keyboard shortcuts (`[`, `]`, `\`). Pitch-shifted by default for the slowed-and-reverb / nightcore vibe; settings has a "preserve pitch" toggle for audiobook use.
- **Stream-URL prefetch** — when the current track has ≤15s remaining, tide pre-resolves the next track's stream URL. Auto-advance is now ~instant on cache hit; falls back silently on miss.
- **UI scale** — `compact` / `normal` / `large` / `huge` presets cascade through every fixed-size widget (track row, album art, cards, album/artist covers, view margins).
- **Adaptive central-area gradient** — opt-in subtle vertical gradient on the main content area (theme `bg` → album-derived `bg_alt`) that retints per track.
- **Soft corners** — `sharp` / `soft` / `rounded` picker applies a sticky `@radius` override across inputs, scrollbars, and the central-area clip.
- **Loading indicator** — customizable status-bar progress for the resolve → buffer → playing window. Five styles: `off`, `numbers`, `blocks`, `dots`, `ascii`.
- **Themed nav icons** — three sets: bundled brutalist SVG line-art (recolored to the active theme's `fg`), classic mono glyphs, and emoji.
- **Bundled fonts** — IBM Plex Mono, JetBrains Mono, and Inter ship in the package. New font picker in Settings overrides any theme's typography (editable to accept arbitrary system family names).

### Fixed
- **Adaptive theme color picker** — was sorting palette candidates purely by HLS saturation, so a single bright pixel could steal the accent. Now weighted by `vibrancy × √frequency` and normalized into a readable lightness band against the theme background.
- **Skip button** — now stops audio the instant you press it instead of letting the previous track play for the 1–3s the resolve worker takes.
- **Discord RP elapsed timer** — used to anchor to "queue selected" instead of "audio actually started," inflating the displayed elapsed time by the entire resolve+buffer window. Now anchors to `PlayState.PLAYING` via `now − player.position`; self-corrects for first-play, resume-from-pause, and reconnect-mid-song. Source label ("YouTube Music" etc.) now runs through `styled_case` so it tracks the active theme's typography.
- **Prefetch crash** — initial implementation held Python dict refs to the QThread that raced with Qt's `deleteLater` and segfaulted under PySide6. Now Qt-owned via `QThread(self)` with a clean `findChildren(QThread)`-based shutdown.

## [1.2.0] — 2026-06-16 — multi-source

tide is now a multi-source music client. A `MusicSource` ABC sits in front of every catalog; a `PlaybackRouter` dispatches audio to the producing source's declared backend.

- **5 sources at launch**: YouTube Music, SoundCloud, Bandcamp, Mixcloud, Local Files (mutagen + SQLite FTS5)
- **`[source]` panel** (Ctrl+8) — per-source enable + active-source + status dot + sub-dialog gear
- **Federated search** — runs every enabled source in parallel and tags each row with its source badge
- **Capability-aware UI** — library / explore / like / radio buttons adapt to what the active source supports
- **Per-source stream cache** — each source picks its own TTL
- **Source-aware Discord RP** — per-source asset keys for `large_image` / `small_image`
- v1.2.1 → Spotify (Librespot backend), v1.2.2 → Apple Music (MusicKit backend)

## [1.1.1] — 2026-06-16

Three lag/glitch fixes from end-of-v1.1 testing.

- **Adaptive theme override leak** — switching themes while adaptive was on left the previous theme's accent + bg_alt stuck in place. Now clears on every explicit theme apply.
- **Adaptive feedback loop** — in-flight accent animation re-triggering itself when token overrides emitted `theme_changed` mid-frame. Now gates on base-slug change.
- **Settings dialog lag** — opening Settings with adaptive on fired apply_layout + apply_theme + palette extraction for every dropdown's initial `setCurrentIndex`. Now `blockSignals` on all pickers during populate.

## [1.1.0] — 2026-06-15 — QOL kitchen sink

Five milestones of polish, discovery, integration, marquee features, and customization landed in this drop.

- **P1 polish + persistence** — resume on launch, sleep timer (Ctrl+I), mini-mode (Ctrl+M / double-click art), drag-to-reorder queue, history view (Ctrl+5), like/unlike (Ctrl+H), up-next preview, cache cap + auto-prune
- **P2 discovery** — explore page (Ctrl+6), artist + album detail, search filter tabs, thumbnails everywhere, per-theme text case (lowercase, UPPERCASE, l33t)
- **P3 visualizer** (Ctrl+7) — 9 theme-aware renderers (bars-mono, bars-filled, oscilloscope, neon-grid, circle-burst, mirror-bars, dot-matrix, starfield, matrix-rain), PipeWire monitor capture
- **P4 integration** — system tray, ListenBrainz scrobbling, LRClib timed lyrics fallback, toast notifications, daily update check, About section in Settings
- **P5 customization** — widget variants (5 slots × multiple variants), 4 layout presets (classic, focused, dj-deck, walkman)

## [1.0.0] — 2026-06-15 — initial release

A brutalist YouTube Music desktop client. Native Qt6, no Electron.

- Search, library, playlists, queue + radio autoplay, lyrics
- 10 swappable themes (brutalist-mono default, gruvbox, terminal-green, solarized-light, paper, nord, catppuccin, rose-pine, ambient, synthwave)
- MPRIS2 over QtDBus — media keys + KDE Plasma / GNOME panel controls
- Discord rich presence (opt-in, bring your own application id)
- GUI-first sign-in by importing cookies from your real browser

[1.2.1]: https://github.com/captiencelovesarch/tide/releases/tag/v1.2.1
[1.2.0.1]: https://github.com/captiencelovesarch/tide/releases/tag/v1.2.0.1
[1.2.0]: https://github.com/captiencelovesarch/tide/releases/tag/v1.2.0
[1.1.1]: https://github.com/captiencelovesarch/tide/releases/tag/v1.1.1
[1.1.0]: https://github.com/captiencelovesarch/tide/releases/tag/v1.1.0
[1.0.0]: https://github.com/captiencelovesarch/tide/releases/tag/v1.0.0
