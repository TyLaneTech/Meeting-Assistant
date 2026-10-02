"""Installs an update into this checkout: a fast-forward that keeps local edits.

``/api/update/apply`` used to run a bare ``git pull``. git will not pull over a
file with uncommitted edits, so one edited file blocked every update that
touched it (reported from a Mac on 2026-10-02). ``install()`` sets the edits
aside, fast-forwards, and puts them back on top. If they clash with the update,
it puts the checkout back exactly as it was and names the files. The app
restarts straight into this tree, so an update that is half in, or a file full
of conflict markers, would stop it starting at all.

A copy with commits of its own is refused rather than merged or rebased, so
the button never rewrites anyone's history; those are updated with git.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

STASH_MESSAGE = "Meeting Assistant update: your edits, set aside"

# Two windows can press Update at once, and a second install running its
# rollback under the first could reset away edits the first had put back.
_installing = threading.Lock()


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)


def _says(proc: subprocess.CompletedProcess) -> str:
    """What git said went wrong, as one line of sentences, without its hints."""
    parts: list[str] = []
    for raw in (proc.stderr.strip() or proc.stdout).splitlines():
        line = raw.strip()
        if not line or line.startswith("hint:") or line == "Aborting":
            continue
        for prefix in ("error: ", "fatal: "):
            line = line.removeprefix(prefix)
        if raw.startswith("\t") and parts:
            # A file git lists under the line before it.
            parts[-1] += (" " if parts[-1].endswith(":") else ", ") + line
            continue
        if parts and parts[-1][-1] not in ".:!?":
            parts[-1] += "."
        parts.append(line)
    text = " ".join(parts) or f"git stopped with code {proc.returncode}."
    return text[0].upper() + text[1:]


def _stash_top(root: Path) -> str:
    return _git(root, "rev-parse", "--verify", "--quiet", "refs/stash").stdout.strip()


def _saved_in_stash() -> str:
    return f"Your edits are saved in git stash as \"{STASH_MESSAGE}\"."


def _put_back(root: Path, before: str, stash: str) -> str:
    """Return the checkout to ``before`` with the edits that were set aside.

    ``reset --hard`` loses nothing because it only runs while every edit is in
    the stash: it clears out whatever the update or a clashing pop left behind.
    Returns what the user needs to know if the edits could not be handed back.
    """
    if stash and _stash_top(root) != stash:
        return ""  # already popped: the edits are back in the tree
    if _git(root, "reset", "--hard", before).returncode != 0:
        return _saved_in_stash() if stash else ""
    if stash and _git(root, "stash", "pop").returncode != 0:
        return _saved_in_stash()
    return ""


def _set_aside(root: Path) -> tuple[str, str]:
    """Stash uncommitted changes to tracked files. Returns (stash, problem).

    Untracked files stay where they are; they only block an update that adds
    the same path, and git names the file when that happens.
    """
    status = _git(root, "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0:
        return "", _says(status)
    if not status.stdout.strip():
        return "", ""
    previous = _stash_top(root)
    pushed = _git(root, "stash", "push", "--message", STASH_MESSAGE)
    stash = _stash_top(root)
    stash = "" if stash == previous else stash
    left = _git(root, "status", "--porcelain", "--untracked-files=no")
    if pushed.returncode == 0 and stash and left.returncode == 0 and not left.stdout.strip():
        return stash, ""
    # Some of it did not go: hand back what did, and change nothing.
    stuck = bool(stash) and _git(root, "stash", "pop").returncode != 0
    problem = _says(pushed) if pushed.returncode != 0 or not stash else " ".join(left.stdout.split())
    return "", f"Your edits could not be set aside: {problem} {_saved_in_stash() if stuck else ''}".strip()


def install(root: Path, target: str = "FETCH_HEAD") -> str | None:
    """Fast-forward the checkout at ``root`` to ``target``, keeping local edits.

    Returns None once the checkout is at ``target`` with the edits on top, or a
    sentence saying why it is not, in which case nothing has changed.
    """
    if not _installing.acquire(blocking=False):
        return "An update is already being installed."
    try:
        return _install(root, target)
    finally:
        _installing.release()


def _install(root: Path, target: str) -> str | None:
    found = _git(root, "rev-parse", "--verify", "--quiet", f"{target}^{{commit}}")
    if found.returncode != 0:
        return "The downloaded update is missing. Check for updates, then try again."
    target = found.stdout.strip()
    head = _git(root, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        return _says(head)
    before = head.stdout.strip()
    if before == target:
        return None
    if _git(root, "merge-base", "--is-ancestor", before, target).returncode != 0:
        return "This copy has commits that are not on main, so update it with git instead."

    stash, problem = _set_aside(root)
    if problem:
        return problem
    try:
        merged = _git(root, "merge", "--ff-only", target)
        if merged.returncode != 0:
            return f"{_says(merged)} {_put_back(root, before, stash)}".strip()
        if stash:
            popped = _git(root, "stash", "pop")
            if popped.returncode != 0:
                clashing = _git(root, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
                why = (f"Your edits to {', '.join(clashing)} clash with this update. "
                       f"Commit or undo them, then try again." if clashing
                       else f"Your edits could not go back on top of the update: {_says(popped)}")
                return f"{why} {_put_back(root, before, stash)}".strip()
    except Exception:
        _put_back(root, before, stash)
        raise
    return None
