"""Generate the toolbar icon set.

Two outlines of the same shape, one grey where it is and one orange where it is
going, with an arrow between them - the fixed body, the moving body, and the
transform. Rendered at 8x and downsampled so it survives 16 px.

    py -3.10 tools/make_icons.py
"""
import math
import os

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'resources', 'GlobalRegistration')

FIXED_FILL = (206, 213, 221, 255)
FIXED_EDGE = (92, 104, 118, 255)
MOVING_EDGE = (255, 138, 0, 255)
ARROW = (44, 122, 168, 255)


def blob(cx, cy, r, rot_deg, squash=0.62):
    """A lumpy closed outline, so the two copies are visibly the same shape."""
    pts = []
    for i in range(48):
        a = 2.0 * math.pi * i / 48.0
        rr = r * (1.0 + 0.22 * math.cos(3.0 * a) + 0.10 * math.sin(5.0 * a))
        x, y = rr * math.cos(a), rr * squash * math.sin(a)
        th = math.radians(rot_deg)
        pts.append((cx + x * math.cos(th) - y * math.sin(th),
                    cy + x * math.sin(th) + y * math.cos(th)))
    return pts


def icon(size):
    S = size * 8
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = max(1, int(S * 0.026))

    # where it is now: outline only, tilted
    d.polygon(blob(S * 0.33, S * 0.34, S * 0.21, -35.0), outline=MOVING_EDGE,
              width=int(w * 1.3))
    # where it is going: filled, upright
    d.polygon(blob(S * 0.66, S * 0.69, S * 0.21, 0.0), fill=FIXED_FILL,
              outline=FIXED_EDGE, width=w)

    # the transform
    x0, y0 = S * 0.44, S * 0.46
    x1, y1 = S * 0.58, S * 0.58
    d.line([(x0, y0), (x1, y1)], fill=ARROW, width=w)
    h = S * 0.055
    d.polygon([(x1 + h * 0.35, y1 + h * 0.35), (x1 - h, y1 + h * 0.15),
               (x1 + h * 0.15, y1 - h)], fill=ARROW)

    return img.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    for s in (16, 32, 64):
        icon(s).save(os.path.join(OUT, '%dx%d.png' % (s, s)))
    print('wrote 16/32/64 px icons to %s' % OUT)


if __name__ == '__main__':
    main()
