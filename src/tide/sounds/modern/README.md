# tide/sounds/modern

The modern personality's sound pack — soft watery sine droplets, clearly
gentler than the default clicks. Selected via
`UiSoundPlayer.set_pack("modern")`; any key missing here falls back to
the default pack per the loader's usual leniency.

## Synthesis

Generated with numpy + stdlib `wave` (no generator script is committed —
the parameters below ARE the recipe):

- sine oscillator whose frequency glides exponentially `f0 -> f1`
  over the clip (phase = cumulative sum of instantaneous frequency)
- a second harmonic mixed in at -20 dB for a hint of body
- envelope: 8 ms raised-cosine attack, exponential decay with
  `tau = duration / 3.2`, and a 5 ms raised-cosine tail fade so the
  buffer ends exactly at zero (no click)
- peak normalized to -6.0 dBFS, then quantized to 16-bit PCM

| File | Duration | Glide |
|---|---|---|
| `nav.wav` | 70 ms | 880 → 720 Hz |
| `back.wav` | 80 ms | 660 → 500 Hz |
| `modal_open.wav` | 120 ms | 520 → 440 Hz |
| `modal_close.wav` | 110 ms | 440 → 340 Hz |
| `toggle_on.wav` | 55 ms | 990 → 900 Hz |
| `toggle_off.wav` | 55 ms | 740 → 620 Hz |

## Format (matches ../README.md contract)

- PCM `.wav`, 16-bit, mono, 44100 Hz
- 30–150 ms per clip
- peak -6 dBFS
