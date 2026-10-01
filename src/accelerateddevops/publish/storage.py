"""Where rendered slides are published so that Instagram can fetch them.

Instagram never receives the image bytes from us. It is handed a URL and
downloads it itself, over the public internet, *while it creates a container*.
So an image has to be live at its public URL before the API call - there is no
upload step, only a URL, and no way to fix it after the fact.

This module owns the *staging* half of publishing: converting the rendered PNG
to the JPEG Instagram accepts and copying it into the folder that will be
served. Two backends, selected with `ASSET_STORAGE`:

`local`
    Copy each JPEG into `ASSET_LOCAL_DIR` and leave the hosting to you: nginx,
    an S3 bucket, a CDN, or a Pages deploy of that same folder.

`pages`
    The same copy, then confirm the deployed site really serves the URL before
    handing it to Instagram.

Note what is *not* here: this app has no write credentials and never calls the
GitHub API. Getting the staged folder onto the Pages branch is a deployment
concern, and it belongs to the workflow - which already checks that branch out
to commit the pipeline's state, so it pushes the slides in the same commit.
That is what lets the pipeline run on GitHub Actions with no local HTTP server
and no tunnel: the runner is a fresh machine every time, so a localhost server
would be useless.

The public URL is always `<base>/<path relative to the render output dir>`, so
the per-fingerprint directory is part of the path. Meta answers a wrong path
with 9004/2207052 "Only photo or video can be accepted as media type", which
reads like a `media_type` bug and is not one.
"""

from __future__ import annotations

import logging
import shutil
import time
from abc import ABC, abstractmethod
from pathlib import Path

import httpx
from PIL import Image

from ..config import Settings
from ..models import Draft
from .errors import PublishError

log = logging.getLogger(__name__)

# A deploy to the Pages branch is not public the instant it lands: the CDN picks
# it up within seconds, sometimes a minute. Instagram fetches the URL during
# container creation, so fetching too early is a 404 that Meta reports as
# 9004/2207052. Poll until the site really serves the file instead.
PAGES_POLL_SECONDS = 3.0


def to_jpeg(path: str | Path) -> Path:
    """Convert a rendered PNG slide to a JPEG sibling and return it.

    Instagram accepts only JPEG for image posts; the renderer deliberately
    writes PNG (lossless, keeps alpha) for review. So publishing works on a
    `.jpg` written next to the PNG - same directory, same public path, only
    the extension differs.

    A JPEG with no PNG beside it is returned as-is: that is a slide restored
    from the Pages artefact, where only the JPEG was ever kept.
    """
    src = Path(path)
    if src.suffix.lower() in (".jpg", ".jpeg"):
        return src
    dst = src.with_suffix(".jpg")
    if not src.exists():
        if dst.exists():
            return dst
        raise PublishError(f"no slide to publish: neither {src} nor {dst} exists")
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    with Image.open(src) as img:
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        else:
            img = img.convert("RGB")
        img.save(dst, "JPEG", quality=92, optimize=True)
    return dst


class AssetStore(ABC):
    """Publishes rendered images to a public location Instagram can fetch.

    Subclasses decide *where* the bytes go; the URL layout and the "is it
    configured" guard live here so both backends produce identical URLs.
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    @property
    def base_url(self) -> str:
        """Public root that every image URL is built from."""
        return self._base_url

    def public_path(self, image: Path) -> str:
        """URL of `image`, whether or not it has been published yet."""
        if not self._base_url:
            raise PublishError(
                "PUBLIC_ASSET_BASE_URL is not set. Instagram fetches images from a "
                "public URL, so the rendered slides have to be hosted somewhere the "
                "internet can reach - a GitHub Pages site, a bucket, a CDN. Set the "
                "variable to that root, e.g. https://<owner>.github.io/<repo>."
            )
        return f"{self._base_url}/{self.relpath(image)}"

    @abstractmethod
    def relpath(self, image: Path) -> str:
        """Path of `image` under `base_url`, mirroring the served layout."""

    @abstractmethod
    def stage(self, image: Path) -> Path:
        """Put `image` where it will be served from, and return that copy."""

    @abstractmethod
    def publish(self, image: Path) -> str:
        """Make `image` fetchable, and return the URL it is fetchable at."""


class LocalAssetStore(AssetStore):
    """Stages images in a local folder; hosting them is someone else's job."""

    def __init__(self, base_url: str, source_root: Path, local_dir: Path) -> None:
        super().__init__(base_url)
        self.source_root = source_root
        self.local_dir = local_dir

    def relpath(self, image: Path) -> str:
        """Path of `image` under the local asset folder, preserving layout.

        The fingerprint directory has to survive: a basename-only URL 404s,
        and Meta reports that 404 as a media-type error. The JPEG sibling lives
        next to the PNG it was converted from, so the on-disk path under
        `source_root` is preserved verbatim.
        """
        target = image.resolve()
        root = self.source_root.resolve()
        try:
            return target.relative_to(root).as_posix()
        except ValueError:
            # Outside the render output (a hand-picked file, a custom data dir).
            # Fall back to the file name, but say so rather than 404 silently.
            log.warning(
                "%s is outside the render directory %s; publishing it as %s",
                target, root, target.name,
            )
            return target.name

    def stage(self, image: Path) -> Path:
        """Copy `image` into the local asset folder and return the copy."""
        target = self.local_dir / self.relpath(image)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.stat().st_mtime < image.stat().st_mtime:
            shutil.copy2(image, target)
        return target

    def publish(self, image: Path) -> str:
        self.stage(image)
        return self.public_path(image)


class PagesAssetStore(LocalAssetStore):
    """Stages images for the GitHub Pages site and checks the site serves them.

    The deploy is deliberately *not* this class's job. The workflow already
    checks the Pages branch out to commit the pipeline's state, so it pushes the
    staged slides in the same commit and needs no extra credential: the one it
    gets from `actions/checkout` covers both. What this class adds over the
    local backend is the one guarantee Instagram depends on - that the URL is
    publicly readable *before* it is handed over.

    The reachability check is an anonymous GET, exactly the request Instagram
    itself will make. No token is involved at any point.

    One-time repo setup: Settings -> Pages -> Deploy from a branch ->
    `gh-pages` / `/ (root)`.
    """

    def __init__(
        self,
        base_url: str,
        source_root: Path,
        local_dir: Path,
        *,
        branch: str = "gh-pages",
        settle_seconds: int = 90,
        poll_seconds: float = PAGES_POLL_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(base_url, source_root, local_dir)
        self.branch = branch
        self.settle_seconds = settle_seconds
        self.poll_seconds = poll_seconds
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30.0)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def publish(self, image: Path) -> str:
        """Stage the image, then block until its public URL really serves it."""
        self.stage(image)
        url = self.public_path(image)
        self._await_live(url)
        return url

    def _await_live(self, url: str) -> None:
        """Block until the Pages site serves the file, or fail with the fix.

        Polls anonymously, the way Instagram will. A Pages deployment is not
        public the instant its commit lands, so publishing before the CDN has
        picked it up is the same 404 Meta reports as 9004/2207052.
        """
        deadline = time.monotonic() + self.settle_seconds
        last = "no response"
        while time.monotonic() < deadline:
            try:
                response = self.client.get(url, follow_redirects=True)
                if response.status_code == 200:
                    log.info("pages serving %s", url)
                    return
                last = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last = str(exc)
            time.sleep(self.poll_seconds)
        raise PublishError(
            f"{url} is not publicly readable after {self.settle_seconds}s "
            f"({last}). The slides have to be deployed to the {self.branch} "
            "branch before publishing - run the render workflow, or push the "
            "staged assets yourself. Then check that Pages is enabled with "
            f"source branch '{self.branch}' and root '/', and that "
            "PUBLIC_ASSET_BASE_URL points at the deployed site."
        )


def fetch_slides(settings: Settings, draft: Draft) -> list[Path]:
    """Download a draft's published JPEGs so it can be published from a clean runner.

    Only the JPEG siblings are kept on the Pages branch, not the source PNGs,
    so a workflow that has just restored the database has no images of its own.
    Fetching them back is a public GET per slide, and it means the bytes we
    publish are byte-for-byte the bytes Instagram will later download.

    Args:
        settings: Resolved settings; supplies the asset backend and output dir.
        draft: The draft whose slides are needed.

    Returns:
        The local JPEG paths, in slide order.

    Raises:
        PublishError: If any slide is missing from the published site.
    """
    store = build_asset_store(settings)
    paths: list[Path] = []
    for slide in draft.slides:
        name = f"{draft.fingerprint}_{slide.index:02d}"
        target = settings.output_dir / draft.fingerprint / f"{name}.jpg"
        url = store.public_path(target)
        try:
            response = httpx.get(url, timeout=30.0, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PublishError(
                f"could not fetch the published slide {url}: {exc}. Run the render "
                "workflow (or `accelerated-devops upload-assets`) first."
            ) from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.content)
        log.info("fetched %s", url)
        paths.append(target)
    return paths


def publish_rendered(settings: Settings, fingerprint: str | None = None) -> list[str]:
    """Stage the rendered slides for the asset backend and return their URLs.

    Used by the render workflow so a render can be reviewed on the Pages site
    before anything is posted to Instagram. The rendered PNGs stay local; only
    the JPEG siblings are staged, since those are all Instagram ever sees.

    Deliberately a staging step, not a publish one. `stage` plus `public_path`
    is all this needs: on Actions the caller points ASSET_LOCAL_DIR at the
    artefact checkout and commits it afterwards, so waiting here for the URL to
    go live would be waiting on a deploy that has not been pushed yet.

    Args:
        settings: Resolved settings; supplies the asset backend and output dir.
        fingerprint: Restrict to one draft. None stages every render.

    Returns:
        One public URL per staged slide.

    Raises:
        PublishError: If the backend is misconfigured or nothing is rendered.
    """
    store = build_asset_store(settings)
    root = settings.output_dir / fingerprint if fingerprint else settings.output_dir
    if not root.exists():
        raise PublishError(f"nothing rendered at {root}")
    images = [to_jpeg(png) for png in sorted(root.rglob("*.png"))]
    if not images:
        raise PublishError(f"no rendered slides found under {root}")
    for image in images:
        store.stage(image)
    # The URL comes from the rendered path, not the staged copy: the layout is
    # mirrored relative to the output dir, and a staged file lives in
    # ASSET_LOCAL_DIR instead.
    return [store.public_path(image) for image in images]


def build_asset_store(settings: Settings) -> AssetStore:
    """Build the asset backend named by `ASSET_STORAGE`."""
    assets = settings.assets
    common: dict = {
        "base_url": assets.base_url,
        "source_root": settings.output_dir,
        "local_dir": assets.local_dir,
    }
    if assets.is_pages:
        return PagesAssetStore(
            **common,
            branch=assets.github_branch,
            settle_seconds=assets.pages_settle_seconds,
        )
    if assets.storage != "local":
        raise PublishError(
            f"unknown ASSET_STORAGE={assets.storage!r} - use 'local' or 'pages'"
        )
    return LocalAssetStore(**common)
