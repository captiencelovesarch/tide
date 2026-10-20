"""The page's server: 127.0.0.1 only, read only, a thread per open page.

/          the page (page.html)
/events    server-sent events: "state" (everything, on connect and on
           change), "pulse" (the beat, ~30 a second), "lyric"
/art       the current cover

The gui thread calls ``publish``; each open page has a queue its own
thread drains, so a stuck page never holds up tide. Requests naming
another host are turned away (a web page elsewhere can't read what you're
playing by pointing a name at 127.0.0.1).
"""
from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
KEEPALIVE_S = 15.0
QUEUE_MAX = 240            # a page this far behind gets dropped, not waited on


class _Quiet(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        # a page closing mid-stream isn't news
        import sys
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)


class Server:
    def __init__(self, port: int) -> None:
        self.port = port
        self.page = (HERE / "page.html").read_bytes()
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self._state: dict = {}
        self._art: tuple[bytes, str] = (b"", "image/png")
        self.httpd = _Quiet(("127.0.0.1", port), self._handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="tide-overlay",
                                       daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        with self._lock:
            for q in self._clients:
                q.put(None)
            self._clients.clear()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2.0)

    # ---- from the gui thread ----

    def clients(self) -> int:
        with self._lock:
            return len(self._clients)

    def set_art(self, data: bytes, mime: str = "image/png") -> None:
        with self._lock:
            self._art = (data, mime)

    def publish(self, kind: str, data: dict) -> None:
        """send an event to every open page. "state" also becomes what
        a page gets when it connects."""
        if kind == "state":
            with self._lock:
                self._state = dict(data)
        msg = f"event: {kind}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()
        with self._lock:
            for q in list(self._clients):
                if q.qsize() >= QUEUE_MAX:
                    self._clients.remove(q)
                    q.put(None)
                else:
                    q.put(msg)

    # ---- the handler ----

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a) -> None:
                pass

            def _host_ok(self) -> bool:
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
                return host in ("127.0.0.1", "localhost", "::1")

            def _send(self, code: int, body: bytes, mime: str, extra: dict | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if not self._host_ok():
                    self._send(403, b"tide's overlay only answers on 127.0.0.1\n", "text/plain")
                    return
                path = urlsplit(self.path).path
                if path in ("/", "/index.html"):
                    self._send(200, server.page, "text/html; charset=utf-8")
                elif path == "/art":
                    with server._lock:
                        data, mime = server._art
                    if data:
                        self._send(200, data, mime)
                    else:
                        self._send(404, b"", "text/plain")
                elif path == "/events":
                    self._events()
                else:
                    self._send(404, b"not here\n", "text/plain")

            def _events(self) -> None:
                q: queue.Queue = queue.Queue()
                with server._lock:
                    first = dict(server._state)
                    server._clients.append(q)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                try:
                    self.wfile.write(b"retry: 2000\n\n")
                    self.wfile.write(f"event: state\ndata: {json.dumps(first)}\n\n".encode())
                    self.wfile.flush()
                    while True:
                        try:
                            msg = q.get(timeout=KEEPALIVE_S)
                        except queue.Empty:
                            msg = b": still here\n\n"
                        if msg is None:
                            return
                        self.wfile.write(msg)
                        self.wfile.flush()
                except OSError:
                    pass
                finally:
                    with server._lock:
                        if q in server._clients:
                            server._clients.remove(q)
                    self.close_connection = True

        return Handler
