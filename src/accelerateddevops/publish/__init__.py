"""Publishing: the Instagram Graph API, and nothing else.

Where the slides are hosted is deliberately not part of this package. They sit
on the gh-pages site already, and publishing only needs to turn
`PUBLIC_ASSET_BASE_URL` plus a fingerprint into the URLs Instagram downloads.
"""

from __future__ import annotations

from .errors import PublishError
from .instagram import (
    SCOPES,
    InstagramPublisher,
    PublishResult,
)

__all__ = [
    "SCOPES",
    "InstagramPublisher",
    "PublishError",
    "PublishResult",
]
