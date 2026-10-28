"""
add_rig.py - turn a side-on photo of your rig into an overlay "edition".

Usage:   python add_rig.py "photo.png" "Edition Name"
   (or use "Add edition..." in the app's Overlay tab, which runs this for you)

Creates rigs/<Edition Name>/ with:
   rig.png    the rig with the background removed
   glass.png  mask of the glass (lit up while heating / in a session)
   base.png   mask of the light band at the bottom of the base
   knob.png   the carb-cap knob cut out on its own, so it can rock during a session
   rig.json   where the smoke, glow and bubbles go - fractions of the image size,
              edit them if something lands in the wrong spot

Works best with a straight-on side shot on a plain white background, framed like a
product photo (glass on the left, heater cap on the right).
Needs: pip install pillow numpy
"""

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).parent
RIGS = HERE / "rigs"

# Layout of a side-on shot, as fractions of the rig's bounding box.
GLASS_LINE = ((0.024, 0.660), (0.611, 0.556))   # bottom edge of the glass (left point, right point)
GLASS_MAX_X = 0.611                              # glass sits left of the heater cap
BASE_LIGHT_Y = 0.962                             # light band at the bottom of the base
GLASS_OPACITY = 0.85                             # how solid the glass stays over the stream


def color_to_alpha_white(rgb, floor=0.0):
    """'Color to alpha' against white: white turns transparent, tinted pixels keep
    their color and look identical when laid back over white."""
    rgb = rgb.astype(np.float32)
    alpha = ((255.0 - rgb) / 255.0).max(axis=2)
    alpha = np.clip(np.maximum(alpha, floor), 1e-4, 1.0)
    out = 255.0 - (255.0 - rgb) / alpha[..., None]
    return np.clip(out, 0, 255), alpha


def find_knob(red, obj, x_lo, x_hi, y_lo, y_hi):
    """The knob is a red disc on a short stem sitting on top of the cap body.
    Walk down from the first red row: disc -> narrow stem/gap -> wide cap body."""
    def span(y):
        xs = np.where(red[y, x_lo:x_hi])[0]
        return (x_lo + xs.min(), x_lo + xs.max(), len(xs)) if len(xs) > 8 else None
    top = next((y for y in range(y_lo, y_hi) if span(y)), None)
    if top is None:
        return None
    disc = [span(y) for y in range(top, min(y_hi, top + 18)) if span(y)]
    disc_w = max(b - a for a, b, _ in disc)
    dx0, dx1 = min(a for a, _, _ in disc), max(b for _, b, _ in disc)
    gap, disc_end, body, in_disc = False, None, None, False
    for y in range(top, y_hi):
        sp = span(y)
        narrow = sp is None or (sp[1] - sp[0]) < 0.6 * disc_w
        if not in_disc:                      # skip the rounded top edge of the disc
            in_disc = sp is not None and (sp[1] - sp[0]) >= 0.8 * disc_w
            continue
        if narrow and not gap:
            gap, disc_end = True, y
        elif gap and not narrow and ((sp[1] - sp[0]) > 1.12 * disc_w or sp[2] > 0.9 * disc_w):
            body = y
            break
    if body is None or disc_end is None or body - top > 3 * disc_w:
        return None
    cx = (dx0 + dx1) / 2
    mask = np.zeros_like(obj)
    for y in range(max(0, top - 2), disc_end + 1):          # the disc, filled row by row
        sp = span(y) if y >= top else None
        a, b = (sp[0], sp[1]) if sp else (dx0 + 4, dx1 - 4)
        mask[y, max(0, a - 1):b + 2] = True
    sw = 0.26 * disc_w                                      # the stem under it
    mask[disc_end:body, int(cx - sw):int(cx + sw) + 1] = True
    mask &= obj
    return mask, (dx0 - 3, top - 3, dx1 + 4, body + 1), (cx, body)


def fill_hole(color, alpha, hole, iters=400):
    """Fill what was behind the knob by diffusing in the surrounding pixels."""
    ys, xs = np.where(hole)
    y0, y1, x0, x1 = max(0, ys.min() - 8), ys.max() + 9, max(0, xs.min() - 8), xs.max() + 9
    win = hole[y0:y1, x0:x1]
    a = alpha[y0:y1, x0:x1].copy()
    pm = color[y0:y1, x0:x1] * a[..., None]                 # premultiplied, so transparent areas don't bleed colour
    img = np.dstack([pm, a])
    img[win] = 0
    for _ in range(iters):
        avg = (np.roll(img, 1, 0) + np.roll(img, -1, 0) + np.roll(img, 1, 1) + np.roll(img, -1, 1)) / 4
        img[win] = avg[win]
    na = img[..., 3]
    color[y0:y1, x0:x1][win] = (img[..., :3] / np.maximum(na, 1e-4)[..., None]).clip(0, 255)[win]
    alpha[y0:y1, x0:x1][win] = na[win].clip(0, 1)


def process(src, name, accent="#ff8a9a"):
    im = Image.open(src).convert("RGB")
    w, h = im.size

    # background = white area connected to the border
    probe = im.copy()
    for s in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
              (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]:
        if sum(probe.getpixel(s)) >= 735:
            ImageDraw.floodfill(probe, s, (1, 2, 3), thresh=30)
    bg = np.all(np.asarray(probe) == (1, 2, 3), axis=2)
    obj = ~bg
    if obj.sum() < 1000:
        raise ValueError("Couldn't find the rig - use a photo on a plain white background.")

    ys, xs = np.where(obj)
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    bw, bh = max(1, x1 - x0), max(1, y1 - y0)
    X, Y = np.meshgrid(np.arange(w), np.arange(h))
    rx, ry = (X - x0) / bw, (Y - y0) / bh

    arr = np.asarray(im)
    ai = arr.astype(int)
    R, G, B = ai[..., 0], ai[..., 1], ai[..., 2]
    sat = ai.max(axis=2) - ai.min(axis=2)
    base = obj & (ry > BASE_LIGHT_Y - 0.03) & (sat < 60)

    # front-on shots are symmetric; side shots aren't
    crop = obj[y0:y1 + 1, x0:x1 + 1]
    view = "front" if (crop & crop[:, ::-1]).sum() / max(1, (crop | crop[:, ::-1]).sum()) > 0.85 else "side"
    if view == "side":
        (ax, ay), (bx, by) = GLASS_LINE
        glass = obj & (ry < ay + (rx - ax) * (by - ay) / (bx - ax)) & (rx < GLASS_MAX_X)
    else:
        # glass = everything above the base, minus the heater cap sitting in front of it
        red = (R - G > 70) & (R - B > 55) & (G < 140)
        cx = (x0 + x1) / 2
        # where the base starts: look just beside the cap, where the glass meets the base
        start = y0 + int(0.45 * bh)
        tops = []
        for x in range(x0, x1 + 1):
            if 0.21 * bw < abs(x - cx) < 0.30 * bw:
                col = np.where(red[start:y1, x])[0]
                if len(col):
                    tops.append(start + col[0])
        yc = int(np.median(tops)) if tops else int(start + 0.1 * bh)
        line = np.full(w, float(yc))
        for x in range(x0, x1 + 1):              # follow the base edge's gentle curve
            col = np.where(red[yc - 25:yc + 25, x])[0]
            if len(col) and abs(x - cx) > 0.21 * bw:
                line[x] = yc - 25 + col[0]
        row = np.where(obj[max(y0, yc - 6)])[0]  # the glass is only as wide as it is at the base
        gl0, gl1 = (row.min(), row.max()) if len(row) else (x0, x1)
        line[:gl0] = 0
        line[gl1 + 1:] = 0
        cap = np.zeros_like(obj)
        gold = (R > 140) & (B < 120) & (R - B > 70)
        for y in range(y0 + int(0.3 * bh), int(line[int(cx)]) + 1):
            xs_ = np.where((red[y] | (gold[y] & (np.abs(np.arange(w) - cx) < 0.21 * bw))) &
                           (np.abs(np.arange(w) - cx) < 0.24 * bw))[0]
            if len(xs_):
                cap[y, xs_.min():xs_.max() + 1] = True
        glass = obj & (Y < line[None, :] - 2) & ~cap

    # alpha: solid body, slightly see-through glass, no white halo on the edges
    alpha = obj.astype(np.float32)
    color = arr.astype(np.float32)
    fringe = (np.asarray(Image.fromarray((bg * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(5))) > 0) & obj
    c_glass, a_glass = color_to_alpha_white(arr, floor=GLASS_OPACITY)
    c_edge, a_edge = color_to_alpha_white(arr)
    color[glass], alpha[glass] = c_glass[glass], a_glass[glass]
    edge = fringe & ~glass
    color[edge] = c_edge[edge]
    alpha[edge] = np.minimum(alpha[edge], a_edge[edge] * 1.6).clip(0, 1)
    alpha[bg] = 0

    # carb-cap knob: cut it out as its own layer and fill in behind it
    red_all = (R - G > 70) & (R - B > 55) & (G < 140)
    if view == "front":
        kx0, kx1 = int((x0 + x1) / 2 - 0.24 * bw), int((x0 + x1) / 2 + 0.24 * bw)
    else:
        kx0, kx1 = int(x0 + 0.6 * bw), x1 + 1
    knob = find_knob(red_all, obj, kx0, kx1, y0 + int(0.25 * bh), y0 + int(0.7 * bh))
    knob_layer = None
    if knob:
        kmask, kbox, kpivot = knob
        soft = np.asarray(Image.fromarray((kmask * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.7))) / 255.0
        knob_layer = np.dstack([color, alpha * soft * 255]).astype(np.uint8)
        hole = np.asarray(Image.fromarray((kmask * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(5))) > 0
        fill_hole(color, alpha, hole & obj)

    m = int(0.04 * max(bw, bh))
    cx0, cy0, cx1, cy1 = max(0, x0 - m), max(0, y0 - m), min(w, x1 + m + 1), min(h, y1 + m + 1)
    cw, ch = cx1 - cx0, cy1 - cy0

    out = RIGS / name
    out.mkdir(parents=True, exist_ok=True)
    rgba = np.dstack([color, alpha * 255]).astype(np.uint8)[cy0:cy1, cx0:cx1]
    Image.fromarray(rgba, "RGBA").save(out / "rig.png", optimize=True)
    for fname, mask, blur in (("glass.png", glass, 1.5), ("base.png", base, 2)):
        mk = Image.fromarray((mask[cy0:cy1, cx0:cx1] * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(blur))
        Image.merge("LA", (Image.new("L", mk.size, 255), mk)).save(out / fname, optimize=True)

    top = np.where(obj[y0:y0 + max(3, bh // 100)])
    mouth_x = (top[1].min() + top[1].max()) / 2
    gy, gx = np.where(glass)
    low = gy > np.percentile(gy, 70)
    by_, bx_ = np.where(base)
    f = lambda v, n: round(float(v) / n, 4)
    meta = {
        "name": name, "view": view, "width": int(cw), "height": int(ch), "accent": accent,
        "mouth": [f(mouth_x - cx0, cw), f(y0 + 2 - cy0, ch)],
        "glow": [f(gx[low].mean() - cx0, cw), f(gy.max() - cy0, ch)],
        "bubbles": [f(np.percentile(gx[low], 15) - cx0, cw), f(np.percentile(gy[low], 5) - cy0, ch),
                    f(np.percentile(gx[low], 85) - cx0, cw), f(gy.max() - 14 - cy0, ch)],
        "base": [f((bx_.min() if len(bx_) else x0) - cx0, cw), f((by_.min() if len(by_) else y1 - 20) - cy0, ch),
                 f((bx_.max() if len(bx_) else x1) - cx0, cw), f(y1 - cy0, ch)],
    }
    if knob_layer is not None:
        bx0_, by0_, bx1_, by1_ = kbox
        Image.fromarray(knob_layer[by0_:by1_, bx0_:bx1_], "RGBA").save(out / "knob.png", optimize=True)
        meta["knob"] = {"box": [f(bx0_ - cx0, cw), f(by0_ - cy0, ch), f(bx1_ - cx0, cw), f(by1_ - cy0, ch)],
                        "pivot": [f(kpivot[0] - cx0, cw), f(kpivot[1] - cy0, ch)]}
    elif (out / "knob.png").exists():
        (out / "knob.png").unlink()
    (out / "rig.json").write_text(json.dumps(meta, indent=2))
    return out, view


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    path, view = process(sys.argv[1], sys.argv[2])
    print(f"Saved {view} view to {path}")
