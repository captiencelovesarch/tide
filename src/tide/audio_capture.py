"""PipeWire/PulseAudio monitor capture for the visualizer.

PortAudio (which sounddevice wraps) doesn't expose sink monitors on
PipeWire+PA reliably — it just sees the default *input* (typically the
mic). Instead we shell out to ``parec``, which is the canonical PA way
to capture from a sink's ``.monitor`` source. Works on PipeWire and
classic PulseAudio identically.

The capture loop runs in a Python thread; FFT and band-binning run there
too so the GUI thread only sees ready-to-render arrays. Results are
delivered on the GUI thread via Qt signals.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QObject, Signal


SAMPLE_RATE = 44_100
CHUNK = 1024              # samples per FFT chunk; ~23ms at 44.1kHz
BANDS = 32
SMOOTH_ALPHA = 0.45
EPS = 1e-9

# Bass-pulse envelope follower — drives the adaptive-background pulse. Kept
# separate from the visualizer's band smoothing. The raw low-frequency energy
# is normalized against a moving floor/peak, so bass-heavy songs establish a
# new baseline instead of pinning the ambient background at max forever.
PULSE_LOW_HZ = 30.0
PULSE_HIGH_HZ = 200.0
PULSE_GAIN = 6.0          # pre-log gain applied to raw bass magnitude
PULSE_TOLERANCE = 0.18    # ignore this much of the local bass range
PULSE_MIN_SPAN = 0.45     # log-domain floor-to-peak range minimum
PULSE_QUIET_FLOOR = 3.1   # below this, bass is treated as too quiet to pulse
PULSE_QUIET_FULL = 4.4    # above this, adaptive contrast has full strength
PULSE_FLOOR_RISE_S = 1.2  # sustained bass becomes "normal" over a few sec
PULSE_FLOOR_FALL_S = 0.7
PULSE_PEAK_FALL_S = 1.4
PULSE_RELEASE_S = 0.35    # decay time constant; attack is instantaneous


def _build_band_edges(n_bands: int = BANDS, sample_rate: int = SAMPLE_RATE,
                       chunk: int = CHUNK, low_hz: float = 30.0,
                       high_hz: float = 16_000.0) -> np.ndarray:
    n_bins = chunk // 2 + 1
    hz_per_bin = (sample_rate / 2.0) / (n_bins - 1)
    log_lo = np.log10(low_hz)
    log_hi = np.log10(high_hz)
    cut_hz = np.logspace(log_lo, log_hi, n_bands + 1)
    edges = np.clip(np.round(cut_hz / hz_per_bin).astype(int), 1, n_bins - 1)
    for i in range(1, len(edges)):
        if edges[i] <= edges[i - 1]:
            edges[i] = min(n_bins - 1, edges[i - 1] + 1)
    return edges


_HANN_WINDOW = np.hanning(CHUNK).astype(np.float32)
_BAND_EDGES = _build_band_edges()


def _pulse_bin_range() -> tuple[int, int]:
    hz_per_bin = (SAMPLE_RATE / 2.0) / (CHUNK // 2)
    lo = max(1, int(round(PULSE_LOW_HZ / hz_per_bin)))
    hi = max(lo + 1, int(round(PULSE_HIGH_HZ / hz_per_bin)))
    return lo, hi


_PULSE_LO, _PULSE_HI = _pulse_bin_range()


@dataclass
class _PulseState:
    env: float
    floor: float
    peak: float


def _ema_alpha(dt: float, tau_s: float) -> float:
    return 1.0 - math.exp(-dt / max(0.001, tau_s))


def _smoothstep(edge0: float, edge1: float, value: float) -> float:
    if edge1 <= edge0:
        return 1.0 if value >= edge1 else 0.0
    x = max(0.0, min(1.0, (value - edge0) / (edge1 - edge0)))
    return x * x * (3.0 - 2.0 * x)


def _compute_pulse(samples: np.ndarray, prev: _PulseState | None) -> _PulseState:
    """Adaptive instant-attack / slow-release bass pulse in 0..1.

    The pulse uses local contrast instead of absolute bass level. On a
    bass-heavy song, sustained bass raises ``floor`` and stops reading as a
    permanent hit; transient kicks still jump above the tolerance band.
    """
    windowed = samples * _HANN_WINDOW
    mag = np.abs(np.fft.rfft(windowed))
    energy = float(mag[_PULSE_LO:_PULSE_HI].mean())
    raw = math.log1p(energy * PULSE_GAIN)
    dt = CHUNK / SAMPLE_RATE

    if prev is None:
        floor = raw * 0.65
        peak = max(raw, floor + 0.55)
        env = 0.0
    else:
        floor_tau = PULSE_FLOOR_RISE_S if raw > prev.floor else PULSE_FLOOR_FALL_S
        floor_a = _ema_alpha(dt, floor_tau)
        floor = prev.floor + (raw - prev.floor) * floor_a

        if raw >= prev.peak:
            peak = raw
        else:
            peak_a = _ema_alpha(dt, PULSE_PEAK_FALL_S)
            peak = prev.peak + (floor - prev.peak) * peak_a
            peak = max(peak, raw)
        env = prev.env

    span = max(PULSE_MIN_SPAN, peak - floor)
    threshold = floor + span * PULSE_TOLERANCE
    level = (raw - threshold) / max(EPS, span * (1.0 - PULSE_TOLERANCE))
    level = max(0.0, min(1.0, level))
    # Local contrast alone is too twitchy on quiet songs, where tiny absolute
    # bass changes can fill the local range. Gate it by real bass energy.
    level *= _smoothstep(PULSE_QUIET_FLOOR, PULSE_QUIET_FULL, raw)
    if level >= env:
        env = level
    else:
        decay = math.exp(-dt / PULSE_RELEASE_S)
        env = env * decay + level * (1.0 - decay)
    return _PulseState(env=env, floor=floor, peak=peak)


def _compute_bands(samples: np.ndarray, prev: np.ndarray | None = None) -> np.ndarray:
    windowed = samples * _HANN_WINDOW
    spec = np.fft.rfft(windowed)
    mag = np.abs(spec)
    bands = np.empty(BANDS, dtype=np.float32)
    for i in range(BANDS):
        lo, hi = _BAND_EDGES[i], _BAND_EDGES[i + 1]
        if hi > lo:
            bands[i] = mag[lo:hi].mean()
        else:
            bands[i] = mag[lo]
    bands = np.log1p(bands * 8.0)
    ref = max(bands.max(), 1.5)
    bands = np.clip(bands / ref, 0.0, 1.0)
    if prev is not None and prev.shape == bands.shape:
        attack = bands > prev
        result = prev.copy()
        result[attack] = SMOOTH_ALPHA * bands[attack] + (1 - SMOOTH_ALPHA) * prev[attack]
        rel_a = SMOOTH_ALPHA * 0.45
        result[~attack] = rel_a * bands[~attack] + (1 - rel_a) * prev[~attack]
        return result
    return bands


def list_monitor_sources() -> list[tuple[str, str]]:
    """Return [(name, human_label), ...] for all available .monitor sources.

    Used by the settings dialog + viz cog so users can pick a non-default sink.
    """
    if shutil.which("pactl") is None:
        return []
    try:
        out = subprocess.run(
            ["pactl", "list", "sources", "short"],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return []
    result: list[tuple[str, str]] = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[1]
        if not name.endswith(".monitor"):
            continue
        # Tidy up the label: drop the alsa_output prefix + .monitor suffix
        label = name
        if label.startswith("alsa_output."):
            label = label[len("alsa_output."):]
        if label.endswith(".monitor"):
            label = label[:-len(".monitor")]
        result.append((name, label))
    return result


def _default_sink_monitor() -> str | None:
    """Return ``<default_sink>.monitor`` or None if pactl is missing."""
    if shutil.which("pactl") is None:
        return None
    try:
        out = subprocess.run(
            ["pactl", "info"], capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return None
    sink = None
    for line in out.stdout.splitlines():
        if line.startswith("Default Sink:"):
            sink = line.split(":", 1)[1].strip()
            break
    if not sink:
        return None
    return sink + ".monitor"


class AudioVisualizerFeed(QObject):
    bands_updated = Signal(object)         # numpy.ndarray (BANDS,)
    waveform_updated = Signal(object)      # numpy.ndarray (CHUNK,)
    pulse_updated = Signal(float)          # bass-energy envelope, 0..1
    error = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self._monitor = "(not started)"
        self._prev_bands: np.ndarray | None = None
        self._pulse_env: _PulseState | None = None
        # Reference-counted consumers. The singleton feed is shared by the
        # visualizer view and the app-wide ambient-pulse controller; capture
        # runs while at least one consumer holds it so neither tears it down
        # under the other.
        self._consumers: set[str] = set()
        self._preferred_source: str | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def device(self) -> str:
        return self._monitor

    # ---------- reference-counted lifecycle ----------

    def add_consumer(self, name: str, source: str | None = None) -> bool:
        """Register a consumer and ensure capture is running. Returns True if
        the feed is live afterwards. ``source`` sets the preferred monitor
        (used only when the feed has to be (re)started)."""
        self._consumers.add(name)
        if source is not None:
            self._preferred_source = source
        if not self._running:
            return self.start(source=self._preferred_source)
        return True

    def remove_consumer(self, name: str) -> None:
        """Drop a consumer; stop capture once nobody holds it."""
        self._consumers.discard(name)
        if not self._consumers and self._running:
            self.stop()

    def set_source(self, source: str | None) -> None:
        """Change the preferred monitor. Restarts an in-flight capture so the
        new device takes effect while keeping consumers registered."""
        self._preferred_source = source
        if self._running:
            holders = set(self._consumers)
            self.stop()
            self._consumers = holders
            self.start(source=self._preferred_source)

    def start(self, source: str | None = None) -> bool:
        if self._running:
            return True
        if shutil.which("parec") is None:
            self.error.emit("parec not found — install libpulse")
            return False
        monitor = source or _default_sink_monitor()
        if not monitor:
            self.error.emit("couldn't resolve a sink monitor — check pactl info")
            return False

        try:
            self._proc = self._spawn_parec(monitor)
        except Exception as exc:
            self.error.emit(f"parec failed to start: {exc}")
            return False

        self._monitor = monitor
        self._stop.clear()
        self._thread = threading.Thread(target=self._process_loop, name="tide-fft", daemon=True)
        self._thread.start()
        self._running = True
        return True

    def stop(self) -> None:
        if not self._running:
            return
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=1.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None
        self._running = False
        self._prev_bands = None
        self._pulse_env = None
        self._consumers.clear()

    # ---------- worker ----------

    def _spawn_parec(self, monitor: str) -> subprocess.Popen:
        return subprocess.Popen(
            [
                "parec",
                "-d", monitor,
                "--rate", str(SAMPLE_RATE),
                "--channels", "1",
                "--format", "float32le",
                "--raw",
                "--latency-msec", "10",
                "--client-name", "tide-visualizer",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

    def _process_loop(self) -> None:
        chunk_bytes = CHUNK * 4   # float32
        respawns = 0
        while not self._stop.is_set():
            proc = self._proc
            if proc is None or proc.stdout is None:
                return
            stdout = proc.stdout
            # With bufsize=0 the pipe is a raw FileIO: read() returns whatever
            # is currently buffered, and parec at --latency-msec 10 delivers
            # ~1.8KB every 10ms — well under a chunk. Short reads are the
            # normal case, so accumulate them here; discarding them (as this
            # loop once did) drops audio and adds 50-70ms of latency per
            # discarded stretch, which read as the background pulsing behind
            # the beat.
            pending = bytearray()
            died = False
            while not self._stop.is_set():
                try:
                    data = stdout.read(chunk_bytes)
                except Exception:
                    died = True
                    break
                if not data:
                    # EOF. Distinguish "stream paused" from "parec exited". A
                    # dead child returns EOF forever; without the poll() this
                    # loop spun on it for the rest of the session while the
                    # process sat unreaped in the table.
                    if proc.poll() is not None:
                        died = True
                        break
                    if self._stop.wait(0.05):
                        break
                    continue
                respawns = 0   # healthy data — reset the give-up counter
                pending += data
                if len(pending) < chunk_bytes:
                    continue
                # If the thread stalled (GUI hiccup, suspend), skip stale
                # audio rather than replaying it late.
                if len(pending) > chunk_bytes * 4:
                    del pending[:len(pending) - chunk_bytes * 2]
                while len(pending) >= chunk_bytes and not self._stop.is_set():
                    samples = np.frombuffer(
                        bytes(pending[:chunk_bytes]), dtype=np.float32
                    )
                    del pending[:chunk_bytes]
                    try:
                        self._prev_bands = _compute_bands(samples, self._prev_bands)
                    except Exception:
                        continue
                    self.bands_updated.emit(self._prev_bands.copy())
                    self.waveform_updated.emit(samples.copy())
                    try:
                        self._pulse_env = _compute_pulse(samples, self._pulse_env)
                    except Exception:
                        self._pulse_env = None
                    else:
                        self.pulse_updated.emit(float(self._pulse_env.env))
            if not died or self._stop.is_set():
                return
            # parec exited underneath us (sink unplugged, pipewire restart).
            # Respawn a few times so an audio-server hiccup doesn't
            # permanently kill the visualizer/ambient pulse mid-session.
            respawns += 1
            if respawns > 3:
                self._running = False
                self.error.emit("audio capture stopped — parec keeps exiting")
                return
            if self._stop.wait(0.5):
                return
            try:
                self._proc = self._spawn_parec(self._monitor)
            except Exception as exc:
                self._running = False
                self.error.emit(f"parec died and couldn't restart: {exc}")
                return


# Singleton.
_instance: AudioVisualizerFeed | None = None


def feed() -> AudioVisualizerFeed:
    global _instance
    if _instance is None:
        _instance = AudioVisualizerFeed()
    return _instance
