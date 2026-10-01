"""Tests for: RSS source expansion, enterprise-priority scoring, feed weights,
and the LLM prompt angles. Plain assert-script, run directly with python."""

import sys
from types import SimpleNamespace


import _paths  # noqa: F401  (sys.path + chdir bootstrap)
from accelerateddevops.sources import rss
from accelerateddevops.llm.prompts import build_prompt


def times(n):
    return " ".join(n)


# ── 1. feed catalogue ─────────────────────────────────────────────────────
assert len(rss.DEFAULT_FEEDS) == 36, len(rss.DEFAULT_FEEDS)
# the enterprise beat feeds exist and are priority-weighted
for name in ("infoq", "dora", "microsoftdevops", "devopsdotcom",
             "pragmaticengineer", "atlassian"):
    assert name in rss.DEFAULT_FEEDS, name
    assert rss.FEED_PRIORITY.get(name, 1.0) > 1.0, name
# trend + deep-dive feeds exist
for name in ("thenewstack", "changelog", "huggingface", "spotify", "dropbox",
             "etsy", "lyft", "meta", "posthog", "aws-architecture", "semaphore",
             "honeycomb", "teleport"):
    assert name in rss.DEFAULT_FEEDS, name
# dead feeds were removed rather than left to 404 in every run
for name in ("uber", "gcp", "platformengineering", "netlify", "stripe"):
    assert name not in rss.DEFAULT_FEEDS, name
print("1. catalogue      OK   36 feeds; enterprise beat weighted, dead feeds gone")

# ── 2. enterprise content out-scores generic infra ───────────────────────
ent = SimpleNamespace(title="How we migrated 2,000 microservices past the change board")
gen = SimpleNamespace(title="Kubernetes release notes with new ingress features")
s_ent = rss._score(ent, "governance held up the rollout; technical debt and compliance everywhere", "infoq")
s_gen = rss._score(gen, "kubernetes ingress and networking updates in this release", "kubernetes")
assert s_ent > s_gen, (s_ent, s_gen)
assert s_ent >= 100, s_ent
assert s_gen < 100, s_gen
print(f"2. score          OK   enterprise {s_ent} > generic {s_gen}")

# ── 3. feed priority multiplier ──────────────────────────────────────────
same = SimpleNamespace(title="A platform team's migration story with governance")
weighted = rss._score(same, "change control, compliance, operating model", "infoq")
base = rss._score(same, "change control, compliance, operating model", "not-a-feed")
assert weighted > base, (weighted, base)
# an unlisted feed keeps weight 1.0
assert base == rss._score(same, "change control, compliance, operating model", None)
print(f"3. weight         OK   infoq {weighted} vs base {base}")

# ── 4. tags now include enterprise terms, and are capped ─────────────────
tags = rss._tags("enterprise governance migration legacy platform team")
assert "enterprise" in tags and "platform-team" in tags, tags
assert all(" " not in t for t in tags)
assert len(rss._tags("migration " * 20)) <= 8
print("4. tags           OK  " + ", ".join(tags[:4]))

# ── 5. default weight for vendor feeds stays 1.0 (no priority creep) ─────
assert rss.FEED_PRIORITY.get("kubernetes", 1.0) == 1.0
assert rss.FEED_PRIORITY.get("grafana", 1.0) == 1.0
print("5. baseline       OK  vendor feeds unweighted")

# ── 6. prompt: angles requested are present, formatting intact ───────────
sys_, user = build_prompt(
    brand="Accelerated DevOps", tagline="Platform engineering, decoded.",
    source_name="rss:infoq", title="T", url="U", body="B",
    max_slides=6, hook_intensity=0.55,
)
assert "CONTENT ANGLES" in sys_
assert "Problem-first" in sys_
assert "large organisations" in sys_
assert "change control" in sys_
assert "large enterprise" in user
assert "case study" in user
# no leftover format braces / no KeyError un-garbage
assert "{brand}" not in sys_ and "{hook_intensity}" not in user
assert sys_.count("{") == sys_.count("}") == 0
assert user.count("{") == user.count("}") == 0
print("6. prompt         OK  angles + org-beat guidance present")

# ── 7. product announcements lose, never win ─────────────────────────────
promo = rss._score(
    SimpleNamespace(title="Announcing the new Platform Engineering Cloud product"),
    "sign up now, product launch webinar, pricing, generally available, free trial",
    "infoq",
)
eng = rss._score(
    SimpleNamespace(title="How we migrated 2,000 services past the change board"),
    "migration at a bank: governance, compliance, technical debt, operating model",
    "infoq",
)
# promo is sunk to the floor even on a priority-weighted feed
assert promo < 40, promo
assert eng > 200, eng
assert eng > promo, (eng, promo)
# on a vendor feed, a promo still trails a plain engineering deep-dive
vendor_promo = rss._score(
    SimpleNamespace(title="Introducing our new observability product"),
    "sign up for the launch webinar and free trial", "datadog",
)
vendor_deep = rss._score(
    SimpleNamespace(title="How we scaled observability to 40k signals/sec"),
    "observability pipeline tuning notes and lessons learned", "datadog",
)
assert vendor_promo < vendor_deep, (vendor_promo, vendor_deep)
assert vendor_promo < 40, vendor_promo
print(f"7. product        OK  engineering {eng} vs promo {promo}")

print("\nRSS + PROMPT TESTS PASSED")
