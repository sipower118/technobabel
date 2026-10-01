"""Reddit via the official OAuth API, with a public-endpoint fallback.

Reddit is the best signal we have for *pain points*: the questions people
actually ask in r/devops, r/kubernetes and r/sre map directly onto the
"problem -> how we fixed it" format that performs well on Instagram.

Two access paths, in order of preference:

1. **OAuth** (recommended, needs credentials). A registered *script* app's
   client_id/secret buy a bearer token from the ``client_credentials`` grant.
   Requests then go to ``oauth.reddit.com``, which allows 100 queries per
   minute per client id. This is the path Reddit expects cloud traffic to
   take, and the only one that works from a datacentre IP.
2. **Public .json endpoints** (fallback, no credentials). Reddit IP-blocks
   these aggressively - 403 with an HTML body from most datacentre ranges,
   and 10 QPM for everyone else. We probe once and skip cleanly.

Set ``REDDIT_CLIENT_ID`` / ``REDDIT_CLIENT_SECRET`` in .env to enable (1).
Free-tier access is for approved personal, non-commercial use, so keep the
volume modest and re-check the terms before this feeds anything monetised.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from ..models import SourceItem
from .base import Source

log = logging.getLogger(__name__)

SUBREDDITS = ["devops", "kubernetes", "sre", "PlatformEngineering", "selfhosted"]
LISTINGS = ["hot", "top"]

AUTH_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH_BASE = "https://oauth.reddit.com"
PUBLIC_BASE = "https://www.reddit.com"

# Refresh this far ahead of expiry so an in-flight batch cannot race the clock
# and 401 halfway through.
TOKEN_REFRESH_MARGIN = 60.0
DEFAULT_TOKEN_TTL = 3600.0

# Free tier allows 100 queries/minute per client id. We sit far below that,
# but back off when a previous run has spent most of the window.
MIN_REMAINING = 5

# Reddit's API rules require a descriptive, unique User-Agent naming the app
# and a contact, so traffic is attributable to a person rather than a scraper.
CONTACT = "accelerated.devops content bot (set SOURCE_CONTACT_EMAIL in .env)"


@dataclass
class _Token:
    value: str
    expires_at: float


def _published(created_utc: float | None) -> datetime:
    if not created_utc:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(float(created_utc), tz=timezone.utc)


def _is_text_post(data: dict) -> bool:
    """Keep self-posts that carry real body text.

    `is_self` is True for text submissions and False for links/images, so this
    has to test it positively. Getting this backwards discards exactly the
    posts worth writing about, since link posts carry no `selftext` either.
    """
    return bool(data.get("is_self")) and bool(data.get("selftext"))


def _tags(text: str) -> list[str]:
    return list(dict.fromkeys(t.lower() for t in text.split() if t.startswith("#")))[:8]


def _float_or(value: str | None, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class RedditSource(Source):
    """Collects text posts from infrastructure subreddits."""

    name = "reddit"

    def __init__(
        self,
        min_comments: int = 40,
        contact: str | None = None,
        client_id: str = "",
        client_secret: str = "",
    ) -> None:
        super().__init__()
        self.min_comments = min_comments
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = (
            f"AcceleratedDevOpsBot/0.1 (by u/{contact})" if contact else CONTACT
        )
        # Reddit requires an app-specific User-Agent on every call, so the
        # shared base builds the client with this header.
        self.client_kwargs = {"extra_headers": {"User-Agent": self.user_agent}}
        self._token: _Token | None = None
        self._reachable_cache: bool | None = None

    @property
    def has_oauth(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # ── auth ─────────────────────────────────────────────────────────────

    def _authenticate(self, client: httpx.Client) -> str | None:
        """Fetch (or reuse) an app-only bearer token.

        The client_credentials grant is read-only and needs no Reddit account,
        which is all we want: we read public listings and never post.
        """
        now = time.time()
        if self._token and now < self._token.expires_at - TOKEN_REFRESH_MARGIN:
            return self._token.value
        if not self.has_oauth:
            return None

        try:
            response = client.post(
                AUTH_URL,
                auth=(self.client_id, self.client_secret),
                data={"grant_type": "client_credentials"},
                headers={"User-Agent": self.user_agent},
            )
        except Exception as exc:  # noqa: BLE001 - network failure, not fatal
            log.warning("reddit OAuth token request failed: %s", exc)
            return None

        if response.status_code != 200:
            # 401/403 here almost always means the app is not approved for
            # Data API access yet, which is a setup problem worth naming.
            log.warning(
                "reddit OAuth token request returned %s - check that the script "
                "app is approved for Data API access: %s",
                response.status_code,
                response.text[:200],
            )
            return None

        try:
            body = response.json()
        except ValueError:
            log.warning("reddit OAuth token response was not JSON")
            return None

        value = body.get("access_token")
        if not value:
            log.warning("reddit OAuth response had no access_token: %s", body)
            return None

        ttl = _float_or(str(body.get("expires_in") or ""), DEFAULT_TOKEN_TTL)
        self._token = _Token(value=str(value), expires_at=time.time() + ttl)
        log.info("reddit: authenticated via OAuth (token valid for %ds)", int(ttl))
        return self._token.value

    # ── rate limiting ────────────────────────────────────────────────────

    @staticmethod
    def _respect_rate_limit(response: httpx.Response) -> None:
        """Pause when the OAuth budget is nearly spent.

        Reddit sends x-ratelimit-{used,remaining,reset} on authenticated
        calls. Sleeping now is far cheaper than getting the client throttled
        for the rest of the window.
        """
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining is None:
            return
        left = _float_or(remaining, 99.0)
        if left >= MIN_REMAINING:
            return
        # x-ratelimit-reset is a countdown in seconds, not a timestamp.
        wait = min(60.0, max(2.0, _float_or(response.headers.get("x-ratelimit-reset"), 5.0)))
        log.warning("reddit rate limit nearly spent (%s left) - pausing %.0fs", remaining, wait)
        time.sleep(wait)

    # ── public fallback probe ────────────────────────────────────────────

    def _reachable(self, client: httpx.Client) -> bool:
        """Probe the unauthenticated endpoint once; 403 is sticky."""
        if self._reachable_cache is not None:
            return self._reachable_cache
        try:
            response = client.get(
                f"{PUBLIC_BASE}/r/devops/hot.json",
                params={"limit": 1, "raw_json": 1},
                headers={"User-Agent": self.user_agent},
            )
            self._reachable_cache = response.status_code == 200
        except Exception as exc:  # noqa: BLE001 - treat network errors as blocked
            log.warning("reddit reachability probe failed: %s", exc)
            self._reachable_cache = False
        return bool(self._reachable_cache)

    # ── fetching ─────────────────────────────────────────────────────────

    def _fetch_listing(
        self,
        client: httpx.Client,
        subreddit: str,
        listing: str,
        token: str | None,
    ) -> list[dict] | None:
        """One listing request. Returns child nodes, or None on failure."""
        params = {"limit": 50, "raw_json": 1, "t": "week"}

        for attempt in (1, 2):
            if token:
                url = f"{OAUTH_BASE}/r/{subreddit}/{listing}"
                headers = {
                    "Authorization": f"bearer {token}",
                    "User-Agent": self.user_agent,
                }
            else:
                url = f"{PUBLIC_BASE}/r/{subreddit}/{listing}.json"
                headers = {"User-Agent": self.user_agent}

            try:
                response = client.get(url, params=params, headers=headers)
            except Exception as exc:  # noqa: BLE001 - skip this listing
                log.warning("reddit r/%s/%s failed: %s", subreddit, listing, exc)
                return None

            if response.status_code == 401 and token and attempt == 1:
                # Expired or revoked mid-batch. Drop the cached token, get a
                # new one, and retry once rather than losing the listing.
                self._token = None
                token = self._authenticate(client)
                if token:
                    continue
                return None

            if response.status_code == 429:
                retry_after = _float_or(response.headers.get("retry-after"), 5.0)
                log.warning("reddit r/%s/%s rate limited - pausing %.0fs", subreddit, listing, retry_after)
                time.sleep(min(30.0, retry_after))
                return None

            if response.status_code >= 400:
                log.warning(
                    "reddit r/%s/%s returned %s%s",
                    subreddit, listing, response.status_code,
                    " (public endpoint blocked on this network)" if not token else "",
                )
                return None

            if token:
                self._respect_rate_limit(response)

            try:
                return response.json().get("data", {}).get("children", [])
            except ValueError:
                log.warning("reddit r/%s/%s returned non-JSON", subreddit, listing)
                return None
        return None

    def _to_items(self, subreddit: str, children: list[dict]) -> list[SourceItem]:
        items: list[SourceItem] = []
        for child in children:
            data = child.get("data") or {}
            if data.get("over_18") or not _is_text_post(data):
                continue
            comments = int(data.get("num_comments") or 0)
            if comments < self.min_comments:
                continue
            permalink = data.get("permalink") or ""
            items.append(
                SourceItem(
                    source=f"reddit:r/{subreddit}",
                    external_id=str(data.get("id", "")),
                    title=(data.get("title") or "").strip(),
                    url=f"{PUBLIC_BASE}{permalink}",
                    summary=(data.get("selftext") or "")[:2000],
                    body=(data.get("selftext") or "")[:6000],
                    author=data.get("author") or "[deleted]",
                    published_at=_published(data.get("created_utc")),
                    score=int(data.get("score") or 0),
                    comments=comments,
                    tags=_tags(data.get("title", "")),
                    discussion_url=f"{PUBLIC_BASE}{permalink}",
                )
            )
        return items

    def _fetch(self, client: httpx.Client) -> list[SourceItem]:
        token = self._authenticate(client)

        if token is None and not self._reachable(client):
            log.warning(
                "reddit: www.reddit.com returns 403 for this network and no "
                "REDDIT_CLIENT_ID/SECRET are configured - skipping. Reddit "
                "blocks most datacentre IPs; OAuth credentials (approved "
                "non-commercial Data API access) are the supported fix.",
            )
            return []

        items: list[SourceItem] = []
        for subreddit in SUBREDDITS:
            for listing in LISTINGS:
                children = self._fetch_listing(client, subreddit, listing, token)
                if children is None:
                    continue
                items.extend(self._to_items(subreddit, children))
                # 10 listings is a fraction of the 100/min budget, but
                # pace anyway so this never looks like a burst.
                time.sleep(0.3)

        # De-dupe across the hot/top overlap.
        unique: dict[str, SourceItem] = {}
        for item in items:
            existing = unique.get(item.external_id)
            if existing is None or item.comments > existing.comments:
                unique[item.external_id] = item

        results = sorted(unique.values(), key=lambda i: i.comments, reverse=True)
        log.info(
            "reddit (%s): %d text posts above %d comments",
            "oauth" if self._token else "public",
            len(results),
            self.min_comments,
        )
        return results
