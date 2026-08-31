# Contributing to tide

Hey — thanks for being here.

tide is a small project with a strong sense of identity. The bar for changes that ship is "does this make tide feel more like itself?" If you're not sure, open a discussion first.

## Running from source

```bash
git clone https://github.com/captiencelovesarch/tide.git
cd tide
PYTHONPATH=src python -m tide
```

Dependencies live in `pyproject.toml`. On Arch you probably already have most of them via the `tide` package — if not, `pacman -S pyside6 python-mpv mpv yt-dlp python-ytmusicapi python-cryptography python-mutagen python-requests`.

## Style

- **Don't add comments that explain WHAT the code does.** Names and structure should make that clear. Add a comment when the WHY isn't obvious — a hidden invariant, a workaround for a specific Qt quirk, a tradeoff you weighed and want the next reader to understand.
- **Lowercase, terse, blunt.** Match the brutalist aesthetic of the README and the UI. Sentences in error messages and status text don't end with periods.
- **Pure functions where you can.** UI-side state lives on the widget; settings live in `Settings`; themes/layouts/sources are plugins.
- **All animation goes through `src/tide/ui/motion.py`.** Ask for `motion.dur("short")` / `motion.ease("out")` — never a raw `QPropertyAnimation` at a call site, never a hardcoded duration. At intensity OFF nothing may animate, so no animation object gets built at all.
- **Bounce belongs to modern.** Overshoot lives in the `springy` profile as `ease("spring")`; `mechanical` stays bounce-free. The profile follows the personality, not the intensity — gate a flourish on `profile() == "springy"`, never on `intensity() == FULL`.

## Adding a theme

1. Make a folder under `src/tide/themes/<slug>/`.
2. Drop in `theme.toml` (tokens + typography + layout flags). Set `uses_base = true` under `[meta]` and a palette-only theme needs no `theme.qss` at all — it composes over `themes/_base.qss`, plus a slim overlay if you want one. Without the flag, ship a full `theme.qss` with `@token` substitutions as before.
3. Declare `aesthetic = "brutalist"` or `aesthetic = "modern"` under `[meta]` — which personality the theme is *for*. It picks the `_base.qss` dialect, and it's what `Settings → appearance → theme` filters on, so a bundled theme without it fails review. Leave it out in a personal theme and tide guesses a dialect from `typography.mono` but files the theme under neither side, so it stays listed for both.
4. Test with `tide --theme <slug>` or pick it from `Settings → appearance → theme`.
5. Add a screenshot at `assets/screenshots/<slug>.png` if you want it in the README gallery.

Keep the theme self-contained — don't introduce new tokens unless every other theme can fall back gracefully. Restyling a *bundled* theme also means updating the `tests/test_qss_split.py` fixture, which pins composed output byte-for-byte.

## Adding a source

A source is a Python class that implements `tide.sources.base.Source`. Look at `tide/sources/ytmusic.py` or `tide/sources/local.py` for examples. The key methods are `search`, `home`, `library`, `album`, `artist`, and `resolve_stream`.

Sources declare their capabilities via `supports("rating")`, `supports("radio")`, etc. — the UI uses these to gray out buttons your source can't satisfy. Failing gracefully is more important than supporting everything.

## Testing

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
```

Always offscreen. A real test window steals focus, which is miserable if you're running the suite while anything else is fullscreen.

Then launch tide and exercise the change by hand under both personalities — brutalist and modern take different paths through motion, controls, and the backdrop. A new setting should hot-swap from the dialog without a restart, and every `Settings` field needs a descriptor in `settings_schema.py` — a meta-test checks.

There's no CI yet, so the suite is only as good as your last local run.

## Commits

Commit messages follow this loose shape, matching the existing history:

```
type(scope): one-line title

· bullet about what changed
· bullet about why
```

`type` is one of `feat`, `fix`, `chore`, `docs`, `refactor`, `release`. `scope` is optional but useful for grouping (`v1.2`, `theming`, `playback`, etc.). The `·` bullets are the project style — feel free to use plain dashes if you prefer.

Co-authored-by trailers are welcome when you actually collaborated.

## License

By contributing, you agree your contribution is licensed under [GPL-3.0-or-later](LICENSE), the same as tide.
