"""paths, decoding and song keys shared by the bench."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import zlib

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

SR = 22_050
AUDIO_EXT = (".mp3", ".flac", ".opus", ".ogg", ".oga", ".m4a", ".aac", ".wav", ".webm")


def cache_dir() -> Path:
    """TIDE_PULSE_BENCH, or ~/.cache/tide-pulse-bench."""
    d = Path(os.environ.get("TIDE_PULSE_BENCH") or Path.home() / ".cache" / "tide-pulse-bench")
    d.mkdir(parents=True, exist_ok=True)
    return d


def ref_dir() -> Path:
    d = cache_dir() / "ref"
    d.mkdir(exist_ok=True)
    return d


def key(path: str) -> str:
    return os.path.basename(path)


def collect(paths: list[str]) -> list[str]:
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, names in os.walk(p):
                files += [os.path.join(root, n) for n in sorted(names) if n.lower().endswith(AUDIO_EXT)]
        elif os.path.isfile(p):
            files.append(p)
        else:
            print(f"skip {p}: not a file or folder", file=sys.stderr)
    return files


def decode(path: str, sr: int = SR, channels: int = 1) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-vn",
                          "-ac", str(channels), "-ar", str(sr), "-f", "f32le", "pipe:1"],
                         check=True, capture_output=True).stdout
    pcm = np.frombuffer(raw, np.float32).copy()
    return pcm.reshape(-1, channels).T.copy() if channels > 1 else pcm


def load(path: str) -> np.ndarray:
    """mono 22.05 khz, cached."""
    d = cache_dir() / "audio"
    d.mkdir(exist_ok=True)
    p = d / (key(path) + ".npy")
    if not p.exists():
        np.save(p, decode(path))
    return np.load(p)


def _words(path: str) -> frozenset[str]:
    name = os.path.splitext(key(path))[0].lower()
    name = re.sub(r"[\(\[].*?[\)\]]", " ", name)
    words = set(re.findall(r"[a-z0-9]+", name))
    return frozenset(words - {"instrumental", "instru", "acapella", "remastered", "version",
                              "official", "music", "video", "the"})


def folds(paths: list[str]) -> dict[str, int]:
    """0 or 1 per song, two halves for cross-fitting. a full mix and its
    instrumental or acapella land in the same half: songs whose name words
    contain one another's are one song."""
    words = {p: _words(p) for p in paths}
    group = {p: p for p in paths}

    def root(p):
        while group[p] != p:
            p = group[p]
        return p
    for a in paths:
        for b in paths:
            if a < b and words[a] and words[b] and (words[a] <= words[b] or words[b] <= words[a]):
                group[root(a)] = root(b)
    out = {}
    for p in paths:
        r = root(p)
        out[p] = zlib.crc32(" ".join(sorted(words[r])).encode()) % 2
    return out
