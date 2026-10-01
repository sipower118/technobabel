"""Run every test suite and report a single pass/fail summary.

Usage (from anywhere):
    .venv\\Scripts\\python.exe tests\\run_all.py

Each suite is a plain assert-script with no test runner, so this runs them as
subprocesses and judges pass/fail by each script's trailing PASSED/OK line -
not by the exit code, because PowerShell reports "Exited with code 1" whenever
a script writes anything to stderr (which several do for expected-error
logging). Use this after manual edits to confirm nothing regressed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import _paths  # noqa: F401  (sys.path + chdir bootstrap)

TESTS = Path(__file__).resolve().parent

SUITES = [
    "test_llm.py",
    "test_pipeline.py",
    "test_sources_prompts.py",
    "test_reddit.py",
    "test_publish.py",
    "test_publish_queue.py",
    "test_storage.py",
    "test_pages_state.py",
    "test_release.py",
    "check_actions.py",
    "test_stat.py",
    "lint_layout.py",
    "check_render.py",
    "check_contrast.py",
]

# Each suite prints a single trailing summary line to stdout. Warnings and
# expected-error logs go to stderr and must NOT count as failures, so we judge
# purely on stdout. Most suites end in a literal "PASSED"; the others report a
# count ("14/14 ... pass", "15/15 ... clean") or, for the contrast check, the
# absence of violations ("dim accent: none").
PASS_MARKERS = ("PASSED", "cases pass", "clean", "dim accent: none")


def run_suite(name: str) -> bool:
    """Run one suite; return True if its stdout carries the pass marker."""
    result = subprocess.run(
        [sys.executable, str(TESTS / name)],
        capture_output=True,
        text=True,
        cwd=str(_paths.REPO_ROOT),
    )
    # Judge on stdout only: several suites log expected errors to stderr on the
    # way past (retry attempts, Reddit 403 skips) that are not failures.
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    passed = any(line.endswith(PASS_MARKERS) for line in lines)
    status = "PASS" if passed else "FAIL"
    tail = lines[-1] if lines else "(no stdout)"
    print(f"{status}  {name:<26} {tail}")
    return passed


def main() -> int:
    results = [(name, run_suite(name)) for name in SUITES]
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print()
    if passed == total:
        print(f"All {total} suites passed.")
        return 0
    failed = [name for name, ok in results if not ok]
    print(f"{passed}/{total} suites passed. Failed: {', '.join(failed)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
