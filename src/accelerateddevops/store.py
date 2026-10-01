"""SQLite persistence: what we've seen, what we've drafted, what we published.

SQLite is deliberate here rather than Postgres - this is a single-account
pipeline that runs a couple of times a day, and a file in `data/` survives
machine restarts without any service to babysit.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Draft, Slide, SourceItem, iso, utcnow

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_items (
    fingerprint    TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    external_id    TEXT NOT NULL,
    title          TEXT NOT NULL,
    url            TEXT NOT NULL,
    summary        TEXT DEFAULT '',
    body           TEXT DEFAULT '',
    author         TEXT DEFAULT '',
    published_at   TEXT,
    score          INTEGER DEFAULT 0,
    comments       INTEGER DEFAULT 0,
    tags           TEXT DEFAULT '[]',
    discussion_url TEXT DEFAULT '',
    discovered_at  TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'new',
    UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_items_status ON source_items (status, score DESC);

CREATE TABLE IF NOT EXISTS drafts (
    fingerprint        TEXT PRIMARY KEY,
    source_fingerprint TEXT NOT NULL,
    caption            TEXT NOT NULL,
    hashtags           TEXT DEFAULT '[]',
    slides             TEXT NOT NULL,
    alt_text           TEXT DEFAULT '',
    cover_layout       TEXT DEFAULT 'cover',
    sources_note       TEXT DEFAULT '',
    image_paths        TEXT DEFAULT '[]',
    status             TEXT NOT NULL DEFAULT 'draft',
    created_at         TEXT NOT NULL,
    scheduled_for      TEXT,
    published_at       TEXT,
    instagram_media_id TEXT DEFAULT '',
    permalink          TEXT DEFAULT '',
    error              TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts (status, created_at DESC);

CREATE TABLE IF NOT EXISTS model_cooldowns (
    model        TEXT PRIMARY KEY,
    until        TEXT NOT NULL,
    reason       TEXT DEFAULT '',
    hits         INTEGER DEFAULT 1,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    items_seen INTEGER DEFAULT 0,
    drafts_made INTEGER DEFAULT 0,
    published  INTEGER DEFAULT 0,
    notes      TEXT DEFAULT ''
);
"""


class Store:
    """Thin data-access layer over SQLite. No ORM, no magic."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        """Close the connection, folding the WAL back into the database file.

        The checkpoint matters more than it looks: on CI the database file is
        the only state that travels between runs, and a live `-wal` sidecar
        holding the last few transactions would not travel with it. A clean
        close leaves everything in the single file.
        """
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:  # pragma: no cover - close must not raise
            log.warning("could not checkpoint the WAL before closing: %s", exc)
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ── source items ──────────────────────────────────────────────────────

    def add_items(self, items: Iterable[SourceItem]) -> int:
        """Insert new items, ignoring ones we have already collected."""
        cur = self._conn.cursor()
        inserted = 0
        now = iso(utcnow())
        for item in items:
            row = item.to_row()
            cur.execute(
                """
                INSERT OR IGNORE INTO source_items (
                    fingerprint, source, external_id, title, url, summary, body,
                    author, published_at, score, comments, tags, discussion_url,
                    discovered_at, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new')
                """,
                (
                    item.fingerprint,
                    row["source"],
                    row["external_id"],
                    row["title"],
                    row["url"],
                    row["summary"],
                    row["body"],
                    row["author"],
                    row["published_at"],
                    row["score"],
                    row["comments"],
                    json.dumps(row["tags"]),
                    row["discussion_url"],
                    now,
                ),
            )
            inserted += cur.rowcount
        return inserted

    def has_drafted(self, source_fingerprint: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM drafts WHERE source_fingerprint = ? LIMIT 1",
            (source_fingerprint,),
        ).fetchone()
        return row is not None

    def candidate_items(self, limit: int, min_score: int) -> list[SourceItem]:
        """Unclaimed items above the score bar, best first. Does NOT claim them.

        Peeking and claiming are separate on purpose: selection ranks across
        sources and may discard candidates, and marking them used here would
        strand items that were never actually written about.
        """
        rows = self._conn.execute(
            """
            SELECT * FROM source_items
            WHERE status = 'new' AND score >= ?
            ORDER BY score DESC, discovered_at DESC
            LIMIT ?
            """,
            (min_score, limit),
        ).fetchall()

        candidates: list[SourceItem] = []
        for row in rows:
            if self.has_drafted(row["fingerprint"]):
                # Already written about in a previous run: retire it so it
                # stops competing for a slot.
                self.mark_used([row["fingerprint"]])
                continue
            candidates.append(_row_to_item(row))
        return candidates

    def mark_used(self, fingerprints: Iterable[str]) -> None:
        """Claim items so no other run writes about them."""
        self._conn.executemany(
            "UPDATE source_items SET status = 'used' WHERE fingerprint = ?",
            [(fp,) for fp in fingerprints],
        )

    def release_item(self, source_fingerprint: str) -> None:
        """Return a claimed item to the pool (e.g. LLM failed)."""
        self._conn.execute(
            "UPDATE source_items SET status = 'new' WHERE fingerprint = ?",
            (source_fingerprint,),
        )

    # ── drafts ───────────────────────────────────────────────────────────

    def save_draft(self, draft: Draft) -> None:
        # Upsert the source item first. A draft is useless without it, and
        # list_drafts() joins on source_items - so without this, a draft built
        # from an item that was never persisted becomes invisible.
        self.add_items([draft.source])
        self._conn.execute(
            """
            INSERT INTO drafts (
                fingerprint, source_fingerprint, caption, hashtags, slides,
                alt_text, cover_layout, sources_note, image_paths, status,
                created_at, scheduled_for, published_at, instagram_media_id,
                permalink, error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'')
            ON CONFLICT(fingerprint) DO UPDATE SET
                caption=excluded.caption,
                hashtags=excluded.hashtags,
                slides=excluded.slides,
                alt_text=excluded.alt_text,
                cover_layout=excluded.cover_layout,
                sources_note=excluded.sources_note,
                image_paths=excluded.image_paths,
                status=excluded.status,
                scheduled_for=excluded.scheduled_for
            """,
            (
                draft.fingerprint,
                draft.source.fingerprint,
                draft.caption,
                json.dumps(draft.hashtags),
                json.dumps([slide.to_dict() for slide in draft.slides]),
                draft.alt_text,
                draft.cover_layout,
                draft.sources_note,
                json.dumps(draft.image_paths),
                draft.status,
                iso(draft.created_at),
                iso(draft.scheduled_for),
                iso(draft.published_at),
                draft.instagram_media_id,
                draft.permalink,
            ),
        )

    def get_draft(self, fingerprint: str) -> Draft | None:
        row = self._conn.execute(
            "SELECT * FROM drafts WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if row is None:
            return None
        item_row = self._conn.execute(
            "SELECT * FROM source_items WHERE fingerprint = ?",
            (row["source_fingerprint"],),
        ).fetchone()
        if item_row is None:
            return None
        return _row_to_draft(row, _row_to_item(item_row))

    def list_drafts(
        self,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Draft]:
        if status:
            rows = self._conn.execute(
                """
                SELECT * FROM drafts WHERE status = ?
                ORDER BY COALESCE(scheduled_for, created_at) DESC
                LIMIT ? OFFSET ?
                """,
                (status, limit, offset),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM drafts ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return self._hydrate(rows)

    def _hydrate(self, rows: list[sqlite3.Row]) -> list[Draft]:
        """Join drafts to their source items, in one query rather than N+1."""
        if not rows:
            return []

        fingerprints = {row["source_fingerprint"] for row in rows}
        placeholders = ",".join("?" * len(fingerprints))
        item_rows = self._conn.execute(
            f"SELECT * FROM source_items WHERE fingerprint IN ({placeholders})",
            tuple(fingerprints),
        ).fetchall()
        items = {row["fingerprint"]: _row_to_item(row) for row in item_rows}

        drafts: list[Draft] = []
        for row in rows:
            item = items.get(row["source_fingerprint"])
            if item is None:
                # Should not happen now that save_draft upserts the source, but
                # surfacing it beats returning a silently short list.
                log.warning(
                    "draft %s has no source item (%s) and was skipped",
                    row["fingerprint"], row["source_fingerprint"],
                )
                continue
            drafts.append(_row_to_draft(row, item))
        return drafts

    def due_drafts(self, now: datetime) -> list[Draft]:
        """Approved/scheduled drafts whose time has arrived."""
        rows = self._conn.execute(
            """
            SELECT * FROM drafts
            WHERE status = 'scheduled' AND scheduled_for IS NOT NULL
              AND scheduled_for <= ?
            ORDER BY scheduled_for ASC
            """,
            (iso(now),),
        ).fetchall()
        return self._hydrate(rows)

    def next_approved(self) -> Draft | None:
        """The oldest approved draft, i.e. the next one to post, or None.

        FIFO on `created_at`, not on the approval order: drafts are generated
        ahead of time and approved in batches, so "first approved" is the one
        that has been waiting longest. The publisher takes exactly one, so a
        retried workflow run can never double-post a carousel.
        """
        row = self._conn.execute(
            """
            SELECT * FROM drafts
            WHERE status = 'approved'
            ORDER BY created_at ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        drafts = self._hydrate([row])
        return drafts[0] if drafts else None

    def update_draft(
        self,
        fingerprint: str,
        *,
        status: str | None = None,
        scheduled_for: datetime | None = None,
        image_paths: list[str] | None = None,
        media_id: str | None = None,
        permalink: str | None = None,
        published_at: datetime | None = None,
        error: str | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("status", status),
            ("scheduled_for", iso(scheduled_for) if scheduled_for else None),
            # The column is instagram_media_id; `media_id` is the caller's
            # vocabulary. Passing the wrong one through used to raise
            # "no such column", which broke every successful publish.
            ("instagram_media_id", media_id),
            ("permalink", permalink),
            ("published_at", iso(published_at) if published_at else None),
            ("error", error),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)
        if image_paths is not None:
            sets.append("image_paths = ?")
            params.append(json.dumps(image_paths))
        if not sets:
            return
        params.append(fingerprint)
        self._conn.execute(
            f"UPDATE drafts SET {', '.join(sets)} WHERE fingerprint = ?", params
        )

    def counts(self) -> dict[str, int]:
        def scalar(sql: str, *args: object) -> int:
            row = self._conn.execute(sql, args).fetchone()
            return int(row[0]) if row else 0

        return {
            "items": scalar("SELECT COUNT(*) FROM source_items"),
            "items_new": scalar(
                "SELECT COUNT(*) FROM source_items WHERE status = 'new'"
            ),
            "drafts": scalar("SELECT COUNT(*) FROM drafts"),
            "draft": scalar("SELECT COUNT(*) FROM drafts WHERE status = 'draft'"),
            "approved": scalar("SELECT COUNT(*) FROM drafts WHERE status = 'approved'"),
            "scheduled": scalar(
                "SELECT COUNT(*) FROM drafts WHERE status = 'scheduled'"
            ),
            "published": scalar("SELECT COUNT(*) FROM drafts WHERE status = 'published'"),
        }

    # ── model cooldowns ──────────────────────────────────────────────────

    def cooldown_model(self, model: str, minutes: int, reason: str) -> None:
        from datetime import timedelta

        until = iso(utcnow() + timedelta(minutes=minutes))
        self._conn.execute(
            """
            INSERT INTO model_cooldowns (model, until, reason, hits, updated_at)
            VALUES (?,?,?,1,?)
            ON CONFLICT(model) DO UPDATE SET
                until=excluded.until,
                reason=excluded.reason,
                hits=model_cooldowns.hits + 1,
                updated_at=excluded.updated_at
            """,
            (model, until, reason, iso(utcnow())),
        )

    def cooled_down_models(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT model FROM model_cooldowns WHERE until > ?", (iso(utcnow()),)
        ).fetchall()
        return {row["model"] for row in rows}

    def clear_cooldowns_for(self, model: str) -> None:
        """A model that just succeeded should not stay benched."""
        self._conn.execute("DELETE FROM model_cooldowns WHERE model = ?", (model,))

    def clear_cooldowns(self) -> None:
        self._conn.execute("DELETE FROM model_cooldowns")

    # ── run bookkeeping ──────────────────────────────────────────────────

    def start_run(self) -> int:
        cur = self._conn.execute(
            "INSERT INTO runs (started_at) VALUES (?)", (iso(utcnow()),)
        )
        return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, notes: str = "") -> None:
        self._conn.execute(
            "UPDATE runs SET ended_at = ? WHERE id = ?", (iso(utcnow()), run_id)
        )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _row_to_item(row: sqlite3.Row) -> SourceItem:
    return SourceItem(
        source=row["source"],
        external_id=row["external_id"],
        title=row["title"],
        url=row["url"],
        summary=row["summary"] or "",
        body=row["body"] or "",
        author=row["author"] or "",
        published_at=_parse_dt(row["published_at"]),
        score=int(row["score"] or 0),
        comments=int(row["comments"] or 0),
        tags=json.loads(row["tags"] or "[]"),
        discussion_url=row["discussion_url"] or "",
    )


def _row_to_draft(row: sqlite3.Row, item: SourceItem) -> Draft:
    return Draft(
        source=item,
        caption=row["caption"],
        slides=[Slide.from_dict(s) for s in json.loads(row["slides"])],
        hashtags=json.loads(row["hashtags"] or "[]"),
        alt_text=row["alt_text"] or "",
        cover_layout=row["cover_layout"] or "cover",
        sources_note=row["sources_note"] or "",
        created_at=_parse_dt(row["created_at"]) or utcnow(),
        image_paths=json.loads(row["image_paths"] or "[]"),
        status=row["status"],
        scheduled_for=_parse_dt(row["scheduled_for"]),
        published_at=_parse_dt(row["published_at"]),
        instagram_media_id=row["instagram_media_id"] or "",
        permalink=row["permalink"] or "",
    )
