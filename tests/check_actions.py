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

Neither is caught by running the pipeline, so they are checked here. This reads
the workflows as data rather than pattern-matching text, so it stays honest if
the steps are reordered or renamed.
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


def main() -> int:
    problems = 0
    workflows = sorted(WORKFLOWS.glob("*.yml"))
    if not workflows:
        print("FAIL: no workflows found")
        return 1

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
        else:
            print(f"ok   {action_dir.name}/action.yml")

    if problems:
        print(f"\n{problems} workflow(s) need fixing")
        return 1
    print("\nWORKFLOW ACTION REFERENCES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())