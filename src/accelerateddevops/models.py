"""Core data structures shared across the pipeline."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

# Instagram hard limits, used to validate generated drafts.
MAX_CAPTION_CHARS = 2200
MAX_HASHTAGS = 30
MAX_CAROUSEL_ITEMS = 10


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# U+2012 figure dash, U+2013 en dash, U+2014 em dash, U+2015 horizontal bar.
_LONG_DASHES = str.maketrans("\u2012\u2013\u2014\u2015", "----")


def plain_dashes(text: str) -> str:
    """Replace typographic dashes with an ASCII hyphen.

    Hard rule: a post never contains a long dash character. They read as a
    typo in a caption and sit badly in a slide's type, so every string that
    can reach a post is passed through here rather than being fixed by hand.
    """
    return text.translate(_LONG_DASHES)


@dataclass
class SourceItem:
    """A single piece of source material pulled from a feed or API."""

    source: str
    external_id: str
    title: str
    url: str
    summary: str = ""
    body: str = ""
    author: str = ""
    published_at: datetime | None = None
    score: int = 0
    comments: int = 0
    tags: list[str] = field(default_factory=list)
    discussion_url: str = ""

    @property
    def text(self) -> str:
        """Everything the LLM should read, de-duplicated and trimmed."""
        parts = [self.title, self.summary, self.body]
        seen: set[str] = set()
        chunks: list[str] = []
        for part in parts:
            cleaned = (part or "").strip()
            if not cleaned:
                continue
            fingerprint = cleaned[:120].lower()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            chunks.append(cleaned)
        return "\n\n".join(chunks)[:8000]

    @property
    def fingerprint(self) -> str:
        """Stable id used for dedupe, claiming, and linking drafts to items.

        Single home for this hash - it was previously reimplemented in both
        `store` and `pipeline`, where the two copies could drift apart.
        """
        seed = f"{self.source}:{self.external_id}:{self.url}"
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]

    def to_row(self) -> dict[str, Any]:
        return asdict(self) | {"published_at": iso(self.published_at)}


@dataclass
class Slide:
    """One frame of a carousel."""

    order: int
    layout: str
    headline: str
    body: str = ""
    footer: str = ""
    cta: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Slide:
        """Rebuild a stored slide; applies the no-long-dash rule on the way in.

        Drafts written before the rule existed still carry em dashes, and they
        are exactly the ones a re-render will put back on a slide.
        """
        return cls(
            order=int(data.get("order", 0)),
            layout=str(data.get("layout", "bullets")),
            headline=plain_dashes(str(data.get("headline", ""))),
            body=plain_dashes(str(data.get("body", ""))),
            footer=plain_dashes(str(data.get("footer", ""))),
            cta=plain_dashes(str(data.get("cta", ""))),
        )


@dataclass
class Draft:
    """A fully-formed post awaiting review, scheduling, or publishing."""

    source: SourceItem
    caption: str
    slides: list[Slide]
    hashtags: list[str] = field(default_factory=list)
    alt_text: str = ""
    cover_layout: str = "cover"
    sources_note: str = ""
    created_at: datetime = field(default_factory=utcnow)
    image_paths: list[str] = field(default_factory=list)
    status: str = "draft"
    scheduled_for: datetime | None = None
    published_at: datetime | None = None
    instagram_media_id: str = ""
    permalink: str = ""

    @property
    def full_caption(self) -> str:
        """Caption as it will actually be posted, hashtags included.

        Hashtags count toward Instagram's 2200-char limit, so the 30-tag cap
        has to live here - otherwise the length validated before rendering
        would not match the text that gets published. The dash rule is applied
        to the assembled string, so it covers a caption no matter which stage
        appended to it.
        """
        tags = " ".join(
            f"#{tag.lstrip('#')}" for tag in self.hashtags[:MAX_HASHTAGS]
        )
        return plain_dashes(f"{self.caption.strip()}\n\n{tags}".strip())

    @property
    def fingerprint(self) -> str:
        """Stable id used for dedupe and file naming."""
        seed = f"{self.source.url}|{self.slides[0].headline if self.slides else ''}"
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]

    def to_json(self) -> str:
        return json.dumps(
            {
                "fingerprint": self.fingerprint,
                "source": self.source.to_row(),
                "caption": self.caption,
                "full_caption": self.full_caption,
                "slides": [slide.to_dict() for slide in self.slides],
                "hashtags": self.hashtags,
                "alt_text": self.alt_text,
                "cover_layout": self.cover_layout,
                "sources_note": self.sources_note,
                "created_at": iso(self.created_at),
                "image_paths": self.image_paths,
                "status": self.status,
                "scheduled_for": iso(self.scheduled_for),
                "published_at": iso(self.published_at),
                "instagram_media_id": self.instagram_media_id,
                "permalink": self.permalink,
            },
            indent=2,
            ensure_ascii=False,
        )
