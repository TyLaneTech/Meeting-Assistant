"""Compute device selection: single source of truth for CUDA/MPS/CPU choice.

Used by diarizer, transcriber, batch transcriber, speaker_db, and the app
settings layer so every component agrees on which torch device to target.

The availability answer comes from core.gpu_probe, which asks a short-lived
child process, so calling best_torch_device() never initializes CUDA in the
long-lived app process.
"""
from __future__ import annotations


def best_torch_device() -> str:
    """Return the best available torch device string: 'cuda', 'mps', or 'cpu'.

    Cached for the process lifetime by core.gpu_probe; the answer never
    changes after startup.
    """
    from core import gpu_probe
    return gpu_probe.torch_device()


def is_gpu_device(device: str) -> bool:
    """True for any non-CPU accelerator device string."""
    return device in ("cuda", "mps")


def empty_cache(device: str) -> None:
    """Best-effort cache flush for the given torch device. No-op on CPU.

    CUDA is flushed only when this process already initialized it: asking
    torch.cuda.is_available() would initialize the driver in a process that
    never used the card."""
    try:
        import torch
    except ImportError:
        return

    if device == "cuda":
        if torch.cuda.is_initialized():
            torch.cuda.empty_cache()
    elif device == "mps":
        # torch.mps.empty_cache() exists on PyTorch >= 2.0 with MPS support.
        mps_ns = getattr(torch, "mps", None)
        if mps_ns is not None and hasattr(mps_ns, "empty_cache"):
            try:
                mps_ns.empty_cache()
            except Exception:
                pass


def plan_batch_devices(reanalysis_device: str, diarizer_device: str,
                       gpu_device: str, on_ac_power: bool,
                       automatic: bool) -> dict:
    """Pick the devices for one batch reanalysis (diarization + Whisper).

    reanalysis_device: the Reanalysis "Device" setting ("auto", "cuda",
        "mps" or "cpu"); it picks the Whisper device.
    diarizer_device: the Diarizer device setting ("" = follow the Whisper
        device, "cpu", or "cuda" meaning "the GPU", which is MPS on a Mac).
    gpu_device: the best device this machine has ("cuda", "mps" or "cpu").
    automatic: True for the post-meeting pass, False for a reanalysis the
        user started.

    On battery, an automatic pass (the opt-in after-meeting transcription)
    that would use CUDA waits for the charger (wait_for_charger=True, devices
    unchanged so it runs on the GPU once the charger is back). A reanalysis the
    user starts is not touched by the power source: it runs on the device the
    settings pick, as it always has. Moving it to the CPU on battery made an
    hour-long meeting take most of an hour, with Record locked out the whole
    time, and overrode an explicit GPU choice. battery_cpu stays in the
    result for callers that log it and is always False now.

    Returns {"whisper", "diarizer", "wait_for_charger", "battery_cpu"}.
    """
    accel = gpu_device if gpu_device in ("cuda", "mps") else None
    pref = (reanalysis_device or "auto").lower()
    if pref == "cpu":
        whisper = "cpu"
    elif pref in ("cuda", "mps"):
        # A requested accelerator this machine lacks falls back to the best
        # one it has, as the batch pipeline always did.
        whisper = pref if pref == accel else (accel or "cpu")
    else:
        whisper = accel or "cpu"

    dpref = (diarizer_device or "").lower()
    if dpref == "cpu":
        diarizer = "cpu"
    elif dpref in ("cuda", "mps"):
        diarizer = accel or "cpu"
    else:
        diarizer = whisper

    uses_cuda = "cuda" in (whisper, diarizer)
    wait = automatic and uses_cuda and not on_ac_power
    return {"whisper": whisper, "diarizer": diarizer,
            "wait_for_charger": wait, "battery_cpu": False}
