"""Hacker News via the official Algolia API.

No scraping involved: HN publishes a public read API, and the point/comment
counts it returns are a genuine popularity signal that tells us which stories
the engineering community actually cares about right now.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from ..models import SourceItem
from .base import Source, get_with_retry

log = logging.getLogger(__name__)

ALGOLIA = "https://hn.algolia.com/api/v1"

# `/search` ranks by relevance across all time, so a strict recent+high-score
# filter frequently returns nothing. `/search_by_date` is the honest
# "what infra stories did the community engage with lately" query, so we
# merge both and let the relevance filter do the topic policing.
ENDPOINTS = ("search", "search_by_date")

# OR-groups: HN search treats a bare word as "title contains word". Narrow
# queries keep the DevOps / platform-engineering focus intact.
SEARCH_QUERIES: list[tuple[str, int]] = [
    ("kubernetes", 80),
    ("platform engineering", 40),
    ("devops", 60),
    ("SRE", 50),
    ("kubernetes OR terraform OR argocd", 40),
    ("incident OR postmortem OR outage", 30),
    ("observability OR opentelemetry OR prometheus", 30),
    ("CI/CD OR continuous delivery", 25),
    ("internal developer platform", 25),
    ("gitops OR platform team", 25),
    ("infrastructure OR cloud native", 20),
    ("CNCF OR service mesh OR istio OR cilium", 20),
]


def _parse_created(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _tags(raw: list | None) -> list[str]:
    return [
        t
        for t in (raw or [])
        if t in {"story", "comment", "poll", "show_hn", "ask_hn", "job"}
    ]


# Algolia matches query terms loosely, so "devops" also returns stories about
# developer relations and "SRE" returns stories about a person's surname. A
# post only enters the queue if the title itself is clearly about the niche.
# Terms are matched on word boundaries to avoid "SRE" hitting "stress".
RELEVANCE_TERMS = re.compile(
    r"\b("
    r"kubernetes|k8s|kubectl|helm|argo ?cd|terraform|pulumi|ansible|"
    r"devops|devops|platform engineer\w*|internal developer platform|"
    r"gitops|backstage|golden path|devex|developer experience|"
    r"site reliability|\bSREs?\b|observability|opentelemetry|prometheus|grafana|"
    r"incident|post-?mortem|outage|reliability|\bSLOs?\b|\bSLIs?\b|error budget|"
    r"ci/cd|continuous (integration|delivery|deployment)|build pipeline|"
    r"infrastructure as code|cloud native|container\w*|docker|service mesh|"
    r"istio|cilium|envoy|karpenter|consul|nomad|vault|"
    r"serverless|kubernetes operator|etcd|kops|eks|gke|aks|"
    r"deployment|rollout|self-hosted|selfhosted|datacenter|data center|"
    r"load balancer|cdn|edge computing|chaos engineering|finops|"
    r"supply chain|artifact registry|monorepo|api gateway|"
    r"on-?call|status page|uptime|throughput|latency|"
    r"aws|azure|gcp|google cloud|cloudflare|digitalocean|hetzner|"
    r"nginx|envoy proxy|ingress|vmware|openstack|ansible tower|argocd"
    r")\b",
    re.IGNORECASE,
)

# Titles matching these are off-niche even if a relevance term appears
# (e.g. "MongoDB CEO resigns" mentions infra only incidentally).
NEGATIVE_TERMS = re.compile(
    r"\b(ceo|resigns?|lawsuit|ipo|stock|crypto|web3|nft|"
    r"front-?end|react|vue|angular|css|javascript|python basics|"
    r"learn to code|course|tutorial for beginners|survey results?)\b",
    re.IGNORECASE,
)


def is_relevant(title: str) -> bool:
    """True when a title is genuinely about DevOps / platform engineering."""
    if not title or not RELEVANCE_TERMS.search(title):
        return False
    return not NEGATIVE_TERMS.search(title)


def _hit_to_item(hit: dict) -> SourceItem | None:
    """Normalise one Algolia hit into a SourceItem, or None when it has no id."""
    object_id = str(hit.get("objectID", ""))
    if not object_id:
        return None
    url = hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
    return SourceItem(
        source="hackernews",
        external_id=object_id,
        title=(hit.get("title") or "").strip(),
        url=url,
        summary=(hit.get("story_text") or "")[:800],
        body="",
        author=hit.get("author") or "",
        published_at=_parse_created(hit.get("created_at")),
        score=int(hit.get("points") or 0),
        comments=int(hit.get("num_comments") or 0),
        tags=_tags(hit.get("_tags")),
        discussion_url=f"https://news.ycombinator.com/item?id={object_id}",
    )


class HackerNewsSource(Source):
    """Pulls top infrastructure stories from the last `lookback_hours`.

    Note: HN's DevOps/platform-engineering volume is genuinely sparse week to
    week (AI and general-news stories dominate the front page). A tight window
    with a high points bar therefore returns nothing, so the default lookback
    is deliberately wider than the RSS one and the bar is lower - the
    relevance filter, not the score, is what keeps this on-niche.
    """

    name = "hackernews"

    def __init__(self, lookback_hours: int = 720, min_points: int = 60) -> None:
        super().__init__()
        self.lookback_hours = lookback_hours
        self.min_points = min_points

    def _fetch(self, client: httpx.Client) -> list[SourceItem]:
        since = datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)
        collected: dict[str, SourceItem] = {}

        for query, _ in SEARCH_QUERIES:
            for endpoint in ENDPOINTS:
                try:
                    response = get_with_retry(
                        client,
                        f"{ALGOLIA}/{endpoint}",
                        params={
                            "query": query,
                            "tags": "story",
                            "numericFilters": (
                                f"created_at_i>{int(since.timestamp())}"
                            ),
                            "hitsPerPage": 30,
                        },
                    )
                    hits = response.json().get("hits", [])
                except Exception as exc:  # noqa: BLE001 - keep going on other queries
                    log.warning("HN %s query %r failed: %s", endpoint, query, exc)
                    continue

                for hit in hits:
                    item = _hit_to_item(hit)
                    if item is None:
                        continue
                    # One story can match several queries; keep the best score.
                    existing = collected.get(item.external_id)
                    if existing is not None and existing.score >= item.score:
                        continue
                    collected[item.external_id] = item

                # Algolia free tier is generous but not unlimited; be polite.
                time.sleep(0.12)

        # Topic filter first, then the popularity bar. A high-scoring off-topic
        # story (a CEO departure that happens to mention "on-call") is worse
        # for the feed than a mid-scoring story that is exactly on-niche.
        relevant = [i for i in collected.values() if i.title and is_relevant(i.title)]
        items = [i for i in relevant if i.score >= self.min_points]
        items.sort(key=lambda i: i.score, reverse=True)
        log.info(
            "hackernews: %d/%d on-topic stories above %d points",
            len(items), len(collected), self.min_points,
        )
        return items
