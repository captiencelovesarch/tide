"""Re-render the launcher icon (assets/icon.svg + every assets/icon-*.png)
from ui/app_icon.py's classic drawing. Run after changing the drawing:

    QT_QPA_PLATFORM=offscreen PYTHONPATH=src python tools/render_icons.py
"""
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from tide.ui import app_icon

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 192, 256, 512)


def main() -> None:
    QApplication.instance() or QApplication(sys.argv[:1])
    svg = app_icon.classic_svg()
    (ASSETS / "icon.svg").write_text(svg, encoding="utf-8")
    for size in SIZES:
        app_icon.render(svg, size).save(str(ASSETS / f"icon-{size}.png"))
    app_icon.render(svg, 1024).save(str(ASSETS / "icon.png"))
    print(f"wrote {len(SIZES) + 2} files to {ASSETS}")


if __name__ == "__main__":
    main()
