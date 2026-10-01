"""RSS/Atom collector for engineering blogs.

These feeds are the backbone of the account: first-party engineering blogs are
high-signal, evergreen, and explicitly intended to be read and discussed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser
import httpx

from ..models import SourceItem
from .base import Source, get_with_retry

log = logging.getLogger(__name__)

# Feeds chosen for DevOps / platform engineering relevance and posting cadence.
# The account's differentiator is *changing large organisations* - governance,
# compliance, slow process, team size. Enterprise / org-change beats come first
# and get a scoring multiplier via FEED_PRIORITY.
DEFAULT_FEEDS: dict[str, str] = {
    # ── Enterprise / org-change beat (priority-weighted in FEED_PRIORITY) ──
    "infoq": "https://www.infoq.com/feed/",
    "microsoftdevops": "https://devblogs.microsoft.com/devops/feed/",
    "dora": "https://dora.dev/index.xml",
    "devopsdotcom": "https://devops.com/feed/",
    "pragmaticengineer": "https://blog.pragmaticengineer.com/rss/",
    "atlassian": "https://www.atlassian.com/engineering/feed/",
    # ── Industry trends + new ideas ──
    "thenewstack": "https://thenewstack.io/feed/",
    "changelog": "https://changelog.com/feed/",
    "huggingface": "https://huggingface.co/blog/feed.xml",
    # ── Practitioner deep dives (a specific problem -> how a team solved it) ──
    "spotify": "https://engineering.atspotify.com/feed",
    "dropbox": "https://dropbox.tech/feed",
    "etsy": "https://codeascraft.com/feed/",
    "lyft": "https://eng.lyft.com/feed",
    "meta": "https://engineering.fb.com/feed/",
    "posthog": "https://posthog.com/rss.xml",
    "aws-architecture": "https://aws.amazon.com/blogs/architecture/feed/",
    "semaphore": "https://semaphoreci.com/blog/feed",
    "honeycomb": "https://www.honeycomb.io/feed",
    "teleport": "https://goteleport.com/blog/rss.xml",
    # Vendor + CNCF project blogs (release notes, architecture deep-dives)
    "kubernetes": "https://kubernetes.io/feed.xml",
    "cncf": "https://www.cncf.io/feed/",
    "grafana": "https://grafana.com/blog/index.xml",
    "hashicorp": "https://www.hashicorp.com/blog/feed.xml",
    "datadog": "https://www.datadoghq.com/blog/engineering/index.xml",
    "vercel": "https://vercel.com/atom",
    "cloudflare": "https://blog.cloudflare.com/rss/",
    "netflix": "https://netflixtechblog.com/feed",
    "github": "https://github.blog/engineering/feed/",
    "docker": "https://www.docker.com/blog/feed/",
    "aws": "https://aws.amazon.com/blogs/devops/feed/",
    "azure": "https://azure.microsoft.com/en-us/blog/feed/",
    # Practitioner writing - consistently the best-performing content
    "incrementals": "https://incrementals.com/atom.xml",
    "sreweekly": "https://sreweekly.com/feed/",
    "martinfowler": "https://martinfowler.com/feed.atom",
    "leaddev": "https://leaddev.com/feed",
    "lastweekinaws": "https://www.lastweekinaws.com/feed/",
}

# Tokens that hint at DevOps/platform relevance; used for a light boost so a
# feed that carries a lot of non-infra noise does not dominate the queue.
SIGNAL_TERMS = {
    "kubernetes", "k8s", "platform engineering", "devops", "sre", "gitops",
    "terraform", "argocd", "observability", "opentelemetry", "prometheus",
    "grafana", "incident", "postmortem", "outage", "reliability", "slo", "sli",
    "ci/cd", "cicd", "pipeline", "deployment", "infrastructure", "cloud native",
    "container", "service mesh", "golden path", "developer experience", "devex",
    "platform team", "internal developer platform", "idp", "finops", "cost",
    "multi-tenancy", "karpenter", "cilium", "envoy", "load balancer", "cdn",
    "build system", "monorepo", "artifact", "supply chain", "chaos",
    "terraform cloud", "vault", "consul", "nomad", "backstage",
    # 2026-era trend signal: AI-driven infra + developer productivity tools.
    "ai infrastructure", "agentic", "generative ai", "copilot", "mlops",
    "developer productivity",
}

# Terms that mark the big-org / slow-change theme specifically. They weigh more
# than generic infra signal: a post about a migration blocked by a change board
# is exactly the account's beat, so it out-scores a release note that merely
# mentions Kubernetes.
ENTERPRISE_TERMS = {
    "enterprise", "corporate", "fortune 500", "large org", "large-scale",
    "large organization", "large organisation",
    "governance", "compliance", "regulat", "audit",
    "change management", "change control", "change board",
    "technical debt", "legacy", "moderniz", "migration",
    "transformation", "bureaucracy", "red tape", "stakeholder",
    "operating model", "team topologies", "conway", "value stream",
    "organizational change", "organisational change",
    "adoption", "institutional",
}

# Language that markets an individual product: launch copy, release notes,
# pricing pitches. The account shares engineering - not vendor advertising - so
# content heavy with these loses its base score instead of being promoted.
# The terms are deliberately narrow (they never name actual products, only the
# act of selling one), so a deep-dive that merely mentions a tool is untouched.
PRODUCT_TERMS = {
    "release note", "announc", "introducing", "product launch",
    "now available", "generally available", "new feature",
    "what's new", "whats new", "pricing", "sign up",
    "free trial", "register now",
}

# Feeds whose editorial beat is the slow, governed, big-org reality of platform
# engineering get a scoring multiplier, so an enterprise-migration story
# out-competes a routine release note with the same keyword count.
# Default weight is 1.0; only upward deviations are listed.
FEED_PRIORITY: dict[str, float] = {
    # Enterprise / org-change beat - the core differentiator.
    "infoq": 1.6,
    "dora": 1.6,
    "microsoftdevops": 1.5,
    "devopsdotcom": 1.5,
    "pragmaticengineer": 1.4,
    "atlassian": 1.3,
    "martinfowler": 1.3,
    "leaddev": 1.3,
    # Trends + sustained practitioner deep dives.
    "thenewstack": 1.4,
    "lastweekinaws": 1.2,
    "honeycomb": 1.2,
    "semaphore": 1.2,
    "spotify": 1.1,
    "dropbox": 1.1,
    "etsy": 1.1,
    "lyft": 1.1,
    "meta": 1.2,
    "posthog": 1.1,
    "aws-architecture": 1.1,
    "teleport": 1.1,
    "changelog": 1.1,
}

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", text or "")).strip()


def _published(entry: object, fallback: datetime) -> datetime:
    raw = (
        getattr(entry, "published_parsed", None)
        or getattr(entry, "updated_parsed", None)
    )
    if raw:
        try:
            return datetime(*raw[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    # Atom entries sometimes only carry an RFC-2822 string.
    published = getattr(entry, "published", None) or getattr(entry, "updated", None)
    if isinstance(published, str):
        try:
            parsed = parsedate_to_datetime(published)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, IndexError):
            pass
    return fallback


def _score(entry: object, body: str, feed_key: str | None = None) -> int:
    """Feeds carry no engagement data, so score on topical + editorial signal.

    Enterprise / org-change terms count more than generic infra terms, and the
    feed's editorial weight (FEED_PRIORITY) multiplies the result - a change
    board story from InfoQ beats a same-keyword release note from a vendor blog.
    Product marketing (PRODUCT_TERMS) is penalised heavily: the account shares
    engineering, not launch copy, so promo-heavy posts sink to the floor instead
    of winning the queue.
    """
    haystack = f"{getattr(entry, 'title', '')} {body}".lower()
    signal = sum(1 for term in SIGNAL_TERMS if term in haystack)
    enterprise = sum(1 for term in ENTERPRISE_TERMS if term in haystack)
    product = sum(1 for term in PRODUCT_TERMS if term in haystack)
    raw = 40 + signal * 12 + enterprise * 18 - product * 30
    # The floor keeps a fully promotional post from scoring negative; it just
    # ranks dead last instead.
    return int(max(10, raw) * FEED_PRIORITY.get(feed_key or "", 1.0))


def _tags(text: str) -> list[str]:
    lower = text.lower()
    terms = SIGNAL_TERMS | ENTERPRISE_TERMS
    return [term.replace(" ", "-") for term in terms if term in lower][:8]


def _entry_content(entry: object) -> str:
    """Feedparser's full entry text, or "" when the feed only has a summary.

    feedparser puts the full HTML body under `content[0].value` (Atom) or
    `summary` (RSS). This is the one shape of entry attribute that varies
    between feed flavours, so it lives behind a single helper.
    """
    content = getattr(entry, "content", None)
    if not content:
        return ""
    return content[0].get("value", "") if isinstance(content, list) else ""


def _entry_to_item(
    entry: object,
    *,
    label: str,
    cutoff: datetime,
    fallback_link: str,
) -> SourceItem | None:
    """Normalise one feed entry into a SourceItem, or None if it is skipped.

    An entry is dropped for exactly three reasons - published outside the
    lookback window, no link to point at, or no readable text. Returning None
    keeps the fetch loop flat instead of nesting three continue-guards.
    """
    published = _published(entry, datetime.now(timezone.utc))
    if published < cutoff:
        return None

    title = _strip_html(getattr(entry, "title", "")) or "Untitled"
    link = getattr(entry, "link", "") or fallback_link
    if not link:
        return None

    summary = _strip_html(getattr(entry, "summary", "") or _entry_content(entry))
    # Search for the full text first (some feeds only publish a teaser in
    # summary); when the entry carries no content we have only the summary.
    body = _strip_html(_entry_content(entry) or summary)
    if not body:
        body = summary

    return SourceItem(
        source=f"rss:{label}",
        external_id=getattr(entry, "id", link) or link,
        title=title,
        url=link,
        summary=summary[:1200],
        body=body[:6000],
        author=_strip_html(getattr(entry, "author", "")),
        published_at=published,
        score=_score(entry, body, label),
        comments=0,
        tags=_tags(f"{title} {body}"),
    )


class RssSource(Source):
    """Fetches and normalises a set of RSS/Atom feeds."""

    name = "rss"

    def __init__(
        self,
        feeds: dict[str, str] | None = None,
        lookback_hours: int = 72,
        max_entries_per_feed: int = 25,
    ) -> None:
        super().__init__()
        self.feeds = feeds or DEFAULT_FEEDS
        self.lookback_hours = lookback_hours
        self.max_entries_per_feed = max_entries_per_feed

    def _fetch(self, client: httpx.Client) -> list[SourceItem]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)
        items: list[SourceItem] = []

        for label, url in self.feeds.items():
            try:
                response = get_with_retry(client, url)
                parsed = feedparser.parse(response.content)
            except Exception as exc:  # noqa: BLE001 - one bad feed must not kill the run
                log.warning("feed %s (%s) failed: %s", label, url, exc)
                continue

            entries = parsed.entries[: self.max_entries_per_feed]
            if not entries:
                log.debug("feed %s returned no entries", label)
                continue

            for entry in entries:
                item = _entry_to_item(
                    entry,
                    label=label,
                    cutoff=cutoff,
                    fallback_link=parsed.feed.get("link", ""),
                )
                if item is not None:
                    items.append(item)

        log.info("rss: collected %d items across %d feeds", len(items), len(self.feeds))
        return items
