"""Publishing: Instagram Graph API + image hosting for the slides it fetches."""

from __future__ import annotations

from .errors import PublishError
from .instagram import (
    SCOPES,
    InstagramPublisher,
    PublishResult,
)
from .storage import (
    AssetStore,
    LocalAssetStore,
    PagesAssetStore,
    build_asset_store,
)

__all__ = [
    "SCOPES",
    "AssetStore",
    "InstagramPublisher",
    "LocalAssetStore",
    "PagesAssetStore",
    "PublishError",
    "PublishResult",
    "build_asset_store",
]
