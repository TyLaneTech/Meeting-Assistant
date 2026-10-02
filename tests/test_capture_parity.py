"""Both audio backends take the same calls (reported from a Mac, 2026-10-02).

app.py talks to whichever backend capture_audio/__init__.py picked for the
platform, so a parameter added to one side only is a TypeError on the other:
loopback_name went into the Windows AudioCapture.start() on 2026-07-19, and
from then on Record and the audio test crashed on every Mac. Neither backend
imports on the other platform, so the signatures are read from the source.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
BACKENDS = {name: ast.parse((ROOT / f"capture_audio/{name}.py").read_text(encoding="utf-8"))
            for name in ("windows", "mac")}


def _signature(fn: ast.FunctionDef) -> str:
    return ast.unparse(fn.args)


def _capture_methods(tree: ast.Module) -> dict[str, str]:
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AudioCapture")
    return {n.name: _signature(n) for n in cls.body
            if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}


def _functions(tree: ast.Module) -> dict[str, str]:
    return {n.name: _signature(n) for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_audio_capture_methods_on_both_backends_take_the_same_parameters():
    win, mac = (_capture_methods(BACKENDS[b]) for b in ("windows", "mac"))
    for name in ("start", "stop", "start_wav", "stop_wav", "inject_mic_data",
                 "compute_spectrum", "finalize_per_source_tracks"):
        assert name in win and name in mac, name
    for name in win.keys() & mac.keys():
        assert win[name] == mac[name], f"AudioCapture.{name}: windows({win[name]}) vs mac({mac[name]})"


def test_the_functions_the_package_exports_take_the_same_parameters():
    init = (ROOT / "capture_audio/__init__.py").read_text(encoding="utf-8")
    exported = ast.literal_eval(init[init.index("__all__ = ") + len("__all__ = "):].strip())
    win, mac = (_functions(BACKENDS[b]) for b in ("windows", "mac"))
    for name in exported:
        if name == "AudioCapture":
            continue
        assert name in win and name in mac, name
        assert win[name] == mac[name], f"{name}: windows({win[name]}) vs mac({mac[name]})"


def test_app_only_passes_start_keywords_both_backends_accept():
    app = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    accepted = {b: {a.arg for a in next(
        n for n in next(c for c in t.body if isinstance(c, ast.ClassDef) and c.name == "AudioCapture").body
        if isinstance(n, ast.FunctionDef) and n.name == "start").args.args} for b, t in BACKENDS.items()}
    calls = [node for node in ast.walk(app)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "start" and isinstance(node.func.value, ast.Name)
             and node.func.value.id == "capture"]
    assert len(calls) >= 2, "the Record and audio-test starts"
    for call in calls:
        for kw in call.keywords:
            for backend, names in accepted.items():
                assert kw.arg in names, f"app.py line {call.lineno} passes {kw.arg}= to the {backend} start()"
