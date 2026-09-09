# flatpak

`io.github.captiencelovesarch.tide.yml` is the flathub manifest. the two
`python3-*.yaml` files next to it are generated pins of the python deps,
and the metainfo file is what flathub shows on the store page.

## build it yourself

everything runs through the flatpak-builder app, so no host packages
beyond flatpak itself:

```sh
flatpak install flathub org.flatpak.Builder org.kde.Sdk//6.11 io.qt.PySide.BaseApp//6.11
cd flatpak
flatpak run org.flatpak.Builder --user --install --force-clean --disable-rofiles-fuse \
  --repo=repo --state-dir=.flatpak-builder builddir io.github.captiencelovesarch.tide.yml
flatpak run io.github.captiencelovesarch.tide
```

the manifest pins tide to a release tag. to build the working tree
instead, swap the tide module's source for a dir source:

```yaml
    sources:
      - type: dir
        path: ..
        skip: ['.git', 'pkg', 'dist', '*.tar.gz', '*.pkg.tar.zst']
```

## lint before pushing

```sh
flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest io.github.captiencelovesarch.tide.yml
flatpak run --command=flatpak-builder-lint org.flatpak.Builder appstream io.github.captiencelovesarch.tide.metainfo.xml
flatpak run --command=flatpak-builder-lint org.flatpak.Builder repo repo
```

flathub treats warnings as errors, so all three should come back clean.

## on every release

1. `tag:` in the tide module here; in flathub's copy also `commit:` with
   `git rev-parse vX.Y.Z^{commit}` (the linter wants both, and a commit
   can't carry its own hash).
2. a new `<release>` at the top of `<releases>` in the metainfo, newest
   first, with the date of the tag. the screenshot urls are pinned to a
   commit; repin them if the screenshots changed.
3. when `pyproject.toml` deps change, regenerate the pins (the generator
   needs the `requirements-parser` and `pyyaml` modules on the host):

   ```sh
   flatpak run --command=cat org.flatpak.Builder /app/bin/flatpak-pip-generator > /tmp/flatpak-pip-generator
   python /tmp/flatpak-pip-generator --runtime org.kde.Sdk//6.11 --yaml --output python3-deps \
     ytmusicapi yt-dlp python-mpv cryptography mutagen spotipy pypresence SecretStorage watchdog
   python /tmp/flatpak-pip-generator --runtime org.kde.Sdk//6.11 --yaml --build-only --output python3-hatchling hatchling
   ```

   the generator swaps binary wheels for sdists. cryptography's sdist
   needs a rust toolchain the sandbox doesn't have, so put its manylinux
   wheels (x86_64 and aarch64, `only-arches`) back by hand, plus cffi's.
   numpy and PySide6 come from the base app and are not pinned here.
4. push the same files to the `flathub/io.github.captiencelovesarch.tide`
   repo once flathub has accepted the app. flathubbot also opens pull
   requests there on its own from the `x-checker-data` blocks when mpv,
   libass, libplacebo or a tide tag moves.
