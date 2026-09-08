"""Launching at sign-in, opening on launch, and how fast the tray icon lands.

Three things that all failed the same way: something reported success while
nothing happened. The Startup toggle read the shortcut file and called that
"enabled" while Windows had the entry switched off; the tray icon took nearly
seven seconds to appear on a headless app, which reads as hung; and a port
probe spent two seconds asking Windows about proxies for 127.0.0.1.

And the one the tray speedup broke: with core.config no longer importing
torchaudio at module level, the four model loaders became the first import of
PyTorch, all at once, and the app crashed on launch. The last section here pins
the order that keeps that first import single-threaded.

Source assertions plus the settings defaults, which import without the ML
stack. The timings themselves are in the commit message, not here: a test that
asserts wall-clock startup would fail on a busy machine for no good reason.
"""
import re
from pathlib import Path

from core import settings

ROOT = Path(__file__).parents[1]
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")
APP_JS = (ROOT / "ui_web/static/app.js").read_text(encoding="utf-8")
SETTINGS_HTML = (ROOT / "ui_web/templates/_settings.html").read_text(encoding="utf-8")
SHORTCUT_PY = (ROOT / "core/shortcut.py").read_text(encoding="utf-8")
CONFIG_PY = (ROOT / "core/config.py").read_text(encoding="utf-8")
TRAY_PY = (ROOT / "ui_desktop/tray.py").read_text(encoding="utf-8")


def _fn(src: str, start: str, end: str) -> str:
    body = src[src.index(start):]
    return body[:body.index(end)]


# ── Launch at Startup tells the truth ────────────────────────────────────────

def test_windows_own_approval_flag_is_read():
    """A shortcut in the Startup folder is only half of it. Task Manager and
    friends switch entries off with a registry flag, and Windows then ignores
    the shortcut: reporting the file's existence showed the toggle on while
    nothing launched at sign-in."""
    body = _fn(SHORTCUT_PY, "def startup_approval(", "def approve_startup(")
    assert "StartupApproved" in SHORTCUT_PY
    assert 'return "enabled"' in body        # no entry means no objection
    assert "blob[0] & 0x01" in body          # bit 0 set means disabled
    # Never raises at a caller: an unreadable registry is "unknown", not a crash.
    assert 'return "unknown"' in body
    assert "except OSError:" in body


def test_enabling_clears_the_flag_and_deletes_rather_than_writes():
    """Absence is Windows' own default for approved, so there is no 12 byte
    blob format to get wrong on the way out."""
    body = _fn(SHORTCUT_PY, "def approve_startup(", "def same_path(")
    assert "winreg.DeleteValue(key, name)" in body
    assert "KEY_SET_VALUE" in body
    assert "except FileNotFoundError:" in body    # nothing to clear is success


def test_the_route_reports_blocked_and_the_toggle_clears_it():
    get = _fn(APP_PY, "def get_startup(", '@app.route("/api/settings/startup", methods=["POST"])')
    assert '_shortcut.startup_approval(lnk.name)' in get
    assert '"enabled": present and not blocked' in get
    assert '"blocked": blocked' in get
    # Turning it on is the user asking for it, so the block is cleared then and
    # only then: never silently on startup, which would undo a Task Manager
    # choice they made deliberately.
    post = _fn(APP_PY, "def set_startup(", '@app.route("/api/settings/status")')
    assert "_shortcut.approve_startup(lnk.name)" in post
    assert 'if _shortcut.startup_approval(lnk.name) == "disabled":' in post
    assert "409" in post
    assert "approve_startup" not in _fn(APP_PY, "def get_startup(", "def set_startup(")


def test_the_client_surfaces_the_block_and_never_lies():
    assert 'id="startup-blocked"' in SETTINGS_HTML
    body = _fn(APP_JS, "async function setStartupLaunch(", "async function loadStartupState(")
    # A refused enable must not leave the toggle claiming it worked.
    assert "if (!res.ok || data.ok === false)" in body
    assert "checked = false" in body
    assert "uiToast(" in body
    assert "loadStartupState();" in body
    reload = _fn(APP_JS, "async function loadStartupState(", "\n}")
    assert "st.blocked" in reload
    assert "st.reason" in reload


# ── Open on Launch ───────────────────────────────────────────────────────────

def test_open_on_launch_is_off_by_default():
    """The app is normally started to sit in the tray, and at sign-in a window
    nobody asked for is in the way."""
    assert settings.DEFAULTS["open_window_on_launch"] is False


def test_the_checkbox_sits_under_launch_at_startup():
    startup = SETTINGS_HTML.index('id="startup-row"')
    on_launch = SETTINGS_HTML.index('id="open-on-launch-toggle"')
    group_end = SETTINGS_HTML.index('<div class="settings-group-title">Sidebar</div>')
    assert startup < on_launch < group_end, "same group, directly below"
    assert "Open on Launch" in SETTINGS_HTML
    assert "savePref('open_window_on_launch', this.checked)" in SETTINGS_HTML


def test_a_launch_that_needs_keys_opens_the_window_regardless():
    """There is nothing to do until the keys are set, so that case ignores the
    preference."""
    body = _fn(APP_PY, "    # Open the window: always when keys are missing",
               "    # Register SIGINT after Flask starts")
    assert "if config.needs_setup(_active_provider):" in body
    assert 'elif settings.get("open_window_on_launch", False):' in body
    assert body.index("needs_setup") < body.index("open_window_on_launch")


# ── The tray icon lands before the model stack ───────────────────────────────

def test_the_pyannote_shim_no_longer_drags_torch_into_every_import():
    """core.config imported torchaudio, which imported all of PyTorch: 1.2 of
    the 1.4 seconds the module cost. ui_desktop.tray pays that to draw an
    icon."""
    assert "def apply_torchaudio_shims()" in CONFIG_PY
    # Not called at import any more.
    top = CONFIG_PY[:CONFIG_PY.index("def apply_torchaudio_shims(")]
    assert "import torchaudio" not in top
    assert "apply_torchaudio_shims()" not in top
    # Idempotent, because it is now called on several paths.
    body = _fn(CONFIG_PY, "def apply_torchaudio_shims(", "REQUIRED_KEYS")
    assert "if _shims_applied:" in body


def test_every_pyannote_import_applies_the_shim_first():
    """The invariant the old import-time call bought: shims before pyannote.
    A fifth pyannote import has to do this too."""
    importers = {
        "ml/diarizer.py": "from pyannote.audio import Model as _PyannoteModel",
        "ml/speaker_db.py": "from pyannote.audio import Inference, Model",
        "ml/batch_transcriber.py": "from pyannote.audio import Pipeline as PyannotePipeline",
        "core/network.py": "from pyannote.audio import Pipeline as PyannotePipeline",
    }
    for name, pyannote_import in importers.items():
        src = (ROOT / name).read_text(encoding="utf-8")
        assert "apply_torchaudio_shims()" in src, name
        assert src.index("apply_torchaudio_shims()") < src.index(pyannote_import), name


def test_the_tray_starts_above_the_model_stack():
    """Headless, the icon is the only sign the app is alive, so it goes up
    before the imports that cost a second and a half."""
    early = APP_PY.index("# ── The tray icon, before the model stack")
    heavy = APP_PY.index("from ai.assistant import AIAssistant")
    ml = APP_PY.index("from ml.transcriber import (")
    assert early < heavy < ml
    # Importing app.py (tests, selftest, a REPL) must not put an icon up.
    guard = APP_PY[early:heavy]
    assert 'if __name__ == "__main__":' in guard
    assert "_start_tray(_server_url, wait=True)" in guard
    # A second instance about to abort must not flash an icon it will discard.
    assert "if not _port_is_busy(_server_url):" in guard


def test_the_tray_state_is_empty_until_main_fills_it():
    """The tray's own .get() defaults already say the right thing while the
    app loads: not ready, "Loading models...". No invented placeholder."""
    body = _fn(APP_PY, "def _tray_state(", "def _tray_quit(")
    assert "return provider() if provider is not None else {}" in body
    assert 'st.get("recording_ready_reason", "Loading models...")' in TRAY_PY
    # main() swaps the real one in rather than building a second tray.
    assert "_tray_state_provider = _state_snapshot" in APP_PY
    assert "if not _tray_started and sys.platform != \"darwin\":" in APP_PY


def test_quitting_from_the_tray_works_during_the_import_window():
    """_force_quit is defined much further down the module, so the menu
    resolves it when clicked instead of binding it at construction."""
    body = _fn(APP_PY, "def _tray_quit(", "def _port_is_busy(")
    assert 'globals().get("_force_quit")' in body
    assert "os._exit(0)" in body


def test_the_startup_probes_skip_the_system_proxy():
    """The first urlopen() in a process asks Windows for the proxy config, and
    on a machine running a VPN client that took two seconds to answer a
    question about 127.0.0.1. A proxy is never right for localhost."""
    probe = _fn(APP_PY, "def _port_is_busy(", "_local_http = None")
    assert "import socket" in probe
    assert "connect_ex((\"127.0.0.1\", port))" in probe
    assert "urllib" not in probe.split('"""')[2]   # body, not the explanation
    opener = _fn(APP_PY, "def _local_opener(", "def _start_tray(")
    assert "ProxyHandler({})" in opener
    # Every self-call goes through it, or the cost comes straight back.
    handshake = _fn(APP_PY, "def _handshake_existing_instance(", "def _keepalive_loop(")
    assert "_local_opener()" in handshake
    assert "urllib.request.urlopen" not in handshake


def test_the_tray_only_repaints_when_what_it_shows_changes():
    """refresh() is called on every status push, and model loading pushes a
    dozen, each of which was rebuilding the native menu to keep saying the
    same thing."""
    body = _fn(TRAY_PY, "    def refresh(self", "    def _refresh_ui(")
    assert "if showing is not None and showing == self._painted:" in body
    assert "if not force:" in body
    # The icon-set change replaces the images behind an unchanged state.
    assert "_refresh_tray(force=True)" in APP_PY
    assert "def _refresh_tray(force: bool = False)" in APP_PY


def test_only_the_icon_being_shown_is_decoded():
    """Six PNG decodes plus six LANCZOS resizes on the tray thread is a second
    of work taken out of the imports next to it."""
    assert "_ensure_icons" not in TRAY_PY
    body = _fn(TRAY_PY, "def _icon_image(", "def reload_icons(")
    assert "_STATE_SLOT.get(state)" in body
    assert "_icons[state] = img" in body
    assert "for state, slot in" not in body


# ── PyTorch is imported once, alone, before the loaders ──────────────────────

def _main_body() -> str:
    body = APP_PY[APP_PY.index("def main() -> None:"):]
    return body[:body.index('if __name__ == "__main__":\n    main()')]


def test_torch_is_imported_on_the_main_thread_before_the_server_exists():
    """The eager torchaudio import had been guaranteeing this for years: torch
    on the main thread before any other thread existed. The launch after it
    went lazy handed one loader a half-built torch module, two others got
    WinError 1114 on c10.dll, and the process died with an access violation
    (0f470a4, 2026-09-08). Twice in a row, so not a fluke."""
    main = _main_body()
    assert "_preload_torch()" in main
    at = main.index("_preload_torch()")
    # A second instance exits at the handshake without paying 1.2 s for torch.
    assert main.index("_handshake_existing_instance(url)") < at
    # Before the server: no request thread can join the first import either.
    assert at < main.index("flask_thread = threading.Thread(")
    assert at < main.index("_start_background_initializers()")


def test_whoever_starts_the_loaders_preloads_first():
    """The guard, for the day main() is reordered or the initializers get a
    second caller. Free once torch is in."""
    body = _fn(APP_PY, "def _start_background_initializers(", "def _level_push_loop(")
    at = body.index("_preload_torch()")
    for loader in ("_load_model", "_load_diarizer", "_load_fingerprint_db",
                   "_load_text_embeddings"):
        start = f"threading.Thread(target={loader}, daemon=True).start()"
        assert start in body, loader
        assert at < body.index(start), f"{loader} starts before torch is imported"


def test_the_preload_is_the_old_import_and_never_raises():
    body = _fn(APP_PY, "def _preload_torch(", "def _start_background_initializers(")
    assert "import torch" in body
    assert "config.apply_torchaudio_shims()" in body    # torchaudio too, as before
    assert "except Exception" in body                    # a broken DLL is an OSError
    code = body.split('"""')[2]                          # the body, not the story
    assert not re.search(r"^[ ]+raise", code, re.M)
    # Idempotent: main() and the initializers both call it.
    assert "if _torch_preloaded:" in body
    assert APP_PY.count("\n    _preload_torch()") == 2   # the calls, not the def


def test_no_loader_thread_is_started_at_import_time():
    """Every loader start is inside a function that runs after the preload.
    A module-level start would run while app.py is still importing, before
    main() gets the chance."""
    import ast
    tree = ast.parse(APP_PY)
    loaders = {"_load_model", "_load_diarizer", "_load_fingerprint_db",
               "_load_text_embeddings"}
    for node in tree.body:                # module level only
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                for kw in sub.keywords:
                    if kw.arg == "target" and isinstance(kw.value, ast.Name):
                        assert kw.value.id not in loaders, (
                            f"{kw.value.id} is started at import time")

