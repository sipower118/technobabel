"""Instagram Graph API publishing.

The official API only: Meta's platform policy forbids unofficial automation
because account bans are a real cost, and the Graph API gives scheduling,
carousels, and insights without that risk.

Three hard constraints shape this module:

1. Instagram fetches image bytes from a **public URL**, while it creates the
   container, so the slides have to be live before the API call. They already
   are: they sit on the gh-pages site. All this module needs is that site's
   root, and `image_urls()` turns it plus the fingerprint into one URL per
   slide. Nothing is staged, converted, uploaded, downloaded or waited for -
   hosting is not publishing, and none of that belongs here.
2. The URL has to mirror how the renderer names things:
   `<fingerprint>/<fingerprint>_NN.jpg` under the site root. A wrong path is a
   404, and Meta reports a 404 as 9004/2207052 "Only photo or video can be
   accepted as media type", which reads like a `media_type` bug and is not one.
3. Carousels are published in three steps: create N item containers, then a
   `media_type=CAROUSEL` parent container, then publish that parent with a
   single `/media_publish`. If any step fails, already-created containers are
   cleaned up best-effort (the IG Login API does not support deletion, so they
   expire on their own within 24 hours).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import httpx

from ..config import Settings
from ..models import Draft, MAX_CAPTION_CHARS, MAX_CAROUSEL_ITEMS
from .errors import PublishError

log = logging.getLogger(__name__)

GRAPH_VERSION = "v26.0"
# The Instagram API with Instagram Login (Business Login for Instagram) uses
# graph.instagram.com; the Facebook-Login variant uses graph.facebook.com and
# its own Graph API token. This app uses Instagram Login, so every call goes to
# graph.instagram.com and the token is an Instagram User access token.
GRAPH_BASE = f"https://graph.instagram.com/{GRAPH_VERSION}"

# Scopes for the Instagram Login flow (no Facebook Page involved). The old
# string mixed in Facebook-Login scopes (pages_show_list, pages_read_engagement)
# that would not exist on a Business Login for Instagram app.
SCOPES = (
    "instagram_business_basic,instagram_business_content_publish,"
    "instagram_business_manage_insights"
)

# When Instagram cannot download the image behind an `image_url`, it does not
# report a fetch failure. It reports that no media type could be determined:
#   {"code": 9004, "error_subcode": 2207052,
#    "message": "Only photo or video can be accepted as media type.",
#    "error_user_msg": "The media could not be fetched from this URI: ..."}
# The wording sends people hunting for a wrong `media_type` parameter; the real
# cause is almost always a URL that does not resolve to a public JPEG. Meta
# also returns this for the same bytes served from a working domain, so a
# single retry is worth it before declaring the URL broken.
MEDIA_FETCH_ERROR_CODE = 9004
MEDIA_FETCH_ERROR_SUBCODE = 2207052
MEDIA_FETCH_RETRY_ATTEMPTS = 3
MEDIA_FETCH_RETRY_DELAY_SECONDS = 2.0


@dataclass
class PublishResult:
    media_id: str
    permalink: str = ""
    raw: dict | None = None


def _is_media_fetch_error(exc: PublishError) -> bool:
    """True when the failure is Instagram failing to download the image_url.

    Meta returns 9004/2207052 both for genuinely unreachable URLs and as a
    transient failure on URLs that work, so the caller can retry before
    declaring the URL broken.
    """
    return f"subcode {MEDIA_FETCH_ERROR_SUBCODE}" in str(exc)


class InstagramPublisher:
    """Creates and publishes Instagram media via the Graph API."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.config = settings.instagram
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=30.0,
                headers={"User-Agent": "AcceleratedDevOps/0.1"},
            )
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> InstagramPublisher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── plumbing ─────────────────────────────────────────────────────────

    @staticmethod
    def _explain(body: dict) -> str | None:
        """Turn a Graph API error body into an actionable message, or None."""
        if "error" not in body:
            return None
        err = body["error"]
        message = err.get("message", "unknown error")
        code = err.get("code", "?")
        detail = err.get("error_subcode")

        # 190/463 = expired or invalid token, 200 = missing permission. These
        # are setup problems only the user can fix, so name the fix.
        hint = ""
        if code in (190, 463):
            hint = " - the access token is expired or invalid; generate a new long-lived token"
        elif code == 200:
            hint = f" - missing permission; the token needs these scopes: {SCOPES}"
        elif code == 324:
            hint = (
                " - Instagram could not fetch the image. Check that "
                "PUBLIC_ASSET_BASE_URL is publicly reachable over https."
            )
        elif code == 9007:
            hint = " - the container expired before publish; re-run the publish"
        elif code == MEDIA_FETCH_ERROR_CODE and detail == MEDIA_FETCH_ERROR_SUBCODE:
            # Meta mislabels a failed image download as a media-type error, so
            # point at the URL rather than at the media_type parameter.
            hint = (
                " - Instagram could not download the image. The URL must be"
                " publicly reachable over https and serve a JPEG at exactly that"
                " path; a 404 or a non-JPEG body surfaces as this error."
            )

        suffix = f" (subcode {detail})" if detail else ""
        # error_user_msg carries the offending URI, which is the one piece of
        # this failure that actually localises the problem.
        user_msg = err.get("error_user_msg") or ""
        extra = f" [{user_msg}]" if user_msg else ""
        return f"Graph API error {code}{suffix}: {message}{hint}{extra}"

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        data: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        """Call the Graph API and surface Meta's error message verbatim.

        Meta returns structured errors with a 200 as often as a 4xx, so the
        JSON body is the source of truth, not the HTTP status code. The access
        token rides the query string for GETs and the form body for POSTs.
        """
        if not self.config.is_configured:
            raise PublishError(
                "Instagram is not configured. Set IG_USER_ID and IG_ACCESS_TOKEN."
            )
        url = f"{GRAPH_BASE}/{endpoint.lstrip('/')}"
        token = {"access_token": self.config.access_token}
        try:
            if method == "GET":
                response = self.client.get(
                    url, params={**token, **(params or {})}
                )
            else:
                response = self.client.post(
                    url, data={**token, **(data or {})}, params=params
                )
        except httpx.HTTPError as exc:
            raise PublishError(f"network error calling {url}: {exc}") from exc

        try:
            body = response.json()
        except json.JSONDecodeError:
            if response.status_code >= 400:
                raise PublishError(
                    f"Graph API HTTP {response.status_code}: {response.text[:300]}"
                ) from None
            raise PublishError(f"non-JSON response from {url}") from None

        explained = self._explain(body)
        if explained:
            raise PublishError(explained)
        if response.status_code >= 400:
            raise PublishError(
                f"Graph API HTTP {response.status_code}: {response.text[:300]}"
            )
        return body

    def _post(self, endpoint: str, data: dict, params: dict | None = None) -> dict:
        return self._request("POST", endpoint, data=data, params=params)

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        return self._request("GET", endpoint, params=params)

    def image_urls(self, draft: Draft) -> list[str]:
        """URL of every slide of `draft`, in order, as the site serves them.

        A pure function of the base URL and the fingerprint. The renderer writes
        `<fingerprint>/<fingerprint>_NN.png` under the output directory and the
        Pages branch keeps that same layout with `.jpg`, so there is nothing to
        stage, convert, upload or wait on - the URL is already correct before
        this method is called.
        """
        base = (self.settings.assets.base_url or "").rstrip("/")
        if not base:
            raise PublishError(
                "PUBLIC_ASSET_BASE_URL is not set. Instagram downloads each "
                "slide from a public URL, so set it to the root of the site "
                "serving them, e.g. https://<owner>.github.io/<repo>."
            )
        if not draft.slides:
            raise PublishError(
                f"{draft.fingerprint} has no slides; render it before publishing"
            )
        stem = draft.fingerprint
        return [
            f"{base}/{stem}/{stem}_{n:02d}.jpg"
            for n in range(1, len(draft.slides) + 1)
        ]

    # ── account helpers ──────────────────────────────────────────────────

    def resolve_user_id(self) -> str:
        """Return the configured IG user id.

        With the Instagram API (Instagram Login) the account id comes directly
        from the app dashboard alongside the token - there is no Facebook Page
        involved, so no lookup is needed or possible.
        """
        if self.config.user_id:
            return self.config.user_id
        raise PublishError(
            "Set IG_USER_ID to the Instagram account id shown in the app "
            "dashboard next to your token."
        )

    def account_info(self) -> dict:
        return self._get(
            f"{self.resolve_user_id()}",
            params={"fields": "id,username,followers_count,media_count"},
        )

    def check_token(self) -> dict:
        """Validate the configured token against the /me endpoint.

        Returns the IG user id and username the token belongs to, which is the
        fastest way to confirm a token pasted from the app dashboard actually
        works and matches the account you intend to publish to.
        """
        if not self.config.access_token:
            raise PublishError(
                "IG_ACCESS_TOKEN is not set - generate a token from the "
                "Instagram product page in the Meta app dashboard."
            )
        return self._get("me", params={"fields": "user_id,username"})

    # ── publishing ───────────────────────────────────────────────────────

    def _caption(self, draft: Draft) -> str:
        # full_caption already applies the hashtag cap, so this is the exact
        # text Instagram will receive and the only length that matters.
        caption = draft.full_caption
        # Instagram silently truncates past 2200 chars; fail loudly instead so
        # a mangled caption is never published.
        if len(caption) > MAX_CAPTION_CHARS:
            raise PublishError(
                f"caption is {len(caption)} chars, over Instagram's 2200 limit - "
                "shorten it in the draft before publishing"
            )
        return caption

    def _create_container(self, data: dict) -> dict:
        """POST a container, retrying Instagram's flaky image-download failure.

        Meta returns 9004/2207052 ("Only photo or video can be accepted as media
        type") when its fetcher could not pull the `image_url`, and its own
        developer forums show the same URL succeeding on a later attempt, so a
        couple of retries convert a failed publish into a successful one. A
        failed attempt creates no container id, so retrying cannot leak one.
        """
        endpoint = f"{self.resolve_user_id()}/media"
        for attempt in range(1, MEDIA_FETCH_RETRY_ATTEMPTS + 1):
            try:
                return self._post(endpoint, data)
            except PublishError as exc:
                if not _is_media_fetch_error(exc) or attempt == MEDIA_FETCH_RETRY_ATTEMPTS:
                    raise
                log.warning(
                    "Instagram could not download %s (attempt %d/%d): %s",
                    data.get("image_url", "the media"),
                    attempt, MEDIA_FETCH_RETRY_ATTEMPTS, exc,
                )
                time.sleep(MEDIA_FETCH_RETRY_DELAY_SECONDS * attempt)
        raise AssertionError("unreachable: retry loop always returns or raises")

    def create_item_container(self, image_url: str) -> str:
        """Create a single carousel item container (no caption of its own)."""
        body = self._create_container(
            {"image_url": image_url, "is_carousel_item": "true"}
        )
        creation_id = body.get("id")
        if not creation_id:
            raise PublishError(f"container creation returned no id: {body}")
        return str(creation_id)

    def create_carousel_container(self, item_ids: list[str], caption: str) -> str:
        """Create the CAROUSEL parent container referencing the item containers."""
        body = self._post(
            f"{self.resolve_user_id()}/media",
            {
                "media_type": "CAROUSEL",
                "children": ",".join(item_ids),
                "caption": caption,
            },
        )
        creation_id = body.get("id")
        if not creation_id:
            raise PublishError(f"carousel container creation returned no id: {body}")
        return str(creation_id)

    def publish_carousel(self, draft: Draft) -> PublishResult:
        """Publish a multi-image carousel. Cleans up partial state on failure."""
        urls = self.image_urls(draft)
        if len(urls) > MAX_CAROUSEL_ITEMS:
            log.warning(
                "carousel has %d images, over the %d limit - publishing the first %d",
                len(urls), MAX_CAROUSEL_ITEMS, MAX_CAROUSEL_ITEMS,
            )
            urls = urls[:MAX_CAROUSEL_ITEMS]
        if len(urls) == 1:
            return self.publish_single_image(draft)

        user_id = self.resolve_user_id()
        caption = self._caption(draft)
        created: list[str] = []

        try:
            for url in urls:
                # IG needs a moment between container creations; going faster
                # is a reliable way to get 429s.
                time.sleep(1.0)
                created.append(self.create_item_container(url))
            log.info("created %d carousel item containers", len(created))

            carousel_id = self.create_carousel_container(created, caption)
            created.append(carousel_id)
            log.info("created carousel container %s", carousel_id)

            body = self._post(
                f"{user_id}/media_publish",
                {"creation_id": carousel_id},
            )
            media_id = str(body.get("id", ""))
            if not media_id:
                raise PublishError(f"publish returned no media id: {body}")
            return PublishResult(media_id=media_id, permalink=self.permalink(media_id), raw=body)
        except Exception:
            self._discard(created)
            raise

    def publish_single_image(self, draft: Draft) -> PublishResult:
        user_id = self.resolve_user_id()
        url = self.image_urls(draft)[0]
        caption = self._caption(draft)
        body = self._create_container({"image_url": url, "caption": caption})
        container = str(body.get("id", ""))
        if not container:
            raise PublishError(f"media creation returned no id: {body}")

        published = self._post(f"{user_id}/media_publish", {"creation_id": container})
        media_id = str(published.get("id", ""))
        if not media_id:
            raise PublishError(f"publish returned no media id: {published}")
        return PublishResult(media_id=media_id, permalink=self.permalink(media_id), raw=published)

    def _discard(self, creation_ids: list[str]) -> None:
        """Best-effort cleanup of containers created by a failed run.

        The IG Login API does not support deleting containers, so this attempts
        the DELETE anyway and warns when it fails - the containers expire by
        themselves within 24 hours regardless, so a warning beats a hard error.
        """
        for creation_id in creation_ids:
            try:
                self.client.delete(
                    f"{GRAPH_BASE}/{creation_id}",
                    params={"access_token": self.config.access_token},
                )
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                log.warning(
                    "could not clean up container %s (expires within 24h anyway): %s",
                    creation_id, exc,
                )

    def permalink(self, media_id: str) -> str:
        """Fetch the short permalink; a missing one is not worth failing over."""
        try:
            body = self._get(media_id, params={"fields": "permalink"})
            return str(body.get("permalink", ""))
        except PublishError as exc:
            log.debug("permalink lookup failed: %s", exc)
            return ""

    def publish_draft(self, draft: Draft) -> PublishResult:
        """Publish a draft, using a carousel when it has 2+ slides.

        Nothing is looked up on disk: the slides are already on the gh-pages
        site, so the URLs are derived from the fingerprint alone.
        """
        if len(draft.slides) >= 2:
            return self.publish_carousel(draft)
        return self.publish_single_image(draft)

    # ── insights ─────────────────────────────────────────────────────────

    def insights(self, media_id: str) -> dict:
        """Read performance metrics for a published post.

        Reach is only available for posts at least 24h old; requesting it
        sooner returns an error, so we treat metrics as best-effort.
        """
        try:
            return self._get(
                f"{media_id}/insights",
                params={
                    "metric": "impressions,reach,likes,comments,saved,shares,total_interactions",
                    "period": "day",
                },
            )
        except PublishError as exc:
            log.warning("insights unavailable for %s: %s", media_id, exc)
            return {}
