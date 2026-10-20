"""push-to-talk capture and the "is tide's speech model good enough"
test: wav2vec2 from lyric_align read straight as text, then a youtube
music search with what it heard."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import wave
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))

RATE = 16000
COMMAND_WORDS = ("play", "queue", "add", "put on")


@dataclass
class Prompt:
    say: str
    title: str
    artist: str


def load_prompts(path: Path) -> list[Prompt]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        say, title, artist = (part.strip() for part in line.split("|"))
        out.append(Prompt(say, title, artist))
    return out


# ---------- capture ----------

class Mic:
    """parec on the default input while the button is held. steamvr makes
    the headset mic the default input when vr starts (its audio setting)."""

    def __init__(self, device: str | None = None) -> None:
        self.device = device
        self._proc: subprocess.Popen | None = None
        self._buf = bytearray()
        self._thread: threading.Thread | None = None
        self.started = 0.0

    def start(self) -> None:
        cmd = ["parec", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1",
               "--latency-msec=20"]
        if self.device:
            cmd.append(f"--device={self.device}")
        self._buf = bytearray()
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        self.started = time.monotonic()

    def _pump(self) -> None:
        proc = self._proc
        while proc is not None and proc.stdout is not None:
            chunk = proc.stdout.read(3200)
            if not chunk:
                return
            self._buf.extend(chunk)

    def stop(self) -> bytes:
        if self._proc is None:
            return b""
        self._proc.terminate()
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._proc = None
        return bytes(self._buf)

    @property
    def seconds(self) -> float:
        return len(self._buf) / (2 * RATE)


def write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"{path.name}: want 16 kHz mono s16")
        pcm = w.readframes(w.getnframes())
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


# ---------- recognition ----------

def greedy_ctc(logits: np.ndarray, vocab: dict[str, int], blank: int) -> str:
    """best token per frame, repeats merged, blanks dropped, "|" is a space."""
    inv = {i: ch for ch, i in vocab.items()}
    best = logits.argmax(axis=-1)
    out, prev = [], None
    for t in best:
        t = int(t)
        if t != prev and t != blank and t in inv:
            out.append(inv[t])
        prev = t
    return re.sub(r"\s+", " ", "".join(out).replace("|", " ")).strip().lower()


def ensure_model() -> bool:
    from tide import lyric_align
    if not lyric_align.runtime_available():
        print("onnxruntime is missing (python-onnxruntime-cpu)")
        return False
    if lyric_align.model_installed():
        return True
    size_mb = lyric_align.MODEL_BYTES / 1e6
    print(f"tide's speech model isn't downloaded yet ({size_mb:.0f} MB, the one "
          f"settings → lyrics → precise timing uses).")
    print(f"it goes to {lyric_align.model_path()}")
    if input("download it now? [y/N] ").strip().lower() != "y":
        return False

    def progress(done: int, total: int) -> None:
        print(f"\r  {done / 1e6:5.1f} / {total / 1e6:.1f} MB", end="", flush=True)

    lyric_align.download_model(progress)
    print()
    return lyric_align.model_installed()


def transcribe(audio: np.ndarray) -> tuple[str, float]:
    from tide import lyric_align
    t0 = time.perf_counter()
    logits = lyric_align._run_model(audio)
    text = greedy_ctc(logits, lyric_align.VOCAB, lyric_align.BLANK)
    return text, (time.perf_counter() - t0) * 1000.0


# ---------- search + scoring ----------

def strip_command(text: str) -> str:
    for word in COMMAND_WORDS:
        if text == word:
            return ""
        if text.startswith(word + " "):
            return text[len(word) + 1:]
    return text


def norm(s: str) -> str:
    folded = unicodedata.normalize("NFKD", s.lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


def is_match(prompt: Prompt, title: str, artists: list[str]) -> bool:
    want, got = norm(prompt.title), norm(title)
    title_ok = want in got or SequenceMatcher(None, want, got).ratio() >= 0.85
    artist_ok = any(norm(prompt.artist) in norm(a) or norm(a) in norm(prompt.artist)
                    for a in artists if a)
    return title_ok and artist_ok


def search(yt, query: str) -> list[tuple[str, list[str]]]:
    if not query:
        return []
    try:
        rows = yt.search(query, filter="songs", limit=5)
    except Exception as exc:  # noqa: BLE001 - one bad query shouldn't end the run
        print(f"  search failed: {exc}")
        return []
    out = []
    for r in rows[:5]:
        artists = [a.get("name", "") for a in (r.get("artists") or [])]
        out.append((r.get("title") or "", artists))
    return out


def rank(prompt: Prompt, results: list[tuple[str, list[str]]]) -> int | None:
    """1-based position of the wanted song in the results, or None."""
    for i, (title, artists) in enumerate(results):
        if is_match(prompt, title, artists):
            return i + 1
    return None
