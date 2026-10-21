"""OpenRGB, over its sdk protocol (tcp, port 6742), no library needed.

tide speaks protocol version 0: it never asks for a newer one, so every
OpenRGB server answers in the oldest, simplest layout. that's enough to
learn each device's name and led count, switch it to direct control and
set its colours. the colours each device had are kept and put back when
tide lets go.

packets: "ORGB", then device index, packet id and data size as
little-endian u32s, then the data.
"""
from __future__ import annotations

import select
import socket
import struct
import time

PORT = 6742
MAGIC = b"ORGB"
HEADER = struct.Struct("<4sIII")

REQUEST_CONTROLLER_COUNT = 0
REQUEST_CONTROLLER_DATA = 1
SET_CLIENT_NAME = 50
DEVICE_LIST_UPDATED = 100
RESIZE_ZONE = 1000
UPDATE_LEDS = 1050
SET_CUSTOM_MODE = 1100
REFRESH_S = 10.0           # look at the device list again this often
# an addressable header openrgb reports as 0 leds hasn't been set up: give
# it this many. more than are plugged in is harmless (the extra ones just
# aren't there), and it covers most coolers and a chain of fans
HEADER_GUESS = 24


def parse_address(text: str) -> tuple[str, int]:
    text = (text or "").strip() or "127.0.0.1"
    host, _, port = text.rpartition(":") if text.count(":") == 1 else (text, "", "")
    return (host or text), int(port) if port.isdigit() else PORT


def packet(device: int, kind: int, data: bytes = b"") -> bytes:
    return HEADER.pack(MAGIC, device, kind, len(data)) + data


def leds_packet(colors: list[tuple[int, int, int]]) -> bytes:
    """UPDATE_LEDS data: its own size, a count, then r g b and a pad byte each."""
    body = struct.pack("<H", len(colors)) + b"".join(bytes((r, g, b, 0)) for r, g, b in colors)
    return struct.pack("<I", 4 + len(body)) + body


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data, self.at = data, 0

    def take(self, fmt: str):
        size = struct.calcsize(fmt)
        if self.at + size > len(self.data):
            raise ValueError("controller data ran short")
        value = struct.unpack_from(fmt, self.data, self.at)
        self.at += size
        return value if len(value) > 1 else value[0]

    def skip(self, n: int) -> None:
        if self.at + n > len(self.data):
            raise ValueError("controller data ran short")
        self.at += n

    def string(self) -> str:
        n = self.take("<H")
        raw = self.data[self.at:self.at + n]
        self.skip(n)
        return raw.rstrip(b"\0").decode("utf-8", "replace")


def parse_controller(data: bytes) -> tuple[str, int, list[tuple[int, int, int]]]:
    """(name, led count, its colours now) from protocol-0 controller data."""
    name, count, colors, _zones = parse_controller_zones(data)
    return name, count, colors


def parse_controller_zones(data: bytes):
    """parse_controller, plus each zone as (name, leds, min, max)."""
    r = _Reader(data)
    r.take("<I")                    # data size
    r.take("<i")                    # device type
    name = r.string()
    for _ in range(4):              # description, version, serial, location
        r.string()
    modes = r.take("<H")
    r.take("<i")                    # active mode
    for _ in range(modes):
        r.string()
        # value, flags, speed min/max, colours min/max, speed, direction, colour mode
        r.skip(4 * 9)
        r.skip(4 * r.take("<H"))    # the mode's colours
    zone_list = []
    for _ in range(r.take("<H")):
        zname = r.string()
        r.take("<i")                # zone type
        lo = r.take("<I")
        hi = r.take("<I")
        n = r.take("<I")
        zone_list.append((zname, n, lo, hi))
        r.skip(r.take("<H"))        # matrix map, if any
    zone_leds = sum(z[1] for z in zone_list)
    for _ in range(r.take("<H")):   # leds: a name and a value each
        r.string()
        r.take("<I")
    count = r.take("<H")
    colors = []
    for _ in range(count):
        c = r.take("<I")
        colors.append((c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF))
    if count == 0:
        count = zone_leds
    return name, count, colors, zone_list


def empty_headers(zones: list) -> list[int]:
    """the zones that look like an addressable header nobody has set up:
    0 leds, but able to take some."""
    return [i for i, (_name, n, _lo, hi) in enumerate(zones) if n == 0 and hi >= HEADER_GUESS]


class Server:
    """every device on one OpenRGB sdk server."""

    kind = "openrgb"

    def __init__(self, address: str) -> None:
        self.address = address
        self.host, self.port = parse_address(address)
        self.name = "openrgb"
        self.sock: socket.socket | None = None
        self.devices: list[str] = []
        self.leds: list[int] = []
        self._saved: list[list[tuple[int, int, int]]] = []
        self._checked = 0.0
        self.size_headers = True
        self.resized: list[str] = []

    # ---- wire ----

    def _send(self, device: int, kind: int, data: bytes = b"") -> None:
        assert self.sock is not None
        self.sock.sendall(packet(device, kind, data))

    def _recv_exact(self, n: int) -> bytes:
        assert self.sock is not None
        out = bytearray()
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise OSError("openrgb hung up")
            out += chunk
        return bytes(out)

    def _reply(self, kind: int) -> bytes:
        """the next packet of ``kind``; anything else (a device list
        update) is read and dropped."""
        while True:
            magic, _dev, got, size = HEADER.unpack(self._recv_exact(HEADER.size))
            if magic != MAGIC:
                raise OSError("that isn't an openrgb sdk server")
            data = self._recv_exact(size) if size else b""
            if got == kind:
                return data

    # ---- what the show calls ----

    def _query(self, size_headers: bool = False) -> list[tuple[str, int, list]]:
        """every device. with ``size_headers``, an addressable header that
        was never set up gets HEADER_GUESS leds first (openrgb keeps the
        size), so a cooler or fans on it light up without opening
        openrgb (2026-10-21)."""
        self._send(0, REQUEST_CONTROLLER_COUNT)
        count = struct.unpack("<I", self._reply(REQUEST_CONTROLLER_COUNT)[:4])[0]
        rows = []
        resized = []
        for i in range(count):
            self._send(i, REQUEST_CONTROLLER_DATA)
            name, leds, colors, zones = parse_controller_zones(self._reply(REQUEST_CONTROLLER_DATA))
            if size_headers:
                for z in empty_headers(zones):
                    self._send(i, RESIZE_ZONE, struct.pack("<ii", z, HEADER_GUESS))
                    resized.append(f"{name}: {zones[z][0]}")
            rows.append((name, leds, colors))
        if resized:
            self.resized = resized
            return self._query()
        return rows

    def _take(self, rows: list) -> None:
        """the device list from ``rows``. a device seen before keeps the
        colours it had before tide (now it shows tide's); a new one, or
        one whose size changed, is put in direct mode."""
        saved = dict(zip(self.devices, self._saved))
        before = dict(zip(self.devices, self.leds))
        self.devices = [name for name, _n, _c in rows]
        self.leds = [n for _name, n, _c in rows]
        self._saved = [saved.get(name, colors) for name, _n, colors in rows]
        # leds that appeared since (a header given a size) had no colour
        # of their own before tide: they go dark when tide lets go
        self._saved = [c + [(0, 0, 0)] * max(0, n - len(c))
                       for c, n in zip(self._saved, self.leds)]
        for i, (name, n, _c) in enumerate(rows):
            if n and before.get(name) != n:
                self._send(i, SET_CUSTOM_MODE)
        self.name = f"openrgb ({len(self.devices)})"
        self._checked = time.monotonic()

    def _news(self) -> bool:
        """read whatever the server sent on its own, without waiting.
        True if its device list changed (a zone resized in openrgb, a
        device plugged in)."""
        changed = False
        while self.sock is not None and select.select([self.sock], [], [], 0)[0]:
            magic, _dev, kind, size = HEADER.unpack(self._recv_exact(HEADER.size))
            if magic != MAGIC:
                raise OSError("that isn't an openrgb sdk server")
            if size:
                self._recv_exact(size)
            changed |= kind == DEVICE_LIST_UPDATED
        return changed

    # ---- what the show calls ----

    def connect(self) -> None:
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=2.0)
        except (ConnectionRefusedError, TimeoutError, socket.timeout):
            # the usual reason: openrgb isn't running, or its server is off
            # (2026-10-20: a bare "connection refused" said nothing useful)
            raise OSError("not running with its sdk server on. "
                          "start it with openrgb --server") from None
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._send(0, SET_CLIENT_NAME, b"tide\0")
        self.devices, self.leds, self._saved = [], [], []
        self._take(self._query(size_headers=self.size_headers))
        if not any(self.leds):
            raise OSError("openrgb has no lights to drive")

    def maintain(self) -> None:
        """before each frame: catch a changed device list, so resizing a
        zone in openrgb (an argb header that said 0 leds) lights up
        without restarting anything (2026-10-20)."""
        if self._news() or time.monotonic() - self._checked > REFRESH_S:
            self._take(self._query())

    def send(self, frames: list[list[tuple[int, int, int]]]) -> None:
        for i, colors in enumerate(frames):
            if colors:
                self._send(i, UPDATE_LEDS, leds_packet(colors))

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            for i, colors in enumerate(self._saved):
                if colors:
                    self._send(i, UPDATE_LEDS, leds_packet(colors))
        except OSError:
            pass
        self.sock.close()
        self.sock = None
