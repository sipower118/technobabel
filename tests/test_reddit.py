"""Verify the Reddit OAuth path against a mock transport.

The live 403 blocks the public endpoint from this network, so the OAuth branch
cannot be exercised for real without credentials. These tests pin the parts
that are easy to get wrong: the auth handshake, the base-URL switch, 401
refresh, rate-limit backoff, and the no-credentials fallback.
"""

import sys


import httpx


import _paths  # noqa: F401  (sys.path + chdir bootstrap)
from accelerateddevops.sources.reddit import (
    AUTH_URL,
    OAUTH_BASE,
    PUBLIC_BASE,
    SUBREDDITS,
    RedditSource,
)

calls = []


def ok(msg):
    print("OK   " + msg)


def listing_payload(n=3, comments=80):
    kids = [
        {
            "kind": "t3",
            "data": {
                "id": f"abc{i}",
                "title": f"How do you handle {i} at scale?",
                "selftext": "We keep hitting this problem in staging.",
                "permalink": f"/r/devops/comments/abc{i}/post/",
                "author": "engineer",
                "created_utc": 1750000000,
                "score": 120,
                "num_comments": comments,
                "is_self": True,
                "over_18": False,
            },
        }
        for i in range(n)
    ]
    # A link post and an NSFW post that must never become SourceItems.
    kids.append({"kind": "t3", "data": {
        "id": "link1", "title": "Blog post about Kubernetes",
        "selftext": "", "permalink": "/r/devops/comments/link1/x/",
        "author": "someone", "created_utc": 1750000000, "score": 500,
        "num_comments": 999, "is_self": False, "over_18": False}})
    kids.append({"kind": "t3", "data": {
        "id": "nsfw1", "title": "adult thread",
        "selftext": "text", "permalink": "/r/devops/comments/nsfw1/y/",
        "author": "someone", "created_utc": 1750000000, "score": 900,
        "num_comments": 900, "is_self": True, "over_18": True}})
    return {"data": {"children": kids}}


# ── 1. token handshake then authenticated listings ───────────────────────
def handler(request: httpx.Request) -> httpx.Response:
    calls.append((str(request.url), dict(request.headers)))
    if str(request.url).startswith(AUTH_URL):
        assert request.headers.get("authorization", "").startswith("Basic "), \
            "token request must use HTTP Basic auth"
        assert request.method == "POST"
        assert b"grant_type=client_credentials" in request.content
        return httpx.Response(200, json={
            "access_token": "tok-123", "token_type": "bearer",
            "expires_in": 3600, "scope": "*",
        })
    if str(request.url).startswith(OAUTH_BASE):
        assert request.headers.get("authorization") == "bearer tok-123", \
            f"missing bearer header: {request.headers.get('authorization')}"
        return httpx.Response(200, json=listing_payload())
    if str(request.url).startswith(PUBLIC_BASE):
        return httpx.Response(403, text="<html>blocked</html>")
    raise AssertionError(f"unexpected URL {request.url}")


client = httpx.Client(transport=httpx.MockTransport(handler))
src = RedditSource(min_comments=40, client_id="CID", client_secret="SECRET")
src.fetch(client)

auth_calls = [u for u, _ in calls if u.startswith(AUTH_URL)]
assert len(auth_calls) == 1, f"expected one token request, got {len(auth_calls)}"
ok("auth: client_credentials handshake happens once and is reused")

listing_calls = [u for u, _ in calls if u.startswith(OAUTH_BASE)]
assert len(listing_calls) == len(SUBREDDITS) * 2, listing_calls
# AUTH_URL lives on www.reddit.com, so exclude it: the only public-endpoint
# traffic allowed is the token request itself.
public_listing = [u for u, _ in calls
                  if u.startswith(PUBLIC_BASE) and not u.startswith(AUTH_URL)]
assert not public_listing, f"must not fall back when OAuth works: {public_listing}"
ok("auth: all listings go to oauth.reddit.com, never the public endpoint")

# ── 2. items are parsed and filtered ─────────────────────────────────────
calls.clear()
client = httpx.Client(transport=httpx.MockTransport(handler))
src = RedditSource(min_comments=40, client_id="CID", client_secret="SECRET")
items = src.fetch(client)
assert items, "expected items"
first = items[0]
assert first.source == "reddit:r/devops"
assert first.url.startswith("https://www.reddit.com/r/devops/comments/")
assert first.comments == 80 and first.score == 120
assert "at scale" in first.title
# The link post (is_self False) and the NSFW post must both be excluded.
ids = {i.external_id for i in items}
assert "link1" not in ids, "link post leaked through"
assert "nsfw1" not in ids, "over_18 post leaked through"
ok("parse: self-posts become SourceItems; link and NSFW posts excluded")


# ── 3. low-comment and link posts are filtered out ──────────────────────
def sparse(request):
    if str(request.url).startswith(AUTH_URL):
        return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    return httpx.Response(200, json=listing_payload(n=2, comments=5))


client = httpx.Client(transport=httpx.MockTransport(sparse))
assert RedditSource(min_comments=40, client_id="CID", client_secret="SECRET").fetch(client) == []
ok("filter: posts under the comment threshold are dropped")


# ── 4. a 401 mid-run refreshes the token once and retries ───────────────
state = {"listing_hits": 0, "tokens": 0}


def expiring(request):
    if str(request.url).startswith(AUTH_URL):
        state["tokens"] += 1
        return httpx.Response(200, json={"access_token": f"tok-{state['tokens']}",
                                         "expires_in": 3600})
    state["listing_hits"] += 1
    if state["listing_hits"] == 1:
        return httpx.Response(401, json={"error": "invalid_grant"})
    assert request.headers.get("authorization") == "bearer tok-2", \
        f"retry must use the NEW token, got {request.headers.get('authorization')}"
    return httpx.Response(200, json=listing_payload())


client = httpx.Client(transport=httpx.MockTransport(expiring))
items = RedditSource(min_comments=40, client_id="CID", client_secret="SECRET").fetch(client)
assert items, "listing should recover after refresh"
assert state["tokens"] == 2, f"expected exactly one refresh, got {state['tokens']}"
ok("refresh: 401 re-authenticates once and retries with the new token")


# ── 5. rate limit headers trigger a backoff ─────────────────────────────
import accelerateddevops.sources.reddit as reddit_mod

slept = []
reddit_mod.time.sleep = lambda s: slept.append(s)


def throttled(request):
    if str(request.url).startswith(AUTH_URL):
        return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    return httpx.Response(200, json=listing_payload(), headers={
        "x-ratelimit-used": "98", "x-ratelimit-remaining": "2",
        "x-ratelimit-reset": "7",
    })


slept.clear()
client = httpx.Client(transport=httpx.MockTransport(throttled))
RedditSource(min_comments=40, client_id="CID", client_secret="SECRET").fetch(client)
assert any(5.0 <= s <= 7.5 for s in slept), f"expected ~7s backoff, slept {slept}"
ok("ratelimit: x-ratelimit-remaining below 5 pauses for the reset window")


# ── 6. healthy rate limit does not pause ─────────────────────────────────
slept.clear()


def healthy(request):
    if str(request.url).startswith(AUTH_URL):
        return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    return httpx.Response(200, json=listing_payload(), headers={
        "x-ratelimit-remaining": "95", "x-ratelimit-reset": "0",
    })


client = httpx.Client(transport=httpx.MockTransport(healthy))
RedditSource(min_comments=40, client_id="CID", client_secret="SECRET").fetch(client)
assert not any(s > 2.0 for s in slept), f"should not back off, slept {slept}"
ok("ratelimit: ample quota does not slow the run")


# ── 7. rejected credentials fall back to the probe, then skip ───────────
probed = {"public": 0}


def rejected(request):
    if str(request.url).startswith(AUTH_URL):
        return httpx.Response(401, json={"error": "invalid_client"})
    probed["public"] += 1
    return httpx.Response(403, text="<html>blocked</html>")


client = httpx.Client(transport=httpx.MockTransport(rejected))
items = RedditSource(min_comments=40, client_id="CID", client_secret="SECRET").fetch(client)
assert items == [], "must return nothing, not raise"
assert probed["public"] == 1, f"probe should run once, ran {probed['public']}x"
ok("fallback: unapproved app -> probe once -> skip cleanly, no exception")


# ── 8. no credentials at all still probes the public endpoint ───────────
probed["public"] = 0
client = httpx.Client(transport=httpx.MockTransport(rejected))
assert RedditSource(min_comments=40).fetch(client) == []
assert probed["public"] == 1
ok("fallback: no credentials -> public probe, then skip")


# ── 9. no credentials but a working public endpoint still collects ──────
probed["public"] = 0


def public_ok(request):
    probed["public"] += 1
    return httpx.Response(200, json=listing_payload())


client = httpx.Client(transport=httpx.MockTransport(public_ok))
items = RedditSource(min_comments=40).fetch(client)
assert items, "public endpoint should still work when reachable"
assert items[0].url.startswith("https://www.reddit.com/r/")
ok("fallback: unauthenticated collection still works when not blocked")

print("\nREDDIT OAUTH TESTS PASSED")
