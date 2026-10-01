"""Configuration loading from environment / .env with sane defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


def _split(value: str | None) -> list[str]:
    """Split a comma-separated env var into a clean, non-empty list."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _float(value: str | None, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class InstagramConfig:
    """Credentials for the official Instagram Graph API."""

    user_id: str
    access_token: str
    page_id: str | None

    @property
    def is_configured(self) -> bool:
        return bool(self.user_id and self.access_token)


@dataclass(frozen=True)
class AssetConfig:
    """Where the rendered slides are served from.

    Instagram downloads every slide from a public URL itself, so the app needs
    one thing: the root of the site that already has them - the gh-pages site,
    e.g. https://user.github.io/repo. The rest of the layout is fixed: a slide
    lives at `<base>/<fingerprint>/<fingerprint>_NN.jpg`, mirroring where the
    renderer wrote it. Staging, backends and deploys are not configuration
    here, because none of them are this app's job.
    """

    # Public root every image URL is built from, e.g. https://user.github.io/repo
    base_url: str = ""


@dataclass(frozen=True)
class Settings:
    """Everything the pipeline needs, resolved once at startup."""

    gemini_api_keys: list[str] = field(default_factory=list)
    gemini_models: list[str] = field(default_factory=list)

    instagram: InstagramConfig = field(
        default_factory=lambda: InstagramConfig("", "", None)
    )

    assets: AssetConfig = field(default_factory=AssetConfig)

    brand_name: str = "Accelerated DevOps"
    brand_handle: str = "@accelerated.devops"
    brand_tagline: str = "Platform engineering, decoded."

    lookback_hours: int = 72
    hn_lookback_hours: int = 720
    min_hn_points: int = 60
    min_reddit_comments: int = 40
    posts_per_run: int = 2

    # Reddit OAuth. Both are required together; with neither, the collector
    # falls back to the public .json endpoints (usually 403 on a datacentre IP).
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    source_contact_email: str = ""
    max_carousel_slides: int = 8
    hook_intensity: float = 0.55

    monetization_enabled: bool = False
    cta_text: str = ""
    affiliate_url: str = ""

    publish_slots: list[str] = field(default_factory=lambda: ["09:15", "17:45"])

    data_dir: Path = Path("data")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "accelerated_devops.sqlite3"

    @property
    def drafts_dir(self) -> Path:
        return self.data_dir / "drafts"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "output"

    @property
    def has_llm(self) -> bool:
        return bool(self.gemini_api_keys and self.gemini_models)

    @property
    def has_reddit_oauth(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    @property
    def can_publish(self) -> bool:
        """True when a publish could build a URL for every slide."""
        return self.instagram.is_configured and bool(self.assets.base_url)

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.drafts_dir, self.output_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings(env_file: str | os.PathLike[str] | None = ".env") -> Settings:
    """Build a Settings object from the environment, loading .env first."""
    load_dotenv(dotenv_path=env_file, override=False)

    instagram = InstagramConfig(
        user_id=os.getenv("IG_USER_ID", "").strip(),
        access_token=os.getenv("IG_ACCESS_TOKEN", "").strip(),
        page_id=(os.getenv("PAGE_ID", "").strip() or None),
    )

    assets = AssetConfig(
        base_url=os.getenv("PUBLIC_ASSET_BASE_URL", "").strip().rstrip("/"),
    )

    return Settings(
        gemini_api_keys=_split(os.getenv("GEMINI_API_KEYS")),
        gemini_models=_split(os.getenv("GEMINI_MODELS"))
        or ["gemini-3.8-flash","gemini-3.7-flash","gemini-3.6-flash","gemini-3.5-flash-lite"],
        instagram=instagram,
        assets=assets,
        brand_name=os.getenv("BRAND_NAME", "Accelerated DevOps").strip(),
        brand_handle=os.getenv("BRAND_HANDLE", "@accelerated.devops").strip(),
        brand_tagline=os.getenv("BRAND_TAGLINE", "Platform engineering, decoded.").strip(),
        lookback_hours=_int(os.getenv("LOOKBACK_HOURS"), 72),
        hn_lookback_hours=_int(os.getenv("HN_LOOKBACK_HOURS"), 720),
        min_hn_points=_int(os.getenv("MIN_HN_POINTS"), 60),
        min_reddit_comments=_int(os.getenv("MIN_REDDIT_COMMENTS"), 40),
        reddit_client_id=os.getenv("REDDIT_CLIENT_ID", "").strip(),
        reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET", "").strip(),
        source_contact_email=os.getenv("SOURCE_CONTACT_EMAIL", "").strip(),
        posts_per_run=_int(os.getenv("POSTS_PER_RUN"), 2),
        max_carousel_slides=_int(os.getenv("MAX_CAROUSEL_SLIDES"), 8),
        hook_intensity=_float(os.getenv("HOOK_INTENSITY"), 0.55),
        monetization_enabled=_bool(os.getenv("MONETIZATION_ENABLED")),
        cta_text=os.getenv("CTA_TEXT", "").strip(),
        affiliate_url=os.getenv("AFFILIATE_URL", "").strip(),
        publish_slots=_split(os.getenv("PUBLISH_SLOTS")) or ["09:15", "17:45"],
        data_dir=Path(os.getenv("DATA_DIR", "data")).expanduser(),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor so CLI entrypoints stay terse."""
    return load_settings()
