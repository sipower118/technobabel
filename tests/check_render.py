"""Verify rendered slides without eyes.

Absolute brightness is the wrong signal: every palette's accent sits between
grey 156-198, so a "count bright pixels" check false-alarms on correct renders
(e.g. yellow-on-graphite stat slides). Instead measure *ink*: pixels that
differ sharply from the local background, which is what text actually is.
"""

import sys
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat


import _paths  # noqa: F401  (sys.path + chdir bootstrap)
from accelerateddevops.render.theme import CANVAS_H, CANVAS_W, MARGIN, PALETTES, pick_palette, seed_for

d = Path("data/output/_smoke")
files = sorted(p for p in d.glob("*.png") if not p.name.startswith("_"))
print("files:", len(files))
assert files, "no slides rendered"


def ink_ratio(im):
    """Fraction of pixels that deviate strongly from the blurred background."""
    from PIL import ImageChops

    grey = im.convert("L")
    blurred = grey.filter(ImageFilter.GaussianBlur(6))
    diff = ImageChops.difference(grey, blurred)
    hist = diff.histogram()
    tot = sum(hist)
    return sum(hist[28:]) / tot, ImageStat.Stat(grey).stddev[0]


ok = True
for f in files:
    im = Image.open(f).convert("RGB")
    w, h = im.size
    size_ok = (w, h) == (CANVAS_W, CANVAS_H)
    ink, stdev = ink_ratio(im)

    problems = []
    if not size_ok:
        problems.append(f"size {w}x{h}")
    if stdev < 10:
        problems.append(f"flat background (stdev {stdev:.1f})")
    if ink < 0.02:
        problems.append(f"ink {ink:.3f} - text is missing or invisible")
    if ink > 0.30:
        problems.append(f"ink {ink:.3f} - frame is too busy to read")

    if problems:
        ok = False
    print("%s %-24s %dx%d ink=%.3f stdev=%5.1f  %s"
          % ("FAIL" if problems else "OK  ", f.name, w, h, ink, stdev, "; ".join(problems)))

names = {pick_palette(seed_for(x, "u")).name for x in ("a", "b", "c")}
print("\ndistinct palettes across 3 posts:", len(names), sorted(names), "| pool:", len(PALETTES))
assert len(names) >= 2, "palette not varying - feed will look monotonous"

print("\nRENDER CHECKS:", "PASSED" if ok else "FAILED")
sys.exit(0 if ok else 1)
