"""Layout linter: detects text overflow / collisions the eye would catch.

We cannot eyeball 8 slides x 8 layouts on every render, so we assert geometry:
every text run must sit inside the safe box, and content must not collide with
the header or footer furniture.
"""

import sys

from PIL import Image, ImageDraw

import _paths  # noqa: F401  (sys.path + chdir bootstrap)
from accelerateddevops.models import Draft, Slide, SourceItem
from accelerateddevops.render import carousel as C
from accelerateddevops.render.theme import CANVAS_H, CANVAS_W, MARGIN, make_background, pick_palette, seed_for

SAFE_L = MARGIN - 40
SAFE_R = CANVAS_W - MARGIN + 40
HEADER_BOTTOM = 150
FOOTER_TOP = CANVAS_H - 150


def collect_boxes(layout, slide):
    """Monkeypatch draw calls to record the bounding boxes a layout produces.

    Only draws issued against the main canvas are recorded: paste_rounded
    builds masks on scratch images in panel-local coordinates, and recording
    those produces false 'left overflow' reports.
    """
    boxes = []
    holder = {}
    real_multiline = ImageDraw.ImageDraw.multiline_text
    real_text = ImageDraw.ImageDraw.text
    real_rect = ImageDraw.ImageDraw.rectangle
    real_ellipse = ImageDraw.ImageDraw.ellipse
    real_round = ImageDraw.ImageDraw.rounded_rectangle
    real_paste = Image.Image.paste

    def rec_multiline(self, xy, text, font=None, fill=None, spacing=4, **kw):
        w = C.text_height(self, text, font, spacing)
        ww = C.text_width(self, C.widest(text), font)
        boxes.append(("text", xy[0], xy[1], xy[0] + ww, xy[1] + w, text[:34]))
        return real_multiline(self, xy, text, font=font, fill=fill, spacing=spacing, **kw)

    # ImageDraw.text(xy, text, fill=None, font=None, ...): font is args[1]
    # positionally, or the "font" kwarg.
    def rec_text(self, xy, text, *args, **kw):
        font = kw.get("font")
        if font is None and len(args) >= 2:
            font = args[1]
        ww = C.text_width(self, text, font)
        hh = C.text_height(self, text, font, 0)
        boxes.append(("text", xy[0], xy[1], xy[0] + ww, xy[1] + hh, text[:34]))
        return real_text(self, xy, text, *args, **kw)

    def rec_round(self, box, radius=0, **kw):
        if self is holder.get("draw"):
            boxes.append(("shape", box[0], box[1], box[2], box[3], "roundrect"))
        return real_round(self, box, radius=radius, **kw)

    def rec_rect(self, box, **kw):
        if self is holder.get("draw"):
            boxes.append(("shape", box[0], box[1], box[2], box[3], "rect"))
        return real_rect(self, box, **kw)

    ImageDraw.ImageDraw.multiline_text = rec_multiline
    ImageDraw.ImageDraw.text = rec_text
    ImageDraw.ImageDraw.rounded_rectangle = rec_round
    ImageDraw.ImageDraw.rectangle = rec_rect
    try:
        fn = C.LAYOUTS[layout]
        img = make_background(pick_palette(1), 1234, variant=0)
        canvas = C.Canvas(image=img, draw=ImageDraw.Draw(img))
        holder["draw"] = canvas.draw
        # content-box furniture is part of the chrome, not the content
        boxes.clear()
        fn(canvas, slide, pick_palette(1), C.HEADER_H, CANVAS_H - C.FOOTER_H)
    finally:
        ImageDraw.ImageDraw.multiline_text = real_multiline
        ImageDraw.ImageDraw.text = real_text
        ImageDraw.ImageDraw.rounded_rectangle = real_round
        ImageDraw.ImageDraw.rectangle = real_rect
    return boxes


CASES = [
    ("cover", Slide(1, "cover", "Your CI is slow because nobody owns it",
                    "A 40-minute pipeline is an ownership problem, not a hardware problem.")),
    ("cover-long", Slide(1, "cover",
        "We rebuilt our entire multi-cluster deployment pipeline from scratch and the results surprised everybody involved",
        "A 40-minute pipeline is an ownership problem, not a hardware problem, and here is exactly how we proved it.")),
    ("cover-tiny", Slide(1, "cover", "AI", "Short.")),
    ("stat", Slide(2, "stat", "38% of build time was queue wait",
                   "Not compilation.\nNot test execution.\nWaiting for a runner nobody provisioned.")),
    ("stat-long", Slide(2, "stat", "Queue time dominated our build for two years straight",
                        "1247 minutes of it.\nAcross 312 builds.\nPer quarter, for eight quarters running.")),
    ("bullets", Slide(3, "bullets", "Split ownership three ways",
                      "Platform owns runners and images | Service teams own test runtime budgets | CI owns the feedback loop")),
    ("bullets-long", Slide(3, "bullets",
        "We eventually split ownership three separate ways across four different teams",
        "Platform engineering owns the runners and also owns the container image build cache and the base image rebuilds | Service teams own their own test runtime budgets and get paged when they blow through them | The CI team owns the developer feedback loop and the merge queue latency target and nothing else")),
    ("contrast", Slide(4, "contrast", "What teams try versus what works",
        "Bigger runners: throws money at queueing || Capacity on demand: fixes the actual constraint ||\nDefault timeouts: hides a slow suite || Delete flaky tests first: makes the signal real")),
    ("contrast-long", Slide(4, "contrast",
        "What most teams try versus what actually works in practice at scale",
        "Bigger runners and more memory: throws money at the queueing problem || Capacity on demand: fixes the actual underlying constraint ||\nShorter default timeouts across the board: hides an already slow test suite || Delete the flaky tests first: makes the remaining signal trustworthy")),
    ("steps", Slide(5, "steps", "The order that actually works",
                    "Measure the wait, not the build | Autoscaling from that measurement | Per-team budgets with alerts | Delete the slow tests you never read")),
    ("steps-long", Slide(5, "steps", "The order that actually worked for us at a very large scale",
                    "Measure the queue wait first, not the build time | Enable cluster autoscaling driven by that specific measurement | Give every team an explicit budget with alerts attached to it | Delete the slow tests that nobody actually reads before shipping anything else")),
    ("quote", Slide(6, "quote", "A build nobody owns is not slow. It is unmanaged.",
                    "The most common finding in any pipeline review we have done.")),
    ("quote-long", Slide(6, "quote",
        "A build that nobody owns is not slow, it is completely unmanaged, and that distinction matters enormously",
        "The single most common finding in every pipeline review we have ever run.")),
    ("takeaway", Slide(7, "takeaway", "Measure queue time first",
                       "It is almost always larger than build time, and it is usually cheaper to fix.")),
    ("cta", Slide(8, "cta", "What is the slowest step in your pipeline?",
                  "Tell us below. We read every one.", "Save this for your next build review")),
]

failures = 0
for name, slide in CASES:
    layout = "cover" if name.startswith("cover") else name.rsplit("-", 1)[0] if name.endswith(("long", "tiny")) else name
    boxes = collect_boxes(layout, slide)
    problems = []
    for kind, x0, y0, x1, y1, label in boxes:
        if x0 < SAFE_L:
            problems.append(f"left overflow {x0} < {SAFE_L} :: {label!r}")
        if x1 > SAFE_R:
            problems.append(f"right overflow {x1} > {SAFE_R} :: {label!r}")
        if y1 > FOOTER_TOP:
            problems.append(f"collides with footer: bottom={y1} > {FOOTER_TOP} :: {label!r}")
        if y0 < HEADER_BOTTOM - 10 and label != "roundrect":
            problems.append(f"collides with header: top={y0} :: {label!r}")
    status = "OK " if not problems else "FAIL"
    if problems:
        failures += 1
    print(f"{status} {name:<14} boxes={len(boxes):>2}  " + ("; ".join(problems[:3]) if problems else ""))

print("\n%d/%d layouts clean" % (len(CASES) - failures, len(CASES)))
