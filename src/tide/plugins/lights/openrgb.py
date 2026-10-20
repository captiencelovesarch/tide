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

import socket
import struct

PORT = 6742
MAGIC = b"ORGB"
HEADER = struct.Struct("<4sIII")

REQUEST_CONTROLLER_COUNT = 0
REQUEST_CONTROLLER_DATA = 1
SET_CLIENT_NAME = 50
DEVICE_LIST_UPDATED = 100
UPDATE_LEDS = 1050
SET_CUSTOM_MODE = 1100


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
    zones = r.take("<H")
    zone_leds = 0
    for _ in range(zones):
        r.string()
        r.take("<i")                # zone type
        r.take("<I")
        r.take("<I")                # leds min, max
        zone_leds += r.take("<I")
        r.skip(r.take("<H"))        # matrix map, if any
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
    return name, count, colors


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

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=2.0)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._send(0, SET_CLIENT_NAME, b"tide\0")
        self._send(0, REQUEST_CONTROLLER_COUNT)
        count = struct.unpack("<I", self._reply(REQUEST_CONTROLLER_COUNT)[:4])[0]
        self.devices, self.leds, self._saved = [], [], []
        for i in range(count):
            self._send(i, REQUEST_CONTROLLER_DATA)
            name, leds, colors = parse_controller(self._reply(REQUEST_CONTROLLER_DATA))
            self.devices.append(name)
            self.leds.append(leds)
            self._saved.append(colors)
        if not any(self.leds):
            raise OSError("openrgb has no lights to drive")
        for i, n in enumerate(self.leds):
            if n:
                self._send(i, SET_CUSTOM_MODE)
        self.name = f"openrgb ({len(self.devices)})"

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
