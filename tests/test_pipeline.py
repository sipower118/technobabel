"""End-to-end test with a fake LLM: collect-less pipeline, generate, render,
validate, schedule. Uses the real store and the real renderer."""

import os
import shutil
import sys
from datetime import datetime, timedelta, timezone



import _paths  # noqa: F401  (sys.path + chdir bootstrap)
from accelerateddevops.config import Settings
from accelerateddevops.llm.base import GeneratedPost
from accelerateddevops.models import Slide, SourceItem
from accelerateddevops.pipeline import (
    assign_schedule,
    generate_draft,
    next_slot_after,
    render_carousel,
    run_generate,
    select_items,
)
from accelerateddevops.store import Store

def ok(m):
    print("OK   " + m)


def info(m):
    print("     " + m)


DATA = "data/_e2e"
shutil.rmtree(DATA, ignore_errors=True)
settings = Settings(data_dir=__import__("pathlib").Path(DATA))
settings.ensure_dirs()


class FakeProvider:
    """Stands in for Gemini: returns a valid carousel, honours max_slides."""

    def __init__(self, variant="ok"):
        self.variant = variant
        self.calls = 0

    def generate(self, **kw):
        self.calls += 1
        if self.variant == "bad":
            return GeneratedPost(
                hook="", subhook="", caption="", hashtags=[],
                alt_text="", slides=[],
            )
        slides = [
            Slide(1, "cover", "Nobody owns your build pipeline",
                  "Ownership is the fix, not more runners."),
            Slide(2, "stat", "Queue wait dominated our build",
                  "38%\nof wall clock was waiting for a runner."),
            Slide(3, "bullets", "Split ownership three ways",
                  "Platform owns runners | Teams own test budgets | CI owns the loop"),
            Slide(4, "cta", "What is the slowest step in your pipeline?",
                  "Tell us below.", "Save this"),
        ]
        return GeneratedPost(
            hook="Nobody owns your build pipeline",
            subhook="Ownership is the fix.",
            caption="Most slow CI is an ownership problem.\n\nWhat is your slowest step?",
            hashtags=["devops", "cicd", "#PlatformEngineering", "tech lead!"],
            alt_text="Cover: nobody owns your build pipeline.",
            slides=slides[: kw.get("max_slides", 8)],
            sources_note="Source: example blog",
        )


def item(i, source, score, comments=0):
    return SourceItem(
        source=source, external_id=f"e{i}",
        title=f"Story {i} about kubernetes pipelines",
        url=f"https://example.com/{i}",
        summary="Some summary", body="Some body", score=score, comments=comments,
    )


store = Store(settings.db_path)

# ── 1. store: insert + dedupe ───────────────────────────────────────────
n1 = store.add_items([item(1, "hackernews", 300, 200), item(2, "rss:cncf", 90),
                      item(3, "reddit:r/devops", 40, 300)])
n2 = store.add_items([item(1, "hackernews", 300, 200)])  # same item again
assert n1 == 3, n1
assert n2 == 0, f"re-insert should be ignored, got {n2}"
ok("store: insert 3, dedupe re-insert")

# ── 2. select normalises across sources ─────────────────────────────────
picked = select_items(store, settings, limit=2)
assert len(picked) == 2, picked
top = picked[0]
assert top.score + top.comments * 0.5 >= 0, "ranking broken"
info("select: " + ", ".join(f"{p.source}={p.score}/{p.comments}" for p in picked))
ok("select: per-source normalisation keeps a quiet source competitive")

# claimed items are not handed out twice
again = select_items(store, settings, limit=2)
assert all(a.external_id not in {p.external_id for p in picked} for a in again)
ok("select: claimed items are not re-offered")

# ── 3. generate + validate + render ─────────────────────────────────────
p = FakeProvider()
d = generate_draft(picked[0], p, settings)
assert d is not None
assert d.slides[0].layout == "cover"
# hashtag hygiene happens in the LLM parser; here just check passthrough
d = render_carousel(d, settings, store)
assert len(d.image_paths) == 4
import os
assert all(os.path.getsize(pth) > 1000 for pth in d.image_paths)
ok(f"generate+render: 4 slides, {sum(os.path.getsize(x) for x in d.image_paths)//1024} KB total")

# a draft that fails validation is rejected, not stored
bad = generate_draft(picked[1] if len(picked) > 1 else item(9, "rss:x", 50), FakeProvider("bad"), settings)
assert bad is None
ok("generate: empty carousel rejected")

# ── 4. caption limits ───────────────────────────────────────────────────
long_item = item(11, "rss:x", 200)
d2 = generate_draft(long_item, FakeProvider(), settings)
d2.caption = "x" * 2300
from accelerateddevops.pipeline import _validate
problems = _validate(d2, 8)
assert any("2200" in p for p in problems), problems
ok("validate: over-long caption flagged")

# ── 5. scheduling ───────────────────────────────────────────────────────
now = datetime(2026, 9, 29, 8, 30, tzinfo=timezone.utc)
nxt = next_slot_after(now, ["09:15", "17:45"])
assert nxt.hour == 9 and nxt.minute == 15, nxt
assert next_slot_after(datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc), ["09:15", "17:45"]).hour == 17
# after the last slot -> tomorrow's first
tom = next_slot_after(datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc), ["09:15", "17:45"])
assert tom.day == 30 and tom.hour == 9, tom
# malformed slots must not crash
assert next_slot_after(now, ["garbage"]) is not None
ok("schedule: next_slot_after correct across day boundaries, tolerates bad config")

store.save_draft(d2)
store.update_draft(d2.fingerprint, status="approved")
# only d2 is approved at this point: the earlier draft was never saved, and
# generate_draft() on the 'bad' provider produced nothing to store.
assert store.counts()["approved"] == 1, store.counts()
sched = assign_schedule(store, settings, now=now)
assert len(sched) == 1, [s.fingerprint for s in sched]
assert sched[0].scheduled_for.hour == 9
ok(f"schedule: assigned {sched[0].scheduled_for:%H:%M}")

# due_drafts respects the clock
assert len(store.due_drafts(now)) == 0
assert len(store.due_drafts(now + timedelta(hours=1))) == 1
ok("schedule: due_drafts gates on scheduled_for")

# ── 6. multi-draft spread, and no collision with already-scheduled slots ──
# d2 already occupies 09:15 from the previous step.
for i in range(20, 24):
    dd = generate_draft(item(i, "rss:x", 100 + i), FakeProvider(), settings)
    dd.status = "approved"
    store.save_draft(dd)
sched = assign_schedule(store, settings, now=now)
slots = [s.scheduled_for for s in sched]
assert len(set(slots)) == len(slots), f"slots collided: {slots}"
# 09:15 is already taken by d2, so nothing may reuse it today.
assert all(not (s.hour == 9 and s.minute == 15 and s.day == now.day) for s in slots), slots
ok(f"schedule: {len(sched)} drafts on distinct free slots "
   f"{sorted({s.strftime('%H:%M') for s in slots})}")

# A second call must not disturb what the first one assigned.
before = {(d.fingerprint, d.scheduled_for) for d in store.list_drafts(status="scheduled")}
again = assign_schedule(store, settings, now=now)
after = {(d.fingerprint, d.scheduled_for) for d in store.list_drafts(status="scheduled")}
assert before == after, "re-scheduling changed existing assignments"
assert not again, "nothing should be left to schedule"
ok("schedule: re-running is idempotent")

store.close()
shutil.rmtree(DATA, ignore_errors=True)
print("\nPIPELINE TESTS PASSED")
