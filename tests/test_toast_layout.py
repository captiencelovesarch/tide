"""Toast sizing regressions.

The auth-expiry toast shipped with its ✕ clipped to a dot: the button was
setFixedWidth(24) while the shared QPushButton rule applied `padding: 3px 10px`,
leaving 4px of content width for the glyph.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest

from PySide6.QtWidgets import QApplication, QWidget

from tide.ui.toast import Toast


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class ToastLayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self.host = QWidget()
        self.host.resize(1280, 800)
        self.host.show()

    def tearDown(self) -> None:
        self.host.close()

    def _toast(self, message: str, action: str | None = None) -> Toast:
        return Toast(
            self.host,
            message,
            action_label=action,
            on_action=(lambda: None) if action else None,
        )

    def test_dismiss_button_has_room_for_its_glyph(self) -> None:
        t = self._toast("youtube music: token expired", "refresh token")
        btn = t._dismiss_btn
        need = btn.fontMetrics().horizontalAdvance(btn.text())
        room = btn.width() - (btn.contentsMargins().left() + btn.contentsMargins().right())
        self.assertGreaterEqual(
            room, need, f"✕ clipped: {room}px of room for a {need}px glyph"
        )

    def test_dismiss_button_is_square(self) -> None:
        t = self._toast("hi")
        self.assertEqual(t._dismiss_btn.width(), t._dismiss_btn.height())

    def test_long_message_is_not_vertically_clipped(self) -> None:
        long = (
            "youtube music: session expired — the imported cookies no longer "
            "work, so search, library and home may come up empty or fail."
        )
        t = self._toast(long, "refresh token")
        label = t._label
        self.assertGreaterEqual(
            label.height(),
            label.heightForWidth(label.width()),
            "wrapped text taller than the label it renders in",
        )

    def test_stays_inside_its_parent(self) -> None:
        # pos() right after construction is the off-screen slide-in start;
        # the resting place is what has to fit.
        t = self._toast("youtube music: token expired", "refresh token")
        target = t._target_position()
        self.assertLessEqual(target.x() + t.width(), self.host.width())
        self.assertLessEqual(target.y() + t.height(), self.host.height())

    def test_action_toast_does_not_auto_dismiss(self) -> None:
        t = self._toast("token expired", "refresh token")
        self.assertEqual(t._lifetime_ms, 0)


if __name__ == "__main__":
    unittest.main()
