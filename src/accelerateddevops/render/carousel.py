"""Carousel rendering: layout engine, text fitting, and the eight slide layouts."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from ..models import Draft, Slide
from . import fonts
from .theme import (
    CANVAS_H,
    CANVAS_W,
    MARGIN,
    Palette,
    make_background,
    pick_palette,
    seed_for,
)

log = logging.getLogger(__name__)

# The renderer's own box, inside the canvas margin.
CONTENT_W = CANVAS_W - 2 * MARGIN
# Room taken by the persistent header and footer furniture.
HEADER_H = 150
FOOTER_H = 150

VALID_LAYOUTS = {
    "cover", "bullets", "stat", "contrast",
    "steps", "quote", "takeaway", "cta",
}


@dataclass
class Canvas:
    """Image + draw handle, so layouts can composite as well as draw text.

    Panels need the image (for alpha compositing) and text needs the draw
    object. Bundling both keeps layout signatures honest and removes the
    `draw._image` attribute-stuffing that would otherwise be needed.
    """

    image: Image.Image
    draw: ImageDraw.ImageDraw

    def paste_rounded(
        self,
        box: tuple[int, int, int, int],
        *,
        fill: tuple[int, int, int],
        radius: int = 22,
        alpha: int = 255,
        outline: tuple[int, int, int] | None = None,
        width: int = 1,
    ) -> None:
        """Alpha-composite a rounded rectangle - used for the contrast panels."""
        x0, y0, x1, y1 = box
        w, h = max(1, x1 - x0), max(1, y1 - y0)

        panel = Image.new("RGBA", (w, h), fill + (alpha,))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, w - 1, h - 1], radius=radius, fill=255
        )
        panel.putalpha(Image.composite(panel.getchannel("A"), Image.new("L", (w, h), 0), mask))
        self.image.paste(panel, (x0, y0), panel)


# ── text measurement helpers ─────────────────────────────────────────────


def text_width(draw: ImageDraw.ImageDraw, text: str, font: object) -> int:
    if not text:
        return 0
    box = draw.textbbox((0, 0), text, font=font)
    return int(box[2] - box[0])


def text_height(draw: ImageDraw.ImageDraw, text: str, font: object, line_spacing: float) -> int:
    if not text:
        return 0
    box = draw.multiline_textbbox((0, 0), text, font=font, spacing=int(line_spacing))
    return int(box[3] - box[1])


def wrap_to_width(
    draw: ImageDraw.ImageDraw, text: str, font: object, max_width: int
) -> str:
    """Greedy wrap that respects explicit newlines and breaks long words."""
    if not text:
        return ""
    out_lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            out_lines.append("")
            continue
        line = words[0]
        for word in words[1:]:
            trial = f"{line} {word}"
            if text_width(draw, trial, font) <= max_width:
                line = trial
            else:
                out_lines.append(line)
                line = word
        out_lines.append(line)

    # Hard-break any single word still too wide for the column.
    final: list[str] = []
    for line in out_lines:
        while text_width(draw, line, font) > max_width and len(line) > 1:
            cut = len(line) - 1
            while cut > 1 and text_width(draw, line[:cut], font) > max_width:
                cut -= 1
            final.append(line[:cut])
            line = line[cut:]
        final.append(line)
    return "\n".join(final)


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    role: str,
    max_width: int,
    max_height: int,
    start: int,
    minimum: int,
    line_spacing_ratio: float = 0.30,
) -> tuple[object, str, int, int]:
    """Shrink a font until the wrapped text fits the box.

    Returns (font, wrapped_text, width, height). Auto-fitting is what stops a
    long headline from overflowing or being truncated mid-word - the two things
    that most make a generated slide look cheap.
    """
    size = start
    while size >= minimum:
        font = fonts.load(role, size)
        wrapped = wrap_to_width(draw, text, font, max_width)
        width = text_width(draw, widest(wrapped), font)
        height = text_height(draw, wrapped, font, size * line_spacing_ratio)
        if width <= max_width and height <= max_height:
            return font, wrapped, width, height
        size -= max(2, size // 14)
    font = fonts.load(role, minimum)
    wrapped = wrap_to_width(draw, text, font, max_width)
    return font, wrapped, text_width(draw, widest(wrapped), font), text_height(
        draw, wrapped, font, minimum * line_spacing_ratio
    )


def widest(text: str) -> str:
    return max(text.split("\n"), key=len, default="")


def draw_block(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    font: object,
    fill: tuple[int, int, int],
    line_spacing_ratio: float = 0.30,
    max_lines: int | None = None,
) -> int:
    """Draw wrapped text at (x, y); returns the height consumed."""
    spacing = int(getattr(font, "size", 16) * line_spacing_ratio)
    lines = text.split("\n")
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        if lines:
            lines[-1] = lines[-1].rstrip(" .,") + "\u2026"
    body = "\n".join(lines)
    draw.multiline_text((x, y), body, font=font, fill=fill, spacing=spacing)
    return text_height(draw, body, font, spacing)


# ── furniture ────────────────────────────────────────────────────────────


def _draw_header(
    draw: ImageDraw.ImageDraw,
    palette: Palette,
    handle: str,
    index: int,
    total: int,
) -> None:
    """Brand handle, thin accent rule, and slide counter."""
    font = fonts.load("body_bold", 30)
    draw.text((MARGIN, MARGIN - 6), handle.upper(), font=font, fill=palette.muted)

    # Counter, right-aligned.
    counter = f"{index:02d} / {total:02d}"
    cfont = fonts.load("mono", 30)
    width = text_width(draw, counter, cfont)
    draw.text((CANVAS_W - MARGIN - width, MARGIN - 6), counter, font=cfont, fill=palette.muted)

    # Accent underline, short and sharp - a designer's margin note.
    draw.rectangle([MARGIN, MARGIN + 52, MARGIN + 96, MARGIN + 56], fill=palette.accent)


def _draw_footer(
    draw: ImageDraw.ImageDraw,
    palette: Palette,
    tagline: str,
    footer_label: str,
) -> None:
    """Progress dots plus the tagline / slide label."""
    y = CANVAS_H - MARGIN - 40

    if footer_label:
        font = fonts.load("mono", 26)
        draw.text((MARGIN, y), footer_label.upper(), font=font, fill=palette.accent)

    tag_font = fonts.load("body", 26)
    tag_w = text_width(draw, tagline, tag_font)
    draw.text(
        (CANVAS_W - MARGIN - tag_w, y),
        tagline,
        font=tag_font,
        fill=palette.muted,
    )


# ── layouts ──────────────────────────────────────────────────────────────


def _layout_cover(
    canvas: "Canvas", slide: Slide, palette: Palette, top: int, bottom: int
) -> None:
    """Big statement. Deliberately sparse - the job is to stop the scroll."""
    draw = canvas.draw
    available = bottom - top - 40
    font, wrapped, _, _ = fit_text(
        draw, slide.headline, "display", CONTENT_W, int(available * 0.72), 108, 52
    )
    y = top + 40

    # Accent bar to the left of the headline block.
    head_h = text_height(draw, wrapped, font, 108 * 0.26)
    draw.rectangle([MARGIN - 30, y + 6, MARGIN - 22, y + head_h], fill=palette.accent)
    draw.multiline_text((MARGIN, y), wrapped, font=font, fill=palette.text, spacing=27)
    y += head_h + 54

    if slide.body:
        bfont, bwrapped, _, _ = fit_text(
            draw, slide.body, "body", CONTENT_W - 20, 260, 44, 30
        )
        draw_block(draw, MARGIN, y, bwrapped, bfont, palette.muted)


def _layout_bullets(
    canvas: "Canvas", slide: Slide, palette: Palette, top: int, bottom: int
) -> None:
    """Headline plus a scannable list - the workhorse layout."""
    draw = canvas.draw
    hfont, headline, _, head_h = fit_text(
        draw, slide.headline, "display", CONTENT_W - 30, 300, 74, 42
    )
    draw_block(draw, MARGIN, top, headline, hfont, palette.text)
    y = top + head_h + 46

    items = _split_items(slide.body)
    if not items:
        return

    # Shrink to fit rather than truncate: a clipped final bullet reads as a bug.
    while len(items) > 3 and not _bullets_fit(draw, items, y, bottom, palette):
        items = items[:3]
    if not _bullets_fit(draw, items, y, bottom, palette):
        items = items[:2]

    item_font, line_spacing = _bullet_font(draw, items, y, bottom)
    body_h = _bullets_height(draw, items, item_font, line_spacing)
    y = top + max(0, (bottom - top - body_h) // 2) + 30

    for item in items:
        wrapped = wrap_to_width(draw, item, item_font, CONTENT_W - 70)
        h = text_height(draw, wrapped, item_font, line_spacing)
        # Accent square marker.
        draw.rounded_rectangle(
            [MARGIN, y + 12, MARGIN + 18, y + 30], radius=5, fill=palette.accent
        )
        draw_block(draw, MARGIN + 46, y, wrapped, item_font, palette.text, max_lines=3)
        y += h + 34


def _bullets_fit(
    draw: ImageDraw.ImageDraw,
    items: list[str],
    y: int,
    bottom: int,
    palette: Palette,
) -> bool:
    font, spacing = _bullet_font(draw, items, y, bottom)
    return y + _bullets_height(draw, items, font, spacing) <= bottom


def _bullets_height(
    draw: ImageDraw.ImageDraw, items: list[str], font: object, spacing: int
) -> int:
    total = 0
    for item in items:
        total += text_height(draw, wrap_to_width(draw, item, font, CONTENT_W - 70), font, spacing) + 34
    return total


def _bullet_font(
    draw: ImageDraw.ImageDraw, items: list[str], y: int, bottom: int
) -> tuple[object, int]:
    """Largest body size at which every bullet fits above the footer."""
    room = max(120, bottom - y)
    per_item = room / max(1, len(items))
    size = int(min(44, max(26, per_item * 0.62)))
    while size >= 24:
        font = fonts.load("body", size)
        spacing = int(size * 0.34)
        if _bullets_height(draw, items, font, spacing) <= room:
            return font, spacing
        size -= 2
    return fonts.load("body", 24), 8


def _split_items(body: str) -> list[str]:
    """Accept bullets as newlines, '|' or ';' separated."""
    text = (body or "").replace("|", "\n").replace(";", "\n")
    parts: list[str] = []
    for chunk in text.split("\n"):
        cleaned = chunk.strip(" -\u2022\u2013\t")
        if cleaned:
            parts.append(cleaned)
    return parts


# A stat hero is only a stat if it reads as one at 190pt. We pull the numeric
# run out of a phrase ("38% of build time" -> "38%") rather than requiring the
# whole line to be a number, but we cap its length and require a unit-ish tail
# so that "In 2024 we rebuilt..." does not become a giant "2024".
#
# Unit suffixes we are willing to display: %, x, currency, time, and the
# multipliers/scales an infrastructure audience actually quotes.
_STAT_UNITS = r"(?:%|x|×|k|K|M|B|T|ms|s|m|h|d|min|hrs?|days?|weeks?|months?|years?|"
_STAT_UNITS += r"req/s|rps|rpm|QPS|TPS|GB|TB|PB|MB|KB|Hz|users?|engines?|nodes?|"
_STAT_UNITS += r"clusters?|regions?|teams?|alerts?|incidents?|p9{2,}|nines)"

# A numeric token: optional currency, digits with optional separators, then an
# optional unit suffix. Must contain at least one digit and start with a digit
# or currency symbol.
_STAT_TOKEN = re.compile(
    r"(?<![\w.])"
    r"([$€£]?\d[\d,]*(?:\.\d+)?"
    rf"\s*(?:{_STAT_UNITS})?)"
    r"(?![\w])",
    re.IGNORECASE,
)

_MAX_STAT_CHARS = 12
# Years are the main false positive: "in 2024 we shipped" is a date, not a stat.
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")


def _stat_value(slide: Slide) -> str:
    """Extract a display-worthy number from the slide, or '' if there isn't one.

    Searches the footer first (models put the number there reliably), then the
    first body line, then the headline.
    """
    for raw in (slide.footer, *(slide.body or "").split("\n")[:1], slide.headline):
        text = (raw or "").strip()
        if not text:
            continue
        for match in _STAT_TOKEN.finditer(text):
            value = re.sub(r"\s+", "", match.group(1)).strip()
            if not value or len(value) > _MAX_STAT_CHARS:
                continue
            if _YEAR_RE.match(value):
                continue
            # A bare year with no unit is a date; require either a unit or a
            # bare number that is not a 4-digit year.
            return value
    return ""


def _layout_stat(
    canvas: "Canvas", slide: Slide, palette: Palette, top: int, bottom: int
) -> None:
    """One number, enormous. The most screenshot-able layout there is."""
    draw = canvas.draw
    hfont, headline, _, head_h = fit_text(
        draw, slide.headline, "display", CONTENT_W, 240, 60, 36
    )
    draw_block(draw, MARGIN, top, headline, hfont, palette.muted)
    y = top + head_h + 40

    value = _stat_value(slide)
    if not value:
        # The model did not give us a number, or gave us a phrase. Rendering a
        # phrase at 190pt in the accent colour looks broken and reads worse
        # than the layout it was meant to be, so fall back deliberately.
        return _layout_bullets(canvas, slide, palette, top, bottom)

    # A number gets the full display face at maximum size.
    sfont, swrapped, _, sh = fit_text(
        draw, value, "display", CONTENT_W, int((bottom - y) * 0.62), 190, 64
    )
    draw.multiline_text((MARGIN, y), swrapped, font=sfont, fill=palette.accent, spacing=48)
    y += sh + 44

    # Everything except the number itself becomes supporting body copy, so the
    # stat is never the only thing on the slide.
    rest = [line for line in (slide.body or "").split("\n")[1:] if line.strip()]
    if rest:
        bfont, bwrapped, _, _ = fit_text(
            draw, "\n".join(rest).strip(), "body", CONTENT_W, bottom - y, 42, 28
        )
        draw_block(draw, MARGIN, y, bwrapped, bfont, palette.text)


def _layout_contrast(
    canvas: "Canvas", slide: Slide, palette: Palette, top: int, bottom: int
) -> None:
    """Two columns: what teams try vs what works."""
    draw = canvas.draw
    hfont, headline, _, head_h = fit_text(
        draw, slide.headline, "display", CONTENT_W - 20, 260, 68, 40
    )
    draw_block(draw, MARGIN, top, headline, hfont, palette.text)
    y = top + head_h + 40

    left, right = _split_contrast(slide.body)
    gap = 36
    col_w = (CONTENT_W - gap) // 2
    panel_h = min(max(240, bottom - y - 20), 560)

    for col_x, (title, content, colour) in (
        (MARGIN, (left[0], left[1], palette.muted)),
        (MARGIN + col_w + gap, (right[0], right[1], palette.accent)),
    ):
        canvas.paste_rounded(
            (col_x, y, col_x + col_w, y + panel_h),
            fill=palette.surface,
            radius=22,
            alpha=170,
        )

        tfont, twrapped, _, th = fit_text(
            draw, title or "—", "body_bold", col_w - 60, 120, 32, 22
        )
        draw_block(draw, col_x + 30, y + 30, twrapped, tfont, colour, max_lines=2)

        if content:
            cfont, cwrapped, _, _ = fit_text(
                draw, content, "body", col_w - 60, panel_h - th - 90, 34, 24
            )
            draw_block(
                draw, col_x + 30, y + 30 + th + 22, cwrapped, cfont, palette.text
            )


def _split_contrast(body: str) -> tuple[tuple[str, str], tuple[str, str]]:
    """Split 'a || b' or a colon pair into two column definitions."""
    text = body or ""
    if "||" in text:
        left, right = text.split("||", 1)
    elif "::" in text:
        left, right = text.split("::", 1)
    else:
        parts = _split_items(text)
        left = "\n".join(parts[: len(parts) // 2 or 1])
        right = "\n".join(parts[len(parts) // 2 or 1 :])
    return _split_column(left), _split_column(right)


def _split_column(text: str) -> tuple[str, str]:
    parts = _split_items(text)
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def _layout_steps(
    canvas: "Canvas", slide: Slide, palette: Palette, top: int, bottom: int
) -> None:
    """Numbered procedure - numbered circles in the accent colour."""
    draw = canvas.draw
    hfont, headline, _, head_h = fit_text(
        draw, slide.headline, "display", CONTENT_W - 20, 240, 66, 40
    )
    draw_block(draw, MARGIN, top, headline, hfont, palette.text)
    y = top + head_h + 50

    items = _split_items(slide.body)[:4]
    if not items:
        return
    room = max(160, bottom - y)
    step_h = room / len(items)
    # Fit the step text to the row: never so big it overflows a tight slide,
    # never so small it is unreadable on a sparse one.
    step_size = int(min(38, max(24, step_h * 0.30)))
    sfont = fonts.load("body", step_size)
    line_spacing = int(step_size * 0.34)

    for i, item in enumerate(items, start=1):
        cy = int(y + (i - 1) * step_h)
        radius = 34
        draw.ellipse(
            [MARGIN, cy, MARGIN + radius * 2, cy + radius * 2], fill=palette.accent
        )
        nfont = fonts.load("display", 36)
        nw = text_width(draw, str(i), nfont)
        nh = text_height(draw, str(i), nfont, 0)
        draw.text(
            (MARGIN + radius - nw / 2, cy + radius - nh / 2 - 4),
            str(i),
            font=nfont,
            fill=palette.bg_top,
        )

        wrapped = wrap_to_width(draw, item, sfont, CONTENT_W - 120)
        text_height(draw, wrapped, sfont, line_spacing)
        draw.multiline_text(
            (MARGIN + radius * 2 + 28, cy + 6),
            wrapped,
            font=sfont,
            fill=palette.text,
            spacing=line_spacing,
        )


def _layout_quote(
    canvas: "Canvas", slide: Slide, palette: Palette, top: int, bottom: int
) -> None:
    """A principle, set like a pull-quote."""
    draw = canvas.draw
    qfont, wrapped, _, _ = fit_text(
        draw, f"\u201c{slide.headline}\u201d", "display", CONTENT_W - 60, bottom - top - 260, 82, 44
    )
    qh = text_height(draw, wrapped, qfont, 82 * 0.28)
    y = top + max(40, (bottom - top - qh) // 2 - 60)

    # Oversized quote mark in the accent colour.
    mark = fonts.load("display", 220)
    draw.text((MARGIN - 18, y - 150), "\u201c", font=mark, fill=palette.accent_soft)

    draw.multiline_text((MARGIN, y), wrapped, font=qfont, fill=palette.text, spacing=23)
    y += qh + 46

    if slide.body:
        bfont, bwrapped, _, _ = fit_text(
            draw, slide.body, "body", CONTENT_W - 60, 200, 38, 26
        )
        draw_block(draw, MARGIN, y, bwrapped, bfont, palette.muted)
    if slide.footer:
        ffont = fonts.load("mono", 28)
        draw.text((MARGIN, min(bottom - 50, y + 190)), slide.footer, font=ffont, fill=palette.accent)


def _layout_takeaway(
    canvas: "Canvas", slide: Slide, palette: Palette, top: int, bottom: int
) -> None:
    """One sentence, centred, with conviction."""
    draw = canvas.draw
    available = bottom - top
    font, wrapped, _, h = fit_text(
        draw, slide.headline, "display", CONTENT_W - 40, int(available * 0.68), 92, 44
    )
    y = top + max(0, (available - h) // 2) - 40

    bar_h = h + 70
    draw.rectangle([MARGIN - 30, y, MARGIN - 22, y + bar_h], fill=palette.accent)
    draw.multiline_text((MARGIN, y), wrapped, font=font, fill=palette.text, spacing=26)

    if slide.body:
        y += h + 60
        bfont, bwrapped, _, _ = fit_text(
            draw, slide.body, "body", CONTENT_W - 60, max(80, bottom - y), 40, 26
        )
        draw_block(draw, MARGIN, y, bwrapped, bfont, palette.muted)


def _layout_cta(
    canvas: "Canvas", slide: Slide, palette: Palette, top: int, bottom: int
) -> None:
    """Closing slide: the question, the handle, the action."""
    draw = canvas.draw
    hfont, wrapped, _, h = fit_text(
        draw, slide.headline, "display", CONTENT_W - 40, 460, 80, 42
    )
    y = top + 60
    draw.multiline_text((MARGIN, y), wrapped, font=hfont, fill=palette.text, spacing=24)
    y += h + 44

    if slide.body:
        bfont, bwrapped, _, bh = fit_text(
            draw, slide.body, "body", CONTENT_W - 40, 260, 40, 28
        )
        draw_block(draw, MARGIN, y, bwrapped, bfont, palette.muted)
        y += bh + 40

    # CTA pill, anchored near the bottom. Drawn only when the model named the
    # action: a closing line that ignores the story is worse than none, and the
    # headline plus body already carry the slide.
    pill_text = slide.cta.strip()
    if pill_text:
        cfont = fonts.load("body_bold", 34)
        tw = text_width(draw, pill_text, cfont)
        pill_w = min(CONTENT_W, tw + 72)
        pill_h = 74
        pill_y = min(bottom - pill_h - 10, y + 30)
        draw.rounded_rectangle(
            [MARGIN, pill_y, MARGIN + pill_w, pill_y + pill_h],
            radius=pill_h // 2,
            outline=palette.accent,
            width=3,
        )
        draw.text(
            (MARGIN + 36, pill_y + 19), pill_text, font=cfont, fill=palette.accent
        )


LAYOUTS = {
    "cover": _layout_cover,
    "bullets": _layout_bullets,
    "stat": _layout_stat,
    "contrast": _layout_contrast,
    "steps": _layout_steps,
    "quote": _layout_quote,
    "takeaway": _layout_takeaway,
    "cta": _layout_cta,
}


# ── public API ───────────────────────────────────────────────────────────


def render_slide(
    slide: Slide,
    palette: Palette,
    *,
    handle: str,
    tagline: str,
    index: int,
    total: int,
    seed: int,
) -> Image.Image:
    """Render one slide to a 1080x1350 image."""
    image = make_background(palette, seed, variant=index)
    canvas = Canvas(image=image, draw=ImageDraw.Draw(image))

    _draw_header(canvas.draw, palette, handle, index, total)
    _draw_footer(canvas.draw, palette, tagline, slide.footer)

    top = HEADER_H
    bottom = CANVAS_H - FOOTER_H
    layout = slide.layout if slide.layout in LAYOUTS else "bullets"
    LAYOUTS[layout](canvas, slide, palette, top, bottom)

    return image


def render_draft(
    draft: Draft,
    output_dir: str | Path,
    *,
    handle: str = "@accelerated.devops",
    tagline: str = "",
    palette_name: str | None = None,
) -> list[Path]:
    """Render every slide of a draft as JPEG; returns the written file paths.

    Instagram accepts JPEG only, so the extension is part of the render: what
    lands here is exactly what a publish URL points at.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    seed = seed_for(draft.fingerprint, draft.source.url)
    palette = pick_palette(seed, palette_name)
    total = len(draft.slides)

    paths: list[Path] = []
    for index, slide in enumerate(draft.slides):
        image = render_slide(
            slide,
            palette,
            handle=handle,
            tagline=tagline,
            index=index,
            total=total,
            seed=seed,
        )
        # Instagram accepts JPEG only for image posts, so the slide is written
        # as one here instead of being converted somewhere on the way out.
        # subsampling=0 keeps chroma at full resolution, which is what stops
        # accent-coloured text bleeding into the background around its edges.
        path = output_dir / f"{draft.fingerprint}_{index + 1:02d}.jpg"
        frame = image if image.mode == "RGB" else image.convert("RGB")
        frame.save(path, "JPEG", quality=92, subsampling=0, optimize=True)
        # An earlier render of the same slide left a PNG of it behind; keeping
        # both would make the output directory disagree with itself about the
        # format every URL is built from.
        stale = path.with_suffix(".png")
        if stale.exists():
            stale.unlink()
        paths.append(path)

    log.info("rendered %d slides for %s", len(paths), draft.fingerprint)
    return paths
