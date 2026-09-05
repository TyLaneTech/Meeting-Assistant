"""Release notes for users, read from CHANGELOG.md at the project root.

The Settings > Changelog tab and the What's new card after an update show this
file and nothing else. Commit messages and pull request text never reach users,
so they can be written for developers again.

Format (see the file's own preamble):

    ## <Title> (YYYY-MM-DD)

    ### <Area>
    - <what changed, in user language>

Every ``## `` heading (exactly two hashes) opens one entry. The date is optional
and may sit in parentheses at the end of the heading or at its start
(``## 2026-09-05 Title``). Everything until the next ``## `` is the entry's
notes, kept as markdown for the client to render. Text before the first entry
is a preamble and is ignored. The first word of the title picks the icon, the
way the first word of a commit subject used to.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

FILE_NAME = "CHANGELOG.md"

_HEADING = re.compile(r"^##(?!#)\s*(.+?)\s*#*\s*$")
_DATE_TAIL = re.compile(r"\s*\((\d{4}-\d{2}-\d{2})\)\s*$")
_DATE_HEAD = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*[:\-]?\s+(.+)$")


def category(title: str) -> str:
    """Icon category from the first word of an entry's title.

    Past tense is the convention ("Added", "Fixed"); imperative forms are
    accepted too. Leading non-letters are stripped before matching.
    """
    s = re.sub(r"^[^\w]+", "", (title or "").strip().lower()).lstrip()
    if s.startswith((
        "fixed ", "fix ", "fix:", "bug ", "bug:",
        "guarded ", "guard ", "hardened ", "harden ",
    )) or s.startswith(("fix-", "self-heal")):
        return "fix"
    if s.startswith((
        "added ", "add ", "add:", "new ", "new:",
        "created ", "create ", "built ", "build ",
    )):
        return "feature"
    if s.startswith((
        "refactored", "refactor",
        "rewrote", "rewrite",
        "restructured", "restructure",
        "reorganized", "reorganize",
        "consolidated", "consolidate",
    )):
        return "refactor"
    if s.startswith((
        "updated", "update",
        "improved", "improve",
        "enhanced", "enhance",
        "polished", "polish",
        "tightened", "tighten",
        "tuned ", "tune ",
        "reworked", "rework",
        "made ", "make ",
        "moved ", "move ",
        "changed", "change",
        "replaced", "replace",
        "renamed", "rename",
        "simplified", "simplify",
    )):
        return "improvement"
    if s.startswith((
        "removed", "remove",
        "deleted", "delete",
        "dropped", "drop",
        "retired", "retire",
    )):
        return "removal"
    return "other"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60]


def parse(text: str) -> list[dict]:
    """Split the file into entries, newest first as written."""
    entries: list[dict] = []
    current: dict | None = None
    for line in (text or "").splitlines():
        m = _HEADING.match(line)
        if m:
            if current is not None:
                entries.append(current)
            current = {"heading": m.group(1).strip(), "lines": []}
            continue
        if current is not None:
            current["lines"].append(line.rstrip())
    if current is not None:
        entries.append(current)

    out: list[dict] = []
    seen: dict[str, int] = {}
    for raw in entries:
        title = raw["heading"]
        date = ""
        m = _DATE_TAIL.search(title)
        if m:
            date, title = m.group(1), title[:m.start()].rstrip()
        else:
            m = _DATE_HEAD.match(title)
            if m:
                date, title = m.group(1), m.group(2).strip()
        body = "\n".join(raw["lines"]).strip("\n")
        base = f"{date or 'undated'}-{_slug(title) or 'entry'}"
        seen[base] = seen.get(base, 0) + 1
        ident = base if seen[base] == 1 else f"{base}-{seen[base]}"
        out.append({
            "id": ident,
            "date": date,
            "title": title,
            "body": body,
            "category": category(title),
        })
    return out


def path(root: Path) -> Path:
    return Path(root) / FILE_NAME


def stamp(root: Path) -> str:
    """Cheap change detector for the file: size and mtime, or '' when absent."""
    try:
        st = path(root).stat()
    except OSError:
        return ""
    return f"{st.st_size}-{st.st_mtime_ns}"


def load(root: Path) -> dict:
    """The payload /api/changelog serves."""
    p = path(root)
    generated = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    if not p.exists():
        return {"source": FILE_NAME, "missing": True, "count": 0, "entries": [],
                "latest": "", "modified": "", "generated_at": generated}
    text = p.read_text(encoding="utf-8", errors="replace")
    entries = parse(text)
    modified = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(p.stat().st_mtime))
    return {
        "source": FILE_NAME,
        "missing": False,
        "count": len(entries),
        "entries": entries,
        "latest": entries[0]["id"] if entries else "",
        "modified": modified,
        "generated_at": generated,
    }
