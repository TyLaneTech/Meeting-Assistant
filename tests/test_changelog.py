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
