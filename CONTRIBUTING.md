# contributing

thanks for looking. tide is one person's project with a strong opinion about how it looks, so for anything bigger than a fix, open a [discussion](https://github.com/captiencelovesarch/tide/discussions) first and we'll figure out if it fits.

## run it

```sh
git clone https://github.com/captiencelovesarch/tide.git
cd tide
PYTHONPATH=src python -m tide
```

deps are in `pyproject.toml`. on arch, installing the `tide` package pulls them all in.

## test it

```sh
QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
```

keep it offscreen, or test windows will steal focus while you work. there's no CI, so your local run is the only run.

then open tide and try the change in **both** brutalist and modern. they take different paths through motion, controls and the backdrop, so a change that works in one can break in the other.

## house rules

- **every setting lives in the gui.** a new `Settings` field needs a descriptor in `settings_schema.py`, and a test fails without one. it should also apply without a restart.
- **animation goes through `ui/motion.py`.** ask for `motion.dur("short")` and `motion.ease("out")`. no raw `QPropertyAnimation`, no hardcoded durations. with motion off, nothing gets built.
- **bounce is modern's.** overshoot is `ease("spring")` in the springy profile. gate a flourish on `profile() == "springy"`, not on intensity.
- **comments explain why.** a qt quirk, a hidden rule, a tradeoff. if the comment just repeats the code, drop it.
- **ui text is lowercase and short.** no trailing periods on status lines.

## adding a theme

1. make `src/tide/themes/<slug>/theme.toml`.
2. under `[meta]`, set `aesthetic = "brutalist"` or `"modern"`. bundled themes need it.
3. set `uses_base = true` and you only need colours. tide builds the stylesheet from `themes/_base.qss`. add a small `theme.qss` on top if you want.
4. try it: `tide --theme <slug>`.

changing a bundled theme means updating the fixture in `tests/test_qss_split.py`, which pins the output byte for byte.

## adding a source

subclass `tide.sources.base.Source`. `sources/local.py` is the simplest example, `sources/ytmusic.py` the fullest. the main methods are `search`, `home`, `library`, `album`, `artist` and `resolve_stream`. declare what you can do with `supports("radio")` and friends, and the ui greys out what you can't.

## commits

```
feat(scope): what changed

· why, if it isn't obvious
```

types: `feat` `fix` `docs` `refactor` `chore` `release`. dashes are fine instead of `·`.

## license

your contribution ships under [GPL-3.0-or-later](LICENSE), same as the rest of tide.
