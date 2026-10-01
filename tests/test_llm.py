"""Mock test for the Gemini provider: ladder rotation, quota handling, parsing."""

import json
import sys

import httpx



import _paths  # noqa: F401  (sys.path + chdir bootstrap)
from accelerateddevops.llm.base import LLMUnavailable
from accelerateddevops.llm.gemini import GENAI_TIMEOUT_MS, GeminiProvider, _parse
from accelerateddevops.models import Slide
from accelerateddevops.store import Store

SAMPLE = {
    "hook": "Your CI is slow because nobody owns it",
    "subhook": "Here is the 3-layer split that fixed it",
    "slides": [
        {"order": 1, "layout": "cover", "headline": "Your CI is slow because nobody owns it",
         "body": "Here is the 3-layer split that fixed it"},
        {"order": 2, "layout": "stat", "headline": "42% of build time was waiting on a flaky cache",
         "body": "Nobody was paged, because the cache was a shared VM with no SLO."},
        {"order": 3, "layout": "bullets", "headline": "Split ownership three ways",
         "body": "Platform owns runners. Service teams own test time. CI owns the feedback loop."},
        {"order": 4, "layout": "takeaway", "headline": "If nobody owns the build, it is not slow. It is unmanaged."},
    ],
    "caption": "Most slow CI is an ownership problem, not a hardware problem.\n\nThe fix is splitting the build into layers and giving each one an owner.\n\nWhat is the slowest part of your pipeline right now?",
    "hashtags": ["#DevOps", "devops", "CI/CD", "platform engineering", "techlead", "CICD!!"],
    "alt_text": "Dark cover slide reading Your CI is slow because nobody owns it.",
    "sources_note": "Source: a large platform team",
}

# ── 1. parsing ───────────────────────────────────────────────────────────
post = _parse(json.dumps(SAMPLE), max_slides=8)
assert post.hook == "Your CI is slow because nobody owns it"
assert len(post.slides) == 4, post.slides
assert post.slides[0].layout == "cover"
assert [s.order for s in post.slides] == [1, 2, 3, 4]
# Hashtag normalisation: strip #, lowercase, drop illegal chars, dedupe.
assert post.hashtags == ["devops", "cicd", "platformengineering", "techlead"], post.hashtags
print("1. parse            OK  hashtags ->", post.hashtags)

# ── 2. fenced JSON + junk around it ──────────────────────────────────────
fenced = "```json\n" + json.dumps(SAMPLE) + "\n```"
assert _parse(fenced, 8).hook == post.hook
print("2. fenced json      OK")

# ── 3. bad layout is coerced, slide cap enforced ─────────────────────────
sample = json.loads(json.dumps(SAMPLE))
sample["slides"][2]["layout"] = "not_a_layout"
sample["slides"].append({"order": 99, "layout": "cover", "headline": "overflow"})
p3 = _parse(json.dumps(sample), max_slides=3)
assert len(p3.slides) == 3, len(p3.slides)
assert p3.slides[2].layout == "bullets", p3.slides[2].layout
print("3. cap + coercion   OK  5 slides -> 3, bad layout -> bullets")

# ── 4. slide 1 is always cover even if mislabelled ───────────────────────
sample2 = json.loads(json.dumps(SAMPLE))
sample2["slides"][0]["layout"] = "stat"
assert _parse(json.dumps(sample2), 8).slides[0].layout == "cover"
print("4. cover enforced   OK")

# ── 5. empty headline slides are dropped ─────────────────────────────────
sample3 = json.loads(json.dumps(SAMPLE))
sample3["slides"].insert(1, {"order": 2, "layout": "bullets", "headline": "   "})
assert len(_parse(json.dumps(sample3), 8).slides) == 4
print("5. empty dropped    OK")


# ── 6. ladder: pro is quota'd, falls through to flash ────────────────────
class QuotaError(Exception):
    def __init__(self):
        super().__init__("429 RESOURCE_EXHAUSTED: quota exceeded for quota metric")
        self.code = 429


store = Store("data/_test_ladder.sqlite3")
store.clear_cooldowns()

calls = []


def attempt(text="", reason=""):
    """Real _Attempt so tests exercise the same fields the provider reads."""
    from accelerateddevops.llm.gemini import _Attempt
    return _Attempt(text=text, should_advance=not text, reason=reason)


def make_fake_provider(models, keys, behaviour):
    p = GeminiProvider(api_keys=keys, models=models, store=store)
    p._client = lambda k: object()
    p._try = lambda model, key, system, prompt: (
        calls.append((model, key)) or behaviour(model)
    )
    return p


def behaviour_quota_then_ok(model):
    if model == "gemini-2.5-pro":
        return attempt(reason="quota: 429")
    return attempt(text=json.dumps(SAMPLE))


p = make_fake_provider(
    ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
    ["keyA", "keyB"],
    behaviour_quota_then_ok,
)
res = p.generate(title="t", body="b", url="u", source_name="s", brand="B",
                 tagline="T", max_slides=8, hook_intensity=0.5)
assert res.hook == post.hook
models_tried = [m for m, _ in calls]
assert models_tried[0] == "gemini-2.5-pro"
assert "gemini-2.5-flash" in models_tried, models_tried
# pro failed on keyA -> break, must NOT try pro/keyB
assert ("gemini-2.5-pro", "keyB") not in calls, calls
print("6. quota -> next    OK  tried", models_tried)

# pro is now benched for 30 minutes
assert "gemini-2.5-pro" in store.cooled_down_models()
assert p.available_models() == ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
print("   cooldown         OK  available ->", p.available_models())

# ── 7. cooldown is honoured on the next run ──────────────────────────────
calls.clear()
res2 = p.generate(title="t", body="b", url="u", source_name="s", brand="B",
                  tagline="T", max_slides=8, hook_intensity=0.5)
assert calls[0][0] == "gemini-2.5-flash", calls
print("7. cooldown honoured OK  first attempt ->", calls[0][0])

# ── 8. success clears that model's cooldown ──────────────────────────────
store.clear_cooldowns()
store.cooldown_model("gemini-2.5-flash", 30, "test")
assert "gemini-2.5-flash" in store.cooled_down_models()
store.clear_cooldowns_for("gemini-2.5-flash")
assert "gemini-2.5-flash" not in store.cooled_down_models()
print("8. success clears   OK")

# ── 9. a rejected key skips to the next key and does NOT bench the model ──
store.clear_cooldowns()
calls.clear()

p2 = GeminiProvider(api_keys=["badKey", "goodKey"], models=["gemini-2.5-pro"], store=store)


def rejected_key():
    exc = Exception("API key not valid. Please pass a valid API key.")
    exc.code = 400
    return exc


p2._try = lambda model, key, system, prompt: (
    calls.append((model, key)) or
    (attempt(text=json.dumps(SAMPLE)) if key == "goodKey" else p2._classify(rejected_key()))
)
p2.generate(title="t", body="b", url="u", source_name="s", brand="B",
            tagline="T", max_slides=8, hook_intensity=0.5)
assert [k for _, k in calls] == ["badKey", "goodKey"], calls
assert "gemini-2.5-pro" not in store.cooled_down_models()
print("9. bad key rotates  OK  calls", [k for _, k in calls])

# ── 9b. once one key is known bad it is skipped on every later model ──────
calls.clear()
p4 = GeminiProvider(api_keys=["badKey", "goodKey"],
                    models=["gemini-2.5-pro", "gemini-2.5-flash"], store=store)
p4._try = lambda model, key, system, prompt: (
    calls.append((model, key)) or
    (attempt(text=json.dumps(SAMPLE)) if key == "goodKey" else p4._classify(rejected_key()))
)
p4.generate(title="t", body="b", url="u", source_name="s", brand="B",
            tagline="T", max_slides=8, hook_intensity=0.5)
# pro/badKey -> pro/goodKey succeeds, so flash is never reached at all.
assert calls == [("gemini-2.5-pro", "badKey"), ("gemini-2.5-pro", "goodKey")], calls
print("9b. bad key not retried OK  calls", calls)

# ── 9c. every key rejected -> one clear setup message, no wall of noise ───
p5 = GeminiProvider(api_keys=["badKey", "badKey2"],
                    models=["gemini-2.5-pro", "gemini-2.5-flash"], store=store)
p5._try = lambda model, key, system, prompt: p5._classify(rejected_key())
try:
    p5.generate(title="t", body="b", url="u", source_name="s", brand="B",
                tagline="T", max_slides=8, hook_intensity=0.5)
    raise SystemExit("FAIL: expected LLMUnavailable for all-bad keys")
except LLMUnavailable as e:
    msg = str(e)
    assert "rejected every configured API key" in msg, msg
    assert "GEMINI_API_KEYS" in msg, msg
    # Only two failures recorded, not one per (model, key) pair.
    assert "gemini-2.5-flash/key1" not in msg, msg
print("9c. all keys bad     OK  one actionable line")

# ── 9d. access denied is per-model, not per-key ──────────────────────────
store.clear_cooldowns()
calls.clear()


def denied():
    exc = Exception("Permission denied for model X")
    exc.code = 403
    return exc


p6 = GeminiProvider(api_keys=["k1", "k2"], models=["gemini-2.5-pro"], store=store)
p6._try = lambda model, key, system, prompt: (
    calls.append((model, key)) or p6._classify(denied())
)
try:
    p6.generate(title="t", body="b", url="u", source_name="s", brand="B",
                tagline="T", max_slides=8, hook_intensity=0.5)
    raise SystemExit("FAIL: expected LLMUnavailable")
except LLMUnavailable as e:
    assert "every model/key in the ladder failed" in str(e), str(e)
# One model, one attempt: the model was benched, k2 was never tried.
assert calls == [("gemini-2.5-pro", "k1")], calls
assert "gemini-2.5-pro" in store.cooled_down_models()
print("9d. access per-model OK  benched, k2 untouched")

# ── 10. all exhausted -> LLMUnavailable ──────────────────────────────────
p3 = make_fake_provider(["m1", "m2"], ["k1"],
                        lambda m: attempt(reason="quota: 429 RESOURCE_EXHAUSTED"))
try:
    p3.generate(title="t", body="b", url="u", source_name="s", brand="B",
                tagline="T", max_slides=8, hook_intensity=0.5)
    raise SystemExit("FAIL: expected LLMUnavailable")
except LLMUnavailable as e:
    assert "every model/key" in str(e)
print("10. all exhausted   OK  LLMUnavailable raised")

# ── 11. SDK client timeout is bounded; timeouts classify as transient ────
client = GeminiProvider(api_keys=["k"], models=["m"])._client("k")
assert client._api_client._http_options.timeout == GENAI_TIMEOUT_MS, (
    client._api_client._http_options.timeout
)
client.close()
timeout_attempt = GeminiProvider(api_keys=["k"], models=["m"])._classify(
    httpx.ReadTimeout("read operation timed out")
)
assert timeout_attempt.reason.startswith("transient"), timeout_attempt.reason
assert timeout_attempt.scope == "model", timeout_attempt.scope
print(f"11. timeout        OK  SDK bounded at {GENAI_TIMEOUT_MS}ms, timeouts transient")

# ── 12. the closing cta is parsed, and no long dash survives parsing ─────
sample_cta = json.loads(json.dumps(SAMPLE))
sample_cta["slides"][3]["cta"] = "Ask where your queue is hiding"
sample_cta["slides"][1]["headline"] = "42% of build time — waiting on a cache"
sample_cta["caption"] = "Slow CI is an ownership problem – not hardware."
cta_post = _parse(json.dumps(sample_cta), max_slides=8)
assert cta_post.slides[3].cta == "Ask where your queue is hiding", cta_post.slides[3].cta
# A slide with no cta stays empty: the renderer draws no pill for it rather
# than falling back to a canned line.
assert cta_post.slides[0].cta == "", cta_post.slides[0].cta
assert "—" not in cta_post.slides[1].headline, cta_post.slides[1].headline
assert "–" not in cta_post.caption, cta_post.caption
print("12. cta + dashes    OK  cta parsed, long dashes flattened")

store.close()
import os
os.remove("data/_test_ladder.sqlite3")
for suffix in ("-wal", "-shm"):
    if os.path.exists("data/_test_ladder.sqlite3" + suffix):
        os.remove("data/_test_ladder.sqlite3" + suffix)
print("\nALL LLM TESTS PASSED")
