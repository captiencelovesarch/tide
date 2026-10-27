# security

## reporting

found something? please don't open a public issue. use github's [private advisory form](https://github.com/captiencelovesarch/tide/security/advisories/new) and i'll get back to you within a few days.

fixes go into the latest release only.

| version | fixes |
|---|---|
| 3.0.x | yes |
| older | no, update |

i aim for 90 days from report to disclosure, sooner if the fix is out sooner. happy to coordinate with packagers.

## worth reporting

- code execution from a stream, track metadata, lyrics, or a theme file
- anything that leaks your youtube session, spotify token or subsonic password
- a way to get a plugin installed or run that you didn't install or switch on
- the store accepting an index, a zip or a picture that doesn't match its signature or checksum
- a store zip writing outside its own folder
- a remote party reading or changing your cache, history or session
- a problem in the fonts, icons or themes tide ships

## plugins

a plugin is python that runs inside tide with your permissions. it can do anything tide can: read your sign-in, read and write your files, talk to the network. tide doesn't sandbox plugins, and a plugin's "can" line on its store page is what its author says it touches, not a lock.

- **the store** lists plugins from [tide-plugins](https://github.com/captiencelovesarch/tide-plugins). its list is signed with an ed25519 key kept offline; tide has the public half built in and refuses a list that doesn't check. each zip and picture has a sha256 in that list, and tide checks it before unpacking. a zip can't write outside its own folder (no `..`, absolute paths or links). every store plugin links its code from its page.
- **your own plugins** in `~/.config/tide/plugins/` start switched off, marked not from the store. they're as safe as whoever wrote them.
- the store plugins are reviewed like the rest of tide and covered by this policy.

## not a vulnerability

- tide crashing after you hand-edit `settings.toml` into something broken
- a plugin you installed or wrote doing something bad after you turned it on
- **tide reading your browser's cookies.** that's how youtube music sign-in works. google blocks outside logins, so tide imports the session from your own browser, and only after your keyring says yes. tide has no embedded browser.

## what tide keeps, and where

| path | what | mode |
|---|---|---|
| `~/.config/tide/` | settings, themes | 0700 |
| `~/.config/tide/browser.json` | imported cookies | 0600 |
| `~/.config/tide/yt_cookies.txt` | the live youtube session | 0600 |
| `~/.config/tide/plugin-settings/` | each plugin's settings | 0600 |
| `~/.local/share/tide/plugins/` | store plugins, plugin data | 0700 |
| `~/.cache/tide/` | stream urls, art, lyrics, beat maps, history, the store's list | 0700 |

writes are atomic, so a crash can't leave half a file. subsonic stream urls are never cached because they carry your login.

over the network, tide talks to the sources you set up, the lyrics services (switchable in settings → playback → lyrics), and github: once a day for the store's list and to check for updates (that one can be switched off in settings → integrations → updates). plugins talk to whatever they say they do. play reporting to youtube is off unless you enable it.
