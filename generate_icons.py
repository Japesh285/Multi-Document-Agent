"""
generate_icons.py — Produce the icon set Tauri's bundler requires.

Renders a clean "SA" monogram on a dark rounded-rect background so the
build doesn't fail looking for icons. Replace with your own artwork
later by running:  npx @tauri-apps/cli icon path\\to\\source.png
"""

from __future__ import annotations
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT      = Path(__file__).parent
ICON_DIR  = ROOT / "desktop" / "src-tauri" / "icons"
ICON_DIR.mkdir(parents=True, exist_ok=True)

BG       = (12, 14, 19, 255)         # ink-900
BORDER   = (59, 108, 255, 255)       # accent-600
LETTERS  = (122, 162, 255, 255)      # accent-400
TEXT     = "SA"


def _font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        r"C:\Windows\Fonts\seguisb.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\consolab.ttf",
    ]
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(size: int) -> Image.Image:
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    radius = int(size * 0.22)
    pad    = max(1, int(size * 0.04))
    draw.rounded_rectangle(
        (pad, pad, size - pad, size - pad),
        radius=radius, fill=BG,
        outline=BORDER, width=max(1, int(size * 0.03)),
    )

    font = _font(int(size * 0.42))
    bbox = draw.textbbox((0, 0), TEXT, font=font, anchor="lt")
    tw   = bbox[2] - bbox[0]
    th   = bbox[3] - bbox[1]
    x    = (size - tw) // 2 - bbox[0]
    y    = (size - th) // 2 - bbox[1]
    draw.text((x, y), TEXT, font=font, fill=LETTERS)
    return img


def save_png(size: int, name: str) -> Path:
    p = ICON_DIR / name
    render(size).save(p, "PNG")
    print(f"  wrote {p.name}  ({size}x{size})")
    return p


def save_ico() -> Path:
    p = ICON_DIR / "icon.ico"
    # ICO multi-resolution
    base = render(256)
    base.save(p, format="ICO", sizes=[(16, 16), (24, 24), (32, 32),
                                       (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"  wrote {p.name}  (multi-res ICO)")
    return p


def save_icns() -> Path:
    # We don't need .icns on Windows builds, but Tauri's config still
    # references it. Write a tiny placeholder PNG renamed to .icns —
    # the Windows build ignores it.
    p = ICON_DIR / "icon.icns"
    render(512).save(p, "PNG")
    print(f"  wrote {p.name}  (placeholder)")
    return p


def main():
    print(f"Generating icons in {ICON_DIR}")
    save_png(32,  "32x32.png")
    save_png(128, "128x128.png")
    save_png(256, "128x128@2x.png")     # @2x is 2× the base — 128 → 256
    save_png(512, "icon.png")
    save_ico()
    save_icns()
    print("Done.")


if __name__ == "__main__":
    main()
