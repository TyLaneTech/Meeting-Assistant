"""Settings shows the diarizer device the models load on, also while asleep.

With the diarizer unloaded (still loading, or dropped by the idle unload) the
Settings dropdown showed the best device, so a user who had picked the CPU saw
"GPU" whenever the models were asleep. It now shows what _load_diarizer() will
load: the saved device when this machine can honor it.

app.py is never imported here (that loads the transcription model): the choice
is lifted out of the source and run against a stand-in settings store.

Run: .venv/Scripts/python -m pytest tests/test_diarizer_device_while_asleep.py
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import core.compute_device as compute_device

ROOT = Path(__file__).parents[1]
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")


class _Settings:
    def __init__(self, values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def _saved_device(monkeypatch, saved, best):
    monkeypatch.setattr(compute_device, "best_torch_device", lambda: best)
    tree = ast.parse(APP_PY)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_saved_diarizer_device")
    scope: dict = {"settings": _Settings({"diarizer_device": saved})}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<app._saved_diarizer_device>",
                 "exec"), scope)
    return scope["_saved_diarizer_device"]()


@pytest.mark.parametrize("saved, best, expected", [
    ("cpu", "cuda", "cpu"),      # the regression: Settings showed "cuda" here
    ("cuda", "cuda", "cuda"),
    ("cuda", "cpu", None),       # a GPU choice saved on another machine
    ("", "cuda", None),          # nothing saved: the diarizer picks the best
])
def test_the_saved_device_is_used_when_this_machine_can(monkeypatch, saved, best, expected):
    assert _saved_device(monkeypatch, saved, best) == expected


def test_a_saved_cpu_never_waits_on_the_gpu_check(monkeypatch):
    def _probe():
        raise AssertionError("the GPU check ran for a saved CPU choice")
    monkeypatch.setattr(compute_device, "best_torch_device", _probe)
    tree = ast.parse(APP_PY)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_saved_diarizer_device")
    scope: dict = {"settings": _Settings({"diarizer_device": "cpu"})}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<app>", "exec"), scope)
    assert scope["_saved_diarizer_device"]() == "cpu"


def _route_body(name: str) -> str:
    start = APP_PY.index(f"def {name}(")
    return APP_PY[start:APP_PY.index("\n\n\n", start)]


def test_loading_and_settings_use_the_same_choice():
    assert "_saved_diarizer_device()" in _route_body("_load_diarizer")
    assert "_saved_diarizer_device() or best_torch_device()" in _route_body("get_models")
