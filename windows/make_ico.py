"""Pack assets/icon-*.png into windows/tide.ico for the exe and installer."""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SIZES = (16, 24, 32, 48, 64, 128, 256)

images = [Image.open(ROOT / "assets" / f"icon-{s}.png").convert("RGBA") for s in SIZES]
out = ROOT / "windows" / "tide.ico"
images[-1].save(out, format="ICO", sizes=[(s, s) for s in SIZES], append_images=images[:-1])
print("wrote", out)
