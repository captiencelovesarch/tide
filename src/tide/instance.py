"""Single-instance guard: one tide per config dir, over a QLocalServer.

A second launch shouldn't spawn a second player fighting over mpris, the
tray, and the audio-capture stream — it should tell the running instance
to raise itself and exit. The handshake is a QLocalServer (a unix socket
on Linux) whose name is derived from the *config dir*, so every user —
and every test sandbox, which redirects tide.config elsewhere — gets its
own name. The name is computed at call time, never at import time: the
test conftest repoints tide.config before anything runs, and a
module-level constant here would leak the real config path into tests.

Stale sockets are taken over, not obeyed: a crashed instance leaves its
socket file behind, and ``listen`` fails on it exactly like it does on a
live one. So on a failed listen we probe-connect — if somebody answers,
they're real and ``acquire`` yields to them; if nobody does, the file is
a corpse, ``removeServer`` unlinks it and we retry.

Wire protocol: newline-terminated utf-8 command lines ("raise\\n").
Deliberately dumb — the second process writes one line and exits, the
first process's ``on_message`` decides what a command means.

app.py owns the wiring (acquire at startup, notify_running + exit when
acquire returns None); this module knows nothing about windows.
"""
from __future__ import annotations

import hashlib
from typing import Callable

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def _server_name() -> str:
    """Per-user, per-config-dir server name.

    Reads ``tide.config.CONFIG_DIR`` through the module attribute *now* so
    a redirected config (tests, portable setups) changes the name too. The
    path is hashed rather than embedded: local-socket names end up as
    filenames in a shared temp dir and shouldn't carry arbitrary path
    characters.
    """
    from tide import config

    digest = hashlib.sha256(str(config.CONFIG_DIR).encode("utf-8")).hexdigest()
    return f"tide-{digest[:16]}"


class InstanceGuard(QObject):
    """Holds the listening server and forwards received command lines.

    Keep the returned guard referenced for the life of the app — it owns
    the QLocalServer (child object) and the callback. ``close()`` releases
    the name so another instance can take it.
    """

    def __init__(self, server: QLocalServer, on_message: Callable[[str], None]):
        super().__init__()
        self._server = server
        server.setParent(self)
        self._on_message = on_message
        # Partial lines per connection: a client's write can arrive split
        # across readyRead bursts; only complete lines are delivered.
        self._partial: dict[QLocalSocket, bytes] = {}
        # Strong refs to live server-side sockets — the qthreads-style
        # retain pattern. NEVER swap this for disconnected→deleteLater:
        # under PySide6 + py3.14 that segfaults inside the deferred
        # ~QObject (reproduced offscreen — Python also owns the wrapper
        # from nextPendingConnection, so the C++ socket dies twice).
        # Explicit ownership + dropping the ref deletes it exactly once,
        # on the GUI thread.
        self._conns: set[QLocalSocket] = set()
        server.newConnection.connect(self._accept)

    @property
    def name(self) -> str:
        return self._server.serverName()

    def close(self) -> None:
        """Stop listening and free the socket name. Safe to call twice."""
        # Abort retained connections before dropping them: freeing a
        # still-open socket would emit disconnected from inside its own
        # C++ destructor, landing in _drop with an already-invalidated
        # wrapper (seen live as a shiboken RuntimeError at exit). Signals
        # are blocked around the abort — it emits disconnected
        # synchronously, and close() means stop, not "flush half-received
        # junk through _drop".
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
        # A client that wrote its command without a trailing newline and
        # hung up still gets heard: drain whatever the socket buffered,
        # then flush the partial line.
        try:
            if sock.bytesAvailable() > 0:
                self._read(sock)
        except RuntimeError:
            # Wrapper already invalidated — a guard freed without close()
            # reaches here from the socket's own destructor at teardown.
            # The buffered partial below is all that's left to flush.
            pass
        rest = self._partial.pop(sock, b"")
        if rest.strip():
            self._deliver(rest)
        # No deleteLater (segfault — see __init__). Dropping the retained
        # ref lets Python free the socket on the GUI thread once the
        # signal emission unwinds.
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
    """Claim the single-instance name, or return None if a live instance
    already owns it.

    ``on_message`` receives each command line another process sends via
    :func:`notify_running`. Requires a running Q(Core)Application for
    delivery (signals), but is safe to call before any window exists.
    """
    name = _server_name()
    server = QLocalServer()
    if not server.listen(name):
        # Name taken: either a live instance or a stale socket file left
        # by a crash. Only a probe can tell them apart. The unclaimed
        # server wrapper is Python-owned; letting it fall out of scope
        # frees it here on the GUI thread (no deleteLater — see
        # InstanceGuard.__init__ for the crash that pattern causes).
        if _probe_alive(name):
            return None
        QLocalServer.removeServer(name)
        if not server.listen(name):
            # Lost a takeover race, or the temp dir is hostile — either
            # way somebody/something else holds the name; yield.
            return None
    return InstanceGuard(server, on_message)


def notify_running(command: str = "raise", timeout_ms: int = 800) -> bool:
    """Send one command line to the live instance. True if delivered.

    Blocking waits only — this runs in the doomed second process before
    (or instead of) any event loop.
    """
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
