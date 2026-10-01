"""Instagram publisher tests against a mock Graph API (Instagram Login).

Verifies the carousel three-phase flow (item containers -> CAROUSEL parent ->
media_publish), the URL layout that mirrors the render output on gh-pages,
best-effort cleanup, the missing-base-URL guard, caption limits, and error
message translation. No image file is ever touched: the slides are already
hosted, so publishing only ever hands Meta a URL.
"""

import re
from pathlib import Path

import httpx


import _paths  # noqa: F401  (sys.path + chdir bootstrap)
from accelerateddevops.config import AssetConfig, InstagramConfig, Settings
from accelerateddevops.models import Draft, Slide, SourceItem
from accelerateddevops.publish import InstagramPublisher, PublishError

DATA_DIR = Path("data/_pubtest")
BASE = "https://cdn.example.com/out"


def make_settings(base=BASE):
    return Settings(
        data_dir=DATA_DIR,
        instagram=InstagramConfig(user_id="1789", access_token="tok", page_id=None),
        assets=AssetConfig(base_url=base),
    )


def ok(m):
    print("OK   " + m)


def form_of(request: httpx.Request) -> dict:
    from urllib.parse import unquote_plus

    form = {}
    for pair in (request.content.decode() or "").split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            form[unquote_plus(k)] = unquote_plus(v)
    return form


def handler(requests, fail_on=None, permalink=True):
    """Mock transport: records calls, optionally fails one of them."""
    calls = requests

    def route(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, form_of(request)))
        path = request.url.path
        form = form_of(request)

        # Carousel item creation (image_url + is_carousel_item).
        if request.method == "POST" and path.endswith("/media") and "image_url" in form:
            if fail_on == "third_container":
                n = len([c for c in calls if c[0] == "POST" and c[1].endswith("/media")])
                if n >= 3:
                    return httpx.Response(200, json={
                        "error": {"message": "Image not accessible", "code": 324}
                    })
            return httpx.Response(200, json={"id": f"container{len(calls)}"})

        # Carousel parent container (media_type=CAROUSEL + children).
        if request.method == "POST" and path.endswith("/media") and form.get("media_type") == "CAROUSEL":
            return httpx.Response(200, json={"id": "parent"})

        if request.method == "POST" and path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "17999900000000001"})

        if request.method == "GET" and path.endswith("/insights"):
            return httpx.Response(200, json={"data": [{"name": "reach", "values": [{"value": 500}]}]})

        if request.method == "GET" and path.endswith("/me"):
            return httpx.Response(200, json={"user_id": "1789", "username": "accelerateddevops"})

        # permalink lookup: GET /{media-id}
        if permalink and request.method == "GET" and path.split("/")[-1].isdigit():
            return httpx.Response(200, json={"id": path.split("/")[-1],
                                             "permalink": "https://www.instagram.com/p/ABC/"})

        return httpx.Response(200, json={})

    return httpx.MockTransport(route)


def make_draft(n_images=3, caption="A caption"):
    item = SourceItem(source="rss:x", external_id="1", title="T", url="https://e.com/1")
    return Draft(
        source=item, caption=caption,
        slides=[Slide(i + 1, "cover" if i == 0 else "bullets", f"H{i}") for i in range(n_images)],
    )


# ── 1. happy path: N items + 1 CAROUSEL parent + 1 publish ────────────────
calls = []
s = make_settings()
draft = make_draft(3)
fp = draft.fingerprint
client = httpx.Client(transport=handler(calls))
with InstagramPublisher(s, client=client) as pub:
    res = pub.publish_draft(draft)

items = [c for c in calls if c[0] == "POST" and c[1].endswith("/media") and "image_url" in c[2]]
parents = [c for c in calls if c[0] == "POST" and c[1].endswith("/media") and c[2].get("media_type") == "CAROUSEL"]
publish_calls = [c for c in calls if c[0] == "POST" and c[1].endswith("/media_publish")]
assert len(items) == 3, f"expected 3 item containers, got {len(items)}"
assert len(parents) == 1, f"expected 1 CAROUSEL parent, got {len(parents)}"
children = parents[0][2].get("children", "")
assert len(children.split(",")) == 3, f"parent children must reference 3 items, got {children}"
assert publish_calls[0][2].get("creation_id") == "parent", \
    f"publish must use the CAROUSEL parent id, got {publish_calls[0][2]}"
assert res.media_id == "17999900000000001"
assert res.permalink == "https://www.instagram.com/p/ABC/"
ok(f"carousel: 3 items -> 1 CAROUSEL parent -> 1 publish -> {res.permalink}")

# ── 2. item containers must not carry captions in the request ─────────────
for _m, _p, form in items:
    assert "caption" not in form, "carousel items must not carry a caption"
assert parents[0][2].get("caption") == "A caption", "the CAROUSEL parent carries the caption"
ok("caption: only the carousel parent carries the caption, not the items")

# ── 3. the URL mirrors the render layout under the site root ──────────────
# Regression: dropping the fingerprint directory (or naming the file by itself)
# is a 404, and Meta reports a 404 as 9004/2207052 "Only photo or video can be
# accepted as media type". The renderer writes
# <fingerprint>/<fingerprint>_NN.jpg under output_dir and gh-pages serves that
# same layout - so that is exactly what has to be handed to Instagram.
urls = [c[2]["image_url"] for c in items]
expected = [f"{BASE}/{fp}/{fp}_{n:02d}.jpg" for n in range(1, 4)]
assert urls == expected, f"got {urls}, want {expected}"
assert all(u.endswith(".jpg") for u in urls), f"image URLs must be .jpg, got {urls}"
ok(f"url: <base>/{fp}/{fp}_NN.jpg mirrors the render output, fingerprint directory and all")

# ── 4. image_urls: same layout from any base, no local file involved ──────
with InstagramPublisher(make_settings()) as pub:
    got = pub.image_urls(make_draft(2))
assert got == [f"{BASE}/{fp}/{fp}_01.jpg", f"{BASE}/{fp}/{fp}_02.jpg"], got
ok("image_urls: one URL per slide, in order, without touching the filesystem")

# A trailing slash on the base must not produce a double slash.
with InstagramPublisher(make_settings(BASE + "/")) as pub:
    assert pub.image_urls(make_draft(1)) == [f"{BASE}/{fp}/{fp}_01.jpg"]
ok("image_urls: base with a trailing slash still yields one separator")

# ── 5. missing PUBLIC_ASSET_BASE_URL fails loudly ─────────────────────────
with InstagramPublisher(make_settings(base="")) as pub:
    try:
        pub.image_urls(make_draft(1))
        raise SystemExit("FAIL: expected PublishError")
    except PublishError as e:
        assert "PUBLIC_ASSET_BASE_URL" in str(e)
ok("guard: refuses to build a URL without a base Instagram could fetch from")

# A draft that was never rendered has nothing to link to, and must say so
# rather than handing Instagram a URL that 404s.
with InstagramPublisher(make_settings()) as pub:
    try:
        pub.publish_draft(make_draft(0))
        raise SystemExit("FAIL: expected PublishError")
    except PublishError as e:
        assert "no slides" in str(e), e
ok("guard: a draft with no slides is refused before any API call")

# ── 6. caption over 2200 chars is rejected before any API call ───────────
calls = []
with InstagramPublisher(make_settings(), client=httpx.Client(transport=handler(calls))) as pub:
    try:
        pub.publish_draft(make_draft(1, caption="x" * 2300))
        raise SystemExit("FAIL: expected PublishError")
    except PublishError as e:
        assert "2200" in str(e)
assert not calls, f"no API call should be made, got {calls}"
ok("guard: over-long caption rejected pre-flight, no wasted API calls")

# ── 7. partial failure cleans up created containers (best-effort) ─────────
calls = []
with InstagramPublisher(make_settings(), client=httpx.Client(transport=handler(calls, fail_on="third_container"))) as pub:
    try:
        pub.publish_draft(make_draft(3))
        raise SystemExit("FAIL: expected PublishError")
    except PublishError as e:
        assert "324" in str(e) or "Image not accessible" in str(e)
deletes = [c for c in calls if c[0] == "DELETE"]
assert len(deletes) == 2, f"expected cleanup attempts for 2 created containers, got {len(deletes)}"
ok(f"cleanup: publish failure after 2 items triggers {len(deletes)} best-effort deletes")

# ── 8. error translation: expired token ──────────────────────────────────
def expired(request):
    return httpx.Response(400, json={"error": {"message": "Invalid OAuth access token.", "code": 190}})

with InstagramPublisher(make_settings(), client=httpx.Client(transport=httpx.MockTransport(expired))) as pub:
    try:
        pub.account_info()
        raise SystemExit("FAIL")
    except PublishError as e:
        assert "190" in str(e) and "long-lived token" in str(e)
ok("errors: expired token message tells the user what to do")

# ── 9. permission error lists the required scopes ────────────────────────
def denied(request):
    return httpx.Response(403, json={"error": {"message": "Missing permission", "code": 200}})

msg = ""
with InstagramPublisher(make_settings(), client=httpx.Client(transport=httpx.MockTransport(denied))) as pub:
    try:
        pub.account_info()
        raise SystemExit("FAIL")
    except PublishError as e:
        msg = str(e)
        assert "instagram_business_content_publish" in msg
        assert "pages_show_list" not in msg, "Facebook-only scopes must not leak into the message"
assert not re.search(r"pages_read_engagement", msg)
ok("errors: permission failure surfaces only the Instagram Login scopes")

# ── 10. insights degrades gracefully ─────────────────────────────────────
with InstagramPublisher(make_settings(), client=httpx.Client(transport=handler([]))) as pub:
    data = pub.insights("179999")
    assert data["data"][0]["values"][0]["value"] == 500
ok("insights: parses metrics")

# ── 11. more than 10 images is capped ────────────────────────────────────
calls = []
with InstagramPublisher(make_settings(), client=httpx.Client(transport=handler(calls))) as pub:
    pub.publish_draft(make_draft(12))
n_items = len([c for c in calls if c[0] == "POST" and c[1].endswith("/media") and "image_url" in c[2]])
n_parents = len([c for c in calls if c[0] == "POST" and c[1].endswith("/media") and c[2].get("media_type") == "CAROUSEL"])
assert n_items == 10, f"12 images should be capped at 10 items, got {n_items}"
assert n_parents == 1
ok(f"limits: 12 images capped at Instagram's 10-item maximum ({n_items}+1 w/ parent)")

# 11 images must still be a carousel, not silently degraded to a single post
calls = []
with InstagramPublisher(make_settings(), client=httpx.Client(transport=handler(calls))) as pub:
    pub.publish_draft(make_draft(11))
assert len([c for c in calls if c[0] == "POST" and c[1].endswith("/media_publish")]) == 1
ok("limits: 11 images stays a carousel after capping")

# ── 12. check_token hits /me and returns the account ─────────────────────
calls = []
with InstagramPublisher(make_settings(), client=httpx.Client(transport=handler(calls))) as pub:
    account = pub.check_token()
assert account["user_id"] == "1789" and account["username"] == "accelerateddevops"
assert any(c[1].endswith("/me") for c in calls)
ok("check_token: /me returns the IG user id and username")

# ── 13. check_token without a token fails loudly ─────────────────────────
with InstagramPublisher(
    Settings(
        instagram=InstagramConfig("1789", "", None),
        assets=AssetConfig(base_url=BASE),
    ),
) as pub:
    try:
        pub.check_token()
        raise SystemExit("FAIL")
    except PublishError as e:
        assert "IG_ACCESS_TOKEN" in str(e)
ok("errors: no token -> clear message instead of a confusing API call")

# ── 14. Instagram's flaky image-download error is retried ────────────────
# Meta returns 9004/2207052 for URLs that work on a later attempt; without a
# retry that fails the whole publish.
def flaky_then_ok(request):
    form = form_of(request)
    if request.method == "POST" and request.url.path.endswith("/media") and "image_url" in form:
        if len(flaky_calls) < 2:  # first 2 attempts: "download failed"
            flaky_calls.append(form)
            return httpx.Response(200, json={"error": {
                "message": "Only photo or video can be accepted as media type.",
                "code": 9004, "error_subcode": 2207052, "is_transient": False,
                "error_user_msg": "The media could not be fetched from this URI: "
                                  + form.get("image_url", ""),
            }})
    return handler([], permalink=True).handle_request(request)


flaky_calls: list = []
calls = []
with InstagramPublisher(
    make_settings(), client=httpx.Client(transport=httpx.MockTransport(flaky_then_ok))
) as pub:
    res = pub.publish_draft(make_draft(2))
assert res.media_id, "a transient download failure must not fail the publish"
assert len(flaky_calls) == 2, f"expected exactly 2 failed attempts, got {len(flaky_calls)}"
ok("retry: transient 9004/2207052 image-download failure recovers and publishes")

# A genuine 404 (unreachable path) must still fail rather than loop forever:
# bounded attempts, and the message names the URL.
def always_unreachable(request):
    return httpx.Response(200, json={"error": {
        "message": "Only photo or video can be accepted as media type.",
        "code": 9004, "error_subcode": 2207052, "is_transient": False,
        "error_user_msg": "The media could not be fetched from this URI: https://cdn.example.com/out/x.jpg",
    }})


attempts = 0


def counting_unreachable(request):
    global attempts
    if request.method == "POST" and request.url.path.endswith("/media"):
        attempts += 1
    return always_unreachable(request)


with InstagramPublisher(
    make_settings(), client=httpx.Client(transport=httpx.MockTransport(counting_unreachable))
) as pub:
    try:
        pub.publish_draft(make_draft(2))
        raise SystemExit("FAIL: expected PublishError")
    except PublishError as e:
        assert "2207052" in str(e)
        assert "could not download" in str(e), "the hint must point at the URL, not media_type"
        assert "cdn.example.com" in str(e), "Meta's error_user_msg carries the URI and must be surfaced"
assert attempts == 3, f"retries must be bounded at 3 attempts, got {attempts}"
ok("errors: unreachable image gives a bounded, URL-naming error")

# ── 15. other errors are not retried ──────────────────────────────────────
token_calls = 0


def bad_token(request):
    global token_calls
    if request.method == "POST" and request.url.path.endswith("/media"):
        token_calls += 1
    return httpx.Response(400, json={"error": {"message": "Invalid OAuth access token.", "code": 190}})


with InstagramPublisher(
    make_settings(), client=httpx.Client(transport=httpx.MockTransport(bad_token))
) as pub:
    try:
        pub.publish_draft(make_draft(2))
        raise SystemExit("FAIL: expected PublishError")
    except PublishError as e:
        assert "long-lived token" in str(e)
assert token_calls == 1, f"a 190 must fail immediately, got {token_calls} attempts"
ok("retry: unrelated errors still fail on the first attempt")

# ── 16. no long dash character ever reaches the post ─────────────────────
dashy = make_draft(2, caption="Slow CI — not hardware — was the problem.")
dashy.hashtags = ["devops", "cicd"]
assert "Slow CI - not hardware - was the problem." in dashy.full_caption, dashy.full_caption
assert "—" not in dashy.full_caption and "–" not in dashy.full_caption
# A slide read back from storage is flattened on the way in, which is what a
# re-render draws; drafts written before the rule existed still carry em dashes.
loaded = Slide.from_dict(
    {"order": 1, "layout": "bullets", "headline": "Kubernetes — at scale",
     "body": "queue depth – and why", "footer": "", "cta": "Ask — where the queue is"}
)
assert loaded.headline == "Kubernetes - at scale", loaded.headline
assert "–" not in loaded.body and "—" not in loaded.cta
ok("no long dash in the posted caption or in a restored slide")

print("\nPUBLISHER TESTS PASSED")
