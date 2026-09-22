r"""Run setup for Codex must leave ~/.codex/config.toml readable by Codex.

The setup edits the user's TOML as text: it replaces the
[mcp_servers.meeting-assistant] section when there is one and appends it
otherwise. Until 2026-09-22 the replace went through re.sub, which reads a
string replacement as a template, so each \\ in the JSON-escaped Windows paths
came out as a single \ and Codex refused to start ("too few unicode value
digits" at the \U of C:\Users). Only a second run hit it, because the first
one appends. The write was also in text mode, so on Windows it turned every
line ending in the file into \r\n.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_api import rest

tomllib = pytest.importorskip("tomllib")

# Codex's own settings, which every run must leave exactly as they were.
OTHER = (
    "# written by Codex\n"
    'model = "gpt-5-codex"\n'
    "\n"
    "[mcp_servers.notes]\n"
    'command = "npx"\n'
    'args = ["-y", "notes-server"]\n'
)


@pytest.fixture(params=["you", "José"], ids=["ascii", "accented"])
def codex(request, tmp_path, monkeypatch):
    """~/.codex under a temporary home, and Windows paths for our entry."""
    root = rf"C:\Users\{request.param}\Meeting Assistant"
    python, script = rf"{root}\.venv\Scripts\python.exe", rf"{root}\mcp_server.py"
    monkeypatch.setattr(rest, "_mcp_command", lambda: (python, script))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".codex").mkdir()
    return SimpleNamespace(path=tmp_path / ".codex" / "config.toml",
                           entry={"command": python, "args": [script]})


def _entry(path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"]["meeting-assistant"]


def test_running_setup_again_changes_nothing(codex):
    codex.path.write_bytes(OTHER.encode())
    written = []
    for action in ("created", "updated", "updated"):
        payload, status = rest._setup_codex()
        assert (status, payload["action"]) == (200, action)
        written.append(codex.path.read_bytes())
    assert written[0] == written[1] == written[2]
    assert written[0].startswith(OTHER.encode())
    assert _entry(codex.path) == codex.entry


def test_running_setup_again_repairs_the_entry_it_broke(codex):
    python, script = codex.entry["command"], codex.entry["args"][0]
    broken = (OTHER + "\n"
              "[mcp_servers.meeting-assistant]\n"
              f'command = "{python}"\n'
              f'args = ["{script}"]\n'
              "\n"
              "[profiles.work]\n"
              'model = "o3"\n')
    with pytest.raises(tomllib.TOMLDecodeError):
        tomllib.loads(broken)
    codex.path.write_bytes(broken.encode())
    payload, status = rest._setup_codex()
    assert (status, payload["action"]) == (200, "updated")
    assert _entry(codex.path) == codex.entry
    doc = tomllib.loads(codex.path.read_text(encoding="utf-8"))
    assert doc["profiles"] == {"work": {"model": "o3"}}
    assert codex.path.read_bytes().startswith(OTHER.encode())


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["LF", "CRLF"])
def test_line_endings_stay_as_the_file_had_them(codex, newline):
    original = OTHER.replace("\n", newline).encode()
    codex.path.write_bytes(original)
    for _ in range(2):
        assert rest._setup_codex()[1] == 200
    data = codex.path.read_bytes()
    assert data.startswith(original)
    if newline == "\n":
        assert b"\r" not in data
    else:
        assert data.count(b"\r\n") == data.count(b"\n")
    assert _entry(codex.path) == codex.entry


def test_a_config_that_would_stop_reading_is_left_alone(codex):
    # A hand-edited header the section matcher does not recognise, so the
    # setup would append a second [mcp_servers.meeting-assistant] table.
    original = (OTHER + "\n"
                "[mcp_servers.meeting-assistant]  # mine\n"
                'command = "python"\n').encode()
    tomllib.loads(original.decode())
    codex.path.write_bytes(original)
    payload, status = rest._setup_codex()
    assert (status, payload["ok"]) == (409, False)
    assert codex.path.read_bytes() == original
    assert not codex.path.with_name("config.toml.bak").exists()
