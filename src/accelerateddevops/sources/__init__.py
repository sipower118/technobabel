"""Source registry - the entry point for collection."""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx

from ..config import Settings
from ..models import SourceItem
from .base import Source, build_client
from .hackernews import HackerNewsSource
from .reddit import RedditSource
from .rss import RssSource

log = logging.getLogger(__name__)

__all__ = ["build_sources", "collect", "Source", "RssSource", "HackerNewsSource", "RedditSource"]


def build_sources(settings: Settings) -> list[Source]:
    """Instantiate the enabled collectors for this configuration."""
    return [
        RssSource(lookback_hours=settings.lookback_hours),
        HackerNewsSource(
            lookback_hours=settings.hn_lookback_hours,
            min_points=settings.min_hn_points,
        ),
        RedditSource(
            min_comments=settings.min_reddit_comments,
            client_id=settings.reddit_client_id,
            client_secret=settings.reddit_client_secret,
            contact=settings.source_contact_email or None,
        ),
    ]


def collect(
    settings: Settings,
    sources: Iterable[Source] | None = None,
    client: httpx.Client | None = None,
) -> list[SourceItem]:
    """Run every source, tolerating individual failures.

    A single dead feed or a rate-limited subreddit must never abort the run,
    so each source is isolated and its errors are logged and swallowed.
    """
    sources = list(sources) if sources is not None else build_sources(settings)
    owns_client = client is None
    client = client or build_client()

    collected: list[SourceItem] = []
    try:
        for source in sources:
            try:
                found = source.fetch(client)
                log.info("source %s -> %d items", source.name, len(found))
                collected.extend(found)
            except Exception as exc:  # noqa: BLE001 - isolate per-source failure
                log.error("source %s failed entirely: %s", source.name, exc)
    finally:
        if owns_client:
            client.close()

    return collected
