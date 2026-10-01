"""The publish queue: one post per run, oldest approved first.

`publish` with no arguments is what the scheduled workflow calls, so its
contract is deliberately narrow: post exactly one draft, the one that has been
approved longest, and never two. Instagram has no undo, so a burst is the one
failure mode a retried workflow cannot recover from.
"""

import shutil
from pathlib import Path

import _paths  # noqa: F401  (sys.path + chdir bootstrap)

from datetime import datetime

import _paths  # noqa: F401  (sys.path + chdir bootstrap)

from accelerateddevops.models import Draft, Slide, SourceItem
from accelerateddevops.store import Store

DATA_DIR = Path("data/_queuetest")
shutil.rmtree(DATA_DIR, ignore_errors=True)
DATA_DIR.mkdir(parents=True)


def ok(msg: str) -> None:
    print("OK   " + msg)


def make_draft(store: Store, key: str, created: str, status: str = "draft") -> str:
    """Insert a draft with a known creation time so ordering is testable.

    The fingerprint is derived by the model, not chosen, so the test reads it
    back off the draft rather than pretending to control it.
    """
    item = SourceItem(
        source="rss:test",
        external_id=key,
        title=f"Story {key}",
        url=f"https://example.com/{key}",
    )
    draft = Draft(
        source=item,
        caption=f"Caption {key}",
        slides=[Slide(order=1, headline="One", body="Body", layout="statement")],
        created_at=datetime.fromisoformat(created),
    )
    store.save_draft(draft)
    store.update_draft(draft.fingerprint, status=status)
    return draft.fingerprint


db = DATA_DIR / "accelerated_devops.sqlite3"  # the name Settings.db_path expects
with Store(db) as store:
    # Deliberately inserted newest-first, so a naive "last row wins" or
    # "ORDER BY created_at DESC" would pick the wrong one.
    oldest = make_draft(store, "oldest", "2026-09-01T10:00:00+00:00")
    middle = make_draft(store, "middle", "2026-09-02T10:00:00+00:00")
    newest = make_draft(store, "newest", "2026-09-03T10:00:00+00:00")

    # ── 1. an empty queue yields nothing, not an error ────────────────────
    assert store.next_approved() is None, "nothing is approved yet"
    ok("queue: an empty publish queue yields nothing")

    # ── 2. approving the middle one makes it the next post ───────────────
    store.update_draft(middle, status="approved")
    nxt = store.next_approved()
    assert nxt is not None and nxt.fingerprint == middle, nxt
    ok("queue: the only approved draft is next")

    # ── 3. oldest approved wins, not newest or arbitrary ─────────────────
    store.update_draft(oldest, status="approved")
    store.update_draft(newest, status="approved")
    order = []
    while (nxt := store.next_approved()) is not None:
        order.append(nxt.fingerprint)
        store.update_draft(nxt.fingerprint, status="published")
    assert order == [oldest, middle, newest], order
    ok("queue: approved drafts drain oldest first")

    # ── 4. only `approved` is publishable ────────────────────────────────
    for key, status in (("still-drafted", "draft"), ("turned-down", "rejected")):
        make_draft(store, key, "2026-09-04T10:00:00+00:00", status=status)
    assert store.next_approved() is None, (
        "drafts and rejected drafts must never be picked up for publishing"
    )
    ok("queue: draft/rejected statuses are not publishable")

    # ── 5. a failed publish leaves the draft in the queue, not lost ──────
    # This is what makes a retried workflow safe: the post is still approved,
    # carries the error, and nothing was dropped on the floor.
    retry_me = make_draft(store, "retry-me", "2026-09-05T10:00:00+00:00", status="approved")
    store.update_draft(retry_me, error="HTTP 429 from Instagram")
    retry = store.next_approved()
    assert retry is not None and retry.fingerprint == retry_me, retry
    ok("queue: a failed draft stays approved and is retried, not lost")

    # ── 6. the drafted backlog, oldest first, is what a workflow feeds to approve
    drafted = [d.fingerprint for d in reversed(store.list_drafts(status="draft", limit=10))]
    assert len(drafted) == 1, f"only the unreviewed draft is still a draft: {drafted}"
    assert store.next_approved().fingerprint == retry_me, (
        "an approved draft outranks everything still waiting for review"
    )
    ok("queue: the approved draft outranks the drafted ones")

# ── 7. `publish` with no arguments posts one draft, then stops ──────────
# The scheduled workflow calls exactly this, so the guarantee that matters is
# "one post per run": two approvals in the queue must still produce one post.
import argparse  # noqa: E402

import accelerateddevops.cli as cli  # noqa: E402
from accelerateddevops.config import (  # noqa: E402
    AssetConfig,
    InstagramConfig,
    Settings,
)
from accelerateddevops.publish import PublishResult  # noqa: E402

posted: list[str] = []


class _RecordingPublisher:
    """Stands in for InstagramPublisher and records what it was asked to post."""

    def __init__(self, settings, client=None):
        self.settings = settings

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def publish_draft(self, draft):
        posted.append(draft.fingerprint)
        return PublishResult(media_id=f"media-{len(posted)}", permalink="https://ig/p/x")


cli.InstagramPublisher = _RecordingPublisher  # type: ignore[assignment]
settings = Settings(
    data_dir=DATA_DIR,
    instagram=InstagramConfig("1789", "tok", None),
    assets=AssetConfig(base_url="https://example.test/repo"),
)

# Reset the queue to three approved drafts, oldest first.
with Store(settings.db_path) as store:
    fps = [d.fingerprint for d in store.list_drafts(limit=20)]
    for fp in fps:
        store.update_draft(fp, status="draft")
    ordered = [d.fingerprint for d in reversed(store.list_drafts(limit=20))]
    for fp in ordered:
        store.update_draft(fp, status="approved")
    expected_order = ordered

args = argparse.Namespace(fingerprint=None, due=False)
# More runs than there are drafts, so the queue draining *and* the empty-queue
# no-op are both covered.
for run_number in range(1, len(expected_order) + 2):
    rc = cli.cmd_publish(args, settings)
    assert rc == 0, f"run {run_number} returned {rc}"

assert posted == expected_order, f"posted {posted}, expected {expected_order}"
ok(
    f"publish: no args posts one draft per run, oldest first "
    f"({len(posted)} runs, {len(expected_order)} drafts)"
)

# The extra run past the end found an empty queue and said so, rather than
# failing or re-posting something.
with Store(settings.db_path) as store:
    assert store.next_approved() is None
    assert all(
        store.get_draft(fp).status == "published" for fp in expected_order
    )
    assert all(store.get_draft(fp).permalink == "https://ig/p/x" for fp in expected_order)
ok("publish: an empty queue is a clean no-op, and every post is recorded")

shutil.rmtree(DATA_DIR, ignore_errors=True)
print("\nPUBLISH QUEUE TESTS PASSED")
