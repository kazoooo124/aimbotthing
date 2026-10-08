"""Draws examples/ocean_depth.png - a simple depth diagram (needs: pip install pillow)."""
import os
from PIL import Image, ImageDraw, ImageFont

W, H = 1440, 1080
TOP, BOTTOM, MAXD = 150, 1040, 11000          # pixel rows for 0 m and 11,000 m
img = Image.new("RGB", (W, H))
px = img.load()
for y in range(H):
    if y < TOP:
        c = (176, 205, 228)
    else:
        k = min(1.0, (y - TOP) / (BOTTOM - TOP))
        c = (int(30 * (1 - k) ** 2 + 2), int(120 * (1 - k) ** 1.6 + 6), int(170 * (1 - k) ** 1.3 + 14))
    for x in range(W):
        px[x, y] = c
d = ImageDraw.Draw(img)


def font(size, bold=True):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf",
              "/System/Library/Fonts/Supplemental/Arial Bold.ttf"):
        if os.path.isfile(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def ypos(m):
    return TOP + m / MAXD * (BOTTOM - TOP)


d.line([(0, TOP), (W, TOP)], fill=(240, 250, 255), width=4)               # sea surface
d.polygon([(560, TOP - 6), (900, TOP - 6), (850, TOP - 46), (610, TOP - 46)], fill=(40, 48, 60))   # ship icon
d.rectangle([690, TOP - 86, 760, TOP - 46], fill=(60, 70, 85))
for m in range(0, 11001, 1000):                                           # depth ruler
    y = ypos(m)
    d.line([(60, y), (110, y)], fill=(255, 255, 255), width=3)
    d.text((124, y - 20), f"{m:,} m", font=font(42), fill=(235, 245, 255))
d.line([(60, TOP), (60, BOTTOM)], fill=(255, 255, 255), width=3)

yt, yc = ypos(3800), ypos(10900)
for x, y, line1, line2, col, left in ((893, yt, "Titanic wreck", "3,800 m", (255, 214, 0), False),
                                      (1150, yc, "Challenger Deep", "~10,900 m", (0, 230, 255), True)):
    d.line([(x, TOP), (x, y)], fill=(255, 255, 255), width=3)             # drop line from the surface
    d.ellipse([x - 20, y - 20, x + 20, y + 20], fill=col, outline=(0, 0, 0), width=4)
    f1, f2 = font(62), font(54)
    w1 = d.textlength(line1, font=f1)
    tx = (x - 40 - w1) if left else (x + 40)
    up = 150 if left else 0                                         # keep the bottom label inside the picture
    d.text((tx, y - 74 - up), line1, font=f1, fill=col)
    d.text((tx, y - 6 - up), line2, font=f2, fill=(255, 255, 255))
img.save(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocean_depth.png"))
print("wrote ocean_depth.png")
