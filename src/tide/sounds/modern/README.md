# tide/sounds/modern

The modern personality's sound pack — soft sine droplets instead of the
default clicks. Selected via `UiSoundPlayer.set_pack("modern")`; missing
keys fall back to the default pack.

## Synthesis

numpy + stdlib `wave`; no generator script is committed — these
parameters ARE the recipe:

- sine with an exponential frequency glide `f0 -> f1` (phase =
  cumulative sum of instantaneous frequency)
- second harmonic mixed in at -20 dB
- envelope: 8 ms raised-cosine attack, exponential decay with
  `tau = duration / 3.2`, 5 ms raised-cosine tail fade so the buffer
  ends exactly at zero (no click)
- peak normalized to -6.0 dBFS, quantized to 16-bit PCM

| File | Duration | Glide |
|---|---|---|
| `nav.wav` | 70 ms | 880 → 720 Hz |
| `back.wav` | 80 ms | 660 → 500 Hz |
| `modal_open.wav` | 120 ms | 520 → 440 Hz |
| `modal_close.wav` | 110 ms | 440 → 340 Hz |
| `toggle_on.wav` | 55 ms | 990 → 900 Hz |
| `toggle_off.wav` | 55 ms | 740 → 620 Hz |

Format: 16-bit mono PCM `.wav` at 44100 Hz, 30–150 ms per clip, peak
-6 dBFS — the `../README.md` contract.
