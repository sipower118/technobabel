"""LLM provider registry."""

from __future__ import annotations

from ..config import Settings
from ..store import Store
from .base import GeneratedPost, LLMProvider, LLMUnavailable
from .gemini import GeminiProvider

__all__ = [
    "GeneratedPost",
    "GeminiProvider",
    "LLMProvider",
    "LLMUnavailable",
    "build_provider",
]


def build_provider(settings: Settings, store: Store | None = None) -> LLMProvider:
    """Construct the configured provider, or raise if it is not usable.

    Pass the Store so exhausted models are remembered between runs; without it
    every run rediscovers the same 429s and burns the top of the ladder again.
    """
    if not settings.has_llm:
        raise LLMUnavailable(
            "no LLM configured - set GEMINI_API_KEYS and GEMINI_MODELS in .env"
        )
    return GeminiProvider(
        api_keys=settings.gemini_api_keys,
        models=settings.gemini_models,
        store=store,
    )
