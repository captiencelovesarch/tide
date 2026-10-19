# steamvr plugin

> **status: design only.** written 2026-10-18. builds on the plugin
> framework in `design/plugins.md` (the `ctx` api, settings, safe mode).
> that doc's §4 is the transport layer (overlays, textures, laser). this
> one is how it should *feel*.

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

**button chord**: shipped as a steamvr action (`/actions/tide/in/summon`)
with a default binding like "hold both menu buttons". you rebind it in
steamvr's own binding ui, per controller, per game. good for people who want
zero false positives. steamvr gives overlay apps the same button presses the
game gets, so the default must be something games rarely use. *verify in
the spike.*

settings: `summon` (headphones / wrist tap / palm / button only, multi-pick)
and `summon hand` (either / left / right).

**the vivecraft bow problem.** drawing a bow in vivecraft puts your hand at
your cheek, near the ear. that's why the headphones gesture needs an *open*
hand: drawing a bow means holding the trigger. with index finger tracking
"open" means fingers extended. on wands it means no button held.

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
| get rid of it now | **throw it away** (grip, toss it off to the side) or do the summon gesture again | |

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

## what it looks like (panel, modern-ish)

```
            and i don't wanna leave the party       ← ribbon (own overlay)
   ┌──────────────────────────────────────────┐
   │ ┌──────┐                                 │
   │ │      │  song title                  ◐  │  ← volume knob on the edge
   │ │ art  │  artist · album                 │
   │ └──────┘  ━━━━━━━━━━━●────────── 1:02 3:40│
   │                                          │
   │    [ |◀ ]   [ ▶ ]   [ ▶| ]   [ ♥ ]   [⌖] │  ← each a raised overlay
   └──────────────────────────────────────────┘
                   ‾‾‾‾ pull ‾‾‾‾                ← up next handle
```

sizes are set by readability, not pixels: at 55 cm, title text 1.6 cm tall,
secondary text 1.1 cm, buttons 4 cm square. the card is ~30 × 14 cm,
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
| dashboard tab | steamvr dashboard | always there |

about 15 overlays, most of them tiny and rarely redrawn. the dashboard tab
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
| summon | headphones / wrist tap / palm / button only (multi) | headphones |
| summon hand | either / left / right | either |
| sensitivity | relaxed / normal / strict | normal |
| linger | 1–6 s | 2.5 s |
| size | small / medium / large | medium |
| brightness | 30–100% | 85% |
| pulse | off / soft / full | soft |
| haptics | off / soft / full | soft |
| lyrics | off / line / ribbon | line |
| on song change | show / don't | show |
| quick controls at your ear | on / off | on |
| reset pinned spots | button | |

`pinned spots` (per game) are internal state in the plugin's data dir, with
the reset button as their gui.

## what the framework needs to give it

on top of the hooks already in `plugins.md`:

- `ctx.track.art(track, size, cb)` for *any* track, not just the current
  one (up next covers).
- `ctx.queue.play_at(index)` (poking an up next card).
- `ctx.lyrics` word timing, for the ribbon.
- `kind = "action"` settings (a button row), for "reset pinned spots".
- multi-pick choice settings, for `summon`.
- `ctx.motion` curves usable off the gui thread (just numbers, no qt
  animation objects).

---

## build order

replaces phases 4 and 5 in `plugins.md`.

**vr 0 · spike** (1–2 evenings, a script outside tide)
- [ ] overlay a png in front of you; then a rendered `QWidget`; then a laser
      click on it
- [ ] check: does the game still get the trigger when you click the overlay?
      do overlay actions (the summon chord) arrive while vivecraft has focus?
- [ ] record pose streams: each gesture ×10, five minutes of normal play
- done when: a qt button clicks in vr and you have pose recordings.

**vr 1 · it shows up** (4–5 days)
- [ ] vr thread, overlays, steamvr retry and quit handling, dashboard tab
- [ ] headphones gesture + button chord, bubble and panel, grow-from-hand
- [ ] attention model, fade and shrink, reversal
- [ ] laser input
- [ ] the glance on song change
- done when: in vivecraft you touch your headphones, skip a song with the
  laser, look back at the game and it's gone.

**vr 2 · it's physical** (4–5 days)
- [ ] poke (index skeleton or a faked fingertip), layered overlays,
      sinking buttons, haptics
- [ ] flick the cover, scrub, volume knob
- [ ] grab, move, throw away, two-hand resize
- [ ] wrist tap and palm gestures
- done when: you never need the laser for something within reach.

**vr 3 · it lives here** (3–4 days)
- [ ] pin and ambient mode, pinned spots per game
- [ ] ear quick controls (flick, twist)
- [ ] lyric ribbon, up next fan, pulse
- [ ] `TIDE_VR_PREVIEW` window (could move earlier if layout work drags)

**vr 4 · experiments**
- [ ] brightness that follows the game's frame
- [ ] "feel the kick" haptics
- [ ] gl texture upload if raw uploads are too slow with this many overlays

## risks, on top of plugins.md

| risk | what to do |
|---|---|
| the headphones gesture fires during play | the replay tests against real play recordings; strict sensitivity; the button-only option |
| overlay action bindings may not reach us while a game has focus | the spike checks it first. if they don't, body gestures are the only summon, which is fine |
| 15 overlays × raw uploads too slow | most are tiny and rarely change. measure in vr 1, gl textures in vr 4 if needed |
| flicks and throws feel wrong | velocity comes straight from steamvr (`vVelocity` on each pose). thresholds are settings internally and tuned against recordings |
| motion sickness from things moving near your face | nothing head-locked, soft pulse by default, intensity off means no motion at all |

## questions for you

1. **controllers?** index (real finger poke), wands, or quest through steam
   link / virtual desktop? decides how much of vr 2 is real tracking vs a
   faked fingertip.
2. **headphones as the default summon:** does that feel right, or did you
   picture a button combo? (both will exist, this is just the default.)
3. **when you turn away, gone or follow?** this sketch says gone (your "stop
   giving it attention"). the other option is a lazy follow that drifts back
   into view. i'd only add it as a setting if gone feels wrong in practice.
4. **anything you want it to *do* in vr** beyond controls, lyrics and up
   next? (search by voice, liking with a thumbs up, a visualizer you can
   stand inside...) these are later, but they change what the panel needs
   room for.
