# security

## reporting

found something? please don't open a public issue. use github's [private advisory form](https://github.com/captiencelovesarch/tide/security/advisories/new) and i'll get back to you within a few days.

fixes go into the latest release only.

| version | fixes |
|---|---|
| 2.3.x | yes |
| older | no, update |

i aim for 90 days from report to disclosure, sooner if the fix is out sooner. happy to coordinate with packagers.

## worth reporting

- code execution from a stream, track metadata, lyrics, or a theme file
- anything that leaks your youtube session, spotify token, subsonic password or listenbrainz token
- a remote party reading or changing your cache, history or session
- a problem in the fonts, icons or themes tide ships

## not a vulnerability

- tide crashing after you hand-edit `settings.toml` into something broken
- **tide reading your browser's cookies.** that's how youtube music sign-in works. google blocks outside logins, so tide imports the session from your own browser, and only after your keyring says yes. tide has no embedded browser.

## what tide keeps, and where

| path | what | mode |
|---|---|---|
| `~/.config/tide/` | settings, themes | 0700 |
| `~/.config/tide/browser.json` | imported cookies | 0600 |
| `~/.config/tide/yt_cookies.txt` | the live youtube session | 0600 |
| `~/.cache/tide/` | stream urls, art, lyrics, beat maps, history | 0700 |

writes are atomic, so a crash can't leave half a file. subsonic stream urls are never cached because they carry your login.

over the network, tide talks to the sources you set up, the lyrics services (switchable in settings → playback → lyrics), and github once a day to check for updates. play reporting to youtube is off unless you enable it.
