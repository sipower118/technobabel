"""Shared HTTP client + the Source interface for all collectors."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

import httpx

from ..models import SourceItem

log = logging.getLogger(__name__)

# Every outbound request identifies the project and (optionally) a contact.
# Anonymous scraping is what gets a scraper IP-blocked, so this is not optional
# politeness - it is load-bearing reliability.
USER_AGENT = (
    "AcceleratedDevOpsBot/0.1 (+https://accelerated.devops; content research bot; "
    "contact: set SOURCE_CONTACT_EMAIL in .env)"
)

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class Source(ABC):
    """Common interface for every collector (RSS, HN, Reddit, future ones).

    Subclasses implement `_fetch`; the shared `fetch` entry point owns an
    httpx client only when none was passed in, so callers keep one client
    across sources and tests can inject a mock transport without it being
    closed underneath them.
    """

    #: Short label recorded in logs and the DB ("rss", "hackernews", "reddit").
    name: str = ""

    def __init__(self) -> None:
        # Extra build_client() args, e.g. Reddit's per-app User-Agent header.
        self.client_kwargs: dict[str, object] = {}

    def fetch(self, client: httpx.Client | None = None) -> list[SourceItem]:
        """Public entry point: return normalised items, one bad item never
        aborts the whole run (each subclass logs and skips failures)."""
        owns_client = client is None
        client = client or build_client(**self.client_kwargs)
        try:
            return self._fetch(client)
        finally:
            if owns_client:
                client.close()

    @abstractmethod
    def _fetch(self, client: httpx.Client) -> list[SourceItem]: ...


def build_client(
    timeout: float = 20.0,
    contact_email: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> httpx.Client:
    headers = {"User-Agent": USER_AGENT.replace("contact: set SOURCE_CONTACT_EMAIL in .env", f"contact: {contact_email}") if contact_email else USER_AGENT}
    headers["Accept-Language"] = "en-US,en;q=0.9"
    if extra_headers:
        headers.update(extra_headers)
    return httpx.Client(
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    )


def get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, object] | None = None,
    attempts: int = 3,
    backoff: float = 1.5,
) -> httpx.Response:
    """GET with exponential backoff on transient failures."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = client.get(url, params=params)
            if response.status_code in RETRY_STATUS:
                raise httpx.HTTPStatusError(
                    f"{response.status_code} from {url}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001 - retried and re-raised below
            last_exc = exc
            if attempt == attempts:
                break
            sleep_for = backoff ** attempt
            log.debug(
                "GET %s failed (attempt %d/%d): %s - retrying in %.1fs",
                url, attempt, attempts, exc, sleep_for,
            )
            time.sleep(sleep_for)
    raise last_exc if last_exc else RuntimeError(f"unreachable: {url}")
