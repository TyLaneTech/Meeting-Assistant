"""
Online speaker diarization using diart's incremental pipeline.

DiartDiarizer buffers audio in a 5-second rolling window and processes it in
0.5-second steps, returning only the latest step's annotation each call to
avoid duplicate segments across calls.  Labels are consistent session-wide:
"Speaker 1", "Speaker 2", etc.
"""
import time
import traceback
import warnings

import log
import numpy as np
import torch
import torchaudio

# torchaudio 2.x removed several symbols that older pyannote.audio references
# at import time.  Shim them before pyannote is imported — we never use
# torchaudio for file I/O so these stubs are never actually called.
if not hasattr(torchaudio, "list_audio_backends"):
    torchaudio.list_audio_backends = lambda: ["soundfile"]
if not hasattr(torchaudio, "set_audio_backend"):
    torchaudio.set_audio_backend = lambda backend: None   # no-op; backend selection removed in 2.x
if not hasattr(torchaudio, "AudioMetaData"):
    import collections
    torchaudio.AudioMetaData = collections.namedtuple(
        "AudioMetaData",
        ["sample_rate", "num_frames", "num_channels", "bits_per_sample", "encoding"],
    )

# huggingface_hub 1.x renamed 'use_auth_token' → 'token'.  pyannote.audio 3.x
# and diart still pass the old name internally.  Shim hf_hub_download to accept
# both so we don't need to patch third-party source files.
def _compat_hf_hub_download(*args, use_auth_token=None, token=None, **kwargs):
    """Accept legacy use_auth_token kwarg; treat '' as None to avoid illegal header."""
    effective = token or use_auth_token or None
    return _orig_hf_hub_download(*args, token=effective, **kwargs)

try:
    import huggingface_hub as _hfh
    import inspect as _inspect
    _orig_hf_hub_download = _hfh.hf_hub_download
    if "use_auth_token" not in _inspect.signature(_orig_hf_hub_download).parameters:
        _hfh.hf_hub_download = _compat_hf_hub_download
except Exception:
    _orig_hf_hub_download = None  # Shim will be a no-op if called

# PyTorch 2.6 changed torch.load's default to weights_only=True, which blocks
# pyannote/lightning checkpoints that embed custom Python objects (TorchVersion,
# Specifications, Problem, etc.). Enumerating all safe globals is brittle, so we
# patch torch.load itself to use weights_only=False for these trusted local files.
try:
    import torch as _torch
    _orig_torch_load = _torch.load

    def _patched_torch_load(f, map_location=None, pickle_module=None,
                             weights_only=None, mmap=None, **kwargs):
        # Pass weights_only=False unless the caller explicitly sets it to True
        effective = False if weights_only is None else weights_only
        kw = dict(kwargs)
        if map_location is not None:
            kw["map_location"] = map_location
        if pickle_module is not None:
            kw["pickle_module"] = pickle_module
        if mmap is not None:
            kw["mmap"] = mmap
        return _orig_torch_load(f, weights_only=effective, **kw)

    _torch.load = _patched_torch_load
except Exception:
    pass

# Suppress the long torchcodec warning — irrelevant since we always pass
# pre-loaded waveform tensors, never file paths.
warnings.filterwarnings(
    "ignore",
    message="torchaudio._backend.set_audio_backend has been deprecated",
    category=UserWarning,
)
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=UserWarning, module="pyannote")
    from pyannote.audio import Pipeline

# Patch the hf_hub_download reference that pyannote.audio already bound at
# import time — the global shim above only covers future callers.
try:
    import pyannote.audio.core.model as _pa_model
    if hasattr(_pa_model, "hf_hub_download"):
        _pa_model.hf_hub_download = _compat_hf_hub_download
    import pyannote.audio.core.io as _pa_io
    if hasattr(_pa_io, "hf_hub_download"):
        _pa_io.hf_hub_download = _compat_hf_hub_download
    import pyannote.audio.pipelines.utils.hook as _pa_hook
    if hasattr(_pa_hook, "hf_hub_download"):
        _pa_hook.hf_hub_download = _compat_hf_hub_download
except Exception:
    pass

# Suppress benign PyTorch std() warning from pyannote's pooling layer.
# Fires at recording start when the audio buffer has only 1 time step and
# std(correction=1) has 0 degrees of freedom. Pyannote recovers gracefully.
warnings.filterwarnings(
    "ignore",
    message="std\\(\\): degrees of freedom is <= 0",
    category=UserWarning,
)
warnings.filterwarnings(
    "ignore",
    message="Module 'speechbrain\\.",
    category=UserWarning,
)
warnings.filterwarnings(
    "ignore",
    message="Mismatch between frames",
    category=UserWarning,
    module="pyannote",
)

# ── Speechbrain lazy-module resilience ───────────────────────────────────────
# Newer speechbrain versions (>=1.1) use LazyModule for optional integrations
# (k2_fsa, huggingface, Kaldi, etc.).  If the optional dependency isn't
# installed the lazy import raises an opaque ImportError that kills the entire
# diarizer init — often triggered indirectly by inspect.stack() inside
# pytorch_lightning.
#
# Strategy: install a custom meta-path finder that intercepts ANY import under
# "speechbrain.integrations" (and the legacy "speechbrain.k2_integration") and
# returns an empty stub module.  This is future-proof — new sub-packages added
# by SpeechBrain updates won't require manual additions here.
import sys as _sys
import types as _types
import importlib.abc as _importlib_abc
import importlib.machinery as _importlib_machinery


class _SpeechBrainIntegrationStubFinder(_importlib_abc.MetaPathFinder):
    """Auto-stub any missing speechbrain.integrations.* submodule."""

    _PREFIXES = ("speechbrain.integrations", "speechbrain.k2_integration")

    def find_module(self, fullname, path=None):
        # find_module is the legacy protocol but still honoured; keep it for
        # broad Python 3.x compat alongside find_spec.
        if any(fullname == p or fullname.startswith(p + ".") for p in self._PREFIXES):
            if fullname not in _sys.modules:
                return self
        return None

    def load_module(self, fullname):
        if fullname in _sys.modules:
            return _sys.modules[fullname]
        mod = _types.ModuleType(fullname)
        mod.__path__ = []
        mod.__package__ = fullname
        mod.__loader__ = self
        _sys.modules[fullname] = mod
        return mod

    def find_spec(self, fullname, path, target=None):
        if any(fullname == p or fullname.startswith(p + ".") for p in self._PREFIXES):
            if fullname not in _sys.modules:
                return _importlib_machinery.ModuleSpec(fullname, self)
        return None

    def create_module(self, spec):
        return None  # use default semantics

    def exec_module(self, module):
        module.__path__ = []
        module.__package__ = module.__name__


# Install early, before any pyannote / diart import triggers speechbrain lazy loads
_sys.meta_path.insert(0, _SpeechBrainIntegrationStubFinder())

def _merge_turns(
    turns: list[tuple[str, float, float]],
    merge_gap: float = 0.1,
) -> list[tuple[str, float, float]]:
    """Merge consecutive same-speaker turns with small gaps between them."""
    if not turns:
        return []
    merged: list[tuple[str, float, float]] = [turns[0]]
    for label, start, end in turns[1:]:
        prev_label, prev_start, prev_end = merged[-1]
        if label == prev_label and (start - prev_end) <= merge_gap:
            merged[-1] = (prev_label, prev_start, end)
        else:
            merged.append((label, start, end))
    return merged



# ── DiartDiarizer ─────────────────────────────────────────────────────────────

class DiartDiarizer:
    """
    Online speaker diarization using diart's incremental pipeline.

    Buffers audio internally and feeds 5-second windows to the diart pipeline
    in 500ms steps.  Each step advances the clustering state by one step and
    returns only the annotation for the newest 500ms slice, avoiding duplicate
    segments across calls.

    Measured latency: ~50 ms/step on GPU, ~165 ms/step on CPU
    (vs 1–3 s / 15–30 s for batch pyannote on the same hardware).

    The public interface is identical to StreamingDiarizer so Transcriber
    can use either class without changes to the calling code.
    """

    SAMPLE_RATE = 16_000

    def __init__(self, hf_token: str, device: str | None = None) -> None:
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self._device_name = device
        self._dev = torch.device(device)
        log.info("diarizer", f"Loading diart pipeline on {self._dev}…")

        try:
            from diart import SpeakerDiarization, SpeakerDiarizationConfig
            from diart.models import SegmentationModel, EmbeddingModel
        except Exception as e:
            raise RuntimeError(
                f"Failed to import diart — check that diart, pyannote.audio, and "
                f"speechbrain are installed and compatible: {e}"
            ) from e

        # ── Neutralise speechbrain LazyModules already in sys.modules ────────
        # SpeechBrain >=1.1 injects LazyModule objects into sys.modules for
        # optional integrations AND deprecated redirect paths (wordemb,
        # lobes.models.*, nnet.loss.*, etc.).  inspect.stack() (called by
        # pytorch_lightning) iterates sys.modules.values() and calls
        # hasattr(mod, '__file__') on each, triggering LazyModule.__getattr__
        # → ensure_module() → ImportError for optional deps.
        # Replace ALL speechbrain LazyModules with inert stubs.
        try:
            from speechbrain.utils.importutils import LazyModule as _LM
            for _key in list(_sys.modules):
                if _key.startswith("speechbrain."):
                    _mod = _sys.modules[_key]
                    if isinstance(_mod, _LM):
                        _stub = _types.ModuleType(_key)
                        _stub.__path__ = []
                        _stub.__package__ = _key
                        _sys.modules[_key] = _stub
        except ImportError:
            pass  # speechbrain too old to have LazyModule — nothing to fix

        # Load saved audio params
        from default_audio_params import DIARIZATION_DEFAULTS
        import settings as _settings
        saved = _settings.load().get("audio_params", {})
        p = {}
        for key, spec in DIARIZATION_DEFAULTS.items():
            p[key] = saved.get(key, spec["value"])

        self._step_seconds     = float(p["step_seconds"])
        self._duration_seconds = float(p["duration_seconds"])
        self._merge_gap        = float(p["merge_gap_seconds"])

        # Load the same underlying models pyannote/speaker-diarization-3.1 uses.
        from network import ensure_warp_disconnected
        ensure_warp_disconnected()
        log.info("diarizer", "Loading segmentation model…")
        try:
            seg = SegmentationModel.from_pretrained(
                "pyannote/segmentation-3.0", use_hf_token=hf_token
            )
        except Exception as e:
            raise RuntimeError(
                f"Failed to load segmentation model (pyannote/segmentation-3.0). "
                f"Check your HuggingFace token and network: {e}"
            ) from e

        log.info("diarizer", "Loading embedding model…")
        try:
            emb = EmbeddingModel.from_pretrained(
                "pyannote/wespeaker-voxceleb-resnet34-LM", use_hf_token=hf_token
            )
        except Exception as e:
            raise RuntimeError(
                f"Failed to load embedding model (wespeaker-voxceleb-resnet34-LM). "
                f"Check your HuggingFace token and network: {e}"
            ) from e

        self._config = SpeakerDiarizationConfig(
            segmentation=seg,
            embedding=emb,
            duration=self._duration_seconds,
            step=self._step_seconds,
            latency=self._step_seconds,   # match step for minimal output delay
            tau_active=float(p["tau_active"]),
            rho_update=float(p["rho_update"]),
            delta_new=float(p["delta_new"]),
            device=self._dev,
        )
        self._pipeline = SpeakerDiarization(self._config)

        # Internal rolling buffer and stream-position tracking
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start_sec = 0.0   # absolute time of buf[0] in the recording
        self._total_fed_sec = 0.0   # cumulative seconds received by process()

        # Map diart's internal speaker IDs → session-wide "Speaker N" labels
        self._speaker_map: dict[str, str] = {}
        self._next_label = 1
        log.info("diarizer", f"diart ready on {self._dev}.")

    # ── Public ────────────────────────────────────────────────────────────────

    def process(
        self,
        audio: np.ndarray,
        new_from_samples: int = 0,  # accepted for interface compatibility
    ) -> list[tuple[str, float, float]]:
        """
        Feed one audio chunk to the diart online pipeline.

        Audio is appended to an internal rolling buffer.  Once the buffer
        reaches _DURATION_SECONDS, the pipeline is called for each 500ms step
        that advances the window.  Only the last 500ms of each window's
        annotation is kept, preventing duplicate segments across calls.

        Returns sorted [(speaker_label, start_sec, end_sec)] with timestamps
        relative to the start of `audio`.  Returns [] until at least
        _DURATION_SECONDS of audio has been accumulated.
        """
        from pyannote.core import SlidingWindowFeature, SlidingWindow

        if len(audio) < int(self.SAMPLE_RATE * 0.1):
            return []

        # Track where the new audio starts in absolute recording time
        new_audio_abs_sec = self._total_fed_sec
        self._total_fed_sec += len(audio) / self.SAMPLE_RATE
        audio_duration_sec = len(audio) / self.SAMPLE_RATE

        # Append new audio to the rolling buffer
        self._buf = np.concatenate([self._buf, audio])

        duration_samples = int(self._duration_seconds * self.SAMPLE_RATE)
        step_samples     = int(self._step_seconds     * self.SAMPLE_RATE)

        all_segs: list[tuple[str, float, float]] = []

        while len(self._buf) >= duration_samples:
            chunk = self._buf[:duration_samples]

            # Create SlidingWindowFeature with correct absolute timestamps.
            # data shape: (samples, 1) — mono, channel-last as diart expects.
            sw = SlidingWindow(
                start=self._buf_start_sec,
                duration=1.0 / self.SAMPLE_RATE,
                step=1.0 / self.SAMPLE_RATE,
            )
            waveform = SlidingWindowFeature(chunk[:, np.newaxis], sw)

            annotation = self._run_step(waveform)

            if annotation is not None:
                # Only use the annotation from the latest step slice to avoid
                # re-emitting speech that was returned in a prior step.
                step_end_abs   = self._buf_start_sec + self._duration_seconds
                step_start_abs = step_end_abs - self._step_seconds

                for segment, _, raw_label in annotation.itertracks(yield_label=True):
                    # Clip segment to the latest step window
                    seg_start = max(segment.start, step_start_abs)
                    seg_end   = min(segment.end,   step_end_abs)
                    if seg_end <= seg_start or (seg_end - seg_start) < 0.05:
                        continue
                    # Only emit speech that falls within the current audio chunk
                    if seg_end <= new_audio_abs_sec:
                        continue

                    speaker = self._resolve(str(raw_label))
                    # Convert absolute times → relative to start of `audio`
                    rel_start = max(0.0, seg_start - new_audio_abs_sec)
                    rel_end   = min(audio_duration_sec, seg_end - new_audio_abs_sec)
                    if rel_end > rel_start:
                        all_segs.append((speaker, rel_start, rel_end))

            # Advance the buffer by one step
            self._buf = self._buf[step_samples:]
            self._buf_start_sec += self._step_seconds

        all_segs.sort(key=lambda x: x[1])

        # Remove overlapping segments (diart can assign two speakers to the same
        # time window at transitions).  First speaker (by start time) wins.
        deduped: list[tuple[str, float, float]] = []
        for label, start, end in all_segs:
            if deduped and start < deduped[-1][2]:
                start = deduped[-1][2]   # trim to after previous segment
            if end - start >= 0.05:
                deduped.append((label, start, end))
        all_segs = deduped

        merged = _merge_turns(all_segs, self._merge_gap)

        return merged

    def reset(self, next_label: int = 1) -> None:
        """Clear all state for a new recording session.

        Args:
            next_label: Starting number for new speaker labels.  On resume,
                        pass max-existing + 1 so new speakers don't collide
                        with labels from previous recording segments.
        """
        from diart import SpeakerDiarization
        self._pipeline = SpeakerDiarization(self._config)
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start_sec = 0.0
        self._total_fed_sec = 0.0
        self._speaker_map.clear()
        self._next_label = next_label

    def apply_params(self, params: dict) -> None:
        """Update runtime-tunable diarization parameters from settings.

        Note: step_seconds, duration_seconds, tau_active, rho_update, and
        delta_new are baked into the pipeline config at init time.  Changing
        them requires a diarizer reload (next session start).  merge_gap is
        applied immediately.
        """
        self._merge_gap = float(params.get("merge_gap_seconds", self._merge_gap))

    def merge_speakers(self, keep_label: str, merge_label: str) -> None:
        """Redirect all internal IDs mapped to merge_label → keep_label."""
        for k, v in list(self._speaker_map.items()):
            if v == merge_label:
                self._speaker_map[k] = keep_label

    # ── Private ───────────────────────────────────────────────────────────────

    def _run_step(self, waveform):
        """Feed one duration-sized SlidingWindowFeature to the pipeline."""
        try:
            results = self._pipeline([waveform])
            annotation, _ = results[0]
            return annotation
        except Exception:
            traceback.print_exc()
            return None

    def _resolve(self, raw_label: str) -> str:
        """Map diart's internal speaker key to a stable 'Speaker N' string."""
        if raw_label not in self._speaker_map:
            self._speaker_map[raw_label] = f"Speaker {self._next_label}"
            self._next_label += 1
        return self._speaker_map[raw_label]
