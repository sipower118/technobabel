"""Pipeline orchestration: collect -> generate -> render -> approve -> publish.

The stages are separate functions rather than one `run()` so you can drive them
individually from the CLI while developing, and so a failure in generation
never loses the already-collected items.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .config import Settings
from .llm import GeneratedPost, LLMProvider, LLMUnavailable
from .models import (
    MAX_CAPTION_CHARS,
    Draft,
    SourceItem,
    utcnow,
)
from .render import render_draft
from .store import Store

if TYPE_CHECKING:  # imported lazily at runtime to keep publish out of import time
    from .publish import InstagramPublisher

log = logging.getLogger(__name__)

# Basename is derived from the URL, so this is a duplicate filter.
_URL_NOISE = re.compile(r"[?&](utm_|ref|source|cmpid|fbclid|gclid)=")


# ── selection ────────────────────────────────────────────────────────────


def _dedupe_url(item: SourceItem) -> str:
    return _URL_NOISE.sub("", item.url).rstrip("/").lower()


# Interaction is the primary deciding factor: upvotes and comments prove an item
# matters to real people. RSS feeds expose no engagement data at all - only the
# topical heuristic score - so those items are discounted to keep proven-popular
# stories ahead of speculative blog posts.
COMMENT_WEIGHT = 2.0  # a comment takes effort to write; an upvote is one click
RSS_HEURISTIC_DISCOUNT = 0.75


def _engagement(item: SourceItem) -> float:
    """How "worth writing about" an item is, dominated by real user interaction.

    Comments deliberately outweigh votes: they signal genuine interest where
    points (HN karma, Reddit upvotes) inflate for free. Sources with real
    engagement - HN points + comments, Reddit votes + comments - rank at full
    strength; RSS items carry no interaction, so their heuristic portion is
    discounted on top.
    """
    interaction = item.score + item.comments * COMMENT_WEIGHT
    source_key = item.source.split(":", 1)[0]
    if source_key == "rss":
        interaction *= RSS_HEURISTIC_DISCOUNT
    return interaction


def select_items(store: Store, settings: Settings, limit: int | None = None) -> list[SourceItem]:
    """Pick the best unused items and claim exactly those.

    Scores are not comparable across sources (HN points vs Reddit comments vs
    a topical RSS heuristic), so each source is normalised to its own recent
    range before ranking - otherwise a busy source crowds everything else out.

    Only the items actually returned are claimed. Items that lost the ranking
    stay available for later runs.
    """
    limit = limit or settings.posts_per_run
    # Look at a wider pool than we need, so per-source normalisation has
    # something to choose between.
    candidates = store.candidate_items(limit * 6, min_score=1)
    if not candidates:
        return []

    by_source: dict[str, list[SourceItem]] = {}
    for item in candidates:
        by_source.setdefault(item.source, []).append(item)

    ranked: list[tuple[float, SourceItem]] = []
    for items in by_source.values():
        # The `or 1` guards division by zero when a whole source scores 0.
        best = max((_engagement(i) for i in items), default=1) or 1
        for item in items:
            ranked.append((_engagement(item) / best, item))

    # Stable ordering: engagement first, then recency as a tiebreaker.
    ranked.sort(key=lambda pair: (pair[0], pair[1].score), reverse=True)
    chosen = [item for _, item in ranked[:limit]]
    store.mark_used([item.fingerprint for item in chosen])
    return chosen


# ── generation ───────────────────────────────────────────────────────────


def _apply_monetisation(draft: Draft, settings: Settings) -> None:
    """Attach the CTA and affiliate link, once the account has traction.

    Off by default: an early account with affiliate links reads as spam and
    suppresses reach, so this is a deliberate later-stage switch.
    """
    if not settings.monetization_enabled:
        return
    cta = settings.cta_text.strip()
    if not cta:
        return
    parts = [draft.caption.strip()]
    if cta and cta.lower() not in draft.caption.lower():
        parts.append(cta)
    if settings.affiliate_url:
        parts.append(f"Link in bio: {settings.affiliate_url}")
    draft.caption = "\n\n".join(parts)
    if draft.slides:
        draft.slides[-1].cta = draft.slides[-1].cta or cta


def _validate(draft: Draft, max_slides: int) -> list[str]:
    """Cheap structural checks before we spend rendering time."""
    problems: list[str] = []
    if not draft.slides:
        problems.append("no slides")
    if len(draft.slides) > max_slides:
        problems.append(f"{len(draft.slides)} slides exceeds {max_slides}")
    full = draft.full_caption
    if len(full) > MAX_CAPTION_CHARS:
        problems.append(f"caption {len(full)} chars exceeds {MAX_CAPTION_CHARS}")
    if not draft.caption.strip():
        problems.append("empty caption")
    for slide in draft.slides:
        if not slide.headline.strip():
            problems.append(f"slide {slide.order} has no headline")
        if len(slide.headline) > 120:
            problems.append(f"slide {slide.order} headline is {len(slide.headline)} chars")
    return problems


def generate_draft(
    item: SourceItem,
    provider: LLMProvider,
    settings: Settings,
) -> Draft | None:
    """Turn one source item into a validated Draft, or None if unusable."""
    try:
        result: GeneratedPost = provider.generate(
            title=item.title,
            body=item.text,
            url=item.url,
            source_name=item.source,
            brand=settings.brand_name,
            tagline=settings.brand_tagline,
            max_slides=settings.max_carousel_slides,
            hook_intensity=settings.hook_intensity,
        )
    except LLMUnavailable:
        # Ladder-wide failure (bad key, or every model throttled). This is not
        # specific to this item, so it propagates and run_generate stops
        # instead of burning the remaining items on the same error.
        raise
    except Exception as exc:  # noqa: BLE001 - one bad item must not stop the run
        log.exception("generation failed for %r: %s", item.title[:60], exc)
        return None

    # An empty slide list would make every downstream field an IndexError, so
    # treat it as a failed generation rather than constructing a broken draft.
    if not result.slides:
        log.warning("model returned no slides for %r", item.title[:60])
        return None

    draft = Draft(
        source=item,
        caption=result.caption,
        slides=result.slides,
        hashtags=result.hashtags,
        alt_text=result.alt_text or f"Carousel: {result.slides[0].headline}",
        cover_layout=result.slides[0].layout,
        sources_note=result.sources_note or f"Source: {item.source}",
    )
    _apply_monetisation(draft, settings)

    problems = _validate(draft, settings.max_carousel_slides)
    if problems:
        log.warning("rejecting draft for %r: %s", item.title[:50], "; ".join(problems))
        return None
    return draft


# ── rendering ────────────────────────────────────────────────────────────


def render_carousel(draft: Draft, settings: Settings, store: Store) -> Draft:
    """Render slides and record the paths on the draft."""
    out_dir = settings.output_dir / draft.fingerprint
    paths = render_draft(
        draft,
        out_dir,
        handle=settings.brand_handle,
        tagline=settings.brand_tagline,
    )
    draft.image_paths = [str(p) for p in paths]
    store.update_draft(draft.fingerprint, image_paths=draft.image_paths)
    return draft


# ── scheduling ───────────────────────────────────────────────────────────


def next_slot_after(now: datetime, slots: list[str]) -> datetime:
    """First publish slot strictly after `now`, today or tomorrow."""
    parsed: list[tuple[int, int]] = []
    for slot in slots:
        try:
            hour, minute = (int(p) for p in slot.strip().split(":", 1))
        except (ValueError, AttributeError):
            log.warning("ignoring malformed PUBLISH_SLOTS entry %r", slot)
            continue
        if 0 <= hour < 24 and 0 <= minute < 60:
            parsed.append((hour, minute))

    if not parsed:
        # A malformed schedule must not silently stop all publishing.
        log.warning("no valid publish slots; defaulting to 09:00 local")
        parsed = [(9, 0)]
    parsed.sort()

    for hour, minute in parsed:
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > now:
            return candidate
    first = parsed[0]
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(
        hour=first[0], minute=first[1], second=0, microsecond=0
    )


def assign_schedule(
    store: Store,
    settings: Settings,
    now: datetime | None = None,
    drafts: list[Draft] | None = None,
) -> list[Draft]:
    """Spread approved drafts across upcoming publish slots.

    Slots already taken by previously-scheduled drafts are skipped, so running
    this repeatedly keeps filling later slots instead of overwriting earlier
    ones. Overflow drafts roll into the following day.
    """
    now = now or utcnow()
    drafts = drafts if drafts is not None else store.list_drafts(status="approved", limit=50)
    if not drafts:
        return []

    taken = {
        when
        for when in (
            draft.scheduled_for
            for draft in store.list_drafts(status="scheduled", limit=500)
        )
        if when
    }

    scheduled: list[Draft] = []
    cursor = now
    for draft in drafts:
        # Advance past every slot that is already occupied, so drafts land on
        # distinct, free slots.
        for _ in range(len(settings.publish_slots) * 3 + 1):
            cursor = next_slot_after(cursor + timedelta(seconds=1), settings.publish_slots)
            if cursor not in taken:
                break
        else:  # pragma: no cover - only if the search window is exhausted
            log.warning("could not find a free slot for %s", draft.fingerprint)
            continue

        taken.add(cursor)
        draft.scheduled_for = cursor
        draft.status = "scheduled"
        store.update_draft(
            draft.fingerprint, status="scheduled", scheduled_for=cursor
        )
        scheduled.append(draft)
        log.info("scheduled %s for %s", draft.fingerprint, cursor.isoformat())
    return scheduled


# ── stages ───────────────────────────────────────────────────────────────


def run_collect(store: Store, settings: Settings) -> tuple[int, int]:
    """Fetch everything and store the new items. Returns (seen, new)."""
    from .sources import collect

    log.info("collecting from all sources...")
    items = collect(settings)
    new = store.add_items(items)
    log.info("collected %d items, %d new", len(items), new)
    return len(items), new


def run_generate(
    store: Store,
    settings: Settings,
    provider: LLMProvider,
    auto_approve: bool = False,
) -> list[Draft]:
    """Select, generate, render, and store drafts for this run."""
    items = select_items(store, settings)
    if not items:
        log.info("no new items to write about")
        return []

    drafts: list[Draft] = []
    for index, item in enumerate(items):
        try:
            draft = generate_draft(item, provider, settings)
        except LLMUnavailable:
            # The whole ladder is down (bad key, or every model throttled).
            # Release this item and every later one, then re-raise: retrying
            # the rest would repeat the identical failure, and the caller needs
            # the reason in order to report one actionable message.
            for pending in items[index:]:
                store.release_item(pending.fingerprint)
            raise
        if draft is None:
            # Bad content for this one item: put it back so a later run can
            # retry it, but keep going with the rest of the batch.
            store.release_item(item.fingerprint)
            continue
        draft = render_carousel(draft, settings, store)
        if auto_approve:
            draft.status = "approved"
            store.update_draft(draft.fingerprint, status="approved")
        store.save_draft(draft)
        drafts.append(draft)
        log.info("draft %s: %r", draft.fingerprint, draft.slides[0].headline[:60])
    return drafts


def _publish_one(
    store: Store,
    publisher: "InstagramPublisher",
    draft: Draft,
) -> bool:
    """Publish a single draft and record the outcome in the store.

    A failure puts the draft back to `approved` rather than dropping it, so a
    transient Instagram failure leaves the post queued instead of losing it.
    The caller decides how many drafts to attempt.

    Args:
        store: Draft state.
        publisher: An open Instagram publisher.
        draft: The draft to publish.

    Returns:
        True if the post went out.
    """
    from .publish import PublishError

    try:
        result = publisher.publish_draft(draft)
    except PublishError as exc:
        log.error("publish failed for %s: %s", draft.fingerprint, exc)
        store.update_draft(draft.fingerprint, status="approved", error=str(exc))
        return False
    now = utcnow()
    draft.status = "published"
    draft.published_at = now
    draft.instagram_media_id = result.media_id
    draft.permalink = result.permalink
    store.update_draft(
        draft.fingerprint,
        status="published",
        published_at=now,
        media_id=result.media_id,
        permalink=result.permalink,
    )
    log.info("published %s -> %s", draft.fingerprint, result.permalink or result.media_id)
    return True


def run_publish_next(store: Store, settings: Settings) -> Draft | None:
    """Publish the single oldest approved draft.

    One at a time, by design: the Actions `publish` workflow fires on a
    schedule, and a burst would post several carousels back to back on a single
    account. Exactly one post per run also means a failed run can be retried
    without risking a duplicate.

    Returns:
        The draft that was published, or None if the queue was empty or the
        publish failed.
    """
    from .publish import InstagramPublisher

    draft = store.next_approved()
    if draft is None:
        log.info("nothing approved to publish")
        return None
    with InstagramPublisher(settings) as publisher:
        if not _publish_one(store, publisher, draft):
            return None
    return draft


def run_publish_due(store: Store, settings: Settings, now: datetime | None = None) -> int:
    """Publish every approved/scheduled draft whose time has come."""
    from .publish import InstagramPublisher

    now = now or utcnow()
    due = store.due_drafts(now)
    if not due:
        log.info("nothing due to publish")
        return 0

    published = 0
    with InstagramPublisher(settings) as publisher:
        for draft in due:
            if _publish_one(store, publisher, draft):
                published += 1
    return published


@dataclass
class RunSummary:
    collected: int = 0
    new_items: int = 0
    drafted: int = 0
    scheduled: int = 0
    published: int = 0


def run_all(
    store: Store,
    settings: Settings,
    provider: LLMProvider | None = None,
    auto_approve: bool = False,
    auto_schedule: bool = True,
) -> RunSummary:
    """One full cycle: collect, generate, render, optionally schedule, publish.

    Used by `accelerated-devops run` and by the scheduler daemon.
    """
    summary = RunSummary()
    run_id = store.start_run()

    try:
        summary.collected, summary.new_items = run_collect(store, settings)

        if provider is not None:
            drafts = run_generate(
                store, settings, provider, auto_approve=auto_approve
            )
            summary.drafted = len(drafts)
            if auto_schedule and (auto_approve or drafts):
                summary.scheduled = len(assign_schedule(store, settings))
        else:
            log.info("no LLM provider - skipping generation")

        summary.published = run_publish_due(store, settings)
    finally:
        store.finish_run(run_id)

    log.info(
        "run complete: %d collected, %d new, %d drafted, %d scheduled, %d published",
        summary.collected, summary.new_items, summary.drafted,
        summary.scheduled, summary.published,
    )
    return summary
