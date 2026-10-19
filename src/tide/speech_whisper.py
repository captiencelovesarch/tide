"""Speech to text with whisper base.en, on the onnxruntime tide already
uses for precise lyric timing. For voice search (ctx.speech): a few
seconds of someone saying a song or an artist.

Why not the lyric-timing model: wav2vec2 read straight as text spells by
ear ("surreal" came out "sireal", 2026-10-19). whisper knows words.

Everything runs on this computer. The model is downloaded on request
(~77 MB, onnx-community's int8 export at a pinned revision, every file
checked against its sha256) into DATA_DIR/models.

Pure numpy + onnxruntime: the log-mel features are whisper's own recipe
(n_fft 400, hop 160, 80 slaney mel bands, log10, clamp to max-8, scale)
redone without torch or librosa, and decoding is greedy.
"""
from __future__ import annotations

import hashlib
import json
import threading
import urllib.request
from pathlib import Path
from typing import Callable

import numpy as np

from . import config

MODEL_NAME = "whisper-base.en-int8"
REVISION = "51eefc0af78b103839eda9e7e4f4186acc6517fe"
BASE_URL = f"https://huggingface.co/onnx-community/whisper-base.en/resolve/{REVISION}/"
# path in the repo → (local name, size in bytes, sha256). the onnx hashes
# are hugging face's own lfs ids; the json ones were taken on download.
FILES: dict[str, tuple[str, int, str]] = {
    "onnx/encoder_model_quantized.onnx": (
        "encoder.onnx", 23201320,
        "6e8001198c490bbae018c0044f630c2915efb826bad957006ce36152d0ab2a10"),
    "onnx/decoder_model_quantized.onnx": (
        "decoder.onnx", 53309666,
        "88f4ffae377556e6326125226c0797f6d08d6075b4cb9891df709404d33ca067"),
    "vocab.json": (
        "vocab.json", 999186,
        "f6bd25a65e4e63ca31360e9fb11c7e4f9a391a78385d640acd814092dd6eee4f"),
    "added_tokens.json": (
        "added_tokens.json", 34604,
        "560be47bea388757f8d4cc185c5d82067426cbb6361e38016dd90ddc01ab203a"),
}

RATE = 16000
N_FFT = 400
HOP = 160
N_MELS = 80
CHUNK = 30 * RATE           # whisper always looks at 30 s
MAX_TOKENS = 48             # a search is a few words

_session_lock = threading.Lock()
_sessions: tuple | None = None


def model_dir() -> Path:
    return config.DATA_DIR / "models" / MODEL_NAME


def total_bytes() -> int:
    return sum(size for _name, size, _sha in FILES.values())


def runtime_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except Exception:
        return False
    return True


def installed() -> bool:
    d = model_dir()
    try:
        return all((d / name).is_file() and (d / name).stat().st_size == size
                   for name, size, _sha in FILES.values())
    except OSError:
        return False


def available() -> bool:
    return runtime_available() and installed()


def download(progress: Callable[[int, int], None] | None = None,
             should_stop: Callable[[], bool] | None = None) -> None:
    """fetch every file, check its size and sha256, then move it into
    place. raises on anything short of perfect."""
    d = model_dir()
    d.mkdir(parents=True, exist_ok=True)
    total, done = total_bytes(), 0
    for remote, (name, size, sha) in FILES.items():
        target = d / name
        if target.is_file() and target.stat().st_size == size:
            done += size
            continue
        tmp = target.with_suffix(target.suffix + ".part")
        digest = hashlib.sha256()
        got = 0
        req = urllib.request.Request(BASE_URL + remote, headers={"User-Agent": "tide"})
        with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as f:
            while True:
                if should_stop is not None and should_stop():
                    raise RuntimeError("download stopped")
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                got += len(chunk)
                if got > size:
                    raise RuntimeError(f"{name}: bigger than expected")
                digest.update(chunk)
                f.write(chunk)
                if progress is not None:
                    progress(done + got, total)
        if got != size or digest.hexdigest() != sha:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"{name}: didn't match its checksum")
        tmp.replace(target)
        done += size
    global _sessions
    with _session_lock:
        _sessions = None


# ---------- features ----------

def _hz_to_mel(f):
    """slaney mel scale (librosa's default)."""
    f = np.asarray(f, dtype=np.float64)
    f_sp = 200.0 / 3
    mels = f / f_sp
    min_log_hz, min_log_mel, logstep = 1000.0, 1000.0 / f_sp, np.log(6.4) / 27.0
    return np.where(f >= min_log_hz, min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) / logstep,
                    mels)


def _mel_to_hz(m):
    m = np.asarray(m, dtype=np.float64)
    f_sp = 200.0 / 3
    freqs = f_sp * m
    min_log_hz, min_log_mel, logstep = 1000.0, 1000.0 / f_sp, np.log(6.4) / 27.0
    return np.where(m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), freqs)


def mel_filters() -> np.ndarray:
    """(80, 201): librosa.filters.mel(sr=16000, n_fft=400, n_mels=80)."""
    fft_freqs = np.linspace(0, RATE / 2, 1 + N_FFT // 2)
    mel_f = _mel_to_hz(np.linspace(_hz_to_mel(0.0), _hz_to_mel(RATE / 2), N_MELS + 2))
    fdiff = np.diff(mel_f)
    ramps = mel_f[:, None] - fft_freqs[None, :]
    weights = np.zeros((N_MELS, len(fft_freqs)))
    for i in range(N_MELS):
        lower = -ramps[i] / fdiff[i]
        upper = ramps[i + 2] / fdiff[i + 1]
        weights[i] = np.maximum(0, np.minimum(lower, upper))
    weights *= (2.0 / (mel_f[2:N_MELS + 2] - mel_f[:N_MELS]))[:, None]
    return weights.astype(np.float32)


def log_mel(audio: np.ndarray) -> np.ndarray:
    """(80, 3000) for whisper: 16 kHz mono float audio, padded to 30 s."""
    audio = np.asarray(audio, dtype=np.float32)[:CHUNK]
    audio = np.pad(audio, (0, CHUNK - len(audio)))
    # torch.stft(center=True, pad_mode="reflect") with a periodic hann
    padded = np.pad(audio, (N_FFT // 2, N_FFT // 2), mode="reflect")
    window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)).astype(np.float32)
    n_frames = 1 + (len(padded) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = padded[idx] * window
    power = np.abs(np.fft.rfft(frames, n=N_FFT, axis=1)) ** 2       # (frames, 201)
    power = power[:-1].T                                             # drop the last frame
    mel = mel_filters() @ power
    log = np.log10(np.maximum(mel, 1e-10))
    log = np.maximum(log, log.max() - 8.0)
    return ((log + 4.0) / 4.0).astype(np.float32)


# ---------- tokens ----------

def _byte_decoder() -> dict[str, int]:
    """gpt-2's printable stand-ins for raw bytes, reversed."""
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + \
        list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return {chr(c): b for b, c in zip(bs, cs)}


class _Tokens:
    def __init__(self, d: Path) -> None:
        vocab = json.loads((d / "vocab.json").read_text(encoding="utf-8"))
        added = json.loads((d / "added_tokens.json").read_text(encoding="utf-8"))
        self.by_id = {i: t for t, i in vocab.items()}
        self.by_id.update({i: t for t, i in added.items()})
        self.special = set(added.values())
        self.sot = added["<|startoftranscript|>"]
        self.eot = added.get("<|endoftext|>", vocab.get("<|endoftext|>"))
        self.no_ts = added["<|notimestamps|>"]
        self._bytes = _byte_decoder()

    def decode(self, ids: list[int]) -> str:
        text = "".join(self.by_id.get(i, "") for i in ids if i not in self.special)
        raw = bytes(self._bytes.get(ch, ord("?")) for ch in text)
        return raw.decode("utf-8", errors="replace")


def _load():
    global _sessions
    with _session_lock:
        if _sessions is None:
            import onnxruntime as ort
            d = model_dir()
            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            opts.intra_op_num_threads = 4
            enc = ort.InferenceSession(str(d / "encoder.onnx"), opts,
                                       providers=["CPUExecutionProvider"])
            dec = ort.InferenceSession(str(d / "decoder.onnx"), opts,
                                       providers=["CPUExecutionProvider"])
            _sessions = (enc, dec, _Tokens(d))
        return _sessions


def transcribe(audio: np.ndarray) -> str:
    """what was said in ``audio`` (16 kHz mono float), lowercase, trimmed."""
    enc, dec, tok = _load()
    feats = log_mel(audio)[None]
    hidden = enc.run(None, {enc.get_inputs()[0].name: feats})[0]
    names = [i.name for i in dec.get_inputs()]
    ids_name = next(n for n in names if "input_ids" in n)
    hid_name = next(n for n in names if "encoder_hidden_states" in n)
    ids = [tok.sot, tok.no_ts]
    out: list[int] = []
    for step in range(MAX_TOKENS):
        logits = dec.run(None, {ids_name: np.array([ids], dtype=np.int64),
                                hid_name: hidden})[0][0, -1]
        # never emit other special tokens; no empty transcript on step one
        for i in tok.special:
            if i != tok.eot and i < len(logits):
                logits[i] = -np.inf
        if step == 0 and tok.eot < len(logits):
            logits[tok.eot] = -np.inf
        nxt = int(np.argmax(logits))
        if nxt == tok.eot:
            break
        out.append(nxt)
        ids.append(nxt)
    text = tok.decode(out)
    return " ".join(text.replace("\n", " ").split()).strip().lower().rstrip(".!?,")
