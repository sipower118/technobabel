"""Asset storage tests: the local folder backend and the Pages backend.

Instagram downloads each image from a public URL while it creates a container,
so an image must be live at that URL *before* the API call. These tests pin
that contract for both backends against a mocked Pages CDN (including the "not
live yet" case, which is the failure mode that produces 9004/2207052).

The app has no write credential and never talks to the GitHub API: staging is
all it does, and a test asserts that the reachability check is anonymous.
"""

import shutil
from pathlib import Path

import _paths  # noqa: F401  (sys.path + chdir bootstrap)
import httpx
from PIL import Image

from accelerateddevops.config import AssetConfig, InstagramConfig, Settings
from accelerateddevops.publish import (
    LocalAssetStore,
    PagesAssetStore,
    PublishError,
    build_asset_store,
)

DATA_DIR = Path("data/_storagetest")
OUTPUT = DATA_DIR / "output"
FINGERPRINT = "abc123def456"
SLIDES = OUTPUT / FINGERPRINT
BASE = "https://owner.github.io/repo"

shutil.rmtree(DATA_DIR, ignore_errors=True)
SLIDES.mkdir(parents=True)


def make_settings(storage="local", base=BASE, **overrides) -> Settings:
    assets = AssetConfig(
        storage=storage,
        base_url=base,
        local_dir=DATA_DIR / "site",
        **overrides,
    )
    return Settings(
        data_dir=DATA_DIR,
        instagram=InstagramConfig("1789", "tok", None),
        assets=assets,
    )


def ok(msg: str) -> None:
    print("OK   " + msg)


def jpeg(name: str = "a_01.jpg", color=(10, 20, 30)) -> Path:
    path = SLIDES / name
    Image.new("RGB", (8, 8), color).save(path, "JPEG")
    return path


# ── 1. local backend stages the file and keeps the render layout ──────────
store = build_asset_store(make_settings("local"))
image = jpeg()
url = store.publish(image)
expected_url = f"{BASE}/{FINGERPRINT}/a_01.jpg"
assert url == expected_url, f"got {url}, expected {expected_url}"
staged = DATA_DIR / "site" / FINGERPRINT / "a_01.jpg"
assert staged.is_file(), f"the JPEG must be staged in ASSET_LOCAL_DIR, missing {staged}"
assert staged.read_bytes() == image.read_bytes(), "staged file must be a byte copy"
ok("local: stages the JPEG under ASSET_LOCAL_DIR preserving the layout")

# Staging is idempotent: re-publishing the same image must not fail.
assert store.publish(image) == expected_url
ok("local: republishing the same image is a no-op")

# A file outside the render output still gets a URL, with a warning, rather
# than a silent 404.
outside = DATA_DIR / "elsewhere.jpg"
Image.new("RGB", (4, 4), (1, 2, 3)).save(outside, "JPEG")
assert store.publish(outside) == f"{BASE}/elsewhere.jpg"
assert (DATA_DIR / "site" / "elsewhere.jpg").is_file()
ok("local: a file outside the render dir falls back to the bare name")


# ── 2. a missing PUBLIC_ASSET_BASE_URL fails loudly ──────────────────────
try:
    build_asset_store(make_settings("local", base="")).publish(jpeg("b_01.jpg"))
    raise SystemExit("FAIL: expected PublishError")
except PublishError as exc:
    assert "PUBLIC_ASSET_BASE_URL" in str(exc), str(exc)
ok("guard: refuses to build a URL with no public base")


# ── 3. an unknown ASSET_STORAGE is rejected, not silently defaulted ──────
try:
    build_asset_store(make_settings("s3"))
    raise SystemExit("FAIL: expected PublishError")
except PublishError as exc:
    assert "local" in str(exc) and "pages" in str(exc), str(exc)
ok("guard: unknown ASSET_STORAGE names the valid values")


# ── 4. the app holds no write credential and calls no GitHub API ──────────
import accelerateddevops.publish.storage as storage_module

source = Path(storage_module.__file__).read_text(encoding="utf-8")
for banned in ("api.github.com", "GITHUB_TOKEN", "github_token", "GITHUB_REPOSITORY"):
    assert banned not in source, f"storage.py must not reference {banned}"
assert not [f for f in dir(storage_module) if "GitHub" in f], (
    "no GitHub-API surface should remain in the storage module"
)
ok("pages: the storage module has no GitHub API surface at all")


# ── mocked Pages CDN ─────────────────────────────────────────────────────
def make_pages_store(recorded, *, site_responses=None, poll_seconds=0.0,
                     settle_seconds=5):
    """A Pages store wired to a mock CDN.

    site_responses: list of status codes returned by successive site GETs.
    A site that never goes 200 exercises the timeout path.
    """
    site_calls = {"n": 0}
    site_responses = site_responses if site_responses is not None else [200]

    def route(request: httpx.Request) -> httpx.Response:
        n = site_calls["n"]
        site_calls["n"] += 1
        recorded.append(("site", request.url.host, request.url.path,
                         request.headers.get("authorization", "")))
        code = site_responses[min(n, len(site_responses) - 1)]
        return httpx.Response(code, content=b"\xff\xd8jpeg-bytes" if code == 200 else b"nope")

    store = PagesAssetStore(
        base_url=BASE,
        source_root=OUTPUT,
        local_dir=DATA_DIR / "pages",
        branch="gh-pages",
        settle_seconds=settle_seconds,
        poll_seconds=poll_seconds,
        client=httpx.Client(transport=httpx.MockTransport(route)),
    )
    return store, site_calls


# ── 5. pages: stages the JPEG, then polls until the CDN serves it ────────
recorded: list = []
store, site_calls = make_pages_store(recorded, site_responses=[404, 404, 200])
url = store.publish(jpeg("c_01.jpg"))
assert url == f"{BASE}/{FINGERPRINT}/c_01.jpg", url

staged = DATA_DIR / "pages" / FINGERPRINT / "c_01.jpg"
assert staged.is_file(), f"the JPEG must be staged for deploy, missing {staged}"
assert staged.read_bytes() == (SLIDES / "c_01.jpg").read_bytes(), "staged byte copy"
assert site_calls["n"] == 3, f"must poll until the site is live, got {site_calls['n']} site GETs"
ok("pages: stages the JPEG then polls until the site actually serves it")

# Every request is an anonymous GET of the public URL - the same one Instagram
# makes. No Authorization header, and nothing aimed at api.github.com.
assert recorded and all(r[0] == "site" for r in recorded), recorded
assert all(r[1] == "owner.github.io" for r in recorded), recorded
assert all(r[3] == "" for r in recorded), f"no credential may be sent: {recorded}"
ok("pages: the reachability check is anonymous and never touches the API")


# ── 6. staging is the app's whole job: no branch, no API, no credential ───
assert not hasattr(store, "token"), "the store must not hold a token"
assert not hasattr(store, "repository"), "the store must not hold a repository"
deploy_side_effects = [
    name for name in dir(store)
    if any(word in name.lower() for word in ("upload", "_api", "_ensure", "_sha", "_commit"))
]
assert not deploy_side_effects, f"deploying is not the app's job: {deploy_side_effects}"
ok("pages: no credential, no branch creation and no upload method on the store")


# ── 7. a site that never goes live fails with the fix, not silently
recorded = []
store, _ = make_pages_store(recorded, site_responses=[404], settle_seconds=1)
try:
    store.publish(jpeg("e_01.jpg"))
    raise SystemExit("FAIL: expected PublishError when the site never goes live")
except PublishError as exc:
    msg = str(exc)
    assert "not publicly readable" in msg, msg
    assert "gh-pages" in msg, f"the message must name the branch, got: {msg}"
    assert "PUBLIC_ASSET_BASE_URL" in msg, f"the message must point at the base URL, got: {msg}"
    assert "render workflow" in msg, f"the message must name the deploying step, got: {msg}"
ok("pages: a site that never goes live fails with an actionable message")


# ── 8. both modes build with no GitHub settings at all ───────────────────
local = build_asset_store(make_settings("local"))
assert isinstance(local, LocalAssetStore)
assert not isinstance(local, PagesAssetStore)
pages = build_asset_store(make_settings("pages"))
assert isinstance(pages, PagesAssetStore), type(pages)
ok("both backends build from a public base URL alone, with no credentials")


# ── 9. upload-assets stages without waiting for a deploy that hasn't run ──
from accelerateddevops.publish.storage import publish_rendered  # noqa: E402

# The PNGs are what the renderer writes; the JPEGs are what gets staged. This
# is the render workflow's shape: render, then stage, then deploy.
Image.new("RGB", (8, 8), (3, 4, 5)).save(SLIDES / "render_01.png")
Image.new("RGB", (8, 8), (6, 7, 8)).save(SLIDES / "render_02.png")

urls = publish_rendered(Settings(
    data_dir=DATA_DIR,
    instagram=InstagramConfig("1789", "tok", None),
    assets=AssetConfig(storage="pages", base_url=BASE,
                       local_dir=DATA_DIR / "deploy"),
), FINGERPRINT)
assert urls, "staging must return the URLs the slides will live at"
rendered = [u for u in urls if "/render_" in u]
assert len(rendered) == 2, f"both rendered PNGs must be staged, got {urls}"
assert all(u.startswith(f"{BASE}/{FINGERPRINT}/") for u in urls), urls
assert all(u.endswith(".jpg") for u in urls), urls
for index in (1, 2):
    staged = DATA_DIR / "deploy" / FINGERPRINT / f"render_{index:02d}.jpg"
    assert staged.is_file(), (
        f"upload-assets must stage into ASSET_LOCAL_DIR so the workflow can "
        f"commit it, missing {staged}"
    )
ok("upload-assets: stages and reports URLs without waiting on the deploy")

shutil.rmtree(DATA_DIR, ignore_errors=True)
print("\nASSET STORAGE TESTS PASSED")
