"""C&G #270 v9.0 (Shaun by mail 20 Nov 05:30 CET: the VVC logo in place of the old icon, no other brand anywhere).
The logo in its own colours on a white rounded tile: VVC blue #253A95 is too dark to read on the player's navy ground,
and the attachment itself is the logo on white. Writes the page icon and the boot splash icon.
  python3 make_vvc_icon.py   (from this folder; Pillow)"""
from pathlib import Path
from PIL import Image, ImageChops, ImageDraw

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
src = Image.open(HERE / "vvc-icon-from-shaun.jpg").convert("RGB")
# the logo's box: everything clearly darker than the white ground
diff = ImageChops.difference(src, Image.new("RGB", src.size, "white")).convert("L").point(lambda v: 255 if v > 40 else 0)
logo = src.crop(diff.getbbox())
S = 1024                      # work size; scaled down at the end for a clean edge
pad = 0.17
inner = int(S * (1 - 2 * pad))
w, h = logo.size
k = inner / max(w, h)
logo = logo.resize((round(w * k), round(h * k)), Image.LANCZOS)
tile = Image.new("RGB", (S, S), "white")
tile.paste(logo, ((S - logo.width) // 2, (S - logo.height) // 2))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=int(S * 0.22), fill=255)
icon = tile.convert("RGBA")
icon.putalpha(mask)
out = {ROOT / "app/static/brand/vvc-icon.png": 384,      # the page (dp-mark, op-mark): shown at most ~120 px
       ROOT / "pizero/splash/icon.png": 120}              # Plymouth: 11vh of 1080, as the page draws it
for path, px in out.items():
    icon.resize((px, px), Image.LANCZOS).save(path, optimize=True)
    print("wrote", path.relative_to(ROOT), f"{px}x{px}")
