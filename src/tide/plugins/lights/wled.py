"""WLED strips, over its realtime udp protocol (port 21324).

A packet takes the strip over for ``TIMEOUT_S`` seconds; when tide stops
sending, the strip goes back to its own effect by itself. The LED count
comes from the strip's json api once, at connect.

https://kno.wled.ge/interfaces/udp-realtime/
"""
from __future__ import annotations

import json
import socket
import urllib.request

PORT = 21324
DRGB, DNRGB = 2, 4
DRGB_MAX = 490             # leds in one plain packet
DNRGB_CHUNK = 489          # leds per packet past that
TIMEOUT_S = 2


def parse_address(text: str) -> tuple[str, int, int]:
    """"host", "host:udp" → (host, udp port, http port)."""
    text = text.strip()
    host, _, port = text.rpartition(":") if text.count(":") == 1 else (text, "", "")
    return (host or text), int(port) if port.isdigit() else PORT, 80


def packets(colors: list[tuple[int, int, int]], timeout: int = TIMEOUT_S) -> list[bytes]:
    """the udp packets for one frame."""
    if len(colors) <= DRGB_MAX:
        return [bytes([DRGB, timeout]) + bytes(c for rgb in colors for c in rgb)]
    out = []
    for start in range(0, len(colors), DNRGB_CHUNK):
        chunk = colors[start:start + DNRGB_CHUNK]
        out.append(bytes([DNRGB, timeout, start >> 8, start & 0xFF])
                   + bytes(c for rgb in chunk for c in rgb))
    return out


class Strip:
    kind = "wled"

    def __init__(self, address: str) -> None:
        self.address = address
        self.host, self.port, self.http_port = parse_address(address)
        self.name = self.host
        self.leds: list[int] = []        # one entry per device: its led count
        self.sock: socket.socket | None = None

    def connect(self) -> None:
        url = f"http://{self.host}:{self.http_port}/json/info"
        with urllib.request.urlopen(url, timeout=2.0) as resp:
            info = json.loads(resp.read().decode("utf-8", "replace"))
        count = int(((info.get("leds") or {}).get("count")) or 0)
        if count <= 0:
            raise OSError(f"{self.host} reported no leds")
        self.name = str(info.get("name") or self.host)
        self.leds = [count]
        addr = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_DGRAM)[0]
        self.sock = socket.socket(addr[0], socket.SOCK_DGRAM)
        self.sock.connect(addr[4])

    def send(self, frames: list[list[tuple[int, int, int]]]) -> None:
        if self.sock is None:
            return
        for pkt in packets(frames[0]):
            self.sock.send(pkt)

    def close(self) -> None:
        # nothing to undo: the strip takes its own effect back once the
        # realtime timeout runs out
        if self.sock is not None:
            self.sock.close()
            self.sock = None
