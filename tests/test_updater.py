"""The Update button installs over local edits (reported from a Mac, 2026-10-02).

It used to run a bare ``git pull``, which refuses to touch a file with
uncommitted edits, so one edited file blocked every update that changed it and
the button failed. core/updater.py sets the edits aside, fast-forwards and puts
them back; when they clash, nothing changes and the message names the files.

These run the real thing against throwaway repositories: an upstream, and a
clone standing in for an install, with no git identity configured anywhere,
as on most installs. Setting edits aside still works there because git stash
signs its commits with an identity of its own when there is none.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from core import updater

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")

ROOT = Path(__file__).parents[1]
_ME = ("-c", "user.name=Test", "-c", "user.email=test@example.com")

MAC = '''def start(self, loopback_index=None, mic_index=None,
          ffmpeg_mic_name=None):
    """Start capture."""
    return loopback_index, mic_index, ffmpeg_mic_name


def stop(self):
    """Stop capture."""
'''
# The fix shipped upstream, the same shape as the real one.
MAC_FIXED = MAC.replace("ffmpeg_mic_name=None):",
                        "ffmpeg_mic_name=None,\n          loopback_name=None):")
# The same fix made by hand on one line, which clashes with it.
MAC_MINE = MAC.replace("ffmpeg_mic_name=None):", "ffmpeg_mic_name=None, loopback_name=None):")
NOTES = "Local notes.\n"


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *_ME, *args], cwd=cwd, capture_output=True,
                         text=True, encoding="utf-8")
    assert out.returncode == 0, f"git {' '.join(args)}: {out.stderr}"
    return out.stdout


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """(upstream, install), the install a clone of the upstream."""
    home = tmp_path / "home"
    home.mkdir()
    # No name or email, and no guessing one from the machine's name either:
    # git can guess on some machines and not others.
    write(home / ".gitconfig", "[user]\n\tuseConfigOnly = true\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(var, raising=False)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "--quiet")
    git(upstream, "checkout", "--quiet", "-b", "main")
    write(upstream / "mac.py", MAC)
    write(upstream / "notes.txt", NOTES)
    git(upstream, "add", ".")
    git(upstream, "commit", "--quiet", "-m", "first")
    install = tmp_path / "install"
    git(tmp_path, "clone", "--quiet", str(upstream), str(install))
    assert subprocess.run(["git", "var", "GIT_COMMITTER_IDENT"], cwd=install,
                          capture_output=True).returncode != 0, "the install has no identity"
    return upstream, install


def ship(upstream: Path, install: Path, files: dict[str, str]) -> str:
    """Commit an update upstream and fetch it, as /api/update/apply does."""
    for name, text in files.items():
        write(upstream / name, text)
    git(upstream, "add", ".")
    git(upstream, "commit", "--quiet", "-m", "update")
    git(install, "fetch", "--quiet", "origin", "main")
    return head(upstream)


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def changed(install: Path) -> list[str]:
    """Tracked files with uncommitted edits."""
    return sorted(line[3:] for line in
                  git(install, "status", "--porcelain", "--untracked-files=no").splitlines())


def stashes(install: Path) -> str:
    return git(install, "stash", "list").strip()


def test_a_clean_install_fast_forwards(repos):
    upstream, install = repos
    target = ship(upstream, install, {"mac.py": MAC_FIXED})
    assert updater.install(install) is None
    assert head(install) == target
    assert read(install / "mac.py") == MAC_FIXED
    assert changed(install) == [] and stashes(install) == ""


def test_an_edit_matching_the_update_no_longer_blocks_it(repos):
    """The report: the Mac fix made by hand, then the same fix shipped."""
    upstream, install = repos
    write(install / "mac.py", MAC_FIXED)
    target = ship(upstream, install, {"mac.py": MAC_FIXED})
    pull = subprocess.run(["git", "pull", "origin", "main"], cwd=install,
                          capture_output=True, text=True, encoding="utf-8")
    assert pull.returncode != 0 and "mac.py" in pull.stderr, "a bare pull refuses this"
    assert updater.install(install) is None
    assert head(install) == target
    assert changed(install) == [] and stashes(install) == ""


def test_edits_the_update_does_not_touch_stay(repos):
    upstream, install = repos
    write(install / "notes.txt", NOTES + "Mine.\n")
    target = ship(upstream, install, {"mac.py": MAC_FIXED})
    assert updater.install(install) is None
    assert head(install) == target
    assert read(install / "notes.txt") == NOTES + "Mine.\n"
    assert changed(install) == ["notes.txt"] and stashes(install) == ""


def test_an_edit_elsewhere_in_an_updated_file_lands_on_top(repos):
    upstream, install = repos
    write(install / "mac.py", MAC.replace("Stop capture.", "Stop capture, all of it."))
    target = ship(upstream, install, {"mac.py": MAC_FIXED})
    assert updater.install(install) is None
    assert head(install) == target
    assert read(install / "mac.py") == MAC_FIXED.replace("Stop capture.", "Stop capture, all of it.")
    assert stashes(install) == ""


def test_a_staged_edit_stays(repos):
    upstream, install = repos
    write(install / "notes.txt", "Staged.\n")
    git(install, "add", "notes.txt")
    ship(upstream, install, {"mac.py": MAC_FIXED})
    assert updater.install(install) is None
    assert read(install / "notes.txt") == "Staged.\n"
    assert stashes(install) == ""


def test_clashing_edits_change_nothing_and_the_files_are_named(repos):
    upstream, install = repos
    write(install / "mac.py", MAC_MINE)
    write(install / "notes.txt", "Mine.\n")
    before = head(install)
    ship(upstream, install, {"mac.py": MAC_FIXED, "notes.txt": "Theirs.\n"})
    problem = updater.install(install)
    assert problem == ("Your edits to mac.py, notes.txt clash with this update. "
                       "Commit or undo them, then try again."), problem
    assert head(install) == before
    # Back exactly as they were: no conflict markers, nothing half-applied.
    assert read(install / "mac.py") == MAC_MINE and read(install / "notes.txt") == "Mine.\n"
    assert changed(install) == ["mac.py", "notes.txt"] and stashes(install) == ""


def test_an_untracked_file_in_the_way_changes_nothing(repos):
    upstream, install = repos
    write(install / "notes.txt", "Mine.\n")
    write(install / "new.py", "local\n")
    before = head(install)
    ship(upstream, install, {"new.py": "shipped\n"})
    problem = updater.install(install)
    assert problem == ("The following untracked working tree files would be overwritten by "
                       "merge: new.py. Please move or remove them before you merge."), problem
    assert head(install) == before
    assert read(install / "notes.txt") == "Mine.\n" and read(install / "new.py") == "local\n"
    assert stashes(install) == ""


def test_an_install_with_its_own_commits_is_left_for_git(repos):
    upstream, install = repos
    write(install / "notes.txt", "Committed here.\n")
    git(install, "commit", "--quiet", "-am", "mine")
    before = head(install)
    ship(upstream, install, {"mac.py": MAC_FIXED})
    assert updater.install(install) == ("This copy has commits that are not on main, "
                                        "so update it with git instead.")
    assert head(install) == before and changed(install) == []


def test_a_second_install_while_one_runs_is_turned_away(repos):
    upstream, install = repos
    target = ship(upstream, install, {"mac.py": MAC_FIXED})
    with updater._installing:
        assert updater.install(install) == "An update is already being installed."
    assert updater.install(install) is None and head(install) == target


def test_up_to_date_is_success_and_a_missing_update_is_said(repos):
    upstream, install = repos
    assert updater.install(install, "HEAD") is None
    assert updater.install(install) == ("The downloaded update is missing. "
                                        "Check for updates, then try again.")


def test_the_route_installs_through_the_updater_and_always_answers_json():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    route = app[app.index('@app.route("/api/update/apply"'):]
    route = route[:route.index("\n    def _restart()")]
    assert "updater.install(root)" in route
    assert '"git", "pull"' not in route
    assert "except Exception as e:" in route and 'jsonify({"error": problem})' in route
