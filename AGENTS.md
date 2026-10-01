# OpenCode System Instructions

## Token Efficiency & Response Formatting
- Be ultra-concise in non-code communication.
- Do NOT repeat back user prompts or explain basic Python concepts.
- Do NOT output preamble, postamble, conversational fluff, or disclaimers.
- Provide direct, minimal explanations only when necessary to explain complex logic.
- Do NOT print full unchanged files in diffs; only emit modified or newly added code snippets.

## Context Summarization & History
- Summarize previous context into key technical facts, architecture choices, and state changes.
- Omit historical chat dialogue, previous attempt iterations, and code snippets that are no longer relevant.

## Python Coding Standards & Architecture
- **Readability Over Optimization:** Prioritize maintainability, clear variable/function names, and self-documenting structure over micro-optimizations or complex one-liners.
- **SOLID Principles:**
  - *Single Responsibility:* Keep functions and classes small, focused on one task.
  - *Open/Closed:* Use abstraction (interfaces/protocols) where extendability is needed.
  - *Liskov Substitution:* Ensure derived classes strictly adhere to base class behavior.
  - *Interface Segregation:* Define small, specific `Protocol` or `ABC` interfaces.
  - *Dependency Inversion:* Rely on abstractions; accept dependencies via dependency injection.
- **Style & Typing:**
  - Follow PEP 8 guidelines strictly.
  - Always include type hints (`typing` module / standard generic types).
  - Include concise docstrings (Google style) on major functions and class interfaces.

---

# Repo Guide (accelerated.devops)

Content pipeline: scrape DevOps/platform-engineering sources → Gemini carousels →
Pillow-rendered slides → Instagram Graph API on a schedule. Single account,
Windows dev machine, Python 3.12 venv (`.venv`), `uv` + `hatchling`.

## Commands
- CLI: `.venv\Scripts\accelerated-devops.exe <cmd>` (editable install). Console
  script is hyphenated `accelerated-devops`; the Python module is
  `accelerateddevops`, package root `src/`.
- Subcommands: `collect generate render list approve reject schedule publish
  check-token run daemon doctor`. `generate run` only draft unless
  `--approve`. `doctor` reports exactly what config is missing.
- `publish` with no args posts **exactly one** draft: `Store.next_approved()`,
  the oldest `approved` row by `created_at`. This is the CI contract — a
  retried run must not double-post. `--due` switches to the slot-based burst
  used by the resident daemon. Failures leave the draft `approved` with
  `error` set, so a retry re-posts the same thing rather than losing it.
- `list --ids` prints fingerprints oldest-first, one per line, for shell
  pipelines — the approve/reject workflows use it instead of scraping output.

## Testing — NOT pytest
- Tests are plain assert-scripts run directly; there is **no test runner, no CI**.
  Running `pytest` fails (not installed).
- They live in **`tests/`** in the repo. Each imports `tests/_paths.py`, which
  puts `src/` on `sys.path` and chdirs to the repo root, so any of them runs
  from any working directory:
  `.venv\Scripts\python.exe tests\test_<name>.py`
- `.venv\Scripts\python.exe tests\run_all.py` runs all 13 suites and prints a
  PASS/FAIL summary. It judges on **stdout only** — several suites log expected
  errors to stderr on the way past.
- Suites (each must end in a `... PASSED`/`OK` line): `test_llm` (Gemini ladder),
  `test_pipeline` (E2E with fake LLM), `test_reddit`, `test_publish` (IG 3-step),
  `test_publish_queue` (FIFO, one-per-run), `test_pages_state` (bash/git state
  round-trip; skips itself if no bash), `test_stat`, `test_release`,
  `test_sources_prompts` (RSS + prompt angles), `check_actions` (workflows
  reference local actions correctly), plus render checks `lint_layout.py`,
  `check_render.py`, `check_contrast.py`.
- `test_stat.py` ends in "N/N … cases pass", `lint_layout.py` in "… clean",
  `check_contrast.py` in "dim accent: none" — `run_all.py`'s `PASS_MARKERS`
  covers all of them; add a marker if a suite changes its ending.
- **Windows quirk:** PowerShell reports "Exited with code 1" whenever stderr is
  written. Judge pass/fail by the trailing PASSED/OK line, never the exit code.

## Config & credentials
- Everything is env-driven via `.env` (gitignored); `.env.example` is the
  annotated reference. `config.py:load_settings()` is the single parser.
- `GEMINI_API_KEYS` is a comma-separated cascade ladder. `.env.example` lists
  gemini-2.5 IDs while the live `.env` uses gemini-3.x models — do NOT "fix"
  `.env` to match the docs. A 13-char placeholder key makes `generate` fail with
  "Gemini rejected every configured API key" — expected, not a bug.
- Reddit: without `REDDIT_CLIENT_ID/SECRET` the collector 403s and skips cleanly
  by design; RSS + HN carry the account. App creation at reddit.com/prefs/apps is
  broken upstream (silent failure); the Devvit portal (developers.reddit.com)
  does NOT issue Data API client id/secret.
- Publishing requires `PUBLIC_ASSET_BASE_URL`; without it publishing fails loudly
  by design. The app does no hosting: slides sit on the `gh-pages` site, and
  `publish/instagram.py:InstagramPublisher.image_urls()` derives
  `<base>/<fingerprint>/<fingerprint>_NN.jpg` from the base URL plus the
  fingerprint — one pure string function, no local files, no HTTP.
- **The app holds no write credential and never calls the GitHub API.** It
  builds public URLs and reads them, nothing else. Deploying to `gh-pages` is
  git, and it belongs to `.github/actions/state_commit`: `git -C .pages add -A .`
  pushes the database and whatever else sits in the checkout in one commit,
  using the auth header `actions/checkout` already left in
  `.pages/.git/config`. No `GITHUB_TOKEN` secret or PAT exists, and `setup`
  exports none. Don't reintroduce one.
- The renderer writes JPEG directly (`render_draft`: quality 92, subsampling 0),
  because that is the only format Instagram accepts — there is no conversion
  step anywhere: `publish/storage.py`, `to_jpeg()`, `fetch_slides()` and the
  `upload-assets` subcommand are deleted. Getting `<fp>_NN.jpg` onto `gh-pages`
  is still outside this app, so if the site has no JPEGs, `publish` gets a 404
  that Meta reports as
  `9004 / 2207052 "Only photo or video can be accepted as media type"`.

## Actions layout (hard-won constraint)
- **Composite actions cannot read `secrets` or `vars`.** Those contexts exist
  only in the calling workflow; inside `.github/actions/*/action.yml` the
  expression silently evaluates to an **empty string** rather than failing. The
  pipeline reads its config from env, so an empty secret means an
  unauthenticated push or a broken public URL, not an error. Everything is
  therefore an `inputs:` entry on `setup/action.yml` and each workflow passes
  it through `with:`. `github` context (including `github.token` and
  `github.workspace`) *is* available in a composite action.
- A reusable workflow (`on: workflow_call`) **cannot** replace the setup action.
  A called workflow's jobs run on separate runners, so its `$GITHUB_ENV` writes
  never reach the caller's later steps. It would have to be the *entire* job.
- Also invalid in `actions/`: a file with `on: workflow_call` and `jobs:` and no
  `runs:`. `uses:` on it fails at load time with a misleading "can't find
  action.yml"-style error.
- `tests/check_actions.py` guards all of this: local `uses:` must point at a
  directory, `actions/checkout` must precede them, every `actions/*/action.yml`
  must parse and have `runs.using: composite`, and none may reference
  `${{ secrets.`/`${{ vars.`, that no action declares a mode input (`operation`,
  `mode`, ...) and none takes a git credential. Add a rule there rather than
  relying on review.
- **One action, one job.** `.github/actions/state` used to be one action with
  `operation: restore|commit`, which meant `if: inputs.operation == ...` on
  every step and a workflow that could not say what would run. It is now
  `state_restore` and `state_commit`, both unconditional. Same reason the setup
  action takes settings as inputs rather than reading contexts: dispatch belongs
  in the workflow file, where it is visible.

## Architecture
- Flow: collect → select → generate → render → approve → schedule → publish,
  orchestrated in `pipeline.py`, exposed as CLI subcommands in `cli.py`.
- All state is one SQLite file `data/accelerated_devops.sqlite3` (WAL mode;
  `-wal/-shm` sidecars are part of it). Rendered images are JPEG files under
  `data/output/<fingerprint>/` — the DB stores paths only, and no draft JSON
  files are written (the old "drafts_dir" is unused). `data/` is gitignored.
- LLM ladder in `llm/gemini.py`: models × keys with per-model cooldowns persisted
  in the `model_cooldowns` table. An invalid key benches only that key; only 403
  benches the model. The SDK client is built with an explicit `180s` HTTP
  timeout (`GENAI_TIMEOUT_MS`) — google-genai's default is `timeout=None`
  (infinite), which made a stalled call hang forever; timeouts classify as
  transient (3-min cooldown) so the ladder moves on. The SDK's per-call "AFC is
  enabled" log is filtered out in `_silence_afc_notice` (no tools are ever
  declared, so AFC never actually loops).
- `llm/prompts.py` builds prompts via `.format()` — any added prose must contain
  **no literal braces** or the prompt breaks at runtime.
- Instagram uses the Instagram-Login (Business Meta app) path: carousel = 3 API
  steps, the renderer writes JPEG (the only format IG accepts), IG Login cannot
  delete containers (they self-expire in 24h).
- `publish/instagram.py` owns every URL: `image_urls(draft)` is
  `<PUBLIC_ASSET_BASE_URL>/<fp>/<fp>_NN.jpg` for each slide, 1-based and in
  order. Never the bare file name — renders live in a per-fingerprint
  subdirectory, so a basename-only URL 404s. Meta reports a failed image
  download as `9004 / 2207052 "Only photo or video can be accepted as media
  type"`, which reads like a `media_type` bug and is not one;
  `_create_container` retries it 3× (Meta also returns it for URLs that work
  later) and `_explain` echoes Meta's `error_user_msg` (the URI).
- Instagram fetches the image **while creating the container**, so the slide has
  to be live before the API call. That is why hosting is the deployment step's
  job and never part of a publish: nothing here stages, uploads or polls.
- `PublishError` lives in `publish/errors.py` so the Graph client raises one
  exception type everywhere.
- RSS scoring: enterprise/org-change terms weigh ~1.5× generic infra terms,
  `FEED_PRIORITY[feed]` multiplies the score (default 1.0), and product-marketing
  terms (`PRODUCT_TERMS`) penalise entries so vendor promos never win.
  `select_items` ranks by engagement (`score + 2×comments`, comments outweigh
  votes) and discounts RSS items (no real interaction data) so proven-popular
  HN/Reddit stories win; it then normalises per source before ranking.
  HN ranks by points, Reddit by comments.

## Gotchas
- `src/` is tracked, so `git diff` shows source changes. `data/` is not.
- The live `.env` still carried `PUBLIC_ASSET_BASE_URL=http://127.0.0.1:8788`
  from the deleted tunnel setup; `doctor` now errors on any private/http base
  (`cli._is_private_host`).
- GitHub Actions runners are **ephemeral**, so state lives on `gh-pages`, not in
  `data/`: `<fp>/<fp>_NN.jpg` (the published slides) plus `state/*.sqlite3`. The
  round-trip is the `run:` block in each of
  `.github/actions/state_restore/action.yml` and
  `.github/actions/state_commit/action.yml` — deliberately inline, not a script;
  it is a `cp`, a `git add`, a `git push`.
  `Store.close()` checkpoints the WAL (TRUNCATE) so the committed single file is
  self-contained, but the commit action still copies a non-empty `-wal` because
  a crash can leave one. `tests/test_pages_state.py` extracts those `run:`
  blocks and executes them against real git repos, so the shell is not only
  tested on CI.
- The `state/` dir is inside the *published* Pages site, so the DB is public.
  No credentials in it, but do not treat it as private.
- Each pipeline stage is a separate workflow (`.github/workflows/*.yml`) sharing
  `.github/actions/setup` (uv install + env export), `.github/actions/state_restore`
  and `.github/actions/state_commit`.
  `approve`/`reject` are manual-only on purpose — that
  is the review gate.
- Repo prerequisites the workflows cannot set themselves: Pages configured to
  deploy from `gh-pages`/root, and Actions workflow permissions set to
  read+write. The commit step rebase-then-pushes, so a `concurrency: pipeline`
  group is belt-and-braces, not the thing keeping the branch consistent.
- Deploying the slides is your step: the renderer writes the JPEGs, but nothing
  in the repo pushes them to `gh-pages`. If publishing 9004s with a URI that
  plainly should exist, check the file really is at `<fp>/<fp>_NN.jpg` on the
  site. A draft last rendered before the JPEG switch keeps a stale `.png`
  beside it until it is re-rendered; `render` deletes that sibling.
- The agent cannot view rendered images; verify layout via `tests/lint_layout.py` /
  `tests/check_render.py` / `tests/check_contrast.py`, never by assumption.
- Changing `DEFAULT_FEEDS`/scoring in `sources/rss.py` implies updating
  `tests/test_sources_prompts.py` (asserts feed count + weighting) and the README
  Sources section by hand.