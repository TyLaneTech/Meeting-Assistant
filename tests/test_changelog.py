"""CHANGELOG.md is the only source of user-facing release notes.

The Settings > Changelog tab and the What's new card read it through
core/changelog.py; git history is no longer consulted (2026-09-05).
"""
import re
from pathlib import Path

from core import changelog

ROOT = Path(__file__).parents[1]

SAMPLE = """# Changelog

A preamble that explains the format. The parser ignores it.

## Added a thing (2026-09-05)

### Area
- Bullet one
- Bullet two

## 2026-09-01: Fixed another thing

A paragraph, with `code` and **bold**.

## Reworked something

- undated, and that is fine

## Added a thing (2026-09-05)

Same heading again, so its id must still be unique.
"""


def test_parse_titles_dates_and_bodies():
    entries = changelog.parse(SAMPLE)
    assert [e["title"] for e in entries] == [
        "Added a thing", "Fixed another thing", "Reworked something", "Added a thing"]
    assert [e["date"] for e in entries] == ["2026-09-05", "2026-09-01", "", "2026-09-05"]
    assert entries[0]["body"] == "### Area\n- Bullet one\n- Bullet two"
    assert entries[1]["body"] == "A paragraph, with `code` and **bold**."
    assert entries[2]["body"] == "- undated, and that is fine"


def test_categories_follow_the_first_word():
    assert changelog.category("Added a Home dashboard") == "feature"
    assert changelog.category("Fixed the desktop device") == "fix"
    assert changelog.category("Improved how quickly recordings stop") == "improvement"
    assert changelog.category("Made the Start Menu shortcut silent") == "improvement"
    assert changelog.category("Refactored the mixer") == "refactor"
    assert changelog.category("Removed the Resolve tab") == "removal"
    assert changelog.category("Notes on nothing in particular") == "other"
    entries = changelog.parse(SAMPLE)
    assert [e["category"] for e in entries] == ["feature", "fix", "improvement", "feature"]


def test_ids_are_stable_and_unique():
    ids = [e["id"] for e in changelog.parse(SAMPLE)]
    assert ids[0] == "2026-09-05-added-a-thing"
    assert ids[2] == "undated-reworked-something"
    assert len(set(ids)) == len(ids)
    assert ids[3] == "2026-09-05-added-a-thing-2"


def test_load_without_the_file_is_empty_not_an_error(tmp_path):
    payload = changelog.load(tmp_path)
    assert payload["missing"] is True
    assert payload["entries"] == [] and payload["count"] == 0 and payload["latest"] == ""
    assert changelog.stamp(tmp_path) == ""


def test_load_reports_the_newest_entry_and_a_change_stamp(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(SAMPLE, encoding="utf-8")
    payload = changelog.load(tmp_path)
    assert payload["count"] == 4
    assert payload["latest"] == "2026-09-05-added-a-thing"
    assert payload["modified"] and payload["generated_at"]
    assert changelog.stamp(tmp_path)


def test_the_repo_changelog_parses_newest_first():
    payload = changelog.load(ROOT)
    entries = payload["entries"]
    assert payload["missing"] is False and len(entries) >= 150
    assert all(e["title"] for e in entries)
    assert entries[0]["date"], "the newest entry carries a date"
    dated = [e["date"] for e in entries if e["date"]]
    assert dated[0] == max(dated), "newest first"
    for e in entries:
        assert not e["title"].lower().startswith("merged pr"), e["title"]
        assert "[internal]" not in e["title"].lower(), e["title"]
        assert "```" not in e["body"], e["title"]
    # The entry that landed with the wrong text on main reads properly here.
    big = next(e for e in entries if e["title"].startswith("Added a Home dashboard"))
    assert "### Home" in big["body"] and "Completion dialog" not in big["body"]


def test_the_app_reads_the_file_not_git():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "changelog.load(" in app and "changelog.stamp(" in app
    assert "_build_changelog" not in app and "_CHANGELOG_EXCLUDE_HASHES" not in app
    js = (ROOT / "ui_web/static/app.js").read_text(encoding="utf-8")
    assert "data.entries" in js
    # data.commits_behind (the update counter) is fine; the old payload key is not.
    assert not re.search(r"data\.commits\b", js), "the client still reads the git-based payload"
    assert "renderMd(markdown" in js

# ── The notes are formatted markdown, so the styling has to keep up ──────────

# What CHANGELOG.md's own preamble tells contributors to use, and what the
# renderer therefore has to style. An element encouraged there but missing here
# lands with browser defaults, which is how a blockquote used to render as an
# ordinary paragraph and a nested bullet as its parent.
_STYLED_ELEMENTS = [
    "h3", "p", "ul", "ol", "li", "strong", "em", "code", "pre", "a",
    "blockquote", "del", "hr", "table", "th", "td",
]


def _changelog_css() -> str:
    css = (ROOT / "ui_web/static/style.css").read_text(encoding="utf-8")
    return "\n".join(
        line for line in css.splitlines() if ".changelog-entry-body" in line
    )


def _changelog_targets() -> set:
    """The element names the .changelog-entry-body rules actually target.

    Parsed from the selectors rather than grepped for, so a rule that merely
    mentions an element in passing does not count as styling it.
    """
    css = (ROOT / "ui_web/static/style.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    targets = set()
    for selectors, _body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        parts = [part.strip() for part in selectors.split(",") if part.strip()]
        if not any(".changelog-entry-body" in part for part in parts):
            continue
        for part in parts:
            pieces = part.split()
            if not pieces:
                continue
            # The rightmost simple selector, minus any pseudo or class suffix.
            last = re.split(r"[:.>#\[]", pieces[-1])[0]
            if last.isalnum():
                targets.add(last)
    return targets


def test_every_element_the_guidelines_encourage_is_styled():
    targets = _changelog_targets()
    missing = [e for e in _STYLED_ELEMENTS if e not in targets]
    assert not missing, (
        f"CHANGELOG.md's preamble encourages {missing}, but "
        f".changelog-entry-body does not style them, so they render with "
        f"browser defaults. Styled: {sorted(targets)}"
    )


def test_the_two_levels_of_bullet_are_told_apart_by_shape():
    """Position alone is not enough: a nested bullet indented under a parent
    with the same filled dot reads as a sibling."""
    styled = _changelog_css()
    assert ".changelog-entry-body li > ul" in styled
    assert ".changelog-entry-body li > ul > li::before" in styled


def test_a_table_scrolls_rather_than_widening_the_card():
    """The What's new card is about 512 px. An unconstrained table pushed the
    whole entry sideways."""
    css = (ROOT / "ui_web/static/style.css").read_text(encoding="utf-8")
    table = css[css.index(".changelog-entry-body table {"):]
    table = table[:table.index("}")]
    assert "overflow-x: auto" in table
    assert "max-width: 100%" in table


def test_the_preamble_documents_the_formatting_and_is_still_a_preamble():
    text = (ROOT / changelog.FILE_NAME).read_text(encoding="utf-8")
    preamble = text[:text.index("\n## ")]
    for hint in ("`**bold**`", "`` `code` ``", "`*italic*`", "Nested", "`> `"):
        assert hint in preamble, hint
    # It says what code is NOT for, which is the rule most easily got wrong.
    assert "Never for a module" in preamble
    # And the parser still throws the whole thing away.
    assert changelog.parse(text)[0]["date"] is not None
    assert all("How an entry is read" not in e["title"] for e in changelog.parse(text))


def test_the_newest_entry_actually_uses_the_formatting():
    """The house style is only real if the entry demonstrating it does."""
    text = (ROOT / changelog.FILE_NAME).read_text(encoding="utf-8")
    body = changelog.parse(text)[0]["body"]
    assert body.count("**") >= 8, "bold on the thing that changed"
    assert body.count("`") >= 4, "code for what the user sees in the app"
    assert "\n  - " in body, "nested detail under a bullet"
    assert "\n> " in body, "one note that is not itself a change"
    # code is for user-visible strings, never a module or a path.
    for banned in ("app.py", "core/", "ui_web/", ".py`", "_renderChangelogBody"):
        assert banned not in body, f"{banned} is developer language"
