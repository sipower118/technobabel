"""Palettes and background treatments.

Design goal: every post should look like a piece of editorial art direction, not
an AI image. That means a real colour system, texture, and a deterministic
per-post seed so a re-render is identical, but consecutive posts do not all
look the same.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFilter

# Instagram feed portrait 4:5 - the largest real estate the app gives you.
CANVAS_W = 1080
CANVAS_H = 1350
MARGIN = 84


@dataclass(frozen=True)
class Palette:
    """One coherent colour scheme."""

    name: str
    bg_top: tuple[int, int, int]
    bg_bottom: tuple[int, int, int]
    text: tuple[int, int, int]
    muted: tuple[int, int, int]
    accent: tuple[int, int, int]
    accent_soft: tuple[int, int, int]
    surface: tuple[int, int, int]


PALETTES: tuple[Palette, ...] = (
    Palette(
        name="midnight",
        bg_top=(11, 15, 26), bg_bottom=(19, 26, 43),
        text=(240, 244, 252), muted=(150, 163, 186),
        accent=(94, 234, 212), accent_soft=(34, 78, 90),
        surface=(24, 32, 52),
    ),
    Palette(
        name="ember",
        bg_top=(26, 15, 12), bg_bottom=(45, 22, 16),
        text=(252, 245, 238), muted=(186, 160, 148),
        accent=(255, 138, 76), accent_soft=(96, 52, 34),
        surface=(46, 28, 22),
    ),
    Palette(
        name="graphite",
        bg_top=(16, 16, 18), bg_bottom=(30, 30, 34),
        text=(245, 245, 247), muted=(158, 158, 168),
        accent=(255, 214, 102), accent_soft=(92, 76, 36),
        surface=(36, 36, 42),
    ),
    Palette(
        name="signal",
        bg_top=(10, 22, 20), bg_bottom=(14, 38, 34),
        text=(236, 250, 245), muted=(140, 178, 168),
        accent=(126, 240, 140), accent_soft=(38, 88, 58),
        surface=(20, 42, 36),
    ),
    Palette(
        name="indigo",
        bg_top=(14, 14, 34), bg_bottom=(26, 22, 58),
        text=(242, 242, 255), muted=(152, 152, 190),
        accent=(165, 140, 255), accent_soft=(62, 52, 112),
        surface=(30, 28, 56),
    ),
    Palette(
        name="slate",
        bg_top=(13, 19, 24), bg_bottom=(22, 32, 40),
        text=(238, 244, 248), muted=(146, 165, 178),
        accent=(120, 190, 255), accent_soft=(38, 68, 96),
        surface=(26, 38, 48),
    ),
    Palette(
        name="sand",
        bg_top=(28, 24, 20), bg_bottom=(46, 38, 30),
        text=(250, 244, 234), muted=(184, 170, 152),
        accent=(255, 176, 128), accent_soft=(102, 70, 48),
        surface=(44, 36, 28),
    ),
    Palette(
        name="plum",
        bg_top=(22, 13, 24), bg_bottom=(38, 20, 42),
        text=(250, 240, 250), muted=(178, 152, 182),
        accent=(255, 140, 200), accent_soft=(96, 48, 82),
        surface=(40, 24, 44),
    ),
)


def seed_for(*parts: str) -> int:
    """Stable integer seed from arbitrary text."""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def pick_palette(seed: int, name: str | None = None) -> Palette:
    """Deterministically choose a palette for a post."""
    if name:
        for palette in PALETTES:
            if palette.name == name:
                return palette
    return PALETTES[seed % len(PALETTES)]


# ── background construction ──────────────────────────────────────────────


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


def _gradient(size: tuple[int, int], top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    """Vertical gradient, built small and upscaled - smooth and fast."""
    w, h = size
    strip = Image.new("RGB", (1, h))
    pixels = strip.load()
    for y in range(h):
        pixels[0, y] = _lerp(top, bottom, y / max(1, h - 1))
    return strip.resize((w, h), Image.BICUBIC)


def _glow(
    size: tuple[int, int],
    centre: tuple[int, int],
    radius: int,
    colour: tuple[int, int, int],
    strength: float,
) -> Image.Image:
    """Soft radial light, drawn small and blurred up for a natural falloff."""
    w, h = size
    scale = 8
    small = Image.new("L", (w // scale, h // scale), 0)
    draw = ImageDraw.Draw(small)
    cx, cy = centre[0] // scale, centre[1] // scale
    r = max(1, radius // scale)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=int(255 * strength))
    small = small.filter(ImageFilter.GaussianBlur(r * 0.55))
    mask = small.resize((w, h), Image.BICUBIC)

    layer = Image.new("RGB", (w, h), colour)
    return Image.composite(layer, Image.new("RGB", (w, h), (0, 0, 0)), mask)


def _grid(size: tuple[int, int], palette: Palette, seed: int, spacing: int = 60) -> Image.Image:
    """Faint grid - the kind of thing a designer lays in to signal 'technical'."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    alpha = 12
    line = palette.accent + (alpha,)
    for x in range(0, size[0], spacing):
        draw.line([(x, 0), (x, size[1])], fill=line, width=1)
    for y in range(0, size[1], spacing):
        draw.line([(0, y), (size[0], y)], fill=line, width=1)
    return layer


def _noise(size: tuple[int, int], seed: int, amount: float = 0.03) -> Image.Image:
    """A touch of film grain. Purely perceptual, but it kills the 'flat vector' look."""
    rng = random.Random(seed)
    w, h = size
    small = Image.new("L", (w // 3, h // 3))
    small.putdata([rng.randint(128 - int(amount * 255 * 4), 128 + int(amount * 255 * 4))
                   for _ in range((w // 3) * (h // 3))])
    return small.resize((w, h), Image.BILINEAR)


def make_background(palette: Palette, seed: int, variant: int = 0) -> Image.Image:
    """Compose the layered background for one slide."""
    size = (CANVAS_W, CANVAS_H)
    rng = random.Random(seed + variant * 977)

    base = _gradient(size, palette.bg_top, palette.bg_bottom)

    # Two glows placed by seed: consistent per post, varied across posts.
    for _ in range(2):
        cx = rng.randint(-100, size[0] + 100)
        cy = rng.randint(-100, size[1] + 100)
        glow = _glow(
            size,
            (cx, cy),
            radius=rng.randint(420, 760),
            colour=palette.accent if rng.random() < 0.6 else palette.accent_soft,
            strength=rng.uniform(0.10, 0.22),
        )
        base = Image.blend(base, glow, 0.55) if rng.random() < 0.5 else _screen(base, glow)

    base = base.convert("RGBA")
    base.alpha_composite(_grid(size, palette, seed))
    base = base.convert("RGB")

    grain = _noise(size, seed + variant)
    base = Image.blend(base, Image.merge("RGB", (grain, grain, grain)), 0.035)
    return base


def _screen(a: Image.Image, b: Image.Image) -> Image.Image:
    """Screen blend - keeps the glow additive instead of washing out."""
    from PIL import ImageChops

    return ImageChops.screen(a, b)
