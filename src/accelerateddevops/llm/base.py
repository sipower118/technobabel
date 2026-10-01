"""LLM provider interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..models import Slide


class LLMUnavailable(RuntimeError):
    """Every configured model/key is exhausted or failing."""


@dataclass
class GeneratedPost:
    """What the model returns for one source item."""

    hook: str
    subhook: str
    caption: str
    hashtags: list[str]
    alt_text: str
    slides: list[Slide]
    sources_note: str = ""


class LLMProvider(Protocol):
    """Anything that can turn source material into a draft post."""

    def generate(self, *, title: str, body: str, url: str, source_name: str,
                 brand: str, tagline: str, max_slides: int,
                 hook_intensity: float) -> GeneratedPost:
        ...
