"""Read cookies from the user's real Chromium-family browser.

Google blocks credential sign-in in embedded webviews. Instead we let the
user sign in via their real (trusted) browser and import the cookies.

The user never sees a config file. From their POV, it's "sign in to YT Music
in chromium, then click 'import'."

Supports Chromium, Chrome, Brave, Vivaldi, Edge on Linux. On KDE the safe-
storage key lives in kwallet under (folder "Chromium Keys", entry
"Chromium Safe Storage"). On GNOME it's in libsecret under application
"chromium". Both stash the same kind of key.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


@dataclass
class BrowserProfile:
    slug: str                     # "chromium", "google-chrome", "brave", ...
    label: str                    # human label
    cookies_path: Path
    keyring_app: str              # "chromium" or "chrome", used for libsecret lookup
    kwallet_folder: str           # "Chromium Keys" / "Chrome Keys" / ...
    kwallet_entry: str            # "Chromium Safe Storage" / "Chrome Safe Storage" / ...


# Order matters — first match wins as the default suggestion.
_CANDIDATES: list[BrowserProfile] = [
    BrowserProfile(
        slug="chromium",
        label="chromium",
        cookies_path=Path.home() / ".config/chromium/Default/Cookies",
        keyring_app="chromium",
        kwallet_folder="Chromium Keys",
        kwallet_entry="Chromium Safe Storage",
    ),
    BrowserProfile(
        slug="google-chrome",
        label="google chrome",
        cookies_path=Path.home() / ".config/google-chrome/Default/Cookies",
        keyring_app="chrome",
        kwallet_folder="Chrome Keys",
        kwallet_entry="Chrome Safe Storage",
    ),
    BrowserProfile(
        slug="brave",
        label="brave",
        cookies_path=Path.home() / ".config/BraveSoftware/Brave-Browser/Default/Cookies",
        keyring_app="brave",
        kwallet_folder="Brave Keys",
        kwallet_entry="Brave Safe Storage",
    ),
    BrowserProfile(
        slug="vivaldi",
        label="vivaldi",
        cookies_path=Path.home() / ".config/vivaldi/Default/Cookies",
        keyring_app="vivaldi",
        kwallet_folder="Vivaldi Keys",
        kwallet_entry="Vivaldi Safe Storage",
    ),
    BrowserProfile(
        slug="microsoft-edge",
        label="microsoft edge",
        cookies_path=Path.home() / ".config/microsoft-edge/Default/Cookies",
        keyring_app="chromium",
        kwallet_folder="Microsoft Edge Keys",
        kwallet_entry="Microsoft Edge Safe Storage",
    ),
]


class ImportError_(RuntimeError):
    pass


def available_profiles() -> list[BrowserProfile]:
    return [p for p in _CANDIDATES if p.cookies_path.is_file()]


# ---------- key retrieval ----------


def _try_kwallet(folder: str, entry: str) -> bytes | None:
    for wallet in ("kdewallet",):
        try:
            out = subprocess.run(
                ["kwallet-query", "-r", entry, "-f", folder, wallet],
                capture_output=True, text=True, timeout=10,
            )
        except FileNotFoundError:
            return None
        except subprocess.TimeoutExpired:
            return None
        if out.returncode == 0:
            value = out.stdout.strip()
            if value:
                return value.encode("utf-8")
    return None


def _try_secret_service(app: str) -> bytes | None:
    try:
        import secretstorage  # type: ignore
    except ImportError:
        return None
    try:
        bus = secretstorage.dbus_init()
        try:
            for coll in secretstorage.get_all_collections(bus):
                try:
                    for item in coll.search_items({"application": app}):
                        try:
                            secret = item.get_secret()
                            if secret:
                                return bytes(secret)
                        except Exception:
                            continue
                except Exception:
                    continue
        finally:
            try:
                bus.close()
            except Exception:
                pass
    except Exception:
        return None
    return None


def get_safe_storage_key(profile: BrowserProfile) -> bytes:
    """Return the per-browser safe-storage password used to derive the cookie key.

    Tries Secret Service first (GNOME, libsecret, portal), then kwallet.
    Returns the hardcoded "peanuts" fallback if both fail (works for v10
    cookies that were written when no keyring was reachable).
    """
    for getter in (
        lambda: _try_secret_service(profile.keyring_app),
        lambda: _try_kwallet(profile.kwallet_folder, profile.kwallet_entry),
    ):
        key = getter()
        if key:
            return key
    return b"peanuts"


# ---------- AES-CBC decryption ----------


def _derive_key(password: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", iterations=1, dklen=16)


def _strip_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > 16:
        return data
    if data[-pad:] != bytes([pad]) * pad:
        return data
    return data[:-pad]


def decrypt_chromium_value(blob: bytes, key: bytes, *, host_key: str = "", name: str = "") -> str:
    """Decrypt a Chromium cookie value.

    Recognizes v10 / v11 prefixes. Modern Chromium (>= ~v116) prepends a
    32-byte SHA256 integrity hash to the plaintext — we strip those bytes
    when present.

    Returns "" for empty/unknown blobs.
    """
    if not blob:
        return ""
    if blob[:3] not in (b"v10", b"v11"):
        try:
            return blob.decode("utf-8", errors="replace")
        except Exception:
            return ""
    ct = blob[3:]
    iv = b" " * 16
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    pt = decryptor.update(ct) + decryptor.finalize()
    pt = _strip_pkcs7(pt)
    # Heuristic: if the first 32 bytes look like binary (non-printable) but
    # bytes after that decode cleanly as UTF-8, treat the prefix as the
    # SHA256 integrity hash and skip it.
    if len(pt) > 32 and not _looks_printable(pt[:32]):
        try:
            return pt[32:].decode("utf-8")
        except UnicodeDecodeError:
            pass
    return pt.decode("utf-8", errors="replace")


def _looks_printable(b: bytes) -> bool:
    if not b:
        return False
    printable = sum(1 for c in b if 0x20 <= c < 0x7f)
    return printable / len(b) > 0.9


# ---------- the public entry point ----------


@dataclass
class ImportResult:
    profile: BrowserProfile
    cookies: dict[str, str] = field(default_factory=dict)

    @property
    def looks_signed_in(self) -> bool:
        return "__Secure-3PAPISID" in self.cookies


def import_cookies(profile: BrowserProfile) -> ImportResult:
    """Pull YouTube cookies from the given browser profile.

    Copies the Cookies SQLite file first so an open browser doesn't lock us out.
    """
    if not profile.cookies_path.is_file():
        raise ImportError_(f"no cookies database at {profile.cookies_path}")

    key = _derive_key(get_safe_storage_key(profile))

    with tempfile.TemporaryDirectory(prefix="tide-import-") as tmp:
        copy = Path(tmp) / "Cookies"
        try:
            shutil.copy2(profile.cookies_path, copy)
            wal = profile.cookies_path.parent / (profile.cookies_path.name + "-wal")
            if wal.is_file():
                shutil.copy2(wal, copy.parent / (copy.name + "-wal"))
        except OSError as exc:
            raise ImportError_(f"couldn't read cookies file: {exc}") from exc

        try:
            conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise ImportError_(f"couldn't open cookies db: {exc}") from exc

        try:
            rows = conn.execute(
                "SELECT host_key, name, value, encrypted_value, expires_utc "
                "FROM cookies "
                "WHERE host_key LIKE '%youtube.com' OR host_key LIKE '%google.com' "
                "ORDER BY expires_utc DESC"
            ).fetchall()
        finally:
            conn.close()

    out: dict[str, str] = {}
    for host_key, name, value, encrypted_value, _expires in rows:
        if name in out:
            continue  # already have a fresher one (ORDER BY expires DESC)
        try:
            if encrypted_value:
                decoded = decrypt_chromium_value(
                    bytes(encrypted_value), key, host_key=host_key or "", name=name or ""
                )
            else:
                decoded = value or ""
        except Exception:
            continue
        if decoded:
            out[name] = decoded

    return ImportResult(profile=profile, cookies=out)
