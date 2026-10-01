# Tests

Plain assert-scripts, no test runner (pytest is not a dependency). Each file is
standalone: it imports `_paths` to put `src/` on `sys.path` and anchor the
working directory to the repo root, so you can launch one from anywhere.

```bash
# everything, with a pass/fail summary
.venv\Scripts\python.exe tests\run_all.py

# one suite
.venv\Scripts\python.exe tests\test_publish.py
```

Run from Windows PowerShell. Note that PowerShell reports "Exited with code 1"
whenever a script writes to stderr, which several do for expected-error
logging. Judge a run by its trailing `PASSED`/`OK` line, never by the exit
code — that is exactly what `run_all.py` does.

`test_pages_state.py` needs `bash` and `git`; it skips itself with a `SKIP`
line on Windows if Git Bash is not installed. It reads the shell out of
`.github/actions/state/action.yml` rather than duplicating it, so editing the
action changes what the suite verifies.

| File | Covers |
| --- | --- |
| `test_llm.py` | Gemini ladder: model×key rotation, quota/cooldown persistence, response parsing, client timeout |
| `test_pipeline.py` | End-to-end cycle with a fake LLM: collect → generate → render → schedule |
| `test_sources_prompts.py` | RSS feed scoring, feed priority, product-marketing penalty, prompt angles |
| `test_reddit.py` | Reddit OAuth token flow and unauthenticated degradation |
| `test_publish.py` | Instagram carousel flow against a mock Graph API, retries, error translation |
| `test_publish_queue.py` | The publish queue: one post per run, oldest approved first, failures stay queued |
| `test_storage.py` | Asset hosting: local folder staging and the GitHub Pages backend |
| `test_pages_state.py` | The gh-pages round-trip: extracts the `run:` blocks from the state composite action and executes them against real git repos |
| `test_release.py` | Failure handling and rollback paths |
| `test_stat.py` | Stat-slide value detection |
| `lint_layout.py` | Geometry linter: text overflow and collisions, all layouts |
| `check_render.py` | Rendered-slide ink/branding checks on a real render |
| `check_contrast.py` | WCAG AA contrast ratios for every palette |

`check_render.py` reads `data/output/_smoke`; run `accelerated-devops render`
first if that directory is missing.
