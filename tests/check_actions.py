"""Guard on how the workflows reference the repo's own composite actions.

Two mistakes are easy to make and both fail at the point the runner tries to
*load* a local action, which is before any step in the job runs:

1. `uses: ./.github/actions/setup/action.yml` - a local `uses:` path is always
   a directory. Pointing it at the file makes the runner look for
   `.../setup/action.yml/action.yml`, and it reports that as a missing
   action.yml with a message about a forgotten checkout.
2. Omitting `actions/checkout` before the first local action. The checkout
   cannot live *inside* the composite action: the action has to be on disk
   before the runner can execute a single one of its steps.
3. Referencing `${{ secrets.* }}` or `${{ vars.* }}` inside an action. Those
   contexts exist only in the calling workflow; inside a composite action the
   expression silently evaluates to an empty string, so the caller has to pass
   them through `with:` instead.
4. A reusable workflow (`on: workflow_call`) sitting in `actions/`. It has no
   `runs.steps`, so it is not an action, and it could not configure a caller
   even if it loaded - a called workflow's jobs are separate runners.

The first three fail silently or with a misleading message. None of them is
caught by running the pipeline, so they are checked here. This reads the
workflows as data rather than pattern-matching text, so it stays honest if the
steps are reordered or renamed.

There is also a house rule, which is a design constraint rather than a bug:
each action is one job. An input that selects between behaviours (`operation:
restore|commit`) rebuilds the dispatch the separate actions were split to
remove, and it hides which steps actually ran. The state round-trip is
`.github/actions/state_restore` and `.github/actions/state_commit`; keep them
that way.
"""

from __future__ import annotations

import sys
from pathlib import Path

import _paths  # noqa: F401  (sys.path + chdir bootstrap)
import yaml

WORKFLOWS = Path(".github/workflows")
ACTIONS = Path(".github/actions")
CHECKOUT = "actions/checkout@v4"
LOCAL_PREFIX = "./"


# Composite actions cannot read these contexts at all - only the calling
# workflow has them. An expression using them inside an action silently
# evaluates to an empty string rather than failing, which for a credential
# means an unauthenticated push and for a URL means a broken one.
FORBIDDEN_IN_ACTIONS = ("${{ secrets.", "${{ vars.", "${{secrets.", "${{vars.")

# An input that decides which steps run means the action is several actions.
MODE_INPUTS = {"operation", "mode", "action", "command"}

# Git write credentials belong to `actions/checkout`, which leaves an auth
# header in the checkout's own .git/config. An action that takes one is
# re-introducing the credential the pipeline was built to not have. Matched
# narrowly, so the third-party API tokens the app genuinely needs are fine.
GIT_CREDENTIAL_INPUTS = ("gh-token", "gh_token", "github-token", "github_token", "pat")


def check_action(action_file: Path, root: Path) -> list[str]:
    """Report contexts an action is not allowed to reference."""
    issues: list[str] = []
    text = action_file.read_text(encoding="utf-8")
    for banned in FORBIDDEN_IN_ACTIONS:
        for number, line in enumerate(text.splitlines(), start=1):
            if banned in line:
                issues.append(
                    f"{action_file.relative_to(root)}:{number}: uses "
                    f"'{banned}' - not available in a composite action. "
                    "Take it as an `inputs` value and pass it in with: `with:`."
                )
    return issues


def main() -> int:
    problems = 0
    workflows = sorted(WORKFLOWS.glob("*.yml"))
    if not workflows:
        print("FAIL: no workflows found")
        return 1

    action_files = sorted(ACTIONS.glob("*/action.yml"))
    for action_file in action_files:
        issues = check_action(action_file, Path("."))
        declared = yaml.safe_load(action_file.read_text(encoding="utf-8")) or {}
        inputs = declared.get("inputs") or {}
        for name in sorted(set(inputs) & MODE_INPUTS):
            issues.append(
                f"{action_file.parent.name}/action.yml: has an '{name}' input, "
                "so it dispatches on its caller. One action, one job: split it."
            )
        for name in sorted(inputs):
            if any(bad in name.lower() for bad in GIT_CREDENTIAL_INPUTS):
                issues.append(
                    f"{action_file.parent.name}/action.yml: takes a '{name}' "
                    "input. Git needs no credential of its own here - reuse the "
                    "auth header actions/checkout left in .git/config."
                )
        if issues:
            problems += 1
            print(f"FAIL {action_file.parent.name}/action.yml")
            for issue in issues:
                print(f"     {issue}")
        else:
            print(f"ok   {action_file.parent.name}/action.yml")

    for path in workflows:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        issues: list[str] = []

        for job_name, job in (doc.get("jobs") or {}).items():
            steps = job.get("steps") or []
            uses = [step.get("uses", "") for step in steps]

            for ref in uses:
                # A local action is referenced by directory, never by file.
                if ref.startswith(LOCAL_PREFIX) and ref.endswith((".yml", ".yaml")):
                    issues.append(
                        f"job '{job_name}': uses '{ref}' points at a file. "
                        "Local actions are directories: drop the '/action.yml'."
                    )
                if ref.startswith(LOCAL_PREFIX):
                    target = Path(ref.removeprefix("./"))
                    if not (target / "action.yml").is_file():
                        issues.append(
                            f"job '{job_name}': '{ref}' has no action.yml "
                            f"({target / 'action.yml'} does not exist)"
                        )

            locals_ = [i for i, ref in enumerate(uses) if ref.startswith(LOCAL_PREFIX)]
            if locals_ and CHECKOUT not in uses:
                issues.append(
                    f"job '{job_name}': uses a local action but never runs "
                    f"{CHECKOUT}. The runner has to read the action off disk "
                    "before it can run any of its steps."
                )
            elif locals_ and uses.index(CHECKOUT) > locals_[0]:
                issues.append(
                    f"job '{job_name}': {CHECKOUT} runs after the first local "
                    "action, which cannot load until the checkout has happened"
                )

        if issues:
            problems += 1
            print(f"FAIL {path.name}")
            for issue in issues:
                print(f"     {issue}")
        else:
            print(f"ok   {path.name}")

    # Every action directory should be reachable: one action.yml per directory.
    for action_dir in sorted(p for p in ACTIONS.iterdir() if p.is_dir()):
        if not (action_dir / "action.yml").is_file():
            problems += 1
            print(f"FAIL {action_dir}: no action.yml")
            continue
        doc = yaml.safe_load((action_dir / "action.yml").read_text(encoding="utf-8"))
        runs = (doc or {}).get("runs") or {}
        # A reusable workflow lives in workflows/ and is called with `uses:` at
        # the job level. One dropped into actions/ looks like an action but has
        # no `runs:`, and every `uses:` on it fails at load time.
        if "uses" in runs or "steps" not in runs:
            problems += 1
            print(f"FAIL {action_dir.name}: has no composite `runs.steps`. "
                  "A workflow_call file belongs in .github/workflows/, and "
                  "cannot configure a caller anyway - its jobs are separate "
                  "runners.")
        elif runs.get("using") != "composite":
            problems += 1
            print(f"FAIL {action_dir.name}: `runs.using` is "
                  f"{runs.get('using')!r}, not 'composite'")

    if problems:
        print(f"\n{problems} workflow(s) need fixing")
        return 1
    print("\nWORKFLOW ACTION REFERENCES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())