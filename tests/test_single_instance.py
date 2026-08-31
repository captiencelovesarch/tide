"""Single-instance guard over QLocalServer — real sockets, no mocks:
second acquire loses, notify_running delivers command lines to
on_message, a crashed instance's stale socket file is taken over, and
the server name tracks tide.config at call time so a redirected config
dir can't collide with the real one.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import os
import socket as pysocket
import sys
import time
import unittest

from PySide6.QtWidgets import QApplication

from tide import config, instance


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _pump(check, timeout_ms=2000):
    """Process events until ``check()`` is truthy or the clock runs out."""
    app = _app()
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        if check():
            return True
        time.sleep(0.01)
    return bool(check())


class ServerNameTest(unittest.TestCase):
    def test_name_follows_config_dir_at_call_time(self):
        # conftest redirects tide.config into a sandbox; the name must be
        # derived from THAT dir, now — a name computed at import time
        # would collide with (and raise) the user's real running tide.
        before = instance._server_name()
        real = config.CONFIG_DIR
        config.CONFIG_DIR = real / "elsewhere"
        try:
            self.assertNotEqual(instance._server_name(), before)
        finally:
            config.CONFIG_DIR = real
        self.assertEqual(instance._server_name(), before)

    def test_name_is_a_safe_flat_token(self):
        # The name becomes a filename in a shared temp dir: no path
        # separators, no raw home-dir path leaking through.
        name = instance._server_name()
        self.assertNotIn("/", name)
        self.assertNotIn(str(config.CONFIG_DIR), name)
        self.assertTrue(name.startswith("tide-"))


class AcquireTest(unittest.TestCase):
    def setUp(self):
        _app()
        self._guards = []

    def tearDown(self):
        for g in self._guards:
            g.close()
        _app().processEvents()

    def _acquire(self, on_message=lambda line: None):
        guard = instance.acquire(on_message)
        if guard is not None:
            self._guards.append(guard)
        return guard

    def test_first_acquire_wins(self):
        guard = self._acquire()
        self.assertIsNotNone(guard)
        self.assertEqual(guard.name, instance._server_name())

    def test_second_acquire_yields_to_live_instance(self):
        first = self._acquire()
        self.assertIsNotNone(first)
        self.assertIsNone(self._acquire())

    def test_name_frees_after_close(self):
        first = self._acquire()
        self.assertIsNotNone(first)
        first.close()
        again = self._acquire()
        self.assertIsNotNone(again)

    def test_notify_running_delivers_command(self):
        got = []
        guard = self._acquire(got.append)
        self.assertIsNotNone(guard)
        self.assertTrue(instance.notify_running("raise"))
        self.assertTrue(_pump(lambda: got))
        self.assertEqual(got, ["raise"])

    def test_multiple_commands_arrive_in_order(self):
        got = []
        guard = self._acquire(got.append)
        self.assertIsNotNone(guard)
        for cmd in ("raise", "play-pause", "raise"):
            self.assertTrue(instance.notify_running(cmd))
        self.assertTrue(_pump(lambda: len(got) >= 3))
        self.assertEqual(got, ["raise", "play-pause", "raise"])

    def test_notify_without_listener_reports_failure(self):
        self.assertFalse(instance.notify_running("raise", timeout_ms=200))

    def test_unterminated_command_delivered_on_hangup(self):
        # A client that writes its line without "\n" and hangs up still
        # gets heard — the disconnect flushes the partial buffer.
        from PySide6.QtNetwork import QLocalSocket

        got = []
        guard = self._acquire(got.append)
        self.assertIsNotNone(guard)
        client = QLocalSocket()
        client.connectToServer(instance._server_name())
        self.assertTrue(client.waitForConnected(800))
        client.write(b"raise")
        client.waitForBytesWritten(800)
        client.flush()
        client.disconnectFromServer()
        client.abort()
        self.assertTrue(_pump(lambda: got))
        self.assertEqual(got, ["raise"])

    def test_close_with_live_connection_is_clean(self):
        # Closing the guard while a client is still connected must not
        # raise or crash — the retained socket is aborted, not leaked
        # into a destructor-time disconnected emission.
        from PySide6.QtNetwork import QLocalSocket

        got = []
        guard = self._acquire(got.append)
        self.assertIsNotNone(guard)
        client = QLocalSocket()
        client.connectToServer(instance._server_name())
        self.assertTrue(client.waitForConnected(800))
        client.write(b"half a li")
        client.waitForBytesWritten(800)
        _pump(lambda: False, timeout_ms=100)   # let the server accept+read
        guard.close()
        _app().processEvents()
        client.abort()
        _app().processEvents()
        self.assertEqual(got, [])   # never newline-terminated, never flushed

    def test_broken_handler_does_not_break_delivery(self):
        got = []

        def handler(line):
            got.append(line)
            raise RuntimeError("handler bug")

        guard = self._acquire(handler)
        self.assertIsNotNone(guard)
        self.assertTrue(instance.notify_running("one"))
        self.assertTrue(instance.notify_running("two"))
        self.assertTrue(_pump(lambda: len(got) >= 2))
        self.assertEqual(got, ["one", "two"])


class StaleSocketTest(unittest.TestCase):
    """A crashed instance leaves its socket file behind; listen fails on
    it exactly like on a live server, so acquire must probe and evict."""

    def setUp(self):
        _app()
        self._guards = []

    def tearDown(self):
        for g in self._guards:
            g.close()
        _app().processEvents()

    def test_stale_socket_is_taken_over(self):
        # Learn the real socket path from a live guard, then fake the
        # crash: bind a unix socket there ourselves and close it without
        # unlinking — the file persists with nobody listening, which is
        # exactly what SIGKILL leaves behind.
        probe = instance.acquire(lambda line: None)
        self.assertIsNotNone(probe)
        path = probe._server.fullServerName()
        self.assertTrue(path, "expected a filesystem-backed local socket")
        probe.close()
        self.assertFalse(os.path.exists(path))

        corpse = pysocket.socket(pysocket.AF_UNIX, pysocket.SOCK_STREAM)
        try:
            corpse.bind(path)
        finally:
            corpse.close()
        self.assertTrue(os.path.exists(path))

        got = []
        guard = instance.acquire(got.append)
        self.assertIsNotNone(guard, "acquire must evict a dead socket")
        self._guards.append(guard)
        self.assertTrue(instance.notify_running("raise"))
        self.assertTrue(_pump(lambda: got))
        self.assertEqual(got, ["raise"])


if __name__ == "__main__":
    unittest.main()
