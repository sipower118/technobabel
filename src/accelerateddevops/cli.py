"""Command line interface.

Commands mirror the pipeline stages so you can inspect and intervene at any
point: `collect`, `generate`, `render`, `list`, `approve`, `schedule`,
`publish`, `run`, `daemon`, `doctor`. Each stage also has its own GitHub
Actions workflow, so they can be triggered independently.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta

from . import __version__
from .config import Settings, load_settings
from .llm import LLMUnavailable, build_provider
from .models import Draft, utcnow
from .pipeline import (
    assign_schedule,
    next_slot_after,
    run_all,
    run_collect,
    run_generate,
    run_publish_due,
    select_items,
)
from .publish import InstagramPublisher, PublishError
from .store import Store

# ── output helpers ───────────────────────────────────────────────────────


def _c(text: str, code: str) -> str:
    """ANSI colour, disabled when not writing to a real terminal."""
    if not sys.stdout.isatty() or sys.platform == "win32" and not _supports_colour():
        return text
    return f"\033[{code}m{text}\033[0m"


def _supports_colour() -> bool:
    import os

    return bool(os.environ.get("ANSICON") or os.environ.get("WT_SESSION"))


BOLD = lambda s: _c(s, "1")  # noqa: E731
DIM = lambda s: _c(s, "2")  # noqa: E731
GREEN = lambda s: _c(s, "32")  # noqa: E731
YELLOW = lambda s: _c(s, "33")  # noqa: E731
RED = lambda s: _c(s, "31")  # noqa: E731
CYAN = lambda s: _c(s, "36")  # noqa: E731


def ok(msg: str) -> None:
    print(f"{GREEN('ok')}   {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW('warn')} {msg}")


def err(msg: str) -> None:
    print(f"{RED('err')}  {msg}")


def info(msg: str) -> None:
    print(f"{DIM('..')}   {msg}")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    if not verbose:
        for noisy in ("httpx", "httpcore", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


# ── commands ─────────────────────────────────────────────────────────────


def cmd_collect(args: argparse.Namespace, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        seen, new = run_collect(store, settings)
    ok(f"collected {seen} items from all sources, {new} new")
    return 0


def cmd_generate(args: argparse.Namespace, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        try:
            # The store is passed in so exhausted models stay benched between
            # runs instead of being rediscovered as 429s every time.
            provider = build_provider(settings, store)
        except LLMUnavailable as exc:
            err(str(exc))
            return 2
        if args.limit:
            settings = _with_overrides(settings, posts_per_run=args.limit)
        try:
            drafts = run_generate(store, settings, provider, auto_approve=args.approve)
        except LLMUnavailable as exc:
            # One clear line naming the actual problem, rather than one
            # failure per selected item.
            err(str(exc))
            return 2
    if not drafts:
        warn("no drafts produced (no new items, or every generation failed)")
        return 1
    for draft in drafts:
        ok(f"{draft.fingerprint}  {len(draft.slides)} slides  {draft.slides[0].headline[:56]}")
    info(f"images in {settings.output_dir}")
    return 0


def cmd_render(args: argparse.Namespace, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        drafts = (
            [d for d in (store.get_draft(args.fingerprint),) if d]
            if args.fingerprint
            else store.list_drafts(status=args.status, limit=args.limit)
        )
        if not drafts:
            warn("no drafts matched")
            return 1
        for draft in drafts:
            from .pipeline import render_carousel

            render_carousel(draft, settings, store)
            ok(f"rendered {len(draft.image_paths)} slides for {draft.fingerprint}")
    return 0


def cmd_list(args: argparse.Namespace, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        if args.ids:
            # One fingerprint per line, oldest first, for shell pipelines. The
            # human view is sorted newest first, which is the wrong end to take
            # from when a workflow is picking what to publish next.
            for draft in reversed(store.list_drafts(status=args.status, limit=args.limit)):
                print(draft.fingerprint)
            return 0
        drafts = store.list_drafts(status=args.status, limit=args.limit)
        counts = store.counts()

    print(BOLD("library"))
    print(
        f"  items {counts['items']} (new {counts['items_new']})  "
        f"drafts {counts['drafts']}  approved {counts['approved']}  "
        f"scheduled {counts['scheduled']}  published {counts['published']}"
    )
    if not drafts:
        info("no drafts yet - run `accelerated-devops run`")
        return 0

    print()
    for draft in drafts:
        when = (draft.scheduled_for or draft.created_at).strftime("%Y-%m-%d %H:%M")
        status = _status_colour(draft.status)
        print(f"{status} {BOLD(draft.fingerprint)}  {DIM(when)}  {len(draft.slides)} slides")
        print(f"     {draft.slides[0].headline[:74] if draft.slides else '(no slides)'}")
        print(f"     {DIM(draft.source.source + '  ' + draft.source.title[:56])}")
    return 0


def _status_colour(status: str) -> str:
    return {
        "published": GREEN(status),
        "scheduled": CYAN(status),
        "approved": CYAN(status),
        "draft": YELLOW(status),
    }.get(status, status)


def cmd_approve(args: argparse.Namespace, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        for fingerprint in args.fingerprints:
            draft = store.get_draft(fingerprint)
            if draft is None:
                err(f"{fingerprint} not found")
                continue
            store.update_draft(fingerprint, status="approved")
            ok(f"approved {fingerprint}  {draft.slides[0].headline[:52] if draft.slides else ''}")
    return 0


def cmd_reject(args: argparse.Namespace, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        for fingerprint in args.fingerprints:
            if store.get_draft(fingerprint) is None:
                err(f"{fingerprint} not found")
                continue
            store.update_draft(fingerprint, status="rejected")
            ok(f"rejected {fingerprint}")
    return 0


def cmd_schedule(args: argparse.Namespace, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        scheduled = assign_schedule(store, settings)
    if not scheduled:
        warn("nothing approved to schedule")
        return 1
    for draft in scheduled:
        ok(f"{draft.fingerprint} -> {draft.scheduled_for:%Y-%m-%d %H:%M} UTC")
    info(f"slots: {', '.join(settings.publish_slots)}")
    return 0


def _publish_and_record(store: Store, publisher, draft: Draft, settings: Settings) -> int:
    """Publish one draft and persist the outcome. Shared by the publish paths."""
    try:
        result = publisher.publish_draft(draft)
    except PublishError as exc:
        err(str(exc))
        store.update_draft(draft.fingerprint, error=str(exc))
        return 1
    store.update_draft(
        draft.fingerprint, status="published", published_at=utcnow(),
        media_id=result.media_id, permalink=result.permalink,
    )
    ok(f"published {draft.fingerprint} -> {result.permalink or result.media_id}")
    return 0


def cmd_publish(args: argparse.Namespace, settings: Settings) -> int:
    """Publish one draft.

    With no arguments this posts the single oldest approved draft and nothing
    else, which is what the scheduled workflow wants: one carousel per run, so
    a retried run cannot double-post. `--due` switches to the slot-based
    behaviour used by the resident daemon.
    """
    if not settings.can_publish:
        err("Instagram is not publishable: set IG_USER_ID, IG_ACCESS_TOKEN and PUBLIC_ASSET_BASE_URL")
        info("run `accelerated-devops doctor` to see what is missing")
        return 2

    with Store(settings.db_path) as store:
        if args.due:
            count = run_publish_due(store, settings)
            ok(f"published {count} post(s)") if count else info("nothing due to publish")
            return 0

        if args.fingerprint:
            draft = store.get_draft(args.fingerprint)
            if draft is None:
                err(f"{args.fingerprint} not found")
                return 1
        else:
            draft = store.next_approved()
            if draft is None:
                info("nothing approved to publish - approve a draft first")
                return 0

        with InstagramPublisher(settings) as publisher:
            return _publish_and_record(store, publisher, draft, settings)


def cmd_check_token(args: argparse.Namespace, settings: Settings) -> int:
    """Validate IG_USER_ID / IG_ACCESS_TOKEN against the live Graph API."""
    ig = settings.instagram
    if not ig.user_id:
        err("IG_USER_ID is not set")
        return 2
    with InstagramPublisher(settings) as publisher:
        try:
            account = publisher.check_token()
        except PublishError as exc:
            err(str(exc))
            return 1
    user_id = account.get("user_id") or ig.user_id
    ok(f"token valid for IG user {user_id} (@{account.get('username', '?')})")
    if str(ig.user_id) != str(user_id):
        warn(f"IG_USER_ID in .env ({ig.user_id}) does not match the token's user ({user_id})")
        return 1
    return 0


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    provider = None
    with Store(settings.db_path) as store:
        if not args.no_llm:
            try:
                provider = build_provider(settings, store)
            except LLMUnavailable as exc:
                warn(str(exc))
                warn("continuing with collection and publishing only")
        if provider is not None:
            settings = _with_overrides(settings, posts_per_run=args.posts)
        try:
            summary = run_all(
                store, settings, provider,
                auto_approve=args.approve, auto_schedule=args.approve,
            )
        except LLMUnavailable as exc:
            err(str(exc))
            warn("collection still completed; fix the LLM config and re-run")
            return 2
    print()
    ok(
        f"collected {summary.collected} ({summary.new_items} new)  "
        f"drafted {summary.drafted}  scheduled {summary.scheduled}  "
        f"published {summary.published}"
    )
    return 0


def cmd_daemon(args: argparse.Namespace, settings: Settings) -> int:
    """Stay resident: run a cycle, then wake at the next publish slot."""
    print(BOLD("accelerated-devops daemon"))
    print(f"  slots: {', '.join(settings.publish_slots)} (local time)")
    print("  Ctrl+C to stop")
    print()

    with Store(settings.db_path) as store:
        try:
            provider = build_provider(settings, store)
        except LLMUnavailable as exc:
            err(str(exc))
            return 2
        while True:
            try:
                summary = run_all(
                    store, settings, provider,
                    auto_approve=args.approve, auto_schedule=args.approve,
                )
                print(DIM(f"  {utcnow():%H:%M:%S}  cycle done, "
                           f"{summary.drafted} drafted, {summary.published} published"))
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - a daemon must not die
                err(f"cycle failed: {exc}")
                import traceback

                traceback.print_exc()

            target = next_slot_after(utcnow(), settings.publish_slots)
            wait = max(60, int((target - utcnow()).total_seconds()))
            print(DIM(f"  next slot {target:%Y-%m-%d %H:%M} UTC, sleeping {wait // 60}m"))
            try:
                time.sleep(wait)
            except KeyboardInterrupt:
                break
    print("stopped")
    return 0


def _is_private_host(url: str) -> bool:
    """True when a URL is not reachable from the public internet.

    Instagram fetches the slide itself, so localhost, 127.0.0.1, a LAN IP or
    a plain http:// tunnel-less host can never work. Worth naming explicitly:
    the failure surfaces later as Meta's misleading 9004/2207052.
    """
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if not host:
        return True
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if host.endswith((".local", ".internal", ".localhost")):
        return True
    if host.startswith("192.168.") or host.startswith("10."):
        return True
    if host.startswith("172."):
        try:
            second_octet = int(host.split(".")[1])
        except (IndexError, ValueError):
            return False
        if 16 <= second_octet <= 31:
            return True
    # Anything not served over https is not fetchable by Meta.
    return not url.lower().startswith("https://")


def cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    """Report what is configured, what is missing, and what is broken."""
    print(BOLD("accelerated-devops doctor"))
    print()
    problems = 0

    print(BOLD("llm"))
    if settings.gemini_api_keys:
        ok(f"{len(settings.gemini_api_keys)} API key(s) configured")
    else:
        err("no GEMINI_API_KEYS - generation is disabled")
        problems += 1
    if settings.gemini_models:
        info(f"model ladder: {' -> '.join(settings.gemini_models)}")
    else:
        err("no GEMINI_MODELS")
        problems += 1

    print()
    print(BOLD("instagram"))
    ig = settings.instagram
    if ig.user_id and ig.access_token:
        ok("credentials present")
    else:
        warn("IG_USER_ID / IG_ACCESS_TOKEN missing - cannot publish")
        problems += 1
    assets = settings.assets
    if assets.base_url:
        ok(f"slides are served from: {assets.base_url}")
        if _is_private_host(assets.base_url):
            err("PUBLIC_ASSET_BASE_URL points at a private address - Instagram "
                "downloads the image from the public internet and cannot reach it")
            info("use your GitHub Pages site: https://<owner>.github.io/<repo>")
            problems += 1
    else:
        warn("PUBLIC_ASSET_BASE_URL missing - Instagram cannot fetch the images")
        info("set it to the root of the Pages site, e.g. "
             "https://<owner>.github.io/<repo>")
        problems += 1

    if ig.is_configured:
        with InstagramPublisher(settings) as publisher:
            try:
                account = publisher.account_info()
                ok(f"connected as @{account.get('username')} "
                   f"({account.get('followers_count', 0)} followers)")
            except PublishError as exc:
                err(f"could not reach Instagram: {exc}")
                problems += 1

    print()
    print(BOLD("sources"))
    from .sources import HackerNewsSource, RedditSource, RssSource

    info(f"rss: {len(RssSource().feeds)} feeds, {settings.lookback_hours}h window")
    info(f"hackernews: >={settings.min_hn_points} points, {settings.hn_lookback_hours}h window")

    reddit = RedditSource(
        min_comments=settings.min_reddit_comments,
        client_id=settings.reddit_client_id,
        client_secret=settings.reddit_client_secret,
        contact=settings.source_contact_email or None,
    )
    # Half-configured credentials are the common typo: the token request would
    # fail 401 and the source would silently degrade. Say so here instead.
    if reddit.has_oauth:
        ok(f"reddit: OAuth configured, >={settings.min_reddit_comments} comments")
    elif settings.reddit_client_id or settings.reddit_client_secret:
        err("reddit: only one of REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET is set")
    else:
        info(f"reddit: >={settings.min_reddit_comments} comments, unauthenticated "
             "(set REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET; public .json is "
             "403 on most datacentre IPs)")
    if not settings.source_contact_email:
        warn("SOURCE_CONTACT_EMAIL not set - Reddit requires a contact in the User-Agent")

    print()
    print(BOLD("fonts"))
    from .render.fonts import available_roles

    for role, path in available_roles().items():
        name = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] if path else "NONE"
        (ok if path else err)(f"{role:<11} {name}")

    print()
    print(BOLD("storage"))
    settings.ensure_dirs()
    if settings.db_path.exists():
        with Store(settings.db_path) as store:
            counts = store.counts()
        ok(f"{settings.db_path}  ({counts['drafts']} drafts, {counts['published']} published)")
    else:
        info(f"{settings.db_path} (will be created)")

    print()
    if problems:
        err(f"{problems} thing(s) need attention before a full run")
        return 1
    ok("everything required for an unattended run is configured")
    return 0


# ── argument parsing ─────────────────────────────────────────────────────


def _with_overrides(settings: Settings, **overrides: object) -> Settings:
    """Return a copy with a few fields changed (Settings is frozen)."""
    import dataclasses

    return dataclasses.replace(settings, **overrides)  # type: ignore[arg-type]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="accelerated-devops",
        description="Turn DevOps / platform-engineering sources into Instagram carousels.",
    )
    parser.add_argument("--version", action="version", version=f"accelerated-devops {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("collect", help="scrape all sources into the item pool")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("generate", help="write drafts for the best new items")
    # --posts is the same option as on `run`, so `generate --posts 3` and
    # `run --posts 3` mean the same thing.
    p.add_argument("--limit", "--posts", dest="limit", type=int, help="max drafts this run")
    p.add_argument("--approve", action="store_true", help="auto-approve the drafts")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("render", help="re-render slides for existing drafts")
    p.add_argument("fingerprint", nargs="?", help="one draft; omit to render many")
    p.add_argument("--status", default="draft", help="which status to re-render")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("list", help="show drafts and library counts")
    p.add_argument("--status", help="filter: draft/approved/scheduled/published/rejected")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--ids", action="store_true",
                   help="print fingerprints only, oldest first, for scripting")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("approve", help="approve drafts for scheduling")
    p.add_argument("fingerprints", nargs="+")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject", help="reject drafts")
    p.add_argument("fingerprints", nargs="+")
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("schedule", help="assign approved drafts to the next publish slots")
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser(
        "publish",
        help="publish the oldest approved draft (or a specific one)",
    )
    p.add_argument("fingerprint", nargs="?",
                   help="publish this draft instead of the oldest approved one")
    p.add_argument("--due", action="store_true",
                   help="publish everything whose scheduled slot has arrived")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("check-token", help="verify the Instagram token against the live API")
    p.set_defaults(func=cmd_check_token)

    p = sub.add_parser("run", help="one full cycle: collect, generate, render, publish")
    p.add_argument("--posts", type=int, help="posts to generate this run")
    p.add_argument("--approve", action="store_true",
                   help="auto-approve, schedule and publish without review")
    p.add_argument("--no-llm", action="store_true", help="collect and publish only")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("daemon", help="stay resident and run a cycle each publish slot")
    p.add_argument("--approve", action="store_true",
                   help="auto-approve, schedule and publish without review")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("doctor", help="check configuration and connectivity")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    settings = load_settings()
    settings.ensure_dirs()
    try:
        return int(args.func(args, settings) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
