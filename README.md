# accelerated.devops

Turns DevOps / platform-engineering reading into Instagram carousels.

It scrapes engineering blogs, Hacker News and Reddit, picks the best on-topic
items, writes a carousel with Gemini, renders designed slides with Pillow, and
publishes through the official Instagram Graph API on a schedule.

```
collect  ->  select  ->  generate (Gemini)  ->  render (Pillow)  ->  review  ->  schedule  ->  publish (Graph API)
```

Every stage is a separate command, so you can stop and look at the output at any
point. Nothing auto-publishes without `--approve`.

---

## Quick start

```bash
pip install -e .
copy .env.example .env                # then fill in GEMINI_API_KEYS
accelerated-devops doctor             # tells you exactly what is still missing
accelerated-devops collect            # fill the item pool
accelerated-devops generate --limit 1 # write + render one draft
accelerated-devops list               # review it
```

Windows: `copy .env.example .env`. Everywhere else: `cp .env.example .env`.

## Commands

| Command | What it does |
| --- | --- |
| `collect` | Fetch all sources into the item pool. Safe to run any time. |
| `generate` | Pick the best unused items, write carousels, render slides. |
| `render [fp]` | Re-render slides for an existing draft (same seed, same result). |
| `list` | Show drafts, statuses, and library counts. |
| `approve <fp>...` / `reject <fp>...` | Review triage. |
| `schedule` | Place approved drafts into the next free publish slots. |
| `publish [fp]` | Post the oldest approved draft, or one specific draft. `--due` posts everything whose slot has arrived. |
| `upload-assets [fp]` | Push the rendered slides to the asset backend without posting them. |
| `check-token` | Verify the Instagram token against the live API. |
| `run` | One full cycle: collect, generate, schedule, publish. |
| `daemon` | Stay resident and run a cycle at each publish slot. |
| `doctor` | Check config, connectivity, fonts, and what is missing. |

`--limit N` / `--posts N` sets how many drafts a run produces (default 2).
`--approve` turns on full automation. Without it, `run` only drafts.

`publish` with no arguments is the CI contract: it posts **exactly one**
carousel — the draft that has been approved longest — and nothing else. One post
per run means a retried workflow cannot double-post, because Instagram has no
undo. Failures leave the draft `approved` with the error recorded, so a retry
picks up the same post rather than losing it.

`list --ids` prints fingerprints only, oldest first, which is what the approve
and reject workflows feed to the CLI instead of scraping the human output.

---

## GitHub Actions

Each stage is its own workflow under `.github/workflows/`, so you can trigger
one without touching the others, on a schedule or by hand:

| Workflow | Trigger | Effect |
| --- | --- | --- |
| `collect.yml` | every 3h + manual | Refresh the item pool |
| `generate.yml` | 06:07 UTC + manual | Write and render new drafts |
| `render.yml` | 06:41 UTC + manual | Re-render, then push the slides to gh-pages |
| `approve.yml` | manual only | Move drafts into the publish queue |
| `reject.yml` | manual only | Drop drafts from the queue |
| `publish.yml` | 09:03 / 17:03 UTC + manual | Post exactly one approved draft |

`approve` is deliberately manual: it is the review gate, and automating it is a
one-line change (add a `schedule:` block) if you decide you do not want one.

### gh-pages as the artefact repository

A runner is a fresh machine on every run, so there is no `data/` to inherit.
The `gh-pages` branch holds everything that has to outlive the run:

```
<some-fingerprint>/<fingerprint>_01.jpg   the slides Instagram downloads
state/accelerated_devops.sqlite3          the database
```

Two composite actions keep the workflows short and the knowledge in one place:

Two composite actions keep the workflows short and the knowledge in one place:

- `.github/actions/setup` — checks out the code, installs with `uv`, and exports
  the configuration to later steps. It takes the settings as **inputs**: a
  composite action cannot read the `secrets` or `vars` contexts, because those
  exist only in the calling workflow. Each workflow therefore passes them
  through `with:`, which is also why the settings are visible and editable in
  the workflow file rather than buried in the action.
- `.github/actions/state_restore` — checks gh-pages out into `.pages` and
  copies the database into `data/`.
- `.github/actions/state_commit` — copies the database back, commits the whole
  checkout (database *and* staged slides) and pushes.

Both are unconditional: one does one thing. Neither takes a credential, and
neither takes an input that chooses what it does — the push reuses the auth
header `actions/checkout` already left in the checkout's `.git/config`.

A reusable workflow cannot do this job in place of the action: a called
workflow's jobs run on *separate* runners, so anything it writes to
`$GITHUB_ENV` is invisible to the caller.

A rejected push is rebased rather than clobbering whatever moved the branch in
the meantime. An empty `-wal` is checkpointed away by `Store.close()` and is not
committed as zero-byte noise; a non-empty one is real unmerged state and does
travel.

Only the JPEG siblings are kept, not the PNGs — a carousel is ~200 KB of PNG per
post and only the JPEG ever reaches Instagram. `publish` on a clean runner
therefore **fetches the slides back from the public URL** rather than
re-rendering them, so the bytes Meta downloads are byte-for-byte the ones that
were reviewed on the Pages site.

Note that `state/` is inside the published site, so the database is publicly
downloadable. It holds draft text, source URLs and media ids — no credentials —
but it is not private.

### Repository secrets and variables

Secrets → Settings → Secrets: `GEMINI_API_KEYS`, `IG_USER_ID`,
`IG_ACCESS_TOKEN`, and optionally `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`.

That is the whole list. The pipeline code holds no write credential and never
calls the GitHub API — it stages images and reads public URLs. Publishing to
the Pages branch is git, done by the workflow using the credentials
`actions/checkout` already provides.

Variables → Settings → Secrets and variables → Actions → Variables:
`PUBLIC_ASSET_BASE_URL` (the deployed Pages URL, e.g.
`https://<owner>.github.io/<repo>`). `ASSET_STORAGE`, `ASSET_LOCAL_DIR`,
`GITHUB_PAGES_BRANCH` and `PAGES_SETTLE_SECONDS` all have sensible defaults in
the setup action and only need setting if you want something unusual.

Two one-time repo settings the workflows depend on:

1. **Pages** → Settings → Pages → Deploy from a branch → `gh-pages` / `/ (root)`.
2. **Actions** → General → Workflow permissions → Read and write. The built-in
   `GITHUB_TOKEN` cannot push to `gh-pages` without it.

---

## Configuration

Everything is environment-driven; see `.env.example` for the annotated list.

### Gemini

```ini
GEMINI_API_KEYS=key_one,key_two
GEMINI_MODELS=gemini-2.5-pro,gemini-2.5-flash,gemini-2.5-flash-lite
```

Keys and models form a **ladder**, not a fallback list. The client walks it:

```
for model in models:        # pro -> flash -> flash-lite
    for key in keys:
        try generate
        quota (429)  -> bench this model for 30 min, drop to the next model
        bad key      -> skip this key, keep the model available
        access (403) -> bench this model for 3 min
        transient    -> bench this model for 3 min
```

Gemini quota is per *(project, model)* pair, so exhausting `pro` says nothing
about `flash`. A run starts on the strongest model and lands on whatever still
has quota instead of failing. Cooldowns are persisted in SQLite, so a 09:15 run
does not rediscover at 10:00 that pro was already exhausted.

If **every** key is rejected you get one actionable line naming the indexes,
rather than a wall of per-model noise.

### Instagram

```ini
IG_USER_ID=1784...
IG_ACCESS_TOKEN=IGQVJ...
PUBLIC_ASSET_BASE_URL=https://<owner>.github.io/<repo>
```

Set up in Meta (the **Instagram API with Instagram Login** path — no Facebook
Page needed):

1. Create a **Business**-type developer app at <https://developers.facebook.com>
   (use case: **Other**).
2. **Add Product → Instagram → "API setup with Instagram login"**.
3. Keep the scopes Meta defaults to: `instagram_business_basic`,
   `instagram_business_content_publish`, `instagram_business_manage_insights`.
   Uncheck messaging/comment-management scopes you don't need.
4. **Add Instagram account** → log in with the account to post from → copy the
   **IG User ID** and **Access Token** into `.env`.
5. Your Instagram account must be a **Professional (Business or Creator)**
   account, not Personal.

Verify the token without burning a post:

```bash
accelerated-devops check-token
```

That calls `graph.instagram.com/me` with the token from `.env` and prints the
account it belongs to.

Publishing details worth knowing: images must be **JPEG** (the renderer writes
PNG; the publisher converts to a JPEG sibling on the fly), Instagram fetches
them from the public URL in `PUBLIC_ASSET_BASE_URL`, and a carousel is three
steps (item containers → a `CAROUSEL` parent → `/media_publish`). If an item
creation fails, the publisher cleans up the containers it created best-effort;
containers Instagram cannot delete expire on their own within 24 hours.
Captions over 2200 characters are rejected before any API call is spent.

### Where the slides are hosted (required)

Instagram downloads each image from a **public URL itself**, while it creates the
container, and it cannot reach `localhost`. So the rendered slides must live
somewhere public. `ASSET_STORAGE` picks the backend:

| `ASSET_STORAGE` | What it does | Hosting |
| --- | --- | --- |
| `local` (default) | Copies each JPEG into `ASSET_LOCAL_DIR` | You host that folder: nginx, S3, R2, a CDN |
| `pages` | The same copy, then checks the site actually serves each URL before it is handed to Instagram | GitHub Pages serves it |

Both stages into a folder and leave the deploying alone — that is deliberate.
The app has no write credential and never talks to the GitHub API, so pushing
is the workflow's job. On Actions the render workflow points
`ASSET_LOCAL_DIR` at the `gh-pages` checkout it already has and commits the
staged slides in the same commit as the pipeline's database, which needs no
credential beyond the one `actions/checkout` leaves behind.

There is no built-in web server and no tunnel to keep alive — the point of
`pages` is that it works on a throwaway GitHub Actions runner.

#### GitHub Pages (recommended for GitHub Actions)

```ini
ASSET_STORAGE=pages
ASSET_LOCAL_DIR=site
PUBLIC_ASSET_BASE_URL=https://<owner>.github.io/<repo>
GITHUB_PAGES_BRANCH=gh-pages
```

One-time setup: repo **Settings → Pages → Deploy from a branch** → `gh-pages` /
`/ (root)`. The branch itself is created automatically on the first publish.

The workflow needs write access to commit the slides and the database:

```yaml
permissions:
  contents: write
```

`PAGES_SETTLE_SECONDS` (default 90) bounds how long a publish waits for the
Pages CDN to actually serve a just-pushed file — Instagram fetches the URL the
moment the container is created, so publishing too early would 404.

The URL sent to Instagram is the file's path **relative to `ASSET_LOCAL_DIR`**,
not just its file name: the renderer writes `<fingerprint>/<fp>_01.png`, so the
published URL is `https://<owner>.github.io/<repo>/<fingerprint>/<fp>_01.jpg`.
Dropping the fingerprint directory yields a 404, and Instagram reports a failed
image download as `9004 (subcode 2207052) "Only photo or video can be accepted
as media type"` — an error about `media_type` that is really about the URL. That
error is retried up to 3 times and Meta's offending URI is echoed in the
message. `doctor` also rejects a private (localhost/LAN/http) base URL, which
Instagram could never fetch.

---

## Sources

- **36 engineering blogs** via RSS, in four groups:
  - *Enterprise / org-change beat* (priority-weighted): InfoQ, Azure DevOps
    Blog, DORA, DevOps.com, The Pragmatic Engineer, Atlassian.
  - *Industry trends + new ideas*: The New Stack, Changelog, Hugging Face.
  - *Practitioner deep dives* (a specific problem → how a team solved it):
    Spotify, Dropbox, Etsy, Lyft, Engineering at Meta, PostHog, AWS
    Architecture, Semaphore, Honeycomb, Teleport.
  - *Vendor + CNCF + practitioner classics*: Kubernetes, CNCF, Grafana,
    HashiCorp, Datadog, Vercel, Cloudflare, Netflix, GitHub, Docker, AWS,
    Azure, Incrementals, SRE Weekly, Martin Fowler, LeadDev, Last Week in AWS.
- **Hacker News** via the Algolia API, filtered by an on-topic regex rather than
  raw score. Infra stories are genuinely sparse week to week, so HN gets a wider
  window (30 days) and a lower point bar than RSS; the relevance filter is what
  keeps it on-niche. Stories are ranked by points, so the most-upvoted on-topic
  stories are what actually reach the pool.
- **Reddit** (r/devops, r/kubernetes, r/sre, r/PlatformEngineering) — see below.

Scoring is deliberately ranked, not uniform:

- **User interaction is the primary deciding factor.** HN points + comments and
  Reddit votes + comments are genuine popularity signals and are ranked above
  everything else. Comments deliberately outweigh votes — writing a comment
  takes effort and signals real interest, where an upvote is one click. Because
  RSS exposures no engagement data, its items are discounted when ranking, so a
  story proven popular by real people always beats a speculative blog post.
- RSS feeds carry no engagement data, so their items score on topical signal.
  Terms about **big-org constraints** (governance, compliance, change control,
  technical debt, migration in a large enterprise) weigh more than generic
  infra terms — a change-board story beats a routine release note with the same
  keyword count.
- **Product announcements are penalised, not promoted.** Release-note and
  launch copy (`PRODUCT_TERMS`: "announcing", "sign up", "pricing", "free
  trial", …) sinks an entry to the bottom of the pool, so the account shares
  engineering rather than vendor advertising.
- Enterprise / org-change feeds also get an **editorial multiplier**, so the
  account's core beat (slow change in large organisations) competes on equal
  footing with high-volume practitioner blogs.
- Scores are then normalised per source before ranking, otherwise a busy source
  crowds everything else out.

### Reddit needs OAuth credentials

Reddit 403s the public `.json` endpoints from cloud and datacentre ranges, and
no User-Agent reliably defeats it. The supported path is the official Data API.

1. Register a **script** app at <https://www.reddit.com/prefs/apps>
   (`redirect_uri` can be anything, e.g. `http://localhost:8080`).
2. Request Data API access from the app page and **wait for approval** — this
   is the step people miss. Until it's granted, the token request 401s and the
   collector skips Reddit.
3. Put the values in `.env`:

```ini
REDDIT_CLIENT_ID=your-client-id
REDDIT_CLIENT_SECRET=your-client-secret
SOURCE_CONTACT_EMAIL=you@example.com
```

With credentials set, the collector uses the `client_credentials` grant
(app-only, read-only — it never posts) and reads from `oauth.reddit.com` at
100 queries per minute per client id. It honours the `x-ratelimit-*` headers and
pauses when the window is nearly spent, and re-authenticates once if a token
expires mid-run.

Without credentials it probes the public endpoint once and skips cleanly, so a
run is never wasted on 403 retries. RSS and HN carry the account regardless.

> **Terms.** Free-tier access is for approved personal, non-commercial use.
> Commercial use needs a separate agreement with Reddit. Reading discussions to
> decide what to write about is the intended use; re-check before this feeds
> anything monetised.

---

## Rendering

Eight layouts, chosen by the model per slide: `cover`, `bullets`, `stat`,
`contrast`, `steps`, `quote`, `takeaway`, `cta`. Eight palettes, all passing
WCAG AA contrast. 1080×1350, no AI image generation — the design is templated
and deterministic, which is also why the slides look consistent.

Two details worth knowing:

- **Text is auto-fitted**, not truncated. A headline shrinks until it wraps
  inside the safe box, so nothing gets cut mid-sentence.
- **The `stat` layout validates its own number.** If the model hands it a phrase
  instead of a figure, it falls back to `bullets` rather than rendering
  "fewer outages" as if it were data.

Each post gets a deterministic seed derived from its fingerprint: re-rendering
a draft reproduces it exactly, while consecutive posts differ.

---

## Scheduling

`PUBLISH_SLOTS=09:15,17:45` (local time) is the default twice-daily cadence.
`schedule` fills the next free slots, skipping ones already taken, and overflow
rolls into the next day.

Two ways to run it:

```bash
accelerated-devops daemon --approve   # resident process, wakes at each slot
```

Or with Task Scheduler / cron, one cycle per invocation:

```cron
15 9,17 * * *  cd /path/to/accelerated.devops && accelerated-devops run --approve
```

`daemon` is simpler and keeps the asset server warm. A scheduled task survives
reboots and is easier to monitor.

---

## Monetisation (later phase)

Off by default, and that is deliberate: an early account with affiliate links
reads as spam and suppresses reach.

```ini
MONETIZATION_ENABLED=false
CTA_TEXT=Full write-up and config in bio.
AFFILIATE_URL=https://...
```

When enabled, the CTA is appended to the caption and to the last slide, without
duplicating text already in the caption. The wiring is done and tested; flip it
once the account has real traction.

---

## Layout

```
src/accelerateddevops/
  cli.py          12 subcommands
  config.py       Settings, env parsing
  models.py       SourceItem, Slide, Draft
  store.py        SQLite: items, drafts, runs, model cooldowns
  pipeline.py     orchestration, selection, scheduling
  sources/        rss.py, hackernews.py, reddit.py
  llm/            gemini.py (the ladder), prompts.py
  render/         theme.py, fonts.py, carousel.py
  publish/        instagram.py, storage.py, errors.py
tests/            standalone assert-suites + run_all.py
site/             staged JPEGs (ASSET_LOCAL_DIR)  (gitignored)
data/             sqlite db, rendered pngs  (gitignored)
.pages/           CI checkout of the gh-pages artefact branch  (gitignored)
```

Everything except the rendered images lives in one SQLite database. A single
account publishing twice a day does not need a database server.

## Tests

Plain assert-scripts, no test runner (pytest is not a dependency). Each one is
standalone and can be launched from any working directory:

```bash
.venv\Scripts\python.exe tests\run_all.py        # everything, with a summary
.venv\Scripts\python.exe tests\test_publish.py   # one suite
```

Judge a run by its trailing `PASSED` line, never the exit code: PowerShell
reports "Exited with code 1" whenever a script writes anything to stderr, and
several suites log expected errors (retries, Reddit skips) on the way past.
See `tests/README.md` for what each suite covers.

## The database

`accelerated_devops.sqlite3` (default under `data/`, configurable with
`DATA_DIR`) is the single source of truth for the whole pipeline: what it has
collected, what it has written, and what it has published. It is deliberately a
file and not a server — a single account makes a handful of writes a couple of
times a day, and a file survives reboots without anything to babysit.

It runs in WAL mode, so the `-wal` and `-shm` sidecar files next to it are part
of the live database. Copy all three together when backing up or moving the
machine; deleting the file is a cold start — every item is collected, scored
and drafted again from scratch. `Store.close()` checkpoints the WAL into the
main file, which is what makes the single file safe to commit to gh-pages after
a CI run.

### What it stores

Four tables:

**`source_items`** — one row per article/story collected from a feed or API.
```
fingerprint      sha1 of (source, external_id, url) - the dedupe key
source           where it came from: rss:infoq, hackernews, reddit:r/devops...
external_id      the source's own id (HN story id, feed entry id, ...)
title / url / summary / body / author / published_at
score, comments  ranking data (HN points, Reddit comments, RSS enterprise score)
tags, discussion_url
status           'new' = available to write about, 'used' = already drafted / retired
```
The `UNIQUE(source, external_id)` constraint makes `add_items` idempotent: a
feed that publishes the same entry twice is a no-op. `status` is the item
queue — `select_items` only ever reads `new` rows, claims exactly what it picks
(`mark_used`), and `release_item` returns any item whose generation failed so a
transient LLM outage does not silently eat the queue.

**`drafts`** — one row per generated carousel, the review/scheduling state.
```
fingerprint      sha1 of (source url, first headline) - stable id for CLI + folders
source_fingerprint  link back to the source_items row
caption, hashtags, slides, alt_text, cover_layout, sources_note
image_paths      where the rendered PNGs live on disk
status           draft -> approved -> scheduled -> published
scheduled_for, published_at, instagram_media_id, permalink, error
```
This is what `list`, `approve`, `reject`, `schedule` and `publish` act on.
Slides are stored as JSON so a re-render (or a future layout tweak) does not
need the original LLM call.

**`model_cooldowns`** — which Gemini models are currently benched, and why.
```
model, until, reason, hits, updated_at
```
Persisted so a run at 09:15 does not waste API calls rediscovering at 10:00
that the same model is still quota-exhausted. A successful call for a model
clears its row.

**`runs`** — bookkeeping for each `run` / `daemon` cycle.
```
id, started_at, ended_at, items_seen, drafts_made, published, notes
```
Used to see the pipeline's behaviour over time; not queried by any command.

### What is NOT in the database

The slides themselves. Rendered images are PNG files under
`data/output/<fingerprint>/`; the database only stores their paths, and the
publisher generates JPEG siblings on the fly for Instagram. No draft JSON files
are written to disk — the old "data/drafts" directory is unused.

### How it is used

The stages form a loop over the same file:

```
collect   -> add_items inserts new rows, dedupes existing ones
generate  -> select_items reads 'new' rows, ranks, claims; failure releases
render    -> update_draft(image_paths)
review    -> approve / reject flips draft status
schedule  -> assign_schedule writes scheduled_for; due_drafts reads it
publish   -> records media_id, permalink, published_at
daemon    -> repeats the loop; the file is the shared memory across processes
```

## Safety notes

- Nothing publishes without an explicit `--approve`.
- Failed generation releases the claimed items back to the pool, so a transient
  LLM outage does not permanently consume the item queue.
- One unusable item does not stop the rest of a batch.
- The asset server is read-only and refuses path traversal.
- Publishing uses only the official Graph API. Unofficial automation is a
  reliable way to lose the account.
