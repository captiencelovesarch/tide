"""Application bootstrap: ensure auth, wire up api + player + window."""
from __future__ import annotations

import argparse
import locale
import sys

# mpv requires LC_NUMERIC=C; set it before anything else can touch locale.
locale.setlocale(locale.LC_NUMERIC, "C")

from PySide6.QtWidgets import QApplication, QMessageBox

from . import auth, config, settings as settings_module, theming
from .api import Api
from .discord_rpc import DiscordPresence
from .mpris import MprisService
from .player import Player
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

    # Apply the theme as early as possible so the wizard renders with it.
    theming.manager().refresh()
    theming.manager().apply(args.theme or user_settings.theme or DEFAULT_THEME)

    yt = ensure_signed_in()
    if yt is None:
        return 1

    api_obj = Api(yt)
    player = Player()
    window = MainWindow(api_obj, player)
    window.show()

    # System integration: MPRIS2 (media keys + KDE/GNOME panel controls).
    mpris = MprisService(player, window.queue, window)
    if not mpris.start():
        print("tide: MPRIS2 registration failed (no session bus?)", file=sys.stderr)

    # Discord rich presence — opt-in, configured via settings dialog.
    discord = DiscordPresence(player, window.queue)
    discord.start_wire()
    discord.configure(user_settings.discord_app_id, user_settings.discord_enabled)
    # Expose so the (later) settings dialog can re-configure live.
    window._discord = discord
    window._settings = user_settings
    window.apply_initial_volume(user_settings.volume)

    rc = app.exec()
    discord.shutdown()
    mpris.stop()
    return rc
