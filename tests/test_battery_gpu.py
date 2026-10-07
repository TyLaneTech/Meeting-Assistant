"""Battery-aware GPU use: the device plan for batch reanalysis, the power
source check and its test override, the charger gate's once-a-minute
re-check, and the batch worker child process (frames, cancel, errors).

Run: .venv/Scripts/python -m pytest tests/test_battery_gpu.py
"""
import os
import sys
import threading
import time

import numpy as np
import pytest

from core import log, power
from core.compute_device import plan_batch_devices

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _keep_out_of_the_app_log(monkeypatch):
    # These tests log fake "charger connected" and "batch worker" lines; keep
    # them out of the live app.log, where they read like real events.
    monkeypatch.setattr(log, "_capture", lambda *a, **k: None)


# ── Device plan ───────────────────────────────────────────────────────────────

def test_pats_settings_on_the_charger_use_the_gpu_for_whisper_only():
    # whisper on auto, diarizer on cpu (Pat's settings since 2026-09-15)
    p = plan_batch_devices("auto", "cpu", "cuda", on_ac_power=True, automatic=True)
    assert p == {"whisper": "cuda", "diarizer": "cpu",
                 "wait_for_charger": False, "battery_cpu": False}


def test_automatic_pass_on_battery_waits_and_keeps_its_gpu_plan():
    p = plan_batch_devices("auto", "cpu", "cuda", on_ac_power=False, automatic=True)
    assert p["wait_for_charger"] is True
    assert p["whisper"] == "cuda"


def test_manual_reanalysis_ignores_the_power_source():
    # A reanalysis the user starts runs on the device the settings pick, on
    # battery as on the charger. Moved to the CPU, an hour-long meeting took
    # most of an hour with Record locked out, and an explicit GPU choice was
    # overridden (review of PR 1086, 2026-10-07).
    for setting in ("auto", "cuda"):
        p = plan_batch_devices(setting, "", "cuda", on_ac_power=False, automatic=False)
        assert p == {"whisper": "cuda", "diarizer": "cuda",
                     "wait_for_charger": False, "battery_cpu": False}
    p = plan_batch_devices("cpu", "", "cuda", on_ac_power=False, automatic=False)
    assert (p["whisper"], p["diarizer"]) == ("cpu", "cpu")


def test_cpu_only_plan_never_waits_for_the_charger():
    p = plan_batch_devices("cpu", "cpu", "cuda", on_ac_power=False, automatic=True)
    assert p["wait_for_charger"] is False and p["battery_cpu"] is False
    assert (p["whisper"], p["diarizer"]) == ("cpu", "cpu")


def test_blank_diarizer_setting_follows_the_whisper_device():
    p = plan_batch_devices("auto", "", "cuda", on_ac_power=True, automatic=False)
    assert (p["whisper"], p["diarizer"]) == ("cuda", "cuda")


def test_diarizer_gpu_setting_means_mps_on_a_mac():
    p = plan_batch_devices("auto", "cuda", "mps", on_ac_power=True, automatic=False)
    assert (p["whisper"], p["diarizer"]) == ("mps", "mps")


def test_mps_is_not_gated_by_the_battery():
    p = plan_batch_devices("auto", "", "mps", on_ac_power=False, automatic=True)
    assert p["wait_for_charger"] is False and p["whisper"] == "mps"


def test_requested_cuda_without_a_gpu_falls_back_to_cpu():
    p = plan_batch_devices("cuda", "cuda", "cpu", on_ac_power=True, automatic=False)
    assert (p["whisper"], p["diarizer"]) == ("cpu", "cpu")


# ── Power source ──────────────────────────────────────────────────────────────

def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("MA_FORCE_POWER", "battery")
    assert power.on_ac_power() is False
    monkeypatch.setenv("MA_FORCE_POWER", "ac")
    assert power.on_ac_power() is True


def test_file_override_switches_a_running_process(monkeypatch, tmp_path):
    monkeypatch.delenv("MA_FORCE_POWER", raising=False)
    from core import paths
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    (tmp_path / "force_power").write_text("battery", encoding="utf-8")
    assert power.on_ac_power() is False
    (tmp_path / "force_power").write_text("ac", encoding="utf-8")
    assert power.on_ac_power() is True


@pytest.mark.skipif(sys.platform != "win32", reason="GetSystemPowerStatus is Windows only")
def test_real_power_status_is_readable():
    assert power._windows_ac_line() in (True, False)


# ── Charger gate ──────────────────────────────────────────────────────────────

def test_gate_rechecks_power_at_most_once_a_minute():
    now = [1000.0]
    gate = power.ChargerGate(clock=lambda: now[0])
    asked = []
    on_ac = [False]

    def plan():
        asked.append(now[0])
        return {"wait_for_charger": not on_ac[0], "whisper": "cuda"}

    assert gate.check(plan) is None and gate.waiting
    for t in (1005.0, 1030.0, 1059.0):        # the worker's 5 s wakeups
        now[0] = t
        assert gate.check(plan) is None
    assert asked == [1000.0], "power must not be re-read inside the minute"
    now[0] = 1060.0
    assert gate.check(plan) is None
    assert asked == [1000.0, 1060.0]
    on_ac[0] = True                            # charger plugged in
    now[0] = 1121.0
    assert gate.check(plan) == {"wait_for_charger": False, "whisper": "cuda"}
    assert not gate.waiting


def test_gate_passes_straight_through_on_the_charger():
    gate = power.ChargerGate(clock=lambda: 5.0)
    plan = {"wait_for_charger": False}
    assert gate.check(lambda: plan) is plan
    assert gate.check(lambda: plan) is plan
    assert not gate.waiting


# ── Batch worker child process ───────────────────────────────────────────────

class FakeBatch:
    """Stands in for BatchTranscriber inside the child (MA_BATCH_WORKER_IMPL)."""

    def __init__(self, on_text_callback, fingerprint_callback=None, hf_token="",
                 on_progress_callback=None):
        self.on_text = on_text_callback
        self.fp = fingerprint_callback
        self.progress = on_progress_callback

    def process_wav_file(self, wav_path, params, tracks_root=None):
        from core import log
        mode = params.get("mode")
        if mode == "fail":
            raise ValueError("boom")
        if mode == "import":
            raise ModuleNotFoundError("No module named 'transformers'")
        if mode == "die":
            os._exit(3)
        if mode == "env":
            self.on_text(str(os.environ.get("CUDA_VISIBLE_DEVICES")), "env", 0.0, 0.0)
            return
        log.info("batch", f"fake run on {wav_path}")
        self.progress(0.4)
        if self.fp:
            self.fp("Speaker 1", np.arange(4, dtype=np.float32), 0.0, 1.0)
        self.on_text("hello there", "Speaker 1", 0.0, 1.0)
        if mode == "slow":
            for i in range(600):
                self.on_text(f"line {i}", "Speaker 2", float(i), float(i) + 0.5)
                time.sleep(0.05)
        self.progress(1.0)


@pytest.fixture
def fake_impl(monkeypatch):
    # The child imports this test module by name, so tests/ must be on its path.
    monkeypatch.setenv("MA_BATCH_WORKER_IMPL", "test_battery_gpu:FakeBatch")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
        [os.path.join(ROOT, "tests"), ROOT, os.environ.get("PYTHONPATH", "")]))


def _run(params, cancel=None, fingerprints=True, cuda=True):
    from ml.batch_worker import run_in_child
    got = {"text": [], "fp": [], "progress": []}
    run_in_child(
        "x.wav", params,
        on_text=lambda *a: got["text"].append(a),
        on_fingerprint=(lambda *a: got["fp"].append(a)) if fingerprints else None,
        on_progress=lambda p: got["progress"].append(p),
        cancel_event=cancel,
        cuda=cuda,
    )
    return got


def test_cpu_only_job_hides_the_nvidia_card_from_the_child(fake_impl, monkeypatch):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    assert _run({"mode": "env"}, cuda=False)["text"][0][0] == "-1"
    assert _run({"mode": "env"}, cuda=True)["text"][0][0] == "None"


def test_child_delivers_text_fingerprints_and_progress(fake_impl):
    got = _run({})
    assert got["text"] == [("hello there", "Speaker 1", 0.0, 1.0)]
    assert len(got["fp"]) == 1
    spk, audio, s, e = got["fp"][0]
    assert spk == "Speaker 1" and np.array_equal(audio, np.arange(4, dtype=np.float32))
    assert got["progress"] == [0.4, 1.0]


def test_child_skips_fingerprints_when_the_parent_has_no_library(fake_impl):
    got = _run({}, fingerprints=False)
    assert got["fp"] == [] and len(got["text"]) == 1


def test_child_error_reaches_the_parent(fake_impl):
    with pytest.raises(RuntimeError, match="boom"):
        _run({"mode": "fail"})


def test_missing_batch_dependencies_raise_importerror(fake_impl):
    # The app's real-time fallback keys off ImportError.
    with pytest.raises(ImportError):
        _run({"mode": "import"})


def test_child_crash_is_reported(fake_impl):
    with pytest.raises(RuntimeError, match="code 3"):
        _run({"mode": "die"})


def test_cancel_kills_the_child_and_stops_delivery(fake_impl):
    from ml.batch_transcriber import ReanalysisCancelled
    from ml.batch_worker import run_in_child
    cancel = threading.Event()
    texts = []

    def on_text(*a):
        texts.append(a)
        if len(texts) == 5:
            cancel.set()

    t0 = time.monotonic()
    with pytest.raises(ReanalysisCancelled):
        run_in_child("x.wav", {"mode": "slow"}, on_text=on_text, on_fingerprint=None,
                     on_progress=None, cancel_event=cancel)
    assert len(texts) == 5, "no segment may be delivered after the cancel"
    assert time.monotonic() - t0 < 25, "the child must be killed, not run to the end"


def test_kill_all_ends_a_running_worker(fake_impl):
    """os._exit ends the app's threads but not the worker process: a Quit,
    Restart or Update mid-pass left it running on (review of PR 1086,
    2026-10-07). Every exit path now calls kill_all() first."""
    from ml import batch_worker
    texts = []
    errors = []

    def _run_slow():
        try:
            batch_worker.run_in_child("x.wav", {"mode": "slow"}, on_text=lambda *a: texts.append(a),
                                      on_fingerprint=None, on_progress=None)
        except Exception as e:   # the worker exits before finishing
            errors.append(e)

    t = threading.Thread(target=_run_slow, daemon=True)
    t.start()
    deadline = time.monotonic() + 20
    while len(texts) < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert texts, "the worker never started"
    assert batch_worker.kill_all() == 1
    t.join(20)
    assert not t.is_alive()
    assert errors and "before finishing" in str(errors[0])
    assert batch_worker.kill_all() == 0, "a finished worker leaves the registry"


def test_every_exit_path_ends_the_worker_before_its_rollback():
    from pathlib import Path
    import re
    app = (Path(ROOT) / "app.py").read_text(encoding="utf-8")
    for name in ("_force_quit", "restart", "update_apply"):
        body = re.search(rf"^def {name}\(.*?\n(.*?)(?=^def |\Z)", app, re.M | re.S).group(1)
        assert "_end_batch_workers()" in body, f"{name} leaves the worker running"
        assert body.index("_end_batch_workers()") < body.index("_rollback_reanalysis("), (
            f"{name}: end the worker first, so no rebuilt segment lands after the rollback")


def test_a_failed_child_probe_falls_back_to_checking_here(monkeypatch):
    """The probe's answer is cached for the life of the app, and "cpu" would put
    live Whisper on the processor for the whole session."""
    from core import gpu_probe
    monkeypatch.delenv("MA_GPU_PROBE_INPROCESS", raising=False)
    monkeypatch.setattr(gpu_probe, "_inprocess_allowed", lambda: False)

    def _broken_child():
        raise RuntimeError("probe exited 3221225477")

    monkeypatch.setattr(gpu_probe, "_probe_child", _broken_child)
    monkeypatch.setattr(gpu_probe, "_probe_inprocess",
                        lambda: {"ct2_cuda": True, "torch_device": "cuda"})
    gpu_probe._reset_for_tests()
    try:
        gpu_probe._run()
        assert gpu_probe.result(timeout=1) == {"ct2_cuda": True, "torch_device": "cuda"}
    finally:
        gpu_probe._reset_for_tests()


def test_fresh_child_can_import_pyannote_after_prepare():
    """Regression 2026-10-01: the first live run in a child died with
    "module 'torchaudio' has no attribute 'AudioMetaData'". The app process
    had always imported ml.diarizer (its torchaudio shims) before a
    reanalysis; a fresh child has to do it itself."""
    import importlib.util
    import subprocess
    # find_spec, not importorskip: importing pyannote here, unpatched, is
    # exactly the failure under test.
    if importlib.util.find_spec("pyannote") is None:
        pytest.skip("pyannote not installed")
    code = ("from ml.batch_worker import prepare_pipeline_env; prepare_pipeline_env(); "
            "from pyannote.audio import Pipeline; print('ok')")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                       text=True, timeout=300)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-800:]
