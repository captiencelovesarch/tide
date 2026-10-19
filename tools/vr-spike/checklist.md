# vr 0 spike checklist

what `design/steamvr.md` needs to know before the plugin gets built. about
two evenings. everything runs from this folder:

```
cd tools/vr-spike
./run.sh check
```

the first run makes a venv in `~/.cache/tide-vr-spike/venv` and installs
pyopenvr into it. results land in `~/.cache/tide-vr-spike/`. ctrl-c ends any
step. if something crashes, the terminal output is the useful part, keep it.

steamvr must be running and the headset on (or at least tracking) for
every step except `replay`, `score` and the `--desktop` / `--preview` modes.

## 1. setup (2 min)

```
./run.sh check
```

both controllers on and in your hands (finger tracking needs hands on the
grips). it waits up to 8 s for finger data the first time; `--wait 20` if
that's not enough.

- [ ] `input` says `ok` and `input_errors` is empty
- [ ] both controllers show `"type": "knuckles"` and a `finger_curl` list
- [ ] `outline_points` is more than 0 for both eyes (index has a nose cut,
      so expect a few dozen)

if input failed: steamvr → settings → controllers → manage controller
bindings, look for this script (it may show up as "python" or "tide"), pick
the "tide vr spike" binding.

## 2. something floats (5 min)

```
./run.sh png
```

- [ ] a "tide" card is in front of you, a little low, facing you
- [ ] it stays put when you turn your head

## 3. laser on a real qt panel (15 min)

if steamvr crashed last time, restart it first.

```
./run.sh panel
```

the panel goes up as an opengl texture (the only upload that didn't crash
steamvr's compositor in testing; `raw` and `file` both did).

- [ ] the panel shows up and stays up for a couple of minutes
- [ ] upside down → rerun with `--no-gl-flip` and tell me
- [ ] steamvr's laser shows up when you point at the panel
- [ ] **click** counts up, the **hold** log says how long you held it,
      the **drag** slider follows the laser
- [ ] if clicks land upside down (you point at the top, the bottom reacts),
      rerun with `--invert-clicks` and note it
- [ ] the bottom line shows `tick p95` and `upload` times
- [ ] click both sticks together three times, fast: both hands buzz and the
      panel jumps back in front of you
- [ ] when you close it the terminal ends with `teardown: qt events
      drained` and no segfault. if it crashes, copy the last lines

if the terminal says `stopping uploads: ...`, the spike noticed a problem
and stopped feeding steamvr. copy that line. it never falls back to another
upload method on its own.

then with a game (vivecraft):

- [ ] start vivecraft with `./run.sh panel` still running. does the `focus:`
      line change to vivecraft's app key?
- [ ] with vivecraft focused: does `stick l down` / `stick r down` still
      show up in the panel log? (this decides whether the stick backup can
      work while a game runs)
- [ ] what do three both-stick clicks do in vivecraft itself?
- [ ] laser-click the panel while vivecraft is focused: did the game also
      react to the trigger (swing, place a block)?

```
./run.sh panel --attach left
```

- [ ] the panel rides on your left controller. still clickable with the
      right hand's laser?

## 4. the ring (10 min)

```
./run.sh ring
```

(uses opengl textures too. if the panel needed `--no-gl-flip`, the ring
does as well.)

a yellow grainy ring at the edge of your view pulsing at 120 bpm. the thin
white line is the exact outline steamvr reports for each eye, the small
cross is where each eye looks straight ahead.

- [ ] close one eye, then the other. does the white line sit right on the
      edge of what you can see, nose cut included, in each eye?
- [ ] nose cut at the top instead of the bottom → rerun with `--flip-y`
- [ ] ring squashed or stretched top to bottom → rerun with `--aspect full`
- [ ] each eye shows the other eye's shape → rerun with `--crossed`
- [ ] once it lines up, `./run.sh ring --no-guide`: how does it feel over a
      game for a couple of minutes? too much, fine, want more?
- [ ] note the flags that made it line up

## 5. gesture recordings (20 min)

each run counts down 10 s (time to get the headset on), buzzes, records for
60 s, buzzes twice at the end. the live detectors buzz hard whenever they
fire, so you can feel false positives as they happen.

```
./run.sh record --label headphones
```
do the headphones gesture 10 times: open hand by your ear, hold half a
second, take it away, wait a couple of seconds. mix left and right.

```
./run.sh record --label wrist-tap
```
tap the inside of one wrist twice with the other hand's index finger, 10
times (nothing detects this yet, it's for later).

```
./run.sh record --label palm
```
turn a palm up toward your face and hold it 1 s, 10 times.

```
./run.sh record --label sticks
```
both-stick triple-click 10 times, and a few single and double clicks in
between that should *not* fire.

```
./run.sh record --label vivecraft-play --seconds 300
```
five minutes of normal vivecraft: walk, mine, build, eat, **draw a bow a
few times**, open the inventory. don't do any tide gesture. every buzz here
is a false positive.

afterwards, at your desk:

```
./run.sh replay
```

- [ ] headphones fired ~10 times in the headphones recording
- [ ] sticks fired ~10 times in the sticks recording
- [ ] nothing fired in vivecraft-play (or note how many)

## 6. voice (20 min)

```
./run.sh voice
```

a panel shows one request at a time. point at **hold to talk**, hold the
trigger, say it, let go. 20 requests. `redo` / `skip` if needed.

- [ ] the clips sound like you (open one: `~/.cache/tide-vr-spike/voice/clip_01.wav`).
      if they're silent, steamvr isn't making the headset mic the default
      input. pick it with `./run.sh voice --device <name>`, names from
      `pactl list short sources`

then at your desk:

```
./run.sh score
```

it offers to download tide's speech model (95 MB, the one precise lyric
timing uses), reads each clip, searches youtube music with what it heard and
prints a score.

- [ ] note `right song first: x/20`, `in the top 3`, and `with a perfect
      transcript` (the most the search could have done)

## what to send back

the files in `~/.cache/tide-vr-spike/` (`check.json`, `panel-*.json`,
`ring-*.json`, `ring-geometry.json`, `voice/score.json`) plus your notes on
the checkboxes above. the pose recordings stay on your machine, they're big;
they become the test fixtures when the plugin's gesture tests get written.
