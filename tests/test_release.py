"""Verify items are returned to the pool when generation fails, so a transient
LLM outage does not permanently consume the item queue."""

import shutil, sys, pathlib

import _paths  # noqa: F401  (sys.path + chdir bootstrap)
from accelerateddevops.config import Settings
from accelerateddevops.llm.base import GeneratedPost, LLMUnavailable
from accelerateddevops.models import Slide, SourceItem
from accelerateddevops.pipeline import run_generate, select_items
from accelerateddevops.store import Store

DATA = "data/_release"
shutil.rmtree(DATA, ignore_errors=True)
# posts_per_run decides how many items run_generate claims, so the batch size
# has to be 3 for this test to be able to produce 2 drafts from 3 items.
s = Settings(data_dir=pathlib.Path(DATA), posts_per_run=3)
s.ensure_dirs()
store = Store(s.db_path)


def ok(m):
    print("OK   " + m)


for i in range(3):
    store.add_items([SourceItem(source="hackernews", external_id=f"e{i}",
                               title=f"Kubernetes story {i}", url=f"https://e.com/{i}",
                               score=200 + i, comments=100)])

class DeadProvider:
    def generate(self, **kw):
        raise LLMUnavailable("quota exhausted on every model")

s2 = s
try:
    run_generate(store, s2, DeadProvider())
    raise SystemExit("FAIL: expected LLMUnavailable to propagate")
except LLMUnavailable:
    pass

# All items must be back in the 'new' pool, not stranded as 'used'.
n_new = store.counts()["items_new"]
assert n_new == 3, f"expected 3 items released, got {n_new}"

ok("failed generation releases every claimed item back to the pool")

# A provider that fails on ONE item must not strand the others.
class FlakyProvider:
    def __init__(self):
        self.n = 0

    def generate(self, **kw):
        self.n += 1
        if self.n == 1:
            return GeneratedPost(hook="", subhook="", caption="", hashtags=[],
                                 alt_text="", slides=[])  # unusable
        return GeneratedPost(hook="H", subhook="", caption="c", hashtags=["devops"],
                             alt_text="a", slides=[Slide(1, "cover", "Good one"),
                                                  Slide(2, "bullets", "Second")])

p = FlakyProvider()
drafts = run_generate(store, s, p)
assert len(drafts) == 2, f"expected 2 good drafts, got {len(drafts)}"
assert all(len(d.image_paths) == 2 for d in drafts)
ok("one unusable item does not stop the rest of the batch")

# The released item is still available for a future run.
leftover = store.counts()["items_new"]
assert leftover == 1, f"expected 1 item left for retry, got {leftover}"
ok("unusable item is retried later rather than lost")

store.close()
shutil.rmtree(DATA, ignore_errors=True)
print("\nFAILURE-HANDLING TESTS PASSED")
