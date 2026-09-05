"""The Windows launchers: a silent Start Menu start, and no launcher that can
hang or die quietly when it runs without a console.

Until 2026-09-05 the Start Menu shortcut ran ``cmd /c launch.bat`` and left a
minimised console window open for the whole session, and ``launch.bat`` ended
in ``pause`` even when the hidden launcher ran it, which nobody could answer.
"""
from pathlib import Path

from core import shortcut

ROOT = Path(__file__).parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_start_menu_shortcut_runs_the_click_launcher():
    launch = _read("launch.py")
    block = launch[launch.index("def _create_start_menu_shortcut"):launch.index("def _create_macos_app_shortcut")]
    assert "$s.TargetPath       = 'wscript.exe'" in block
    assert "vbs_path" in block and 'root / "app_launcher.vbs"' in block
    assert "'cmd.exe'" not in block
    assert "_other_checkout(cur_args, root)" in block


def test_sign_in_shortcut_is_migrated_and_created_tray_only():
    launch = _read("launch.py")
    assert "_migrate_startup_shortcut()" in launch[launch.index("def main"):]
    app = _read("app.py")
    block = app[app.index("def set_startup"):app.index("def settings_status")]
    assert "launch_hidden.vbs" in block
    assert '"wscript.exe"' in block
    assert "cmd.exe" not in block


def test_click_launcher_asks_the_app_to_open_its_window():
    vbs = _read("app_launcher.vbs")
    assert "/api/window/open" in vbs
    assert "launch_hidden.vbs" in vbs
    # A first run shows the installer in a console instead of hiding it.
    assert '.venv\\Scripts\\python.exe' in vbs and 'launch.bat"""' in vbs
    assert "For i = 1 To 180" in vbs
    app = _read("app.py")
    assert '@app.route("/api/window/open", methods=["POST"])' in app
    handler = app[app.index("def open_window"):app.index("def get_startup")]
    assert "browser.open_app_window(" in handler


def test_hidden_launcher_never_pauses():
    assert '" --hidden > "' in _read("launch_hidden.vbs")
    bat = _read("launch.bat")
    assert 'if /i "%~1"=="--hidden" goto :failed_hidden' in bat
    assert "MessageBox" in bat[bat.index(":failed_hidden"):]
    tail = bat[bat.index('"%ROOT%launch.py"'):]
    commands = [l.strip().lower() for l in tail.splitlines()]
    assert commands.count("pause") == 1, "one pause command, on the visible-console path only"


def test_points_at_accepts_old_and_new_shortcut_forms():
    bat = Path(r"C:\Apps\Meeting Assistant\launch.bat")
    cmd_old = {"target": r"C:\Windows\system32\cmd.exe", "arguments": r'/c ""C:\Apps\Meeting Assistant\launch.bat""'}
    click = {"target": r"C:\Windows\System32\wscript.exe", "arguments": r'"C:\Apps\Meeting Assistant\app_launcher.vbs"'}
    hidden = {"target": "wscript.exe", "arguments": r'"C:\Apps\Meeting Assistant\launch_hidden.vbs"'}
    other_bat = {"target": "cmd.exe", "arguments": r'/c ""C:\Elsewhere\launch.bat""'}
    other_vbs = {"target": "wscript.exe", "arguments": r'"C:\Elsewhere\app_launcher.vbs"'}
    assert shortcut.points_at(cmd_old, bat)
    assert shortcut.points_at(click, bat)
    assert shortcut.points_at(hidden, bat)
    assert not shortcut.points_at(other_bat, bat)
    assert not shortcut.points_at(other_vbs, bat)
    assert not shortcut.points_at(None, bat)
