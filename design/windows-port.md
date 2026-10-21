# Windows port

A friend wants tide on Windows. Most of tide is portable already (PySide6,
mpv, yt-dlp, ytmusicapi, numpy). This file lists the Linux-only parts and
what replaces each one, in the order they should land.

The person this is for isn't technical. Installing has to be one
downloaded file and double-clicking it, and launching has to be a Start
menu or desktop icon, like any other app. No terminal, no installing
Python, mpv or ffmpeg separately, no console window popping up, and no
error text that only makes sense to a developer. Anything that needs a
command to fix is a bug.

Nobody on the team runs Windows, so every phase ends with the friend
testing a build. Nothing here gets called done until it's been run on a
real Windows machine.

## Phase 1: starts and plays music

Things that crash at import or startup, or write to the wrong place.

- [x] `history.py`: `fcntl` doesn't exist on Windows. It now uses
      `msvcrt.locking` on the same sidecar lock file.
- [x] `config.py`: XDG paths. On Windows, config and data go under
      `%APPDATA%\tide` and cache under `%LOCALAPPDATA%\tide\cache`.
      If an XDG variable is set it still takes priority, so tests and
      people who set those on purpose keep working.
- [x] `app.py`: `mpris.py` imports `PySide6.QtDBus` at the top, and the
      Windows PySide6 wheels don't include QtDBus. The import only happens
      on Linux now, and the service is skipped on other platforms.
- [ ] `instance.py`: nothing to do. `QLocalServer` uses a named pipe on
      Windows, and the XDG_RUNTIME_DIR branch only runs inside a flatpak.
- [ ] `app.py`: nothing to do. The glibc `malloc_trim` setup already
      returns early when not on Linux.
- [ ] `ui/fullscreen.py`, `ui/mini.py`: the D-Bus imports are lazy and
      inside `try`, so they turn into no-ops. Test it anyway.
- [ ] mpv: python-mpv needs `libmpv-2.dll` next to the app or on PATH.
      Grab one from the shinchiro mpv-winbuild builds.
- [ ] ffmpeg: beat maps and some audio fx call `ffmpeg` by name. Ship
      `ffmpeg.exe` with the build. `beat_map.py` already checks
      `shutil.which("ffmpeg")` and skips when it's missing.
- [ ] Look for subprocess calls that rely on POSIX behaviour.
      `start_new_session=True` is ignored on Windows, which is fine, but
      any child process will flash a console window unless it's started
      with `creationflags=subprocess.CREATE_NO_WINDOW`.

Done when: the friend can install it, sign in, search, play a song, and
quit without errors.

## Phase 2: features that fall back quietly right now

- **Bass pulse capture.** `audio_capture.py` and the plugin host read
  audio through `parec`, which is PulseAudio only. On Windows use WASAPI
  loopback (`pyaudiowpatch` or `soundcard`) behind the same interface.
  The rest of the pipeline (FFT, pulse maths, beat maps) stays as it is.
  This is the biggest job in the port.
- **Media keys and the volume flyout.** MPRIS has no Windows version. Use
  System Media Transport Controls through `winsdk`
  (`Windows.Media.SystemMediaTransportControls`). It's optional, but
  without it media keys won't work.
- **Keeping the screen on in fullscreen.** Use
  `SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED)` through
  ctypes instead of org.freedesktop.ScreenSaver.
- **Browser sign-in.** `browser_import.py` only knows Linux profile paths
  and kwallet/libsecret. On Windows, use the `%LOCALAPPDATA%` and
  `%APPDATA%` profile roots and let yt-dlp decrypt them (DPAPI). Recent
  Chrome versions use app-bound encryption, so Chrome cookies may not be
  readable. Firefox and pasted headers should still work, and the
  onboarding text should say so.
- [x] **Mini keep-on-top.** `ui/mini.py` now uses Qt's
      `WindowStaysOnTopHint` on Windows the same way it does on X11.
- **Halo backdrop.** The see-through margin and input mask should work
  with DWM, but it needs testing. If it misbehaves, turn it off on
  Windows.
- **Plugins.** The kde plugin stays Linux-only. Check what lights,
  overlay and steamvr assume about the platform before listing them.
- **UI sounds.** `ui_sounds.py` plays clicks through pw-play/paplay, so
  they're silent on Windows. Use `winsound.PlaySound` with `SND_ASYNC`.
- **Local music folder.** `sources/local.py` already falls back to
  `~/Music`, which is right on Windows.

## Phase 3: packaging and releases

What the friend sees: download `tide-setup.exe`, double-click it, click
Next a couple of times, and tide is in the Start menu and on the desktop.

Built so far (all in `windows/`, plus `.github/workflows/windows.yml`):

- [x] `tide.spec`: PyInstaller, windowed, one-folder. Ships Python,
      `libmpv-2.dll`, `ffmpeg.exe`, `deno.exe` (yt-dlp needs a JS runtime
      for YouTube), and tide's package as plain files so the plugin scan
      and the themes/layouts/fonts lookups work unchanged.
- [x] `launch.py`: the exe's entry point. Puts `bin/` on PATH, sends
      stdout/stderr to `%LOCALAPPDATA%\tide\cache\tide.log` (a windowed
      exe has neither), starts every child process with
      `CREATE_NO_WINDOW`, and gives tide its own taskbar identity.
      `tide.exe --selftest` checks imports, libmpv, bundled data and tools.
- [x] `tide.iss`: Inno Setup. Per-user install with no admin prompt and no
      folder picker. The only choices are a desktop icon checkbox (ticked)
      and "Open tide now". Upgrades close a running tide and replace the
      old files.
- [x] `fetch-bin.ps1`, `make_ico.py`: grab mpv/ffmpeg/deno, build the
      `.ico` from the PNGs.
- [x] Workflow: runs by hand or on a `v*` tag, builds, installs silently
      on the runner, runs the installed exe's self test, uploads the
      installer as an artifact and attaches it to the tag's release.
- [ ] First real run on the runner. Only the spec and the self test have
      been tried, and only on Linux.

- Errors that currently only go to stderr (mpris failure, missing ffmpeg)
  either go away on Windows or show up as a plain toast. No one will
  ever see stderr there.
- [x] Updates, with nothing to click through: on every launch the
      Windows build checks the latest release, downloads its
      `tide-setup.exe` in the background (size and sha256 checked), and
      shows one toast, "tide X is ready. restart to update". Restart quits,
      the installer runs silently and reopens tide. If the toast is
      ignored, the update installs silently the next time tide quits.
      The workflow runs on "release published", so every release gets an
      installer attached. Without one, nothing updates.
- Builds run on a GitHub Actions `windows-latest` runner, so nobody needs
  a Windows box to make a release. The installer goes on the GitHub
  release next to the Linux bundles, with one obvious download link on
  the site.
- SmartScreen will show "Windows protected your PC" for an unsigned
  installer. Until there's a code signing certificate, the download page
  needs a short note with a screenshot: click "More info", then "Run
  anyway". Signing would remove that step, so it's worth doing if more
  people end up using the Windows build.
- Sign-in has to work without any terminal steps either, and right now
  that's the weakest part. tide has no in-app login page; it imports
  cookies from a browser or takes pasted request headers. Pasting headers
  is out of the question for the friend. Firefox import should work.
  Chrome and Edge on Windows encrypt cookies in a way outside programs
  can't read anymore, so if the friend uses one of those, tide needs a
  real in-app sign-in window (QtWebEngine) before this is usable.

## Open questions

- Does the friend want media keys from day one, or is in-app control fine
  at first?
- Windows 10 and 11, or only 11? WASAPI loopback and SMTC both work on
  10, so supporting it probably costs nothing.
