# Security policy

## Supported versions

tide is small and moves fast. Only the latest released minor version receives security fixes — older versions get whatever lands in the next release.

| version | supported |
|---------|-----------|
| 1.3.x   | ✅        |
| 1.2.x   | ❌        |
| < 1.2   | ❌        |

## Reporting a vulnerability

**Do not open a public issue for a security report.** Instead, report it privately via GitHub's [security advisory form](https://github.com/captiencelovesarch/tide/security/advisories/new). I'll respond within a few days.

Things worth reporting:

- arbitrary code execution from a malicious stream / metadata field / theme file
- credential leakage (YouTube Music cookie jar, Spotify OAuth tokens, Subsonic/Navidrome passwords, ListenBrainz token, Discord ID)
- anything that lets a remote party read or modify your local audio cache, history, or session
- supply-chain issues in tide's bundled fonts / icons / themes

Things that are **not** vulnerabilities:

- a crash from a malformed manual edit to `~/.config/tide/settings.toml`
- inability to play a track that requires DRM tide doesn't support (Apple Music — the lack itself isn't a CVE)
- "tide reads my browser's cookie database" — that's the YouTube Music sign-in working as designed: the wizard imports cookies from your own browser session, with your keyring's consent, because Google blocks credential entry in embedded webviews (tide ships no webview at all)

## Disclosure timeline

I'll aim for the standard 90-day disclosure window. If a fix lands sooner the advisory ships sooner. Coordinated disclosure with downstream packagers (AUR) is welcome.
