"""Font resolution across platforms.

Typography is what separates a designed post from a template, so we resolve a
real font stack per platform rather than falling back to Pillow's bitmap
default (which cannot be scaled and looks obviously generated).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

log = logging.getLogger(__name__)

# role -> candidate filenames, searched in order. First hit wins.
FONT_CANDIDATES: dict[str, tuple[str, ...]] = {
    # Heavy display face for headlines - the "poster" voice.
    "display": (
        "ariblk.ttf",          # Arial Black
        "ArchivoBlack-Regular.ttf",
        "Inter-Bold.ttf",
        "DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    ),
    # Clean UI face for body copy.
    "body": (
        "segoeui.ttf",
        "Inter-Regular.ttf",
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "arial.ttf",
    ),
    # Semibold for emphasis inside body text and slide footers.
    "body_bold": (
        "seguisb.ttf",
        "segoeuib.ttf",
        "Inter-SemiBold.ttf",
        "DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "arialbd.ttf",
    ),
    # Monospace for code-ish fragments, stat labels, step numbers.
    "mono": (
        "consola.ttf",
        "JetBrainsMono-Regular.ttf",
        "DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "cour.ttf",
    ),
}

FONT_DIRS = (
    Path("C:/Windows/Fonts"),
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path.home() / "Library/Fonts",
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts"),
    Path("assets/fonts"),
)


@lru_cache(maxsize=1)
def _index() -> dict[str, Path]:
    """Map lowercase filename -> path for everything we can see."""
    found: dict[str, Path] = {}
    for directory in FONT_DIRS:
        try:
            if not directory.is_dir():
                continue
            for path in directory.rglob("*.tt[fc]"):
                found.setdefault(path.name.lower(), path)
        except (PermissionError, OSError):
            continue
    return found


@lru_cache(maxsize=64)
def font_path(role: str) -> str:
    """Absolute path to the best available font for a role."""
    index = _index()
    for name in FONT_CANDIDATES.get(role, FONT_CANDIDATES["body"]):
        hit = index.get(Path(name).name.lower()) or index.get(Path(name).lower())
        if hit and Path(hit).is_file():
            return str(hit)

    fallback = index.get("arial.ttf") or index.get("dejavusans.ttf")
    if fallback:
        log.warning("no dedicated font for role %r, using %s", role, fallback)
        return str(fallback)

    # Last resort: let Pillow try, it can sometimes find a default.
    log.warning("no TrueType fonts found; rendering with Pillow default")
    return ""


@lru_cache(maxsize=512)
def load(role: str, size: int) -> ImageFont.FreeTypeFont:
    """Load a cached font at a given pixel size."""
    path = font_path(role)
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        # Variable/default font path: Pillow >= 10.1 supports sized defaults.
        return ImageFont.load_default(size=size)


def available_roles() -> dict[str, str]:
    """Resolved font per role - surfaced by `accelerated-devops doctor`."""
    return {role: font_path(role) for role in FONT_CANDIDATES}
