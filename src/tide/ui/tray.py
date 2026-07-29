"""System tray icon — KDE Plasma / GNOME / waybar StatusNotifier protocol.

Tooltip shows current track. Menu has now-playing label, play/pause, next,
previous, show/hide window, quit. Left-click toggles window visibility.

Tide can run hidden in tray after the user closes the window. Quitting
through the tray menu (or File menu equivalent) actually exits the app.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


class TideTray(QObject):
    """Wraps QSystemTrayIcon + Qt menu. Owns no playback state of its own."""

    def __init__(self, window, player, queue, parent=None) -> None:
        super().__init__(parent)
        self.window = window
        self.player = player
        self.queue = queue
        self._tray = QSystemTrayIcon(self._best_icon(), parent or window)
        self._tray.setToolTip("tide")
        self._tray.activated.connect(self._on_activated)
        self._menu = QMenu()
        self._build_menu()
        self._tray.setContextMenu(self._menu)
        self._tray.show()

        # Update label + tooltip whenever state or track changes.
        from ..player import PlayState
        queue.current_changed.connect(self._on_current_changed)
        player.state_changed.connect(self._on_state_changed)

    # ---------- icon ----------

    def _best_icon(self) -> QIcon:
        # Prefer the installed hicolor icon; fall back to the bundled asset path.
        from PySide6.QtGui import QIcon
        icon = QIcon.fromTheme("tide")
        if not icon.isNull():
            return icon
        # Last resort: app icon
        return QIcon()

    # ---------- menu ----------

    def _build_menu(self) -> None:
        self._menu.clear()
        self.now_action = QAction("nothing playing", self._menu)
        self.now_action.setEnabled(False)
        self._menu.addAction(self.now_action)
        self._menu.addSeparator()

        self.play_action = QAction("play / pause", self._menu)
        self.play_action.triggered.connect(self.player.toggle)
        self.next_action = QAction("next", self._menu)
        self.next_action.triggered.connect(lambda: self.window._on_next_clicked())
        self.prev_action = QAction("previous", self._menu)
        self.prev_action.triggered.connect(lambda: self.window._on_prev_clicked())
        for a in (self.play_action, self.prev_action, self.next_action):
            self._menu.addAction(a)
        self._menu.addSeparator()

        self.show_action = QAction("show / hide window", self._menu)
        self.show_action.triggered.connect(self._toggle_window)
        self._menu.addAction(self.show_action)

        self._menu.addSeparator()
        quit_action = QAction("quit tide", self._menu)
        quit_action.triggered.connect(self._real_quit)
        self._menu.addAction(quit_action)

    # ---------- activation ----------

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_window()

    def _toggle_window(self) -> None:
        # "The window" is whichever one is active — the mini player stands in
        # for the main window while mini mode is on.
        target = getattr(self.window, "active_app_window", lambda: self.window)()
        if target.isVisible() and not target.isMinimized():
            target.hide()
        elif hasattr(self.window, "present_active"):
            self.window.present_active()
        else:
            self.window.showNormal()
            self.window.raise_()
            self.window.activateWindow()

    def _real_quit(self) -> None:
        # Mark a flag so MainWindow.closeEvent actually closes (otherwise we
        # hide-to-tray instead).
        try:
            self.window._wants_quit = True
        except Exception:
            pass
        self.window.close()
        QApplication.instance().quit()

    # ---------- state ----------

    def _on_current_changed(self, track) -> None:
        if track is None:
            self.now_action.setText("nothing playing")
            self._tray.setToolTip("tide")
            return
        from .. import theming
        title = theming.styled_case(track.title or "")
        artists = theming.styled_case(track.artists or "")
        label = f"{artists} — {title}" if artists and title else (title or artists)
        # Truncate for menu / tooltip.
        if len(label) > 72:
            label = label[:69] + "…"
        self.now_action.setText(label)
        self._tray.setToolTip(f"tide · {label}")

    def _on_state_changed(self, state) -> None:
        from ..player import PlayState
        # Tooltip suffix for state.
        cur_tip = self._tray.toolTip()
        head = cur_tip.split(" · ", 1)[0] if " · " in cur_tip else "tide"
        if state == PlayState.PLAYING:
            self._tray.setToolTip(f"{head} · playing")
        elif state == PlayState.PAUSED:
            self._tray.setToolTip(f"{head} · paused")
        elif state == PlayState.LOADING:
            self._tray.setToolTip(f"{head} · loading")
        else:
            self._tray.setToolTip(head)

    def teardown(self) -> None:
        try:
            self._tray.hide()
        except Exception:
            pass
