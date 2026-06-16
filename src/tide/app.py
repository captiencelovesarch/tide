"""Application bootstrap: ensure auth, wire up api + player + window."""
from __future__ import annotations

import argparse
import locale
import sys

# mpv requires LC_NUMERIC=C; set it before anything else can touch locale.
locale.setlocale(locale.LC_NUMERIC, "C")

from PySide6.QtWidgets import QApplication, QMessageBox

from . import auth, cache, config, session as session_module, settings as settings_module, theming
from .api import Api
from .discord_rpc import DiscordPresence
from .mpris import MprisService
from .playback import MpvBackend, PlaybackRouter
from .player import Player
from .sources import registry as source_registry
from .sources.bandcamp import BandcampSource
from .sources.local import LocalSource
from .sources.mixcloud import MixcloudSource
from .sources.soundcloud import SoundCloudSource
from .sources.ytmusic import YTMusicSource
from .ui.window import MainWindow
from .ui.wizard import SignInDialog


DEFAULT_THEME = "brutalist-mono"


def ensure_signed_in():
    """Return a valid YTMusic client, prompting via GUI if needed."""
    if auth.have_auth():
        try:
            return auth.yt_client()
        except Exception:
            auth.clear_saved_auth()

    dlg = SignInDialog()
    if dlg.exec() != dlg.DialogCode.Accepted:
        return None
    try:
        return auth.yt_client()
    except Exception as exc:
        QMessageBox.critical(None, "tide", f"couldn't connect to youtube music:\n\n{exc}")
        return None


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tide", description="a brutalist youtube music client")
    parser.add_argument("--theme", help="theme slug to load (overrides saved preference)")
    parser.add_argument("--list-themes", action="store_true", help="print available themes and exit")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    config.ensure_dirs()
    # Trim art cache on startup so the directory doesn't grow forever.
    try:
        cache.prune_art_cache()
    except Exception:
        pass
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    if args.list_themes:
        # No Qt app needed for a listing.
        for t in theming.discover_themes().values():
            print(f"{t.slug}\t{t.name}")
        return 0

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("tide")
    app.setOrganizationName("tide")
    app.setDesktopFileName("tide")

    user_settings = settings_module.load()

    # Initialize the motion system once, before any UI is built. Reduced-
    # motion detection needs QGuiApplication.instance() (created above) to
    # be live; doing it here means every helper sees the right intensity
    # from the first frame.
    from .ui import motion as motion_module
    motion_module.initialize(user_settings.motion)

    # Lock in the UI scale BEFORE the theme applies. theming.apply() multi-
    # plies the typography size and the QSS @font_size token by scale.factor(),
    # so the first theme application has to see the correct scale or the
    # whole window renders at 1.0× and then snaps when settings are touched.
    from .ui import scale as scale_module
    scale_module.set_factor(user_settings.ui_scale)

    # Apply thumbnail override before any view paints.
    from .ui.track_row import set_thumbnail_override
    set_thumbnail_override(user_settings.show_thumbnails or "theme")

    # Register tide's bundled fonts so they're available regardless of
    # what's installed system-wide, and push the user's font override (if
    # any) into the theming manager BEFORE the first apply so the very
    # first frame uses the right family.
    theming.register_bundled_fonts()
    theming.manager().set_user_font(user_settings.font_family_override or "")

    # Apply the theme as early as possible so the wizard renders with it.
    theming.manager().refresh()
    theming.manager().apply(args.theme or user_settings.theme or DEFAULT_THEME)

    # Layout — pick + overrides. Applied before window construction so the
    # initial UI uses the right variants.
    from . import layout as layout_module
    layout_module.manager().refresh()
    layout_module.manager().apply(
        user_settings.layout or "classic",
        user_settings.layout_overrides or {},
    )

    yt = ensure_signed_in()
    if yt is None:
        return 1

    # ---------- source registry (v1.2 multi-source) ----------
    reg = source_registry()
    yt_source = YTMusicSource(yt)
    reg.register(yt_source, enabled=user_settings.sources_enabled.get("ytmusic", True))
    reg.register(SoundCloudSource(),
                 enabled=user_settings.sources_enabled.get("soundcloud", True))
    reg.register(BandcampSource(),
                 enabled=user_settings.sources_enabled.get("bandcamp", True))
    reg.register(MixcloudSource(),
                 enabled=user_settings.sources_enabled.get("mixcloud", False))
    local_dir = user_settings.local_music_dir or None
    local_source = LocalSource(music_dir=local_dir)
    reg.register(local_source,
                 enabled=user_settings.sources_enabled.get("local", True))
    reg.set_active(user_settings.active_source or "ytmusic")

    if user_settings.local_auto_index and reg.is_enabled("local"):
        # Index the music dir in the background so the UI stays responsive.
        from PySide6.QtCore import QThreadPool, QRunnable
        class _IndexJob(QRunnable):
            def run(self_inner):
                try:
                    local_source.rescan()
                    local_source.start_watcher()
                except Exception:
                    pass
        QThreadPool.globalInstance().start(_IndexJob())

    # The active source is what the rest of the UI binds to as "self.api".
    api_obj = reg.active or yt_source

    # ---------- playback router ----------
    router = PlaybackRouter()
    router.register(MpvBackend())
    # v1.2.1+ will append LibrespotBackend / MusicKitBackend here.
    player = router
    window = MainWindow(api_obj, player)

    # Restore last session (queue + paused at last position) before showing.
    saved_session = session_module.load()
    if saved_session is not None and saved_session.tracks:
        window.restore_session(saved_session)

    window.show()

    # System integration: MPRIS2 (media keys + KDE/GNOME panel controls).
    mpris = MprisService(player, window.queue, window)
    if not mpris.start():
        print("tide: MPRIS2 registration failed (no session bus?)", file=sys.stderr)

    # System tray (KDE/GNOME panel). Falls back silently if no tray host.
    from PySide6.QtWidgets import QSystemTrayIcon
    if QSystemTrayIcon.isSystemTrayAvailable():
        from .ui.tray import TideTray
        window._tray = TideTray(window, player, window.queue, parent=window)
    else:
        window._tray = None

    # Discord rich presence — opt-in, configured via settings dialog.
    discord = DiscordPresence(player, window.queue)
    discord.start_wire()
    discord.configure(user_settings.discord_app_id, user_settings.discord_enabled)

    # ListenBrainz scrobbling — opt-in via Settings → ListenBrainz.
    from .listenbrainz import ListenBrainzScrobbler
    scrobbler = ListenBrainzScrobbler(player, window.queue)
    scrobbler.configure(user_settings.listenbrainz_token, user_settings.listenbrainz_enabled)
    window._scrobbler = scrobbler

    # Adaptive accent driver — shifts theme accent toward album art.
    from .ui.adaptive import AdaptiveDriver
    adaptive = AdaptiveDriver(window.queue)
    adaptive.set_enabled(user_settings.adaptive_accent)
    # Also drive bg_alt extraction if the user wants the central-area
    # gradient. This is independent of the accent shift.
    adaptive.set_background_enabled(user_settings.adaptive_background)
    window._adaptive = adaptive

    # Apply user's central-area + corner preferences. CentralBg paints
    # gradient + clips corners; corner radius is also pushed as a sticky
    # theming override so widgets that use @radius (inputs, scrollbars)
    # match.
    from .ui.central_bg import corner_radius as _corner_radius
    window.central_bg.set_enabled(user_settings.adaptive_background)
    radius_px = _corner_radius(user_settings.corner_style)
    window.central_bg.set_radius(radius_px)
    if radius_px > 0:
        theming.manager().set_user_override("radius", f"{radius_px}px")
    # Nav-rail icons (per the user's nav_icon_set preference).
    window.apply_nav_icons(user_settings.nav_icon_set or "off")

    # Once-a-day update check.
    from PySide6.QtCore import QMetaObject, Qt, Q_ARG
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtCore import QUrl as _QUrl
    from . import __version__, updates
    from .ui.toast import show_toast

    def _on_newer(tag: str, url: str) -> None:
        # Cross-thread → marshal to GUI thread via QTimer.singleShot(0, ...).
        from PySide6.QtCore import QTimer as _QT
        def deliver():
            show_toast(
                window,
                f"new tide release: {tag}",
                action_label="view",
                on_action=lambda: QDesktopServices.openUrl(_QUrl(url)),
            )
        _QT.singleShot(0, deliver)
    try:
        updates.check_in_background(__version__, _on_newer)
    except Exception:
        pass
    # Expose so the (later) settings dialog can re-configure live.
    window._discord = discord
    window._settings = user_settings
    # The SourcePanel was constructed with a placeholder Settings; rebind to
    # the real one so toggles persist.
    try:
        window.source_view.bind_settings(user_settings)
    except Exception:
        pass
    window.apply_initial_volume(user_settings.volume)
    # Push persisted playback speed + pitch policy. set_pitch_correction
    # MUST come first so the speed change applies under the right filter
    # (toggling pitch-correction while speed != 1.0 can otherwise cause a
    # brief pitch-glitch on the next audio chunk).
    try:
        player.set_pitch_correction(bool(user_settings.preserve_pitch))
        player.set_speed(float(user_settings.playback_speed or 1.0))
        window.speed_btn.set_speed(float(user_settings.playback_speed or 1.0), emit=False)
    except Exception:
        pass

    rc = app.exec()
    # Best-effort teardown so threads + native handles close cleanly.
    try:
        window.visualizer_view.teardown()
    except Exception:
        pass
    try:
        if window._tray is not None:
            window._tray.teardown()
    except Exception:
        pass
    # Quiesce the prefetcher before the MainWindow (its parent) destructs —
    # otherwise an in-flight resolve thread gets torn down mid-network call
    # and segfaults.
    try:
        window._prefetch.shutdown()
    except Exception:
        pass
    discord.shutdown()
    mpris.stop()
    return rc
