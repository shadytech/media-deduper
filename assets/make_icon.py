"""Draws the app icon: two overlapping photos (a copy behind the original) under a
magnifying glass. Writes assets/app.ico (16-256 px) and assets/app.png (256 px).

Run:  .venv\\Scripts\\python.exe assets\\make_icon.py

Each size is drawn separately (4x supersampled), and sizes up to 24 px use a
simpler drawing with thicker lines so the icon stays readable in Explorer.
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
SS = 4  # supersampling

BACK = (124, 92, 246)       # violet card (the copy)
FRAME = (255, 255, 255)
SKY_TOP, SKY_BOT = (56, 189, 248), (186, 230, 253)
SUN = (253, 224, 71)
HILL_FAR, HILL_NEAR = (45, 160, 140), (22, 120, 96)
LENS_RING = (255, 255, 255)
OUTLINE = (17, 24, 39)
HANDLE = (245, 158, 11)


def _card(size: int, box, radius: float, border: float, scene: bool, back_fill=None) -> Image.Image:
    """One rounded photo card (white frame) as an RGBA layer."""
    layer = Image.new("RGBA", (size, size))
    d = ImageDraw.Draw(layer)
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, radius, fill=OUTLINE)                                  # thin dark edge
    e = max(1.0, size / 256 * 3)
    d.rounded_rectangle((x0 + e, y0 + e, x1 - e, y1 - e), radius - e, fill=FRAME)   # white frame
    inner = (x0 + border, y0 + border, x1 - border, y1 - border)
    ir = max(0.0, radius - border)
    if back_fill is not None:
        d.rounded_rectangle(inner, ir, fill=back_fill)
        return layer
    # photo: sky gradient, sun, two hills, clipped to the inner rounded rect
    w, h = int(inner[2] - inner[0]), int(inner[3] - inner[1])
    photo = Image.new("RGB", (w, h))
    pd = ImageDraw.Draw(photo)
    for yy in range(h):
        t = yy / max(1, h - 1)
        pd.line([(0, yy), (w, yy)], fill=tuple(int(a + (b - a) * t) for a, b in zip(SKY_TOP, SKY_BOT)))
    r = h * 0.17
    sx, sy = w * 0.72, h * 0.30
    pd.ellipse((sx - r, sy - r, sx + r, sy + r), fill=SUN)
    if scene:
        pd.polygon([(-w * 0.1, h), (w * 0.35, h * 0.38), (w * 0.85, h)], fill=HILL_FAR)
        pd.polygon([(w * 0.25, h), (w * 0.68, h * 0.55), (w * 1.15, h)], fill=HILL_NEAR)
    else:  # tiny sizes: one simple hill
        pd.polygon([(-w * 0.1, h), (w * 0.45, h * 0.40), (w * 1.1, h)], fill=HILL_NEAR)
    mask = Image.new("L", (w, h))
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), ir, fill=255)
    layer.paste(photo, (int(inner[0]), int(inner[1])), mask)
    return layer


def draw(px: int) -> Image.Image:
    size = px * SS
    u = size / 256              # 1 design unit
    small = px <= 24
    img = Image.new("RGBA", (size, size))

    border = (16 if small else 11) * u
    radius = (30 if small else 24) * u
    if small:   # bigger cards, smaller lens
        back_box = (10 * u, 18 * u, 176 * u, 150 * u)
        front_box = (52 * u, 62 * u, 236 * u, 206 * u)
    else:
        back_box = (14 * u, 22 * u, 172 * u, 144 * u)
        front_box = (52 * u, 64 * u, 226 * u, 198 * u)
    img.alpha_composite(_card(size, back_box, radius, border, False, back_fill=BACK))
    front = _card(size, front_box, radius, border, scene=not small)
    img.alpha_composite(front)

    # magnifying glass, bottom right
    cx, cy, r = (178 * u, 176 * u, 50 * u) if not small else (170 * u, 166 * u, 40 * u)
    ring = (17 if not small else 24) * u
    hw = (26 if not small else 40) * u
    edge = max(SS, 4 * u)
    a0 = (cx + r * 0.62, cy + r * 0.62)
    end = min(cx + r * 1.4, size - hw / 2 - edge - 2 * u)   # keep the rounded tip on the canvas
    a1 = (end, end)

    # lens content: the photo magnified 1.6x (sampled from inside the photo, so the
    # glass never shows the background through it), on white
    zoom = 1.6
    sx, sy = (front_box[0] + front_box[2]) / 2 + 8 * u, (front_box[1] + front_box[3]) / 2 + 6 * u
    sr = r / zoom
    crop = front.crop((int(sx - sr), int(sy - sr), int(sx + sr), int(sy + sr)))
    lens_px = int(2 * r)
    glass = Image.new("RGBA", (lens_px, lens_px), FRAME + (255,))
    glass.alpha_composite(crop.resize((lens_px, lens_px), Image.Resampling.LANCZOS))
    glass.alpha_composite(Image.new("RGBA", (lens_px, lens_px), (255, 255, 255, 45)))  # glassy tint
    lmask = Image.new("L", (lens_px, lens_px))
    ImageDraw.Draw(lmask).ellipse((0, 0, lens_px - 1, lens_px - 1), fill=255)
    img.paste(glass, (int(cx - r), int(cy - r)), lmask)

    d = ImageDraw.Draw(img)
    d.line([a0, a1], fill=OUTLINE, width=int(hw + 2 * edge))
    for p in (a1,):
        d.ellipse((p[0] - (hw / 2 + edge), p[1] - (hw / 2 + edge), p[0] + hw / 2 + edge, p[1] + hw / 2 + edge), fill=OUTLINE)
    d.line([a0, a1], fill=HANDLE, width=int(hw))
    d.ellipse((a1[0] - hw / 2, a1[1] - hw / 2, a1[0] + hw / 2, a1[1] + hw / 2), fill=HANDLE)
    # ring: white with a dark outline
    ro = r + ring / 2
    d.ellipse((cx - ro - edge, cy - ro - edge, cx + ro + edge, cy + ro + edge), outline=OUTLINE, width=int(ring + 2 * edge))
    d.ellipse((cx - ro, cy - ro, cx + ro, cy + ro), outline=LENS_RING, width=int(ring))
    if not small:  # glint
        g = r * 0.55
        d.arc((cx - g, cy - g, cx + g, cy + g), 200, 260, fill=(255, 255, 255), width=int(7 * u))
    return img.resize((px, px), Image.Resampling.LANCZOS)


def main():
    images = {px: draw(px) for px in SIZES}
    images[256].save(os.path.join(HERE, "app.png"))
    largest = images[256]
    largest.save(os.path.join(HERE, "app.ico"), format="ICO", sizes=[(p, p) for p in SIZES],
                 append_images=[images[p] for p in SIZES if p != 256])
    # preview sheet (not committed): every size on light and dark backgrounds
    sheet = Image.new("RGBA", (sum(SIZES) + 12 * len(SIZES) + 12, 2 * 256 + 36), (240, 240, 240, 255))
    ImageDraw.Draw(sheet).rectangle((0, 256 + 24, sheet.width, sheet.height), fill=(32, 32, 32, 255))
    x = 12
    for p in SIZES:
        sheet.alpha_composite(images[p], (x, 12))
        sheet.alpha_composite(images[p], (x, 256 + 24 + 6))
        x += p + 12
    sheet.save(os.path.join(HERE, "_preview.png"))
    print("wrote app.ico and app.png")


if __name__ == "__main__":
    main()
