"""Single-instance guard: one tide per config dir, over a QLocalServer.

The server name hashes the config dir, computed at call time, never at
import — the test conftest repoints tide.config before anything runs.
``listen`` fails the same way on a live socket and on a stale file left
by a crash, so on failure we probe-connect: an answer means yield, no
answer means removeServer and retry.

Wire protocol: newline-terminated utf-8 command lines ("raise\\n"); a
second launch writes one line and exits, the first process's
``on_message`` decides what it means. app.py owns the wiring; this
module knows nothing about windows.
"""
from __future__ import annotations

import hashlib
from typing import Callable

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def _server_name() -> str:
    """Reads ``tide.config.CONFIG_DIR`` through the module attribute
    *now* so a redirected config (tests) changes the name. Hashed, not
    embedded — socket names become filenames in a shared temp dir."""
    from tide import config

    digest = hashlib.sha256(str(config.CONFIG_DIR).encode("utf-8")).hexdigest()
    return f"tide-{digest[:16]}"


class InstanceGuard(QObject):
    """Holds the listening server and forwards received command lines.
    Keep the guard referenced for the life of the app; ``close()``
    releases the name so another instance can take it."""

    def __init__(self, server: QLocalServer, on_message: Callable[[str], None]):
        super().__init__()
        self._server = server
        server.setParent(self)
        self._on_message = on_message
        # a write can arrive split across readyRead bursts; only complete
        # lines are delivered
        self._partial: dict[QLocalSocket, bytes] = {}
        # Strong refs to live server-side sockets (qthreads-style retain).
        # NEVER swap for disconnected→deleteLater: under PySide6 + py3.14
        # that segfaults in the deferred ~QObject (reproduced offscreen —
        # Python also owns the wrapper from nextPendingConnection, so the
        # C++ socket dies twice). Dropping the ref frees it exactly once,
        # on the GUI thread.
        self._conns: set[QLocalSocket] = set()
        server.newConnection.connect(self._accept)

    @property
    def name(self) -> str:
        return self._server.serverName()

    def close(self) -> None:
        """Stop listening and free the socket name. Safe to call twice."""
        # Abort retained conns before dropping them — freeing a still-open
        # socket emits disconnected from its own C++ destructor, landing
        # in _drop with an invalidated wrapper (shiboken RuntimeError at
        # exit). Signals blocked: abort emits disconnected synchronously,
        # and close() means stop, not flush.
        conns = list(self._conns)
        self._conns.clear()
        self._partial.clear()
        for sock in conns:
            try:
                sock.blockSignals(True)
                sock.abort()
            except RuntimeError:
                pass
        name = self._server.serverName()
        self._server.close()
        if name:
            QLocalServer.removeServer(name)

    # ---------- receiving ----------

    def _accept(self) -> None:
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            if sock is None:
                break
            # Take the socket out of the server's C++ parentage so Python
            # is the one owner (see _conns above for why this matters).
            sock.setParent(None)
            self._conns.add(sock)
            self._partial[sock] = b""
            sock.readyRead.connect(lambda s=sock: self._read(s))
            sock.disconnected.connect(lambda s=sock: self._drop(s))

    def _read(self, sock: QLocalSocket) -> None:
        data = self._partial.get(sock, b"") + bytes(sock.readAll().data())
        *lines, rest = data.split(b"\n")
        self._partial[sock] = rest
        for line in lines:
            self._deliver(line)

    def _drop(self, sock: QLocalSocket) -> None:
        # a client that hung up without a trailing newline still gets
        # heard: drain the buffer, then flush the partial line
        try:
            if sock.bytesAvailable() > 0:
                self._read(sock)
        except RuntimeError:
            # wrapper invalidated — a guard freed without close() reaches
            # here from the socket's destructor; flush the partial below
            pass
        rest = self._partial.pop(sock, b"")
        if rest.strip():
            self._deliver(rest)
        # no deleteLater (segfault — see __init__); dropping the ref frees
        # the socket on the GUI thread once the emission unwinds
        self._conns.discard(sock)

    def _deliver(self, raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            return
        try:
            self._on_message(line)
        except Exception:
            # A broken handler must not kill the socket loop.
            pass


def acquire(on_message: Callable[[str], None]) -> InstanceGuard | None:
    """Claim the single-instance name, or None when a live instance owns
    it. ``on_message`` receives lines sent via :func:`notify_running`.
    Needs a running Q(Core)Application for signal delivery, no window."""
    name = _server_name()
    server = QLocalServer()
    if not server.listen(name):
        # Name taken: live instance or stale file — only a probe can
        # tell. The unclaimed wrapper is Python-owned; scope exit frees
        # it on the GUI thread (no deleteLater — see __init__'s crash).
        if _probe_alive(name):
            return None
        QLocalServer.removeServer(name)
        if not server.listen(name):
            # lost a takeover race, or the temp dir is hostile — yield
            return None
    return InstanceGuard(server, on_message)


def notify_running(command: str = "raise", timeout_ms: int = 800) -> bool:
    """Send one command line to the live instance. True if delivered.
    Blocking waits only — this runs in the doomed second process before
    any event loop."""
    sock = QLocalSocket()
    sock.connectToServer(_server_name())
    if not sock.waitForConnected(timeout_ms):
        sock.abort()
        return False
    payload = (command.rstrip("\n") + "\n").encode("utf-8")
    sock.write(payload)
    ok = sock.waitForBytesWritten(timeout_ms)
    sock.flush()
    sock.disconnectFromServer()
    if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        sock.waitForDisconnected(timeout_ms)
    sock.abort()
    return bool(ok)


def _probe_alive(name: str, timeout_ms: int = 400) -> bool:
    """True if something actually accepts connections on ``name``."""
    sock = QLocalSocket()
    sock.connectToServer(name)
    alive = sock.waitForConnected(timeout_ms)
    sock.abort()
    return bool(alive)
