"""The gh-pages round-trip, by running the composite action's own shell.

The logic lives in `.github/actions/state/action.yml` as two `run:` blocks
rather than a separate script. That is the right amount of machinery for a few
git commands, but it does mean the shell is only otherwise tested on CI, so
these cases extract and execute the real blocks against local git repos.

Covers the things that can silently lose state or lose slides: a cold start, an
unchanged run, and a push rejected because someone else moved the branch.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import _paths  # noqa: F401  (sys.path + chdir bootstrap)

REPO = Path(__file__).resolve().parents[1]
BASH = r"C:\Program Files\Git\bin\bash.exe"
ACTION = REPO / ".github" / "actions" / "state" / "action.yml"


def ok(msg: str) -> None:
    print("OK   " + msg)


def run(*args: str, cwd: Path, env: dict | None = None, check: bool = True):
    """Run a command, raising with its output when it fails."""
    result = subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, env=env, shell=False
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"{' '.join(args)} failed ({result.returncode})\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def git(*args: str, cwd: Path):
    """Run a git command in `cwd`."""
    return run("git", *args, cwd=cwd)


def shell_block(title: str) -> str:
    """Pull one `run:` block out of the composite action by its step name.

    Keeps the test honest: it runs the shell the runner will run, so editing
    the action without thinking about this suite changes what is verified.
    """
    lines = ACTION.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip() != f"- name: {title}":
            continue
        for offset, candidate in enumerate(lines[index + 1:], start=index + 1):
            match = re.match(r"^(\s*)run: \|-?\s*$", candidate)
            if match:
                indent = len(match.group(1))
                body: list[str] = []
                for line in lines[offset + 1:]:
                    if line.strip() and len(line) - len(line.lstrip()) <= indent:
                        break
                    body.append(line[indent + 2:])
                return "\n".join(body) + "\n"
        break
    raise AssertionError(f"no run: block found for the '{title}' step in {ACTION}")


def sandbox(root: Path, origin: Path, *, with_branch: bool = True) -> Path:
    """Build a workspace with a .pages checkout and an empty data/ dir."""
    checkout = root / ".pages"
    data = root / "data"
    data.mkdir(parents=True)
    if with_branch:
        checkout.mkdir()
        git("init", "-b", "gh-pages", cwd=checkout)
        git("remote", "add", "origin", str(origin), cwd=checkout)
        (checkout / "index.html").write_text("<html></html>", encoding="utf-8")
        git("add", "index.html", cwd=checkout)
        git("-c", "user.name=t", "-c", "user.email=t@example.com",
            "commit", "-q", "-m", "init", cwd=checkout)
        git("push", "-q", "-u", "origin", "gh-pages", cwd=checkout)
    return checkout


def env_for(root: Path, origin: Path) -> dict:
    return {
        **os.environ,
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "example/technobabel",
        # The action reads this on the cold-start path, where there is no
        # checkout to have left credentials behind.
        "GH_TOKEN": "ghs_test",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "MESSAGE": "test commit",
    }


def main() -> int:
    if not Path(BASH).exists():
        print(f"SKIP: no bash at {BASH}")
        return 0

    restore = shell_block("Restore the state")
    commit = shell_block("Commit the state and any staged assets")

    root = Path(tempfile.mkdtemp(prefix="state-action-"))
    try:
        origin = root / "origin.git"
        git("init", "--bare", "-b", "gh-pages", str(origin), cwd=root)
        checkout = sandbox(root, origin)
        data = root / "data"
        env = env_for(root, origin)

        def restore_state() -> str:
            script = root / "_restore.sh"
            script.write_text(restore, encoding="utf-8", newline="\n")
            return run(BASH, str(script), cwd=root, env=env).stdout

        def commit_state() -> str:
            script = root / "_commit.sh"
            script.write_text(commit, encoding="utf-8", newline="\n")
            return run(BASH, str(script), cwd=root, env=env).stdout

        # ── 1. a run that changed nothing commits nothing ────────────────
        out = commit_state()
        assert "nothing to commit" in out, out
        print("OK   commit: nothing staged means no commit, not a failure")

        # ── 2. the database round-trips onto the branch ───────────────────
        (data / "accelerated_devops.sqlite3").write_bytes(b"stage-1 payload")

        # Staged JPEGs, exactly as `upload-assets` leaves them when
        # ASSET_LOCAL_DIR points at this checkout.
        slides = checkout / "abc123def456"
        slides.mkdir(parents=True, exist_ok=True)
        (slides / "abc123def456_01.jpg").write_bytes(b"\xff\xd8jpeg-one")
        (slides / "abc123def456_02.jpg").write_bytes(b"\xff\xd8jpeg-two")

        out = commit_state()
        assert "state pushed to gh-pages" in out, out

        clone = root / "verify"
        git("clone", "-q", "--branch", "gh-pages", str(origin), str(clone), cwd=root)
        committed = clone / "state" / "accelerated_devops.sqlite3"
        assert committed.read_bytes() == b"stage-1 payload", committed.read_bytes()
        assert not (clone / "state" / "accelerated_devops.sqlite3-wal").exists(), (
            "an empty -wal is checkpointed noise, not state worth committing"
        )
        # The slides go out in the same commit: the pipeline code stages them
        # into this checkout and deploying is a git concern, so they must not be
        # left behind on the branch.
        for index in (1, 2):
            name = f"abc123def456_{index:02d}.jpg"
            assert (clone / "abc123def456" / name).read_bytes() == (
                slides / name
            ).read_bytes(), f"{name} must be published by the commit step"
        assert (clone / "index.html").is_file(), (
            "the branch's existing files must survive; only added files change"
        )
        print("OK   commit: the database and the staged slides both land")

        # ── 3. a live -wal is real state and has to travel ────────────────
        (data / "accelerated_devops.sqlite3-wal").write_bytes(b"unmerged tail")
        commit_state()
        git("clone", "-q", "--branch", "gh-pages", str(origin), str(clone / "wal"), cwd=root)
        assert (clone / "wal" / "state" / "accelerated_devops.sqlite3-wal").read_bytes() == (
            b"unmerged tail"
        ), "a non-empty -wal must travel with the database"
        (data / "accelerated_devops.sqlite3-wal").unlink()
        commit_state()
        print("OK   commit: a non-empty -wal travels with the database")

        # ── 4. restore brings it back ────────────────────────────────────
        shutil.rmtree(data)
        data.mkdir()
        restore_state()
        assert (data / "accelerated_devops.sqlite3").read_bytes() == b"stage-1 payload"
        print("OK   restore: the database comes back off the branch")

        # ── 5. a second run with identical state and slides commits nothing ─
        out = commit_state()
        assert "state unchanged" in out, out
        print("OK   commit: an unchanged run pushes nothing")

        # ── 5b. a re-render of the same fingerprint must replace the slides ─
        # Staging is mtime-guarded, but the workflow also re-runs the renderer,
        # so the committed bytes have to follow whatever is on disk now.
        (slides / "abc123def456_02.jpg").write_bytes(b"\xff\xd8jpeg-two-v2")
        out = commit_state()
        assert "state pushed to gh-pages" in out, out
        git("clone", "-q", "--branch", "gh-pages", str(origin), str(clone / "v2"), cwd=root)
        assert (clone / "v2" / "abc123def456" / "abc123def456_02.jpg").read_bytes() == (
            b"\xff\xd8jpeg-two-v2"
        ), "a changed slide must be re-published, not left stale"
        print("OK   commit: a changed slide is replaced on the branch")

        # ── 6. a push rejected by a competing commit is rebased, not clobbered
        (data / "accelerated_devops.sqlite3").write_bytes(b"stage-2 payload")
        (checkout / "README.md").write_text("added by someone else\n", encoding="utf-8")
        git("add", "README.md", cwd=checkout)
        git("-c", "user.name=t", "-c", "user.email=t@example.com",
            "commit", "-q", "-m", "unrelated change", cwd=checkout)
        git("push", "-q", "origin", "gh-pages", cwd=checkout)

        out = commit_state()
        assert "state pushed to gh-pages" in out, out
        git("clone", "-q", "--branch", "gh-pages", str(origin), str(clone / "b"), cwd=root)
        assert (clone / "b" / "state" / "accelerated_devops.sqlite3").read_bytes() == (
            b"stage-2 payload"
        )
        assert (clone / "b" / "README.md").is_file(), "the rebase must keep the other commit"
        print("OK   commit: a rejected push is rebased, not clobbered")

    finally:
        shutil.rmtree(root, ignore_errors=True)

    # ── 7. the very first run: no branch yet, so commit creates one ──────
    fresh = Path(tempfile.mkdtemp(prefix="state-fresh-"))
    try:
        # No .pages checkout at all, which is exactly the cold-start case: the
        # action's own `git init` has to stand the branch up.
        origin = fresh / "remote" / "example.git"
        origin.parent.mkdir(parents=True)
        git("init", "--bare", "-b", "gh-pages", str(origin), cwd=fresh)
        (fresh / "data").mkdir(parents=True)
        (fresh / "data" / "accelerated_devops.sqlite3").write_bytes(b"first ever run")

        # The action builds the origin as $GITHUB_SERVER_URL/$GITHUB_REPOSITORY,
        # so a local "server" directory makes that URL a real bare repo.
        env = env_for(fresh, origin)
        env["GITHUB_SERVER_URL"] = str(fresh / "remote").replace("\\", "/")
        env["GITHUB_REPOSITORY"] = "example"

        script = fresh / "_commit.sh"
        script.write_text(commit, encoding="utf-8", newline="\n")
        out = run(BASH, str(script), cwd=fresh, env=env).stdout
        assert "state pushed to gh-pages" in out, out

        clone = fresh / "verify"
        git("clone", "-q", "--branch", "gh-pages", str(origin), str(clone), cwd=fresh)
        assert (clone / "state" / "accelerated_devops.sqlite3").read_bytes() == b"first ever run"
        print("OK   commit: the very first run bootstraps the branch")

        # ── 8. cold start with slides and no database ────────────────────
        # The commit block used to bail out with "no database yet" before
        # looking at anything else. On a brand new repo the first thing a run
        # may have is slides, so that guard must not swallow them.
        slides_root = Path(tempfile.mkdtemp(prefix="state-slides-"))
        try:
            bare = slides_root / "origin" / "example.git"
            bare.parent.mkdir(parents=True)
            git("init", "--bare", "-b", "gh-pages", str(bare), cwd=slides_root)
            (slides_root / "data").mkdir()
            staged = slides_root / ".pages" / "feed1234"
            staged.mkdir(parents=True)
            (staged / "feed1234_01.jpg").write_bytes(b"\xff\xd8first-slide")

            env2 = env_for(slides_root, bare)
            env2["GITHUB_SERVER_URL"] = str(slides_root / "origin").replace("\\", "/")
            env2["GITHUB_REPOSITORY"] = "example"
            script2 = slides_root / "_commit.sh"
            script2.write_text(commit, encoding="utf-8", newline="\n")
            out = run(BASH, str(script2), cwd=slides_root, env=env2).stdout
            assert "state pushed to gh-pages" in out, out

            clone2 = slides_root / "verify"
            git("clone", "-q", "--branch", "gh-pages", str(bare), str(clone2),
                cwd=slides_root)
            assert (clone2 / "feed1234" / "feed1234_01.jpg").read_bytes() == (
                b"\xff\xd8first-slide"
            ), "a first run with slides but no database must still publish them"
            print("OK   commit: slides land even when there is no database yet")
        finally:
            shutil.rmtree(slides_root, ignore_errors=True)
    finally:
        shutil.rmtree(fresh, ignore_errors=True)

    print("\nPAGES STATE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
