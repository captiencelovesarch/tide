# steamvr plugin

> **status: design only.** written 2026-10-18. builds on the plugin
> framework in `design/plugins.md` (the `ctx` api, settings, safe mode).
> that doc's §4 is the transport layer (overlays, textures, laser). this
> one is how it should *feel*.
>
> **decided (2026-10-18):** valve index controllers, so poke uses real
> finger tracking. body gestures summon it, with three quick clicks of both
> thumbsticks as the backup. when you stop paying attention it's gone, it
> never follows you. added on top: voice search, and the ring, a visualizer
> around the edge of your view that pulses over the game.
>
> **decided, round 2:** the ring follows the lens edge (the index nose cut
> was the point of the drawing). it's a toggle and only shows while music
> plays. voice is english only and push to talk, on a physical button on the
> panel, nothing else. the stick backup is both sticks at once, three times.

the brief: you're in a game (vivecraft is the test case). you do a small
gesture, tide floats up in front of you. you use it, you look back at your
game, it goes away on its own. it should be fun, and it should feel like a
thing in the room, not a window stuck to your face.

---

## the one rule

**eyes keep it, hands open it.**

the panel's size follows your attention. looking at it keeps it alive.
reaching toward it opens it up. looking away shrinks it back down, a step
at a time, until it's gone. you never press "close".

```
                summon gesture
   hidden ───────────────────────► bubble ──── hand comes within 25 cm ───► panel
     ▲                               │  ▲                                     │
     │                               │  └──── hand leaves + 1.5 s ────────────┘
     │   not looked at for 2.5 s     │                                        │
     └───────── (fades, 0.6 s) ◄─────┘◄──────── not looked at for 2.5 s ──────┘
                                       (panel shrinks to bubble first, then fades)

   look back while it's fading → it comes back. nothing is lost.
```

three sizes:

| state | what you see | size | how you get there |
|---|---|---|---|
| **bubble** | cover, title, one play/pause button | ~12 cm | the summon gesture |
| **panel** | cover, title, artist, seek bar, prev / play / next / like / pin, current lyric line | ~30 cm | reach toward the bubble |
| **up next** | the next 5 covers fanned out under the panel | panel + a row | pull down on the panel's bottom edge |

why a bubble first: a gesture that fires by accident in the middle of a
fight costs you a small thing at the edge of your hands, not a 30 cm wall.
and most of the time a glance is all you wanted ("what song is this?").

---

## summoning it

you said "a set of macros". in vr there are two kinds, and they're very
different to live with:

**body gestures** (my pick for the default): no buttons, so they never
fight the game's bindings.

| gesture | how it works | why it's good |
|---|---|---|
| **touch your headphones** (default) | hold a hand next to your ear, open (no trigger, no grip), for half a second | it's what you already do to music. it's easy to find without looking. ears are off to the side, so it doesn't collide with eating in vivecraft (hand to mouth) |
| **tap your wrist** | tap the inside of one wrist twice with the other hand's finger, within 0.6 s | feels like checking a watch. two taps almost never happen by accident |
| **look at your palm** | turn a palm up toward your face and hold it there for 0.8 s | the quest menu idiom, people know it. more false positives in games where you look at your hands |

**the backup: click both thumbsticks, three times, fast.** for when your
hands are busy or a gesture won't fire.

- "both" means the two clicks land within 150 ms of each other. three of
  those inside 1.2 s fires it. one or two does nothing.
- built as two plain steamvr actions (`/actions/tide/in/stick_left`,
  `/actions/tide/in/stick_right`) bound to the stick clicks by default. the
  triple-click pattern is detected in our code, so it's covered by the same
  replay tests as the gestures. you can rebind the two actions in steamvr's
  binding ui like any other.
- steamvr gives overlay apps the same presses the game gets, so the game
  sees three stick clicks too. *check in the spike what that does in
  vivecraft* (if stick click toggles something there, three clicks leave it
  flipped once).
- same pattern again while it's open closes it.

settings: `summon` (headphones / wrist tap / palm, multi-pick),
`summon hand` (either / left / right) and `stick triple-click` (on / off,
default on).

**the vivecraft bow problem.** drawing a bow in vivecraft puts your hand at
your cheek, near the ear. that's why the headphones gesture needs an *open*
hand: drawing a bow means holding the trigger. index controllers report how
curled each finger is (`IVRInput.getSkeletalSummaryData`, 0 = straight, 1 =
fist), so "open" is simply all four fingers under 0.3 curl. a bow draw or a
held block never looks like that.

**not summoning by accident:** hold times, a 1 s cooldown after it hides,
and a `sensitivity` setting (relaxed / normal / strict) that scales the hold
time and the distance.

---

## where it appears

- **in front of you, a little low.** 55 cm away, 15° below eye line, turned
  to face you, kept upright (never tilted with your head). that's where you'd
  hold a phone. it doesn't cover what you were looking at.
- **it grows out of the hand that called it.** the bubble starts at your
  hand and floats to its spot in 0.25 s. this is the whole "it's a thing"
  trick: your eyes follow it, so you know where it came from and where it
  is.
- **it stays put.** once there, it's fixed in your room. it doesn't follow
  your head. if you turn away, that's "not looking", and it goes.
- **it travels with you.** steamvr overlays live in your playspace, not the
  game world. when you walk around in vivecraft with the stick, your
  playspace moves with you, so the panel comes along at the same spot
  relative to your body. if you physically walk away from it, it stays
  behind (and fades, because you stopped looking).

## making it belong in the room

steamvr draws overlays on top of the game, always. a block in vivecraft can
never pass in front of the panel. so "mixing in" has to come from depth,
light and motion:

- **real depth.** the panel isn't one flat picture. it's a few overlays a
  centimetre apart: the card at the back, the cover 2 cm in front of it,
  each button its own little overlay 1 cm in front of the card. with two
  eyes that reads as an object with thickness. buttons physically sink 6 mm
  when you press them.
- **it feels the music.** the card's edge breathes with the bass and the
  cover nudges forward a couple of mm on the kick (`ctx.beats.on_pulse`).
  a `pulse` setting: off / soft / full. soft is the default because in vr
  big motion near your face is tiring.
- **it doesn't glow in a dark cave.** first version: a `brightness` setting.
  later experiment: sample the game's frame once a second at very low
  resolution (`IVRCompositor` mirror texture) and dim the panel when the
  world is dark, the vr version of tide's adaptive ink. marked as an
  experiment because it means reading game frames.
- **personality carries over.** brutalist: a flat slab, hard edges, mono
  type, buttons that snap down, appearance cuts in. modern: a rounded glassy
  card, springy pop-in, a soft glow on the kick. same theme tokens as the
  desktop, so your theme is your theme in vr too.
- **motion intensity off means off.** no growing, no breathing, it just
  appears and disappears. same rule as the rest of tide.

## using it

everything works two ways: **poke** (reach out and touch) and **laser**
(point and pull the trigger, steamvr draws the laser). poke is the fun one,
the laser is for when it's out of reach.

with index controllers poke is your real index fingertip (the skeleton's
fingertip bone), and only counts while that finger is pointing (index curl
low, the others curled), so a loose hand brushing the panel doesn't press
anything. grabbing is closing your hand around the frame (grip curl past
0.7), the way you'd pick up a real object. no buttons involved in either.

| you want to | you do | you feel |
|---|---|---|
| play / pause / skip / like | poke the button | a tick when it sinks, a lighter one when it comes back |
| skip a song | **flick the cover** left or right, like flipping through records. it flies off, the next one slides in | a little thud |
| scrub | grab the playhead dot on the seek bar and drag | ticks every 10 s |
| volume | grab and twist the volume knob on the side of the card, or: hand at your ear (the summon pose) and **twist your wrist like a dial**, no panel needed | a detent every 5% |
| see what's next | pull down on the bottom edge. up next fans out as small cards. poke one to play it | |
| move it | grip the card's frame, move, let go | |
| resize it | grip with both hands and stretch | |
| keep it around | poke the pin | |
| get rid of it now | **throw it away** (grab, toss it off to the side) or do the summon gesture again | |
| search | hold the mic button and talk (see voice search) | a tick when the mic opens and closes |
| turn the ring on or off | poke the ring button | |

quick controls without opening anything: while your hand is in the
headphones pose, a **flick forward** is next, a **flick back** is previous,
the **twist** is volume. a tiny label next to your hand says what happened
(`next · song title`) and fades. this is probably what you'll use most in a
game.

haptics are `soft` by default (off / soft / full). one extra for later:
"feel the kick", a faint pulse on the beat in the hand holding the panel.
off by default.

## pinned: tide as part of your space

pin the panel and the attention rule stops. it stays where you put it and
goes into **ambient mode**: cover and the current lyric line only, at low
opacity. look at it and it brightens to normal, reach for it and it opens
up. unpin to go back to normal.

this is the "radio on the wall" mode: building a house in vivecraft with
the lyrics floating by the window. with roomscale games it stays exactly
where you left it. with stick movement it rides along with you (see "it
travels with you").

pinned spots are remembered **per game**. steamvr tells us which game has
focus (`IVRCompositor.getCurrentSceneFocusProcess` →
`IVRApplications.getApplicationKeyByProcessId`), so the pin you set in
vivecraft comes back in vivecraft, and beat saber gets its own.

## the glance

when the song changes and the panel isn't open, a small card shows the
cover and title for 3 s, down and to the left at 1.2 m, then fades. no
gesture needed. it's placed where your head was pointing when it appeared
and stays there (never stuck to your face). setting: `on song change` show
/ don't.

## lyrics

`lyrics` setting: off / line / ribbon.

- **line**: the current line under the cover, karaoke sweep when tide has
  word timing.
- **ribbon**: the current line floats a few cm above the panel, words lift
  slightly as they're sung, the finished line drifts up and fades. works in
  pinned mode too.

---

## the ring

tide's backdrop pulse, moved into vr: a grainy glow that hugs the edge of
what you can see and pulses with the music over the game. the middle of
your view stays clear.

```
   ┌──────────────── one eye's view ────────────────┐
   │        ░░▒▒▒▓▓▓▓▓▓▓▓▓▓▓▓▓▓▒▒▒░░                │
   │     ░▒▓▓▓                     ▓▓▓▒░            │
   │   ░▒▓▓                           ▓▓▒░          │
   │  ░▓▓                               ▓▓░         │
   │  ▒▓             the game            ▓▒         │
   │  ░▓▓                               ▓▓░         │
   │   ░▒▓▓                           ▓▓▒░          │
   │     ░▒▓▓▓                     ▓▓▓▒░            │
   │        ░░▒▒▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▒▒░░                 │
   └──────── edge follows the lens, nose cut too ───┘
```

**the shape is the lens.** steamvr knows the exact outline each eye can
see, nose cutout included (`IVRSystem.getHiddenAreaMesh(eye,
HiddenAreaMesh_LineLoop)`). the ring is drawn just inside that line, so it
sits on the edge of your vision on any headset, not on a generic circle.
on the index that means the nose cut. headsets without one report their
own shape and get a ring that matches it, no special case needed. a headset
that reports no outline at all gets a plain ellipse.

**getting it on screen.**
- one overlay locked to the headset
  (`setOverlayTransformTrackedDeviceRelative` to device 0), ~2 m out and wide
  enough to cover the whole field of view.
- a stereo texture: `VROverlayFlags_SideBySide_Parallel` sends the left half
  to the left eye and the right half to the right eye, so each eye gets its
  own outline. each half is lined up using that eye's projection
  (`getProjectionRaw`). it'll be a little off at the very edge because of
  your eye spacing. for a soft glow that doesn't matter.
- normally anything stuck to your face in vr is bad. this one is fine:
  games already darken the edge of your view to cut motion sickness
  ("comfort vignettes"). the ring is one of those that dances.

**animating it without redrawing.** the ring is a big texture (about
2048 × 1024) and uploading it 90 times a second is out of the question. so
it's uploaded a few times and then only moved and faded:
- the texture is white. color comes from `setOverlayColor`, so a new
  cover color costs nothing.
- the grain shimmers by cross-fading 4 copies of the ring with different
  grain (4 overlays, alpha only).
- the pulse is alpha plus a slight squeeze inward
  (`setOverlayWidthInMeters`), driven by `ctx.beats.on_pulse`.
- kicks come from the beat map, so the flash can be timed to land exactly
  on the beat instead of a frame late (the same idea as
  `TIDE_BEAT_LEAD_MS`).

**settings.**
- `ring`: off / on the kick (dark until a kick, flashes, fades) / breathing
  (always faintly there, swells with the bass). default: off.
- `strength`: subtle / normal / loud.
- `ring color`: from the cover / theme accent.
- a ring button on the panel turns it on and off without opening settings.

**personality.** brutalist: a hard stepped edge, chunky grain cells, flashes
that cut. modern: a soft glow, fine grain, a springy swell.

**rules it keeps.**
- only while music is playing. fades out on pause.
- never reaches more than ~12% in from the edge. the game stays the game.
- **no more than 3 flashes a second.** fast music (drum and bass, a 16th
  note hi-hat) turns into a swell instead of strobing. photosensitive
  seizure guidelines draw the line at 3, and in a headset you can't look
  away from it.
- motion intensity off means no ring (it is nothing but motion). the
  setting is greyed with a note saying why.

## voice search

**talking.** push to talk, one way only: the mic button on the panel. it's
the biggest button on the card and it's built like a real one. push it in
with your finger and hold, it sinks a full centimetre (deeper than the
others) and lights red, your hand gets a firm click. talk. let go, it springs
back with a softer click and the mic closes. it works with the laser too
(hold the trigger on it).

the mic is never open any other way: no wake word, no gesture, nothing
while the button is up.

**while you talk** the music drops to 30% (the index's speakers sit off your
ears and leak into the mic), and the words appear above the panel as they're
heard, if the engine can stream (see below).

**what it does with what you said.** a short list of commands first,
anything else is a search:

| you say | it does |
|---|---|
| "play daft punk one more time" | searches, plays the top song |
| "queue ..." / "add ..." | searches, puts the top song next |
| "skip" / "next" · "back" / "previous" | that |
| "pause" / "play" | that |
| "louder" / "quieter" | volume ±10% |
| "like this" | likes the current song |
| anything else | search, results float up |

**results.** five cards in an arc in front of the panel: songs, plus an
artist card when the top hit is an artist. poke one to play it. **grab one
and drop it on the panel to play it next.** throw it away to clear the
results. look away and the results go first, then the usual attention rule.

**the engine.** everything runs on your computer. no audio leaves it.
- **first try: the model tide already has.** precise lyric timing downloads
  a speech model (`lyric_align.py`: wav2vec2-base-960h, int8 onnx, ~95 MB,
  on onnxruntime). it's a speech recognizer already. reading its output
  straight as text gives a transcript with no new dependency. it has no
  sense of words, so names come out spelled by ear ("dafft punk won more
  time"), but youtube music search is forgiving about spelling. the spike
  tries 20 real requests and decides.
- **if that's too rough: whisper through sherpa-onnx.**
  `python-sherpa-onnx` (aur, 1.13.8) runs on the same onnxruntime tide
  already uses, and also has streaming models, which is what makes words
  appear while you talk. a bigger download.
  `python-faster-whisper` (aur, 1.2.1) is the other option but pulls in
  ctranslate2, a second ml runtime.
- either way the model downloads on request from settings, pinned to a
  revision and a sha256, the way `lyric_align.download_model` does it.

**the mic.** `parec` on the default input at 16 kHz mono. steamvr switches
the default input to the headset mic when vr starts (a steamvr audio
setting), so tide doesn't need to know which mic it is. same subprocess
approach as `audio_capture.py`.

**settings.** `voice search` (on / off), `duck music while talking`
(on / off), `speech model` (with a download
button and its size). english only for now.

---

## what it looks like (panel, modern-ish)

```
            and i don't wanna leave the party       ← ribbon (own overlay)
   ┌──────────────────────────────────────────┐
   │ ┌──────┐                                 │
   │ │      │  song title                  ◐  │  ← volume knob on the edge
   │ │ art  │  artist · album                 │
   │ └──────┘  ━━━━━━━━━━━●────────── 1:02 3:40│
   │                                          │
   │    [ |◀ ]   [ ▶ ]   [ ▶| ]   [ ♥ ]        │  ← each a raised overlay
   │  [ mic ]                    [ ◯ ]  [⌖]   │  ← talk · ring · pin
   └──────────────────────────────────────────┘
                   ‾‾‾‾ pull ‾‾‾‾                ← up next handle
```

sizes are set by readability, not pixels: at 55 cm, title text 1.6 cm tall,
secondary text 1.1 cm, buttons 4 cm square. the card is ~30 × 18 cm,
rendered at ~2.5 px/mm (750 × 350 for the card texture).

---

## how it's built

### threads

two threads, one rule each:

- **vr thread** owns every openvr call. it runs at the headset's frame rate
  (`Prop_DisplayFrequency_Float`, usually 90 or 120 hz) while anything is
  visible, and 10 hz while hidden (just watching for the summon). each tick:
  read poses, run the gesture and attention logic, move overlays, set alpha,
  push any new textures.
- **gui thread** (tide's) renders the panel widgets into `QImage`s when
  something changed and hands them over through a queue. it never waits on
  the vr thread.

so a busy tide ui can't make vr stutter, and a native crash is confined to
one place in the code (safe mode still catches it).

### overlays

| overlay | attached to | redrawn when |
|---|---|---|
| card | world (absolute transform) | song, state, theme |
| cover | card + 2 cm | song |
| buttons ×5 | card + 1 cm, each | hover, press, state |
| seek bar | card + 0.5 cm | 4 hz while visible |
| lyric ribbon | card, above | each word / line |
| up next ×5 | below card | queue change |
| glance | world | song change |
| hand label | controller (device-relative transform) | on a quick control |
| ring ×4 (grain variants) | headset, side-by-side stereo | uploaded once, then alpha / color / width only |
| transcript line | card, above | as words are heard |
| search results ×5 | arc in front of card | on a search |
| dashboard tab | steamvr dashboard | always there |

about 25 overlays, most of them tiny and rarely redrawn. the dashboard tab
(`createDashboardOverlay`) is the fallback: if you forget the gesture, tide
is still in the steamvr menu.

animations (grow from hand, sink, shrink, fade) are transform + alpha
changes on the vr thread (`setOverlayTransformAbsolute`,
`setOverlayWidthInMeters`, `setOverlayAlpha`), so they cost no redraw. the
curves come from `ctx.motion` so they match the personality.

### attention, as numbers

each tick:

```
gaze  = 1 inside 20° of the head's forward ray to the panel, 0 past 40°, linear between
reach = 1 if a fingertip is within 25 cm of the panel, else 0
touch = 1 for 3 s after any press, drag or grab
attention = max(gaze, reach, touch)
```

- attention < 0.5 for `linger` seconds (default 2.5) → step down one size
  (panel → bubble → hidden). each step is a 0.6 s fade/shrink, and
  attention coming back reverses it.
- between 0.5 and 1 (looking near it, not at it), the panel dims to 70% as a
  hint that it's about to go.
- no eye tracking: openvr doesn't give it to us on most headsets, so "gaze"
  is where your head points. it works because people turn their heads to
  look at things that close.

### gestures are pure functions

every gesture is a small class that eats one pose sample at a time and says
`none` / `started` / `fired`. no openvr inside, just numbers. that means:

- the spike records pose streams to jsonl (`TIDE_VR_RECORD=path`): do each
  gesture ten times, do "normal vivecraft play" for five minutes.
- `tests/test_vr_gestures.py` replays them offscreen: every gesture fires
  on its recordings and **never** fires on the five minutes of normal play.
  that's how the false-positive rate gets tuned without putting the headset
  on each time.

body points we need, from the hmd pose and controller poses (or index
skeleton bones when available): ears (hmd ± 8 cm along its right axis),
wrists, fingertips, palm normals.

### developing without the headset on

- `TIDE_VR_PREVIEW=1`: a plain desktop window that draws the overlay stack
  from the side and from your eye, with a fake fingertip on the mouse
  (scroll wheel = push in / pull out). for layout, pressing and animation
  work.
- steamvr's null driver (`"forcedDriver": "null"` in `steamvr.vrsettings`)
  runs the real runtime with no headset, so the openvr calls get exercised.
- both are debug knobs (env vars, like `TIDE_BEAT_LEAD_MS`), not settings.

---

## settings

everything in the gui, under settings → plugins → steamvr:

| setting | choices | default |
|---|---|---|
| summon | headphones / wrist tap / palm (multi) | headphones |
| summon hand | either / left / right | either |
| stick triple-click | on / off | on |
| sensitivity | relaxed / normal / strict | normal |
| linger | 1–6 s | 2.5 s |
| size | small / medium / large | medium |
| brightness | 30–100% | 85% |
| pulse | off / soft / full | soft |
| haptics | off / soft / full | soft |
| lyrics | off / line / ribbon | line |
| on song change | show / don't | show |
| quick controls at your ear | on / off | on |
| ring | off / on the kick / breathing | off |
| ring strength | subtle / normal / loud | normal |
| ring color | from the cover / theme accent | from the cover |
| voice search | on / off | off until a model is there |
| duck music while talking | on / off | on |
| speech model | download button, shows size | |
| reset pinned spots | button | |

`pinned spots` (per game) are internal state in the plugin's data dir, with
the reset button as their gui.

## what the framework needs to give it

on top of the hooks already in `plugins.md`:

- `ctx.track.art(track, size, cb)` for *any* track, not just the current
  one (up next and search result covers).
- `ctx.queue.play_at(index)` (poking an up next card) and
  `ctx.queue.add_next(track)` (dropping a result on the panel).
- `ctx.search(query, kind="songs") → list[TrackInfo]` through the active
  source, off the gui thread. new: nothing in `plugins.md` searched yet.
- `ctx.playback.duck(level)` / `unduck()`: a temporary volume dip that
  doesn't touch the saved volume.
- `ctx.theme.cover_colors()`: the palette `AdaptiveDriver` already pulls from
  the cover, for the ring.
- `ctx.lyrics` word timing, for the ribbon.
- `kind = "action"` settings (a button row), for "reset pinned spots" and
  the model download.
- multi-pick choice settings, for `summon`.
- `ctx.motion` curves usable off the gui thread (just numbers, no qt
  animation objects).

---

## build order

replaces phases 4 and 5 in `plugins.md`.

**vr 0 · spike** (2 evenings, a script outside tide)
- [ ] overlay a png in front of you; then a rendered `QWidget`; then a laser
      click on it
- [ ] check: does the game still get the trigger when you click the overlay?
      do our stick-click actions arrive while vivecraft has focus, and what
      do three stick clicks do in vivecraft?
- [ ] a head-locked side-by-side overlay with the lens outline from
      `getHiddenAreaMesh` drawn on it: does it line up with the edge of
      your view in each eye?
- [ ] record pose streams: each gesture ×10, stick triple-clicks ×10, five
      minutes of normal vivecraft play
- [ ] record 20 spoken requests through the headset mic, run them through
      the wav2vec2 model tide already has, search youtube music with the
      text, count how many find the right song
- done when: a qt button clicks in vr, the ring outline sits on the lens
  edge, and you have pose and voice recordings.

**vr 1 · it shows up** (4–5 days)
- [ ] vr thread, overlays, steamvr retry and quit handling, dashboard tab
- [ ] headphones gesture + stick triple-click, bubble and panel,
      grow-from-hand
- [ ] attention model, fade and shrink, reversal
- [ ] laser input
- [ ] the glance on song change
- done when: in vivecraft you touch your headphones, skip a song with the
  laser, look back at the game and it's gone.

**vr 2 · it's physical** (4–5 days)
- [ ] poke with the index fingertip, layered overlays, sinking buttons,
      haptics
- [ ] flick the cover, scrub, volume knob
- [ ] grab, move, throw away, two-hand resize
- [ ] wrist tap and palm gestures
- done when: you never need the laser for something within reach.

**vr 3 · the ring** (2–3 days)
- [ ] lens outline → ring textures, 4 grain variants, side-by-side stereo
- [ ] pulse from `ctx.beats`, kicks timed from the beat map, the 3-a-second
      cap
- [ ] the three modes, color from the cover, brutalist and modern looks
- done when: you'd leave it on through a whole vivecraft session.

**vr 4 · voice** (3–4 days)
- [ ] the mic button (deep press, hold to talk), mic capture, ducking
- [ ] speech engine (whichever won the spike), command list, search
- [ ] result cards: poke to play, drop on the panel to queue, throw to clear
- done when: "play daft punk one more time" plays it, and holding a result
  over the panel and letting go puts it next.

**vr 5 · it lives here** (3–4 days)
- [ ] pin and ambient mode, pinned spots per game
- [ ] ear quick controls (flick, twist)
- [ ] lyric ribbon, up next fan, pulse on the panel
- [ ] `TIDE_VR_PREVIEW` window (could move earlier if layout work drags)

**vr 6 · experiments**
- [ ] brightness that follows the game's frame
- [ ] "feel the kick" haptics
- [ ] streaming speech (words while you talk) if the first engine can't
- [ ] (moved: gl textures are the default now, see spike findings)

## built: the first slice (2026-10-19)

`src/tide/plugins/steamvr/`, bundled, off by default, needs pyopenvr.

- **opening:** the wrist watch from the spike (`run.sh watch`), poke or
  laser. the headphones gesture, the glance card and the dashboard tab
  come later.
- **the panel:** cover, title, artist · album, seek, prev / play / next /
  like, an x. themed from the current theme (both personalities), laser
  input.
- **going away:** the x, or the attention fade (look away for `linger`
  seconds, look back to keep it). motion off: no grow, no fade.
- **threads:** every openvr call on the vr thread (`vr.py`); the gui
  thread renders the qt panel and posts frames. gl textures only.
- **steamvr comes and goes:** a background-type check first, so tide
  never launches steamvr; retries every 10 s; recovers after steamvr
  quits or the compositor stops answering.
- **settings:** watch on (left / right wrist), panel size, goes away
  after, haptics.
- **TIDE_VR_OFFLINE=1** keeps steamvr out of it (tests, panel work).

## spike findings

running log from `tools/vr-spike`. each one changes the plan above.

- **2026-10-18 · `check`:** index at 144 hz, input bindings load (after a
  few seconds the first time), finger curl and bone tips live on both
  hands, 27-point lens outline per eye. pinky reads 0.45–0.6 curled while
  middle and ring read ~0, so the "open hand" rule may need to skip the
  pinky.
- **2026-10-18 · `panel`:** `setOverlayRaw` crashed vrcompositor
  (`Vulkan transfer ring buffer overflow`) two seconds in. raw uploads are
  out; gl textures are the plan. one thread ticked steadily at 144 hz
  (6.24 ms, p95 6.31). a separate pyside teardown segfault at exit led to
  an explicit shutdown order (overlays, openvr, then qt objects).
- **2026-10-19 · `panel`, file uploads:** `setOverlayFromFile` at 10 fps
  wrapped the compositor's transfer ring (`Ring Buffer Wrapped!`) and
  vrcompositor segfaulted 17 s in. after that every call from our side
  blocked ~1 s waiting for a reply: **openvr calls must stay off tide's gui
  thread, and the plugin must notice a dead compositor** (the spike now
  stops when an upload takes over 0.5 s).
- **2026-10-19 · laser input works:** in the same run, before the crash,
  two laser clicks landed on the right button. steamvr's mouse y is
  bottom-up as expected; flipping it is right. the explicit teardown
  order held: no segfault on exit even with the compositor gone.
- **2026-10-19 · the path that should work:** wayvr updates 2560×1440
  overlays constantly through vrclient's shared vulkan images. vrclient
  takes an opengl texture the same way (`CVRSharedGLTexture`, needs
  `GL_EXT_memory_object_fd` + `GL_EXT_semaphore_fd`; the rx 9070 /
  radeonsi has both under wayland). **gl textures are the only upload the
  plugin uses.** file uploads only for one-off static images, if at all.
- **2026-10-19 · gl uploads confirmed:** ~2 minutes, 915 redraws at
  0.42 ms each (p95 0.67), one shared vulkan image reused the whole time,
  no compositor errors, clean exit. the loop held 144 hz (p95 6.39 ms).
  one gotcha: re-uploading with `QOpenGLTexture.setData(QImage)` re-runs
  texture setup and throws a warning plus `GL_INVALID_OPERATION` per frame
  (pixels still land). redraws use `setData(RGBA, UInt8, bits)` instead.
- **2026-10-19 · gl textures come out upside down:** qimage rows run
  top-down, gl reads them bottom-up, steamvr shows gl textures the gl way.
  it also hid the clicks: the laser maps to where a button really is, so
  clicking where the flipped picture showed it hit empty space. the fix is
  free: flip the texture bounds (`setOverlayTextureBounds`, v 1→0), on by
  default now.
- **2026-10-19 · laser y follows the texture bounds:** with the bounds
  flipped the panel showed right way up and the slider dragged, but clicks
  and holds missed. steamvr reports laser y in the texture's frame, so
  flipped bounds already turn it top-down; flipping it again sent clicks to
  the mirror spot. the slider only worked because it sits at mid height.
  rule for the plugin: **flip the texture or the laser y, never both.**
- **2026-10-19 · stick triple-click never fires (open):** click, hold and
  drag work (they come through the overlay system), but no stick press
  reaches us: the panel reports both stick actions `inactive`, and neither
  the action system nor the old controller-state api saw one press. steamvr only gives button input to the app with input focus,
  which an overlay never has. finger curl and poses arrive anyway, so
  **body gestures don't have this problem**. what's left to try: the old
  controller-state api (steamvr enabled "legacy input" for us), and a
  global-priority action set, which needs steamvr's "enable global input
  from overlays (experimental)" developer setting
  (`steamvr/globalActionSetPriority`, off by default, advanced settings
  only) and takes those buttons away from the game. if neither works without the developer setting, the stick backup
  is out and the gestures carry summoning alone.
- **2026-10-19 · global input doesn't help, stick backup dropped:** with
  `steamvr/globalActionSetPriority` on and our action set at global
  priority, the stick actions read `inactive` with no game open and with
  vivecraft running. button input is off the table for summoning.
- **2026-10-19 · we can see which game has focus:** with vivecraft
  running the panel read `org.jrbudda.vivecraft.steamvrinput`. pinned
  spots per game will work.
- **2026-10-19 · the laser takes the stick too:** while the laser points
  at the panel, steamvr turns a stick click into a mouse click (same as
  the trigger) and stick up/down into smooth scroll events. that's the
  overlay mouse layer, not our actions. free scrolling for the up next
  list and lyrics; nothing for summoning, since it only works while
  you're already pointing at the panel.
- **2026-10-19 · laser input confirmed:** with only the texture flipped,
  click, hold and drag all work on the gl panel. the laser path (overlay
  mouse events → synthesized qt mouse events) is proven.

## risks, on top of plugins.md

| risk | what to do |
|---|---|
| the headphones gesture fires during play | the replay tests against real play recordings, strict sensitivity, turning gestures off and using the stick triple-click |
| stick-click actions may not reach us while a game has focus | the spike checks it first. if they don't, body gestures are the only summon |
| three stick clicks do something in vivecraft | the spike checks. if it's bad, rebind our two actions to something else in steamvr's binding ui |
| the ring doesn't line up with the lens edge (eye spacing, reprojection, a headset that reports its outline oddly) | the spike checks on your index. fallback: a plain ellipse with a size setting |
| the ring is too much in a dark game | off by default, `strength`, kick-only mode, and the panel button to kill it fast |
| photosensitivity | the 3-flashes-a-second cap is not a setting, it's always on |
| wav2vec2 transcripts too rough to search with | the spike measures it. sherpa-onnx whisper is the fallback |
| the mic hears the game | push to talk keeps clips short, music ducks. game audio can't be ducked by tide |
| raw uploads crash the compositor (seen in the spike) | never use `setOverlayRaw`. gl textures, png files as the fallback. the ring uploads once by file |
| flicks and throws feel wrong | velocity comes straight from steamvr (`vVelocity` on each pose). thresholds are tuned against recordings |
| motion sickness | nothing big is head-locked (the ring stays at the edge, like a comfort vignette), soft pulse by default, intensity off means no motion at all |

## questions for you

none open. next step is the vr 0 spike.
