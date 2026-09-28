"""Audio FX state + mpv filter-chain builder.

A pure-Python module — no Qt, no mpv handle, no UI imports — that owns
the canonical audio-rack state and renders it into the mpv ``af`` (audio
filter chain) string. The UI (full panel + quick popover) mutates an
``AudioFxState`` and the playback router pushes ``build_filter_chain()``
into mpv. Persistence is JSON-in-a-settings-string so the TOML stays
shallow (the existing serializer doesn't handle lists of dicts).

Filter order in the chain is deliberate:
    preamp → EQ bands → exciter → bass shelf → treble shelf → lofi →
    stereo width → compressor → chorus → flanger → phaser → tremolo →
    reverb → crossfeed → loudness norm → mono → safety limiter

EQ first to shape the source signal cleanly, the exciter right after so
its synthesized harmonics ride the corrected spectrum before the broad
shelves, lofi next since it band-limits and crushes the *source*
character, then image/dynamics, then the modulation family (motion
before space — a chorused signal into reverb sounds like an ensemble in
a room, a reverbed signal into chorus sounds like seasickness), then
the convolution reverb, headphone crossfeed on the summed result,
loudness leveling near the end, and the optional mono fold as the last
real effect.

Gain staging: nothing in the rack may push the signal past full scale,
because past full scale is clipping, and clipping is what made the rack
sound "crunchy" next to the bypass. Measured on a real track: bass
boost peaked +3.6 dB over, the compressor +6.1, hall reverb +5.8, and
"slowed" at full wet +12.2 with 9% of samples clipped. So the chain
opens with a preamp that cancels the EQ + shelf boost (computed from
the actual filter response, since overlapping bands stack), the
exciter and width filters run with their built-in hard clip off, the
reverb mixes equal-power instead of adding the wet on top, and a
lookahead limiter a hair under full scale catches whatever is left.
"""
from __future__ import annotations

import cmath
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, fields


# ---------- frequencies + bands ----------

# Classic 10-band graphic EQ centers. Each band is ~1 octave from the
# next; we set the per-band width to half an octave so adjacent bands
# overlap gently — typical graphic-EQ feel.
EQ_FREQUENCIES_HZ: tuple[int, ...] = (
    32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000,
)
EQ_BAND_COUNT = len(EQ_FREQUENCIES_HZ)
# Gain range exposed to the UI. ffmpeg's `equalizer` accepts a wider
# range but anything past ±12 dB sounds destroyed.
EQ_GAIN_MIN_DB = -12.0
EQ_GAIN_MAX_DB = 12.0
# Octave width per band — half-octave gives the smooth-overlapping
# response a classic graphic EQ has.
EQ_BAND_WIDTH_OCTAVES = 0.5


def _flat_bands() -> list[float]:
    return [0.0] * EQ_BAND_COUNT


# ---------- EQ presets ----------

# Each preset is a list of 10 dB values aligned with EQ_FREQUENCIES_HZ.
# Values curated to be obvious-but-musical at ±6 dB peaks.
EQ_PRESETS: dict[str, list[float]] = {
    "flat":          [0,  0,  0,  0,  0,  0,  0,  0,  0,  0],
    "bass boost":    [6,  5,  4,  2,  0,  0,  0,  0,  0,  0],
    "treble boost":  [0,  0,  0,  0,  0,  0,  2,  4,  5,  6],
    "vocal boost":   [-2, -2, -1, 0,  3,  4,  4,  2,  0, -1],
    "v-shape":       [5,  4,  2,  0, -2, -2,  0,  2,  4,  5],
    "soft warmth":   [3,  2,  1,  0, -1, -1, -2, -2, -1,  0],
}


def detect_eq_preset(bands: list[float]) -> str:
    """Return the preset name whose curve exactly matches ``bands``, or
    ``"custom"`` if none does. Used by the UI to highlight the active
    preset card after manual slider edits / preset clicks."""
    for name, curve in EQ_PRESETS.items():
        if len(curve) == len(bands) and all(
            abs(float(a) - float(b)) < 1e-4 for a, b in zip(curve, bands)
        ):
            return name
    return "custom"


# ---------- reverb presets ----------

# Convolution reverb. Each preset maps to a synthesized stereo impulse
# response (see ``fx_ir`` — exponentially decaying colored noise with
# per-preset RT60 / pre-delay / damping / width), convolved in mpv via
# an ffmpeg ``afir`` lavfi graph. The old ``aecho`` "reverb" was a
# handful of discrete delay taps — audibly just the song echoing —
# and is gone.
#
# The value here is the wet-path mix weight the preset gets when the
# user's wet knob is at 1.0; the knob scales it linearly. The IRs are
# energy-normalized, so weight 1.0 ≈ wet as loud as dry on noise-like
# music; bigger rooms get a bit more so cranking the knob actually
# drenches. "off" = no reverb. Room acoustics live in
# ``fx_ir.IR_SPECS`` under the same preset names.
REVERB_PRESETS: dict[str, float] = {
    "off":       0.0,
    "room":      0.9,
    "hall":      1.1,
    "plate":     1.0,
    "cathedral": 1.25,
    # Signature tide preset — huge, dark, maximally wide; pairs with
    # speed < 1.0 + pitch-shift off (the "slowed + reverb" aesthetic).
    "slowed":    1.5,
}


# ---------- state ----------

@dataclass
class CustomSlot:
    """One user-saved EQ slot. Three of these slots ship; users overwrite
    them via [save 1/2/3] in the full panel."""
    name: str = ""
    bands: list[float] = field(default_factory=_flat_bands)


@dataclass
class AudioFxState:
    """The whole rack as a plain Python object. Mutated by the UI, read
    by ``build_filter_chain``, round-tripped to JSON for settings.toml."""

    master_enabled: bool = False
    eq_bands: list[float] = field(default_factory=_flat_bands)
    bass_db: float = 0.0
    treble_db: float = 0.0
    reverb_preset: str = "off"
    reverb_wet: float = 0.5
    loudness_norm: bool = False
    # 0 (mono) — 1 (normal) — 2 (wide). Stored as the raw extrastereo `m`
    # value so build_filter_chain can pass it straight through.
    stereo_width: float = 1.0
    compressor: bool = False
    mono: bool = False
    # ---- the fx drawer (v1.6) ----
    # Modulation family — enable-only, defaults tuned to sound right
    # the moment they're switched on.
    chorus: bool = False
    flanger: bool = False
    phaser: bool = False
    tremolo: bool = False
    tremolo_speed: float = 5.0        # Hz, 0.5–10
    # Exciter (ffmpeg crystalizer) — adds sparkle/definition up top.
    exciter: bool = False
    exciter_amount: float = 2.0       # crystalizer intensity, 0–5
    # Headphone crossfeed — bleeds a filtered bit of each channel into
    # the other, like speakers in a room instead of hard L/R on cans.
    crossfeed: bool = False
    crossfeed_strength: float = 0.4   # 0–1
    # Lofi — a composite: band-limit + bit crush + slow tape wow.
    lofi: bool = False
    custom_slots: list[CustomSlot] = field(
        default_factory=lambda: [CustomSlot() for _ in range(3)]
    )

    # ---------- helpers ----------

    def apply_eq_preset(self, name: str) -> None:
        curve = EQ_PRESETS.get(name)
        if curve is None:
            return
        # Copy so the preset table doesn't get mutated by later edits.
        self.eq_bands = [float(v) for v in curve]

    def load_custom_slot(self, idx: int) -> bool:
        if not (0 <= idx < len(self.custom_slots)):
            return False
        slot = self.custom_slots[idx]
        if not slot.bands or len(slot.bands) != EQ_BAND_COUNT:
            return False
        self.eq_bands = [float(v) for v in slot.bands]
        return True

    def save_custom_slot(self, idx: int, name: str = "") -> None:
        if not (0 <= idx < len(self.custom_slots)):
            return
        self.custom_slots[idx] = CustomSlot(
            name=name or f"slot {idx + 1}",
            bands=[float(v) for v in self.eq_bands],
        )

    def clear_custom_slot(self, idx: int) -> None:
        if not (0 <= idx < len(self.custom_slots)):
            return
        self.custom_slots[idx] = CustomSlot()

    # ---------- persistence ----------

    def to_dict(self) -> dict:
        return {
            "master_enabled": bool(self.master_enabled),
            "eq_bands": [float(v) for v in self.eq_bands],
            "bass_db": float(self.bass_db),
            "treble_db": float(self.treble_db),
            "reverb_preset": str(self.reverb_preset),
            "reverb_wet": float(self.reverb_wet),
            "loudness_norm": bool(self.loudness_norm),
            "stereo_width": float(self.stereo_width),
            "compressor": bool(self.compressor),
            "mono": bool(self.mono),
            "chorus": bool(self.chorus),
            "flanger": bool(self.flanger),
            "phaser": bool(self.phaser),
            "tremolo": bool(self.tremolo),
            "tremolo_speed": float(self.tremolo_speed),
            "exciter": bool(self.exciter),
            "exciter_amount": float(self.exciter_amount),
            "crossfeed": bool(self.crossfeed),
            "crossfeed_strength": float(self.crossfeed_strength),
            "lofi": bool(self.lofi),
            "custom_slots": [
                {"name": s.name, "bands": [float(v) for v in s.bands]}
                for s in self.custom_slots
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping | None) -> "AudioFxState":
        # The payload comes from a settings string anyone can edit, so
        # valid JSON that isn't an object ('[1,2,3]', '"hello"') must
        # fall back to defaults rather than crash attribute lookups.
        if not isinstance(data, Mapping) or not data:
            return cls()
        state = cls()
        if "master_enabled" in data:
            state.master_enabled = bool(data["master_enabled"])
        bands = data.get("eq_bands")
        if isinstance(bands, list) and len(bands) == EQ_BAND_COUNT:
            state.eq_bands = [_clamp_db(v) for v in bands]
        state.bass_db = _clamp_db(data.get("bass_db", 0.0))
        state.treble_db = _clamp_db(data.get("treble_db", 0.0))
        rv = str(data.get("reverb_preset", "off"))
        state.reverb_preset = rv if rv in REVERB_PRESETS else "off"
        state.reverb_wet = max(0.0, min(1.0, float(data.get("reverb_wet", 0.5))))
        state.loudness_norm = bool(data.get("loudness_norm", False))
        state.stereo_width = max(0.0, min(2.5, float(data.get("stereo_width", 1.0))))
        state.compressor = bool(data.get("compressor", False))
        state.mono = bool(data.get("mono", False))
        # v1.6 fx drawer — every key optional so payloads from older
        # builds load with these at their defaults (all off).
        state.chorus = bool(data.get("chorus", False))
        state.flanger = bool(data.get("flanger", False))
        state.phaser = bool(data.get("phaser", False))
        state.tremolo = bool(data.get("tremolo", False))
        state.tremolo_speed = _clamp(data.get("tremolo_speed", 5.0), 0.5, 10.0, 5.0)
        state.exciter = bool(data.get("exciter", False))
        state.exciter_amount = _clamp(data.get("exciter_amount", 2.0), 0.0, 5.0, 2.0)
        state.crossfeed = bool(data.get("crossfeed", False))
        state.crossfeed_strength = _clamp(
            data.get("crossfeed_strength", 0.4), 0.0, 1.0, 0.4
        )
        state.lofi = bool(data.get("lofi", False))
        # custom_slots could be persisted as anything — coerce to a
        # list first so a dict ({"a": 1}) can't blow up the [:3] slice.
        slots = data.get("custom_slots")
        if not isinstance(slots, list):
            slots = []
        out_slots: list[CustomSlot] = []
        for raw in slots[:3]:
            try:
                name = str(raw.get("name", ""))
                bs = raw.get("bands") or []
                if isinstance(bs, list) and len(bs) == EQ_BAND_COUNT:
                    out_slots.append(CustomSlot(
                        name=name, bands=[_clamp_db(v) for v in bs],
                    ))
                else:
                    out_slots.append(CustomSlot(name=name))
            except (AttributeError, TypeError, ValueError):
                out_slots.append(CustomSlot())
        # Pad to exactly 3 so the UI's slot row always has 3 buttons.
        while len(out_slots) < 3:
            out_slots.append(CustomSlot())
        state.custom_slots = out_slots
        return state

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: str) -> "AudioFxState":
        """Junk in, defaults out — this runs at startup on a persisted
        string, so no payload shape may ever raise past here."""
        if not payload:
            return cls()
        try:
            return cls.from_dict(json.loads(payload))
        except Exception:
            return cls()


def _clamp_db(value) -> float:
    try:
        return max(EQ_GAIN_MIN_DB, min(EQ_GAIN_MAX_DB, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _clamp(value, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


# ---------- gain staging ----------

# Ceiling for the safety limiter, a hair under full scale (-0.5 dBFS) so
# inter-sample peaks from the resampler after us don't clip either.
LIMITER_CEILING = 0.944

# Frequency grid for the boost estimate: 1/24 octave, 20 Hz – 20 kHz. Fine
# enough to land within ~0.05 dB of a half-octave band's true peak.
_RESPONSE_FS = 48000.0
_RESPONSE_GRID_HZ: tuple[float, ...] = tuple(
    20.0 * 2.0 ** (k / 24.0) for k in range(int(24 * math.log2(1000.0)) + 1)
)
# ffmpeg's bass/treble default width: Q 0.707 (checked against an impulse
# through the real filters: 0.00 dB off; the same check put the octave-
# width peaking model 0.01 dB off).
_SHELF_Q = 0.7071


def _peaking(f0: float, gain_db: float, bw_oct: float) -> tuple:
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / _RESPONSE_FS
    alpha = math.sin(w0) * math.sinh(math.log(2.0) / 2.0 * bw_oct * w0 / math.sin(w0))
    c = math.cos(w0)
    return ((1 + alpha * a, -2 * c, 1 - alpha * a),
            (1 + alpha / a, -2 * c, 1 - alpha / a))


def _shelf(low: bool, f0: float, gain_db: float) -> tuple:
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / _RESPONSE_FS
    c = math.cos(w0)
    s = 2.0 * math.sqrt(a) * math.sin(w0) / (2.0 * _SHELF_Q)
    sign = 1.0 if low else -1.0
    b = (a * ((a + 1) - sign * (a - 1) * c + s),
         sign * 2 * a * ((a - 1) - sign * (a + 1) * c),
         a * ((a + 1) - sign * (a - 1) * c - s))
    d = ((a + 1) + sign * (a - 1) * c + s,
         -sign * 2 * ((a - 1) + sign * (a + 1) * c),
         (a + 1) + sign * (a - 1) * c - s)
    return b, d


def eq_peak_boost_db(state: AudioFxState) -> float:
    """Highest point of the EQ bands + bass/treble shelves' combined
    magnitude response, in dB (0 when nothing boosts). Adjacent boosted
    bands overlap and a shelf stacks on top of them, so this can run well
    past any single slider: the bass boost preset plus a +4 bass shelf
    peaks at +10.3 dB."""
    sections = [
        _peaking(freq, float(g), EQ_BAND_WIDTH_OCTAVES)
        for freq, g in zip(EQ_FREQUENCIES_HZ, state.eq_bands)
        if abs(float(g or 0.0)) >= 0.05
    ]
    if abs(state.bass_db) >= 0.05:
        sections.append(_shelf(True, 120.0, state.bass_db))
    if abs(state.treble_db) >= 0.05:
        sections.append(_shelf(False, 8000.0, state.treble_db))
    if not sections:
        return 0.0
    peak = 0.0
    for f in _RESPONSE_GRID_HZ:
        z1 = cmath.exp(-1j * 2.0 * math.pi * f / _RESPONSE_FS)
        z2 = z1 * z1
        h = 1.0 + 0j
        for b, d in sections:
            h *= (b[0] + b[1] * z1 + b[2] * z2) / (d[0] + d[1] * z1 + d[2] * z2)
        peak = max(peak, 20.0 * math.log10(abs(h)))
    return peak


# ---------- chain builder ----------

def _ffmpeg_quote(text: str) -> str:
    """One layer of ffmpeg token quoting (av_get_token): wrap in single
    quotes, and a literal quote becomes ``'\\''`` — close the quote,
    backslash-escape the quote character, reopen."""
    return "'" + text.replace("'", r"'\''") + "'"


def _reverb_lavfi_entry(preset: str, wet: float) -> str | None:
    """Build the mpv af entry for the convolution reverb, or None when
    the reverb is bypassed (preset off/unknown, wet at 0, or the IR
    could not be generated).

    The entry is a full lavfi graph inside one af-list item:

        lavfi=%len%asplit[dry][srcw];amovie=filename=<ir>[ir];
               [srcw][ir]afir=…[wet];[dry][wet]amix=…

    Two quoting layers, both verified empirically against mpv 0.41 /
    ffmpeg 9 with hostile cache paths (apostrophes, colons, unbalanced
    brackets, commas, semicolons, percent signs, backslashes — a themed
    $XDG_CACHE_HOME can contain any of them):

      * mpv af-list layer — ``%len%`` length-prefixed quoting hands mpv
        the whole graph verbatim. The balanced-``[...]`` form used
        previously dies at option parse on an unbalanced ``]`` in the
        path, taking all playback with it.
      * lavfi layer — the IR filename crosses two ffmpeg tokenizer
        passes (the graph splitter, then the filter's own arg splitter),
        each of which strips one layer of quoting, so the path gets
        ``_ffmpeg_quote`` applied twice. A single shell-style layer
        (the old code) mangled apostrophes and left ``:`` bare, which
        killed af init and with it all audio.

    Why the parallel graph: afir's own dry/wet params are just I/O
    gains around the convolution — there is no unprocessed passthrough
    (measured: dry=1 leaves the dry signal 40+ dB down). So we split,
    convolve one branch (``irnorm=-1`` — afir's auto IR normalization
    also crushed the level ~40 dB on our long unit-energy IRs), and
    amix it back against the untouched branch. amix weights carry the
    user's wet knob, scaled equal-power so the room doesn't raise the
    level; ``normalize=0`` so amix applies those weights exactly as
    given. afir adds no latency (measured
    with an impulse: wet onset = pre-delay exactly), so the branches
    stay time-aligned. The IR carries no impulse at t=0 — the wet
    branch is pure room.
    """
    gain = REVERB_PRESETS.get(preset, 0.0)
    w = max(0.0, min(1.0, float(wet))) * gain
    if w <= 0.0:
        return None
    from . import fx_ir  # lazy — keeps this module import-light (numpy)
    path = fx_ir.ensure_ir(preset)
    if path is None:
        return None
    # Quoted twice: once for the graph splitter, once for the filter
    # arg splitter (each strips one layer — see docstring).
    escaped = _ffmpeg_quote(_ffmpeg_quote(str(path)))
    # Equal-power mix: the wet tail is uncorrelated with the dry signal,
    # so their powers add. Scaling both by 1/sqrt(1 + w²) keeps the
    # loudness where it was instead of adding the room on top (which
    # pushed "slowed" at full wet +7 dB louder and deep into clipping).
    # The wet/dry balance is unchanged, and w → 0 still converges on
    # bypass.
    norm = 1.0 / math.sqrt(1.0 + w * w)
    graph = (
        "asplit[dry][srcw];"
        f"amovie=filename={escaped}[ir];"
        "[srcw][ir]afir=dry=1:wet=1:irnorm=-1[wet];"
        f"[dry][wet]amix=inputs=2:weights='{norm:.3f} {w * norm:.3f}':normalize=0"
    )
    # %len% counts bytes, not characters — encode before measuring so a
    # non-ascii cache path doesn't truncate the graph mid-token.
    return f"lavfi=%{len(graph.encode('utf-8'))}%{graph}"


def build_filter_chain(state: AudioFxState) -> str:
    """Render ``state`` to the mpv ``af`` string. Returns ``""`` when
    nothing should be applied (master disabled or every knob at default)
    so mpv stays in fully-bypassed mode.

    Each filter is appended only if it actively does something — a 0 dB
    EQ band, no-op reverb preset, etc. all get skipped — so the chain
    we hand mpv is as short as possible.
    """
    if not state.master_enabled:
        return ""

    chain: list[str] = []

    # 0. Preamp: cancel the EQ + shelf boost up front so the boosted
    # band lands where the loudest part of the song already was, not
    # past full scale. Also leaves the later stages the headroom they
    # expect.
    boost = eq_peak_boost_db(state)
    if boost >= 0.05:
        chain.append(f"volume=volume={-boost:.2f}dB")

    # 1. 10-band graphic EQ (only emit non-zero bands).
    for freq, gain in zip(EQ_FREQUENCIES_HZ, state.eq_bands):
        g = float(gain or 0.0)
        if abs(g) < 0.05:
            continue
        chain.append(
            f"equalizer=f={freq}:t=o:w={EQ_BAND_WIDTH_OCTAVES}:g={g:g}"
        )

    # 2. Exciter (crystalizer) — synthesized top-end sparkle, placed
    # right after the EQ so the shelves below still get the last word
    # on overall brightness.
    # c=0: crystalizer hard-clips at full scale by default, which is
    # exactly the crunch this rack must not add. The limiter at the end
    # handles its overs smoothly instead.
    if state.exciter and state.exciter_amount > 0.05:
        chain.append(f"crystalizer=i={state.exciter_amount:g}:c=0")

    # 3. Bass shelf at 120 Hz.
    if abs(state.bass_db) >= 0.05:
        chain.append(f"bass=g={state.bass_db:g}:f=120")

    # 4. Treble shelf at 8 kHz.
    if abs(state.treble_db) >= 0.05:
        chain.append(f"treble=g={state.treble_db:g}:f=8000")

    # 5. Lofi composite — thin the lows, roll the top, crush the bit
    # depth (log mode + heavy anti-aliasing so it's gritty, not harsh),
    # and add a slow shallow vibrato for tape wow. Early in the chain
    # because it reshapes the *source* character; everything after
    # (width, reverb, crossfeed) then treats the lofi'd signal as the
    # song.
    if state.lofi:
        chain.append("highpass=f=120")
        chain.append("lowpass=f=5800")
        chain.append("acrusher=bits=10:mode=log:aa=0.8:mix=0.35")
        chain.append("vibrato=f=0.4:d=0.04")

    # 6. Stereo width (1.0 == identity, skip).
    if abs(state.stereo_width - 1.0) >= 0.01:
        # c=0 for the same reason as the exciter: no built-in hard clip.
        chain.append(f"extrastereo=m={state.stereo_width:g}:c=0")

    # 7. Compressor.
    if state.compressor:
        # Conservative defaults that catch peaks without pumping the
        # signal. makeup=4 dB recovers the headroom the threshold ate.
        chain.append(
            "acompressor=threshold=-20dB:ratio=4:attack=20:release=250:makeup=4"
        )

    # 8–11. Modulation family, always ahead of the reverb (motion into
    # space, not space into motion). Two-voice chorus (positional sox
    # args: in_gain:out_gain:delays:decays:speeds:depths), a slow
    # feedback flanger, a wide triangular phaser, and tremolo with the
    # one exposed knob (rate).
    if state.chorus:
        chain.append("chorus=0.6:0.9:50|60:0.4|0.32:0.25|0.4:2|1.3")
    if state.flanger:
        chain.append("flanger=delay=1:depth=3:regen=20:width=70:speed=0.4")
    if state.phaser:
        chain.append("aphaser=in_gain=0.6:out_gain=0.9:delay=3:decay=0.5:speed=0.6")
    if state.tremolo:
        chain.append(f"tremolo=f={state.tremolo_speed:g}:d=0.6")

    # 12. Reverb — convolution against the preset's synthesized IR.
    reverb_entry = _reverb_lavfi_entry(state.reverb_preset, state.reverb_wet)
    if reverb_entry is not None:
        chain.append(reverb_entry)

    # 13. Headphone crossfeed — after the reverb so the room itself
    # gets blended between the ears too, which is the point.
    if state.crossfeed:
        chain.append(f"crossfeed=strength={state.crossfeed_strength:g}")

    # 14. Loudness normalization (EBU R128 target -14 LUFS — streaming-
    # platform-typical so cross-source queues feel level).
    if state.loudness_norm:
        chain.append("loudnorm=I=-14:LRA=11:tp=-1.5")

    # 15. Mono fold (channel-collapse via pan). Wrapped in a lavfi
    # graph because mpv 0.41's native af param parser splits on ':' and
    # chokes on pan's 'mono|c0=…' layout syntax ("AVOption 'mono|c0'
    # not found" at filter creation) — inside lavfi brackets ffmpeg
    # parses its own syntax and it just works.
    if state.mono:
        chain.append("lavfi=[pan=mono|c0=0.5*c0+0.5*c1]")

    # 16. Safety limiter. level=0 turns off alimiter's auto makeup gain,
    # which would otherwise pump everything up toward the ceiling; with
    # it off, anything under the ceiling passes through untouched.
    if chain:
        chain.append(
            f"alimiter=limit={LIMITER_CEILING}:attack=5:release=50:level=0"
        )

    return ",".join(chain)


__all__ = [
    "AudioFxState",
    "CustomSlot",
    "EQ_BAND_COUNT",
    "EQ_BAND_WIDTH_OCTAVES",
    "EQ_FREQUENCIES_HZ",
    "EQ_GAIN_MAX_DB",
    "EQ_GAIN_MIN_DB",
    "EQ_PRESETS",
    "REVERB_PRESETS",
    "build_filter_chain",
    "detect_eq_preset",
    "eq_peak_boost_db",
]
