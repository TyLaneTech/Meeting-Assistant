"""
Real-time transcription using faster-whisper.
Auto-detects CUDA (RTX 4080) and uses large-v3 + float16 if available,
falling back to small + int8 on CPU.

When a DiartDiarizer is attached (via load_diarizer), each audio chunk
is first split into per-speaker segments by diart, then each segment is
transcribed individually and emitted as ("Speaker N", text).
Without a diarizer the audio is transcribed as a single chunk using
Whisper's built-in VAD.
"""
import os
import queue
import sys
import threading
import traceback
from typing import Callable
from typing import Any

import wave

# ── Windows: register nvidia pip-package DLL directories ──────────────────
# nvidia-cublas-cu12 / nvidia-cudnn-cu12 etc. install their DLLs under
# site-packages/nvidia/*/bin/ but don't add that to PATH or the Windows DLL
# search path. os.add_dll_directory() fixes this before ctranslate2 or torch
# try to load cublas / cudnn.
if sys.platform == "win32":
    import glob as _glob
    import site
    try:
        for _sp in site.getsitepackages():
            for _d in _glob.glob(os.path.join(_sp, "nvidia", "*", "bin")):
                if os.path.isdir(_d):
                    os.add_dll_directory(_d)
    except Exception:
        pass

from core import log as log
import numpy as np
from scipy import signal as scipy_signal


def detect_cuda_available() -> bool:
    """Check whether CUDA is actually usable for ctranslate2 (Whisper)."""
    try:
        import ctranslate2
        types = ctranslate2.get_supported_compute_types("cuda")
        if types and ctranslate2.get_cuda_device_count() > 0:
            import ctypes, sys
            for ver in ("12", "13", "11"):
                try:
                    lib = f"cublas64_{ver}.dll" if sys.platform == "win32" else f"libcublas.so.{ver}"
                    ctypes.CDLL(lib)
                    return True
                except OSError:
                    continue
    except Exception:
        pass
    return False


def detect_device() -> tuple[str, str, str]:
    """Returns (device, compute_type, model_size). Best for current platform."""
    if sys.platform == "darwin":
        # Apple Silicon: mlx-whisper drives Metal directly. Compute-type is
        # an mlx-whisper concept (fp16) and the device string is purely
        # informational since mlx selects Metal automatically.
        log.info("whisper", "Using mlx-whisper on Metal - large-v3.")
        return "mlx", "fp16", "large-v3"
    if detect_cuda_available():
        log.info("whisper", "CUDA OK - using large-v3 (float16).")
        return "cuda", "float16", "large-v3"
    log.info("whisper", "CUDA unavailable - using CPU.")
    return "cpu", "int8", "small"


# Whisper presets shown in the settings UI dropdown. The "requires_gpu" key
# replaces the old "requires_cuda" so the UI gates correctly on Mac too —
# but we keep "requires_cuda" as an alias for backward compat with app.py.
def _preset(*, id: str, label: str, device: str, compute_type: str,
            model_size: str, requires_gpu: bool, platforms: tuple[str, ...]) -> dict:
    return {
        "id": id, "label": label, "device": device,
        "compute_type": compute_type, "model_size": model_size,
        "requires_cuda": requires_gpu and "cuda" in (device,),  # legacy alias
        "requires_gpu": requires_gpu,
        "platforms": platforms,
    }


# All presets across platforms; app.py filters by sys.platform via the
# `platforms` tuple so each OS only sees relevant choices.
_ALL_WHISPER_PRESETS = [
    # Windows / Linux — faster-whisper / CTranslate2
    _preset(id="cuda-large-v3", label="GPU - large-v3 (float16)",       device="cuda", compute_type="float16", model_size="large-v3",       requires_gpu=True,  platforms=("win32", "linux")),
    _preset(id="cuda-turbo",    label="GPU - large-v3-turbo (float16)", device="cuda", compute_type="float16", model_size="large-v3-turbo", requires_gpu=True,  platforms=("win32", "linux")),
    _preset(id="cuda-medium",   label="GPU - medium (float16)",         device="cuda", compute_type="float16", model_size="medium",         requires_gpu=True,  platforms=("win32", "linux")),
    _preset(id="cuda-small",    label="GPU - small (float16)",          device="cuda", compute_type="float16", model_size="small",          requires_gpu=True,  platforms=("win32", "linux")),
    _preset(id="cpu-medium",    label="CPU - medium (int8)",            device="cpu",  compute_type="int8",    model_size="medium",         requires_gpu=False, platforms=("win32", "linux")),
    _preset(id="cpu-small",     label="CPU - small (int8)",             device="cpu",  compute_type="int8",    model_size="small",          requires_gpu=False, platforms=("win32", "linux")),
    _preset(id="cpu-tiny",      label="CPU - tiny (int8)",              device="cpu",  compute_type="int8",    model_size="tiny",           requires_gpu=False, platforms=("win32", "linux")),
    # macOS — mlx-whisper / Metal
    _preset(id="mlx-large-v3",       label="Metal - large-v3 (fp16)",       device="mlx", compute_type="fp16", model_size="large-v3",       requires_gpu=False, platforms=("darwin",)),
    _preset(id="mlx-large-v3-turbo", label="Metal - large-v3-turbo (fp16)", device="mlx", compute_type="fp16", model_size="large-v3-turbo", requires_gpu=False, platforms=("darwin",)),
    _preset(id="mlx-medium",         label="Metal - medium (fp16)",         device="mlx", compute_type="fp16", model_size="medium",         requires_gpu=False, platforms=("darwin",)),
    _preset(id="mlx-small",          label="Metal - small (fp16)",          device="mlx", compute_type="fp16", model_size="small",          requires_gpu=False, platforms=("darwin",)),
    _preset(id="mlx-tiny",           label="Metal - tiny (fp16)",           device="mlx", compute_type="fp16", model_size="tiny",           requires_gpu=False, platforms=("darwin",)),
]

WHISPER_PRESETS = [p for p in _ALL_WHISPER_PRESETS if sys.platform in p["platforms"]]

# Diarizer device options. IDs ("cuda" / "cpu") are kept for backward
# compatibility with saved user settings. On macOS the diarizer translates
# "cuda" → "mps" automatically (see diarizer.py).
DIARIZER_OPTIONS = [
    {"id": "cuda", "label": "GPU", "requires_cuda": False, "requires_gpu": True},
    {"id": "cpu",  "label": "CPU", "requires_cuda": False, "requires_gpu": False},
]

_RUNTIME_LOCK = threading.Lock()
_CUDA_AVAILABLE: bool | None = None
_DEFAULT_DEVICE = "cpu"
_DEFAULT_COMPUTE_TYPE = "int8"
_DEFAULT_MODEL_SIZE = "small"


def get_cuda_available() -> bool:
    """Return whether CUDA is usable for Whisper, probing once lazily."""
    global _CUDA_AVAILABLE
    if _CUDA_AVAILABLE is None:
        with _RUNTIME_LOCK:
            if _CUDA_AVAILABLE is None:
                _CUDA_AVAILABLE = detect_cuda_available()
    return _CUDA_AVAILABLE


def get_default_model_config() -> tuple[str, str, str]:
    """Return the preferred default Whisper config, probing once lazily."""
    if sys.platform == "darwin":
        # Apple Silicon: mlx-whisper drives Metal directly. Mirror
        # detect_device() so the streaming engine defaults to large-v3, the
        # model the launcher pre-caches — without this darwin branch the
        # function falls through to the CPU default ("small"), which on macOS
        # maps to whisper-small-mlx (never pre-downloaded) and fails to load
        # under HF_HUB_OFFLINE, so live transcription produces no segments.
        log.info("whisper", "Using mlx-whisper on Metal - large-v3.")
        return "mlx", "fp16", "large-v3"
    if get_cuda_available():
        log.info("whisper", "CUDA OK - using large-v3 (float16).")
        return "cuda", "float16", "large-v3"
    log.info("whisper", "CUDA unavailable - using CPU.")
    return _DEFAULT_DEVICE, _DEFAULT_COMPUTE_TYPE, _DEFAULT_MODEL_SIZE

# Minimum samples to pass to Whisper - very short clips produce garbage output.
# Raised from 0.2s to 0.5s: diarized segments shorter than this yield
# unreliable speaker embeddings and single-word Whisper hallucinations.
_MIN_WHISPER_SAMPLES = 8_000   # 0.5 s at 16 kHz

# Short-fragment threshold: Whisper adds a trailing period to tiny audio clips
# (e.g. a single word from diarization).  Outputs with this few words or fewer
# are considered fragments and have their trailing period stripped.
_FRAGMENT_MAX_WORDS = 2

# Repetition / hallucination-loop detection.
# Whisper can get stuck repeating a short phrase when conditioned on a
# contaminated context (common with noisy mic input).  We measure the ratio of
# unique N-grams to total N-grams; a low ratio means the text is a loop.
_HALLUCINATION_NGRAM      = 4     # n-gram size for repetition check
_HALLUCINATION_THRESHOLD  = 0.50  # unique-ratio below this → treat as loop

# Known Whisper hallucination phrases.  These are artifacts from the training
# data (subtitles, YouTube outros, etc.) that Whisper hallucinates when the
# audio is silent or contains only background noise.  Checked against the
# lowercased text - if ANY phrase appears as a substring, the segment is
# discarded.  Also applied as a strip: if the phrase appears at the start or
# end, it's removed and the remainder is kept (if any real speech remains).
import re as _re

_HALLUCINATION_PHRASES = [
    "subtitles by",
    "subtitle workshop",
    "amara.org",
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "like and subscribe",
    "subscribe to",
    "click the bell",
    "hit the notification",
    "www.mooji.org",
    "subs by",
    "subtitling by",
    "captions by",
    "transcription by",
    "translated by",
    "captioned by",
    "closed captioning",
    "captioning by",
    "captioning sponsored by",
]

# Pre-compile a single regex for efficiency
_HALLUCINATION_RE = _re.compile(
    "|".join(_re.escape(p) for p in _HALLUCINATION_PHRASES),
    _re.IGNORECASE,
)


def _clean_hallucinations(text: str) -> str:
    """Remove known Whisper hallucination phrases from text.

    If the entire text is a hallucination, returns ''.
    If real speech is mixed with hallucination artifacts, strips the artifacts.
    """
    cleaned = _HALLUCINATION_RE.sub("", text).strip()
    # If stripping left only punctuation/whitespace or very short residue, discard
    stripped = _re.sub(r"[^\w]", "", cleaned)
    if len(stripped) < 3:
        return ""
    return cleaned


def _dedup_sentences(text: str) -> str:
    """Remove repeated sentences/clauses from Whisper output.

    Whisper sometimes loops a phrase like "SO I THINK THAT'S A GOOD QUESTION.
    QUESTION. SO I THINK THAT'S A GOOD QUESTION." - the n-gram filter may not
    catch this if there's enough variation.  This splits on sentence boundaries,
    normalises each, and keeps only the first occurrence.
    """
    # Split on sentence-ending punctuation, keeping the delimiter
    parts = _re.split(r'(?<=[.!?])\s+', text)
    if len(parts) <= 1:
        return text

    seen: set[str] = set()
    kept: list[str] = []
    for part in parts:
        # Normalise: lowercase, strip punctuation, collapse whitespace
        key = _re.sub(r'[^\w\s]', '', part.lower()).strip()
        key = _re.sub(r'\s+', ' ', key)
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        kept.append(part)

    result = " ".join(kept).strip()
    # If we removed more than half the sentences, the whole thing is suspect
    if len(kept) < len(parts) * 0.4:
        return ""
    return result


def _clean_ellipses(text: str) -> str:
    """Collapse Whisper's per-word ellipsis artifacts into normal text.

    Whisper sometimes outputs "Word... Next... Word..." when it's uncertain.
    This strips trailing ellipses from each word and re-joins, preserving a
    single trailing ellipsis if the original text ended with one (to indicate
    an incomplete thought).
    """
    if "..." not in text:
        return text
    trailing = text.rstrip().endswith("...")
    cleaned = _re.sub(r'\.{2,}', ' ', text)
    cleaned = _re.sub(r'\s{2,}', ' ', cleaned).strip()
    if trailing and cleaned and not cleaned.endswith("..."):
        cleaned += "..."
    return cleaned


def _strip_repeated_char_runs(text: str) -> str:
    """Strip 'stuck character' Whisper hallucinations: long runs of a single
    repeated letter (e.g. 'ŠŠŠŠŠ…'), which Whisper emits over near-silent or
    noisy audio. Digit runs (large numbers) and punctuation runs ('...', '!!!')
    are deliberately left alone."""
    cleaned = _re.sub(r"([^\W\d_])\1{4,}", " ", text)   # 5+ of the same letter
    cleaned = _re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


# Per-word-period collapse: a single Whisper call can return many tiny
# Segment objects (one per sentence-boundary timestamp token), each carrying
# its own trailing period.  Joined with spaces this produces output like
# "And. Uh. It. Would. Not. Move. Ahead.".  The polluted text then enters
# self._contexts[label] and is fed back as initial_prompt, so Whisper reinforces
# the pattern on every subsequent call for that speaker — explaining why the
# bug only manifests after a meeting has been running for a while.
#
# _repetition_ratio can't catch this (every word is unique), so we detect the
# pattern directly: a high density of capitalized alphabetic tokens that each
# end in a single period.  The capitalization check is what distinguishes the
# bug pattern (every word capitalized, since Whisper treats each as a fresh
# sentence) from legitimate prose (lowercase words ending sentences).
_PERIOD_RATIO_TRIGGER = 0.70   # fraction of tokens that must look like "Word." to trigger
_PERIOD_MIN_TOKENS    = 3      # texts shorter than this are left alone


def _is_short_period_token(token: str) -> bool:
    """True if *token* looks like a per-word artifact (e.g. 'Word.', 'Don\\'t.').

    The signal is that Whisper, when in the per-word-period failure mode,
    treats each word as a complete sentence — so every token starts with an
    uppercase letter and ends in a single period.  Legitimate sentence-ending
    words inside prose (e.g. "heavily.") are usually lowercase, which is what
    keeps this from matching ordinary text.
    """
    if not token.endswith(".") or token.endswith(".."):
        return False
    if len(token) < 2:
        return False
    body = token[:-1].replace("'", "")
    if not body.isalpha():
        return False
    return body[0].isupper()


def _collapse_word_periods(text: str) -> str:
    """Strip rogue per-word periods, preserving the final sentence terminator.

    When Whisper enters the per-word-period failure mode, most of its output
    tokens are short alphabetic words each ending in a period.  This function
    detects that pattern and removes the trailing period from every matching
    token except the last, so the displayed transcript reads as a normal
    sentence and — just as importantly — the cleaned text doesn't re-poison
    self._contexts[label] on the next flush.

    Also applied to the prompt before each Whisper call, so an already-
    poisoned context can't seed new bad output.

    Two passes:
      1. *Bulk* pollution — most of the text is the pattern (covers a fully
         poisoned Whisper output or an entire long-running prompt).
      2. *Tail* pollution — clean prefix followed by a run of ≥ N consecutive
         short-period tokens at the end (covers the prompt during the early
         stages of the snowball, before bulk dilution flips).
    """
    if "." not in text:
        return text
    tokens = text.split()
    if len(tokens) < _PERIOD_MIN_TOKENS:
        return text

    last = len(tokens) - 1

    def _strip_matches_in_range(start: int, end: int) -> str:
        """Strip period from short-period tokens in [start, end), keep last token intact."""
        cleaned = list(tokens)
        for i in range(start, end):
            if i < last and _is_short_period_token(cleaned[i]):
                cleaned[i] = cleaned[i][:-1]
        return " ".join(cleaned)

    # Pass 1: bulk pollution
    matches = sum(1 for t in tokens if _is_short_period_token(t))
    if matches / len(tokens) >= _PERIOD_RATIO_TRIGGER:
        return _strip_matches_in_range(0, len(tokens))

    # Pass 2: tail pollution — walk back from the end while we keep seeing
    # short-period tokens; if the run reaches _PERIOD_MIN_TOKENS, clean it.
    run_start = len(tokens)
    for i in range(len(tokens) - 1, -1, -1):
        if _is_short_period_token(tokens[i]):
            run_start = i
        else:
            break
    if len(tokens) - run_start >= _PERIOD_MIN_TOKENS:
        return _strip_matches_in_range(run_start, len(tokens))

    return text


def _strip_fragment_period(text: str) -> str:
    """Strip a trailing period from short fragments.

    Whisper treats every audio clip as a complete utterance and appends a period.
    For very short clips (typically one diarized word) this produces output like
    "word." which looks wrong when displayed.  This function removes the trailing
    period only when the text is short enough to be a fragment, preserving real
    sentence-ending punctuation on longer outputs.
    """
    if not text:
        return text
    words = text.split()
    if len(words) <= _FRAGMENT_MAX_WORDS and text.endswith("."):
        # Only strip a plain period - preserve "..." or "!." etc.
        if not text.endswith(".."):
            text = text[:-1].rstrip()
    return text


def _repetition_ratio(text: str, n: int = _HALLUCINATION_NGRAM) -> float:
    """Return the fraction of unique n-grams in *text* (1.0 = no repetition)."""
    words = text.lower().split()
    if len(words) < n * 2:
        return 1.0  # too short to judge reliably
    grams = [tuple(words[i : i + n]) for i in range(len(words) - n + 1)]
    return len(set(grams)) / len(grams)


class _StreamAccumulator:
    """Silence-gated audio buffer for one source stream.

    Factored out of Transcriber._loop so the mic-only and desktop-only streams
    (the "mic = Me" feature) can each run the identical accumulate/flush logic.
    Buffers raw int16 PCM chunks, tracks sample offsets for wall-clock timing,
    and calls ``on_flush(buffer, start_t, end_t)`` when speech is followed by a
    long-enough pause (or the max buffer length is hit). Buffers that contain no
    voice at all are dropped rather than transcribed.
    """

    def __init__(self, sample_rate, chunk_size, silence_threshold,
                 silence_chunk_thresh, min_buffer_chunks, max_buffer_chunks,
                 on_flush):
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.silence_threshold = silence_threshold
        self.silence_chunk_thresh = silence_chunk_thresh
        self.min_buffer_chunks = min_buffer_chunks
        self.max_buffer_chunks = max_buffer_chunks
        self.on_flush = on_flush
        self.buffer: list[bytes] = []
        self.silence_chunks = 0
        self.had_voice = False
        self.first_offset = -1
        self.last_offset = -1

    def feed(self, chunk: bytes, offset: int) -> None:
        self.buffer.append(chunk)
        if offset >= 0:
            if self.first_offset < 0:
                self.first_offset = offset
            self.last_offset = offset
        samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32_768.0
        rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0.0
        if rms < self.silence_threshold:
            self.silence_chunks += 1
        else:
            self.silence_chunks = 0
            self.had_voice = True

        enough_audio = len(self.buffer) >= self.min_buffer_chunks
        long_pause   = self.silence_chunks >= self.silence_chunk_thresh
        hit_max      = len(self.buffer) >= self.max_buffer_chunks
        if (enough_audio and long_pause) or hit_max:
            self.flush()

    def flush(self) -> None:
        if not self.buffer:
            return
        buf = self.buffer
        had_voice = self.had_voice
        if self.first_offset >= 0 and self.sample_rate:
            start_t = self.first_offset / self.sample_rate
            end_t   = (self.last_offset + self.chunk_size) / self.sample_rate
        else:
            start_t = end_t = 0.0
        # Reset before invoking the (blocking) flush callback.
        self.buffer = []
        self.silence_chunks = 0
        self.had_voice = False
        self.first_offset = -1
        self.last_offset = -1
        # Pure-silence buffers (common on the mic stream when the user is quiet)
        # are dropped — no point running Whisper over them.
        if had_voice:
            self.on_flush(buf, start_t, end_t)


class Transcriber:
    TARGET_RATE = 16_000
    CHUNK_SIZE = 512           # Must match AudioCapture.CHUNK_SIZE

    def __init__(
        self,
        audio_queue: queue.Queue,
        on_text_callback: Callable[[str, str, float, float], None],
    ):
        self.audio_queue = audio_queue
        self.on_text_callback = on_text_callback
        self.model: Any | None = None
        self.diarizer = None   # StreamingDiarizer | None, set via load_diarizer()
        self.is_running = False
        self._thread: threading.Thread | None = None
        self.sample_rate: int | None = None
        self.channels: int | None = None
        # Per-speaker prompt context (label -> recent transcript text), fed
        # back into Whisper as initial_prompt to maintain coherence across
        # segments. Kept per-label so one speaker's bug pattern (per-word
        # periods, hallucination loops) can't contaminate other speakers'
        # prompts. The non-diarized path uses the audio source ("loopback",
        # "mic") as the label, naturally giving a single shared bucket there.
        self._contexts: dict[str, str] = {}
        self.device = _DEFAULT_DEVICE
        self.compute_type = _DEFAULT_COMPUTE_TYPE
        self.model_size = _DEFAULT_MODEL_SIZE
        self._auto_model_config = True
        self.diarization_enabled = True  # Can be toggled via the UI
        # When set (the "mic = Me" feature is on and the capture delivers
        # per-source PCM), microphone audio is routed to a separate stream that
        # is never diarized and always labelled with this reserved speaker key.
        # See _loop_two_stream(). None -> legacy single mixed-stream behavior.
        self.me_label: str | None = None
        self.fingerprint_callback: Callable | None = None
        # (speaker_key: str, audio: np.ndarray, abs_start: float, abs_end: float) -> None
        self.on_diarizer_error: Callable[[str], None] | None = None
        # Called when the diarizer fails so the UI can surface the error.
        self._diarizer_error_fired = False  # only fire once per session

        # Tunable parameters — resolved against the active transcription preset.
        # When the user is on a non-custom preset, preset values win over any
        # stale per-key entries in audio_params, so updates to the preset
        # definitions in default_audio_params.py auto-propagate.
        from capture_audio.params import resolve_audio_params
        p = resolve_audio_params()
        self.silence_threshold   = float(p["silence_threshold"])
        self.silence_duration    = float(p["silence_duration"])
        self.min_buffer_seconds  = float(p["min_buffer_seconds"])
        self.max_buffer_seconds  = float(p["max_buffer_seconds"])
        self.beam_size           = int(p["beam_size"])
        self.prompt_chars        = int(p["prompt_chars"])
        self.vad_min_silence_ms  = int(p["vad_min_silence_ms"])
        self.vad_speech_pad_ms   = int(p["vad_speech_pad_ms"])
        self.compression_ratio_threshold = float(p["compression_ratio_threshold"])
        self.segment_break_silence = float(p.get("segment_break_silence", 1.5))
        # "Mic = Me" desktop-bleed rejection by VOICEPRINT (secondary, opt-in). A
        # microphone segment whose speaker embedding matches the concurrent desktop
        # (loopback) audio at least this closely (cosine similarity) is treated as
        # bleed and dropped. Disabled by default (0.0): with the built-in mic and
        # speakers the mic and loopback clocks drift, so the two never line up well
        # enough for the embedding to match reliably; the level gate below carries
        # the load instead. Set > 0 to re-enable as an extra check.
        self.mic_bleed_threshold = float(p.get("mic_bleed_threshold", 0.0))
        # "Mic = Me" desktop-bleed LEVEL gate (the primary, reliable mechanism on
        # macOS, where the mic and loopback clocks drift and an echo canceller
        # cannot lock on). When the desktop is playing, a microphone segment is
        # kept as the user only if its RAW level rises at least this far above the
        # learned bleed floor (coupling x desktop level); otherwise the whole
        # segment is dropped as desktop bleed. Higher = stricter (drops more,
        # needs you clearly louder than the desktop); lower = keeps more mic.
        self.mic_bleed_slack = float(p.get("mic_bleed_slack",
                                           p.get("bleed_duck_slack", 2.0)))

    @property
    def device_info(self) -> str:
        return f"{self.model_size} on {self.device} ({self.compute_type})"

    @property
    def whisper_preset_id(self) -> str:
        for p in WHISPER_PRESETS:
            if p["device"] == self.device and p["model_size"] == self.model_size:
                return p["id"]
        return f"{self.device}-{self.model_size}"

    @property
    def diarizer_device(self) -> str | None:
        if self.diarizer is None:
            return None
        return getattr(self.diarizer, "_device_name", None)

    def load_model(self) -> None:
        """Download (first run) and load Whisper. Blocking - run in a thread.

        Engine selection is automatic per platform: faster-whisper on
        Windows/Linux, mlx-whisper on macOS. See transcriber_engine.py.
        """
        if self._auto_model_config:
            self.device, self.compute_type, self.model_size = get_default_model_config()
        log.info("whisper", f"Loading {self.model_size} on {self.device} ({self.compute_type})…")
        from ml.transcriber_engine import make_engine
        try:
            self.model = make_engine(self.model_size, self.device, self.compute_type)
        except Exception as e:
            if not self._clear_bad_model_cache(str(e)):
                raise
            log.info("whisper", "Retrying after cache clear…")
            self.model = make_engine(self.model_size, self.device, self.compute_type)
        # Warm up the model - first inference is significantly slower due to
        # kernel compilation, weight loading, and memory allocation. The kwargs
        # set used here matches the steady-state call site below so all hot
        # codepaths get JIT'd / cached on this dummy run.
        try:
            _warmup = np.zeros(self.TARGET_RATE, dtype=np.float32)
            _segs, _info = self.model.transcribe(_warmup, language="en")
            list(_segs)
        except Exception:
            pass
        log.info("whisper", "Model ready.")

    def _clear_bad_model_cache(self, error_msg: str) -> bool:
        """Delete a corrupted HuggingFace model cache and return True if cleared."""
        import shutil
        if sys.platform == "darwin":
            # mlx-whisper pulls from MLX-quantized repos under mlx-community/.
            from ml.transcriber_engine import _MLX_MODEL_REPOS
            repo_id = _MLX_MODEL_REPOS.get(self.model_size, self.model_size)
        else:
            from faster_whisper.utils import _MODELS  # type: ignore[import-not-found]
            repo_id = _MODELS.get(self.model_size, self.model_size)
        # HF hub cache stores models under models--<org>--<name>
        cache_dir_name = "models--" + repo_id.replace("/", "--")
        hf_cache = os.path.join(
            os.environ.get("HF_HOME", os.path.join(os.path.expanduser("~"), ".cache", "huggingface")),
            "hub",
        )
        model_cache = os.path.join(hf_cache, cache_dir_name)
        if os.path.isdir(model_cache):
            log.info("whisper", f"Clearing corrupted model cache: {model_cache}")
            try:
                shutil.rmtree(model_cache)
                return True
            except OSError as rm_err:
                log.info("whisper", f"Failed to clear cache: {rm_err}")
        return False

    def reload_model(self, device: str, compute_type: str, model_size: str) -> None:
        """Reload Whisper with a different configuration. Blocking."""
        self.device = device
        self.compute_type = compute_type
        self.model_size = model_size
        self._auto_model_config = False
        self.model = None
        self.load_model()

    def load_diarizer(self, hf_token: str, device: str | None = None) -> None:
        """Load streaming diarization pipeline. Blocking - run in a thread."""
        from ml.diarizer import StreamingDiarizer
        self.diarizer = StreamingDiarizer(hf_token, device=device)
        # Mirror diarizer speaker merges into our per-speaker prompt contexts
        # so a merged speaker's prompt history follows their audio.
        self.diarizer.on_merge_speakers = self._merge_contexts

    def reload_diarizer(self, hf_token: str, device: str) -> None:
        """Reload the diarizer on a different device. Blocking."""
        self.diarizer = None
        self.load_diarizer(hf_token, device=device)

    def start(self, sample_rate: int, channels: int, next_speaker_label: int = 1) -> None:
        # Stop any previous loop that's still running (e.g. if the cleanup
        # thread from a prior stop_recording hasn't finished yet).
        if self._thread is not None and self._thread.is_alive():
            self.is_running = False
            self._thread.join(timeout=5)
            self._thread = None
        self.sample_rate = sample_rate
        self.channels = channels
        self._contexts.clear()
        self._diarizer_error_fired = False
        if self.diarizer is not None:
            self.diarizer.reset(next_label=next_speaker_label)
        self.is_running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        diar = "with diarization" if self.diarizer else "no diarization"
        log.info("transcriber", f"Started ({diar}, {sample_rate} Hz)")

    def stop(self) -> None:
        self.is_running = False
        if self._thread:
            self._thread.join(timeout=12)
            self._thread = None
        log.info("transcriber", "Stopped.")

    def unload(self) -> None:
        """Release the Whisper engine and diarizer to reclaim memory.

        The loaded models (plus the CUDA context behind them) hold gigabytes
        of commit charge for as long as the process lives, even when no
        meeting has run for hours. Idle unload drops them; `load_model()` /
        `load_diarizer()` bring them back on the next use. Refuses while a
        capture is running (a wrong unload destroys a live transcription;
        a missed one only keeps memory held). Idempotent.
        """
        if self.is_running:
            log.warn("transcriber", "unload() called while running; ignored")
            return
        had_models = self.model is not None or self.diarizer is not None
        self.model = None
        self.diarizer = None
        self._contexts.clear()
        if had_models:
            import gc
            gc.collect()
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            log.info("transcriber", "Models unloaded (idle); they reload on next use.")

    def process_wav_file(self, wav_path: str) -> None:
        """Transcribe a saved WAV file synchronously (blocking).

        Reads the file in MAX_BUFFER_SECONDS windows and runs the full
        diarization + Whisper pipeline for each window, firing on_text_callback
        exactly as the live loop does.  Intended to be called from a background
        worker thread (e.g. _run_reanalysis in app.py).
        """
        import math

        with wave.open(wav_path, "rb") as wf:
            n_channels   = wf.getnchannels()
            sample_width = wf.getsampwidth()   # bytes per sample per channel
            file_rate    = wf.getframerate()
            total_frames = wf.getnframes()

            # Configure transcriber state to match the file's audio format
            self.sample_rate = file_rate
            self.channels    = n_channels
            self._contexts.clear()
            if self.diarizer is not None:
                self.diarizer.reset()

            frames_per_window = int(self.max_buffer_seconds * file_rate)
            offset = 0

            log.info(
                "reanalysis",
                f"Processing {wav_path} "
                f"({total_frames / file_rate:.1f}s, {n_channels}ch, {file_rate}Hz)"
            )

            while offset < total_frames:
                n_read = min(frames_per_window, total_frames - offset)
                raw_window = wf.readframes(n_read)

                # Split the window into CHUNK_SIZE-frame byte chunks so that
                # _convert() receives the same format as the live audio path.
                bytes_per_frame = n_channels * sample_width
                chunk_bytes = self.CHUNK_SIZE * bytes_per_frame
                chunks: list[bytes] = []
                for i in range(0, len(raw_window), chunk_bytes):
                    chunk = raw_window[i : i + chunk_bytes]
                    if len(chunk) == chunk_bytes:
                        chunks.append(chunk)
                    # Drop the last partial chunk - too short for Whisper anyway.

                start_t = offset / file_rate
                end_t   = (offset + n_read) / file_rate

                if chunks:
                    self._transcribe(chunks, "reanalysis",
                                     start_time=start_t, end_time=end_t)

                offset += n_read

        log.info("reanalysis", "Complete.")

    # ── Private ───────────────────────────────────────────────────────────────

    def _convert(self, raw_chunks: list[bytes]) -> np.ndarray:
        raw = b"".join(raw_chunks)
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32_768.0
        if self.channels > 1:
            audio = audio.reshape(-1, self.channels).mean(axis=1)
        if self.sample_rate != self.TARGET_RATE:
            audio = scipy_signal.resample_poly(audio, self.TARGET_RATE, self.sample_rate)
        return audio

    def _mic_is_desktop_bleed(
        self, mic_buffer: list[bytes], start_t: float, end_t: float, desktop_ring,
    ) -> bool:
        """True if a "mic = Me" segment is actually desktop audio bleeding into
        the microphone (same voice as what was playing) rather than the app user.

        Compares the microphone segment's speaker embedding against the embedding
        of the desktop (loopback) audio from the SAME time window. When the mic is
        just picking up the speakers, both carry the same voiceprint, so a high
        cosine similarity means bleed. Deliberately conservative: returns False
        (keep the segment) whenever it cannot decide, so genuine user speech is
        never dropped on a hunch."""
        thr = self.mic_bleed_threshold
        diar = self.diarizer
        if thr <= 0.0 or diar is None or getattr(diar, "_emb_model", None) is None:
            return False
        try:
            mic_audio = self._convert(mic_buffer)
        except Exception:
            return False
        min_samples = int(0.6 * self.TARGET_RATE)
        if len(mic_audio) < min_samples or float(np.sqrt(np.mean(mic_audio ** 2))) < 0.005:
            return False
        # Concurrent desktop audio for [start_t, end_t], widened by 1 s on each
        # side. mic and desktop chunks share the mixer's sample offset, but the
        # two capture clocks drift, so the desktop that bled into the mic can sit
        # slightly earlier or later; the margin gives the comparison a stable,
        # same-speaker reference window.
        sr = self.sample_rate or 48000
        lo, hi = int((start_t - 1.0) * sr), int((end_t + 1.0) * sr)
        parts = [b for (off, b) in list(desktop_ring) if off >= 0 and lo <= off <= hi]
        if not parts:
            return False
        try:
            dsk_audio = self._convert(parts)
        except Exception:
            return False
        if len(dsk_audio) < min_samples or float(np.sqrt(np.mean(dsk_audio ** 2))) < 0.005:
            return False
        try:
            e_mic = diar._extract_embedding(mic_audio)
            e_dsk = diar._extract_embedding(dsk_audio)
        except Exception:
            return False
        if e_mic is None or e_dsk is None:
            return False
        sim = float(np.dot(e_mic, e_dsk))
        bleed = sim >= thr
        log.info(
            "transcriber",
            f"[me] bleed check sim={sim:.2f} thr={thr:.2f} -> "
            f"{'DROP (desktop bleed)' if bleed else 'keep'}",
        )
        return bleed

    def _mic_segment_is_bleed(self, start_t: float, end_t: float) -> bool:
        """True when a "mic = Me" segment is just the desktop bleeding into the
        microphone, judged on RAW levels over the WHOLE segment.

        This is the primary bleed gate on macOS, where the mic and loopback clocks
        drift and an echo canceller cannot stay locked. While the desktop plays,
        pure bleed sits at a roughly fixed coupling below the desktop's own level.
        We estimate that coupling live as a low percentile of the raw mic/desktop
        ratio across a trailing window (the quiet-mic moments are pure bleed), then
        keep the segment only if its mic level, averaged over [start_t, end_t], rose
        clearly above that bleed floor (i.e. there was genuine near-end speech).

        Deciding once per segment, rather than muting per chunk, means word onsets
        are never sliced into fragments that Whisper transcribes as spurious "You"
        lines (the failure mode of the earlier per-chunk gate). Conservative:
        returns False (keep) when the desktop is silent or there is too little
        history to judge, so the user's own speech is never dropped on a hunch."""
        ring = getattr(self, "_level_ring", None)
        if not ring:
            return False
        sr = self.sample_rate or 48000
        lo, hi = int(start_t * sr), int(end_t * sr)
        seg_mic: list[float] = []
        seg_dsk: list[float] = []
        ratios:  list[float] = []
        for off, m, l in list(ring):
            if l > 0.003:                      # desktop audible -> a coupling sample
                ratios.append(m / l)
            if off >= 0 and lo <= off <= hi:
                seg_mic.append(m)
                seg_dsk.append(l)
        if not seg_dsk or len(ratios) < 8:
            return False
        mean_dsk = sum(seg_dsk) / len(seg_dsk)
        if mean_dsk < 0.003:                   # desktop effectively silent this segment
            return False
        mean_mic = sum(seg_mic) / len(seg_mic)
        # Bleed floor = how loud pure desktop bleed is in the mic, self-calibrated to
        # this machine's speaker volume and mic. The 20th percentile tracks the quiet
        # (pure-bleed) moments while ignoring the user's own speech spikes.
        coupling = float(np.percentile(np.asarray(ratios, dtype=np.float64), 20.0))
        coupling = min(max(coupling, 0.02), 1.0)
        keep_above = coupling * mean_dsk * self.mic_bleed_slack
        is_bleed = mean_mic < keep_above
        log.info(
            "transcriber",
            f"[me] level gate mic={mean_mic:.4f} dsk={mean_dsk:.4f} "
            f"coupling={coupling:.2f} need>{keep_above:.4f} -> "
            f"{'DROP (desktop bleed)' if is_bleed else 'keep'}",
        )
        return is_bleed

    def _transcribe(
        self,
        buffer: list[bytes],
        source: str,
        start_time: float = 0.0,
        end_time: float = 0.0,
        force_no_diarize: bool = False,
    ) -> None:
        """Orchestrate diarization (if available) then Whisper transcription.

        ``force_no_diarize`` is used by the microphone ("mic = Me") stream: the
        audio is always the app user, so it skips the diarizer entirely and uses
        ``source`` (the reserved Me key) as the segment label."""
        if not buffer or self.model is None:
            return

        skip_diarizer = force_no_diarize or not self.diarization_enabled

        try:
            audio = self._convert(buffer)
        except Exception:
            log.error("transcriber", "Error converting audio buffer:")
            traceback.print_exc()
            return

        # ── Diarized path ────────────────────────────────────────────────────
        if self.diarizer is not None and not skip_diarizer:
            # diart processes the new audio incrementally - no rolling buffer needed.
            try:
                segments = self.diarizer.process(audio)
            except Exception:
                # Diarizer failure must not kill transcription - fall through.
                log.error("diarizer", "Error in process() - falling back to plain Whisper:")
                traceback.print_exc()
                segments = []
                if not self._diarizer_error_fired and self.on_diarizer_error:
                    self._diarizer_error_fired = True
                    try:
                        self.on_diarizer_error(
                            "Speaker diarization failed — transcript will "
                            "continue without speaker labels."
                        )
                    except Exception:
                        pass

            if segments:
                # Timestamps from diart are relative to the start of `audio`.
                # Reconstruct absolute recording-clock times from end_time.
                chunk_start = end_time - len(audio) / self.TARGET_RATE

                # ── Batch consecutive same-speaker segments ──────────────
                # Diarization often produces many tiny per-speaker slices
                # (0.3–0.5 s each).  Feeding each to Whisper individually
                # yields one-word outputs with spurious trailing periods.
                # Merge consecutive runs for the same speaker into a single
                # audio buffer so Whisper receives sentence-level context.
                batches: list[tuple[str, list[np.ndarray], float, float]] = []
                for label, start, end in segments:
                    start_i   = int(start * self.TARGET_RATE)
                    end_i     = int(end   * self.TARGET_RATE)
                    seg_audio = audio[start_i:end_i]
                    if len(seg_audio) < _MIN_WHISPER_SAMPLES:
                        continue
                    if self.fingerprint_callback is not None:
                        try:
                            self.fingerprint_callback(label, seg_audio,
                                                      chunk_start + start,
                                                      chunk_start + end)
                        except Exception:
                            pass
                    # Merge consecutive same-speaker segments, but break into
                    # a new segment if the silence gap exceeds the threshold.
                    if batches and batches[-1][0] == label:
                        prev_end = batches[-1][3] - chunk_start  # relative end of previous
                        gap = start - prev_end
                        if gap < self.segment_break_silence:
                            # Same speaker, short gap - append to batch
                            batches[-1][1].append(seg_audio)
                            batches[-1] = (label, batches[-1][1],
                                           batches[-1][2], chunk_start + end)
                        else:
                            # Same speaker but long pause - start new segment
                            batches.append((label, [seg_audio],
                                            chunk_start + start, chunk_start + end))
                    else:
                        batches.append((label, [seg_audio],
                                        chunk_start + start, chunk_start + end))

                for label, audio_parts, abs_start, abs_end in batches:
                    merged_audio = np.concatenate(audio_parts) if len(audio_parts) > 1 else audio_parts[0]
                    self._run_whisper(merged_audio, label, use_vad=False,
                                     start_time=abs_start, end_time=abs_end)
            # Diarizer was active - don't also run plain Whisper on the same audio.
            return

        # ── Plain Whisper path (no diarizer) ────────────────────────────────
        self._run_whisper(audio, source, use_vad=True,
                          start_time=start_time, end_time=end_time)

    def _run_whisper(
        self,
        audio: np.ndarray,
        label: str,
        use_vad: bool,
        start_time: float = 0.0,
        end_time: float = 0.0,
    ) -> None:
        """Run Whisper on a pre-converted float32 array and fire the callback."""
        if len(audio) < _MIN_WHISPER_SAMPLES:
            return

        # Per-speaker prompt context. Each label has its own bucket so a
        # bug pattern in one speaker's prompt can't contaminate another's.
        context = self._contexts.get(label, "")

        # Proactively clear context if it has itself become repetitive - this
        # prevents a contaminated prompt from seeding the next call.
        prompt = context[-self.prompt_chars:]
        if prompt and _repetition_ratio(prompt) < _HALLUCINATION_THRESHOLD:
            log.warn("transcriber", f"[{label}] Context contaminated by repetition - clearing")
            self._contexts[label] = ""
            context = ""
            prompt = ""

        # Strip per-word periods from the prompt before sending to Whisper.
        # If the stored context was poisoned by a previous flush, conditioning
        # on it would make Whisper continue producing per-word-period output.
        if prompt:
            cleaned_prompt = _collapse_word_periods(prompt)
            if cleaned_prompt != prompt:
                log.warn("transcriber", f"[{label}] Prompt had per-word-period pattern - cleaned")
                context = _collapse_word_periods(context)
                self._contexts[label] = context
                prompt = cleaned_prompt

        try:
            segments, _ = self.model.transcribe(
                audio,
                beam_size=self.beam_size,
                language="en",
                vad_filter=use_vad,
                vad_parameters=(
                    {
                        "min_silence_duration_ms": self.vad_min_silence_ms,
                        "speech_pad_ms": self.vad_speech_pad_ms,
                    }
                    if use_vad else None
                ),
                condition_on_previous_text=True,
                initial_prompt=prompt or None,
                compression_ratio_threshold=self.compression_ratio_threshold,
                word_timestamps=False,
            )
            parts = [seg.text.strip() for seg in segments if seg.text.strip()]
            if parts:
                text = _strip_fragment_period(" ".join(parts))
                text = _clean_ellipses(text)
                # Strip 'stuck character' runs (e.g. 'ŠŠŠŠ…') that Whisper emits
                # over near-silent audio; if nothing meaningful remains, drop it.
                stripped = _strip_repeated_char_runs(text)
                if stripped != text:
                    log.warn("transcriber", f"[{label}] Repeated-character hallucination cleaned")
                    text = stripped
                if len(_re.sub(r"[^\w]", "", text, flags=_re.UNICODE)) < 3:
                    return
                # Collapse per-word periods (e.g. "And. Uh. It. Would.") into
                # normal text. Done after _strip_fragment_period so the latter
                # still handles the simple single-fragment case.
                collapsed = _collapse_word_periods(text)
                if collapsed != text:
                    log.warn("transcriber", f"[{label}] Per-word-period pattern detected - cleaned")
                    text = collapsed
                # Strip known hallucination phrases (subtitle credits, etc.)
                text = _clean_hallucinations(text)
                if not text:
                    return
                # Remove repeated sentences (e.g. "QUESTION. SO I THINK...")
                text = _dedup_sentences(text)
                if not text:
                    log.warn("transcriber", f"[{label}] Sentence-loop detected - discarding")
                    self._contexts[label] = ""
                    return
                # Discard and clear context if output is a hallucination loop.
                if _repetition_ratio(text) < _HALLUCINATION_THRESHOLD:
                    log.warn("transcriber", f"[{label}] Hallucination loop detected - discarding")
                    self._contexts[label] = ""
                    return
                self._contexts[label] = (context + " " + text)[-self.prompt_chars * 2:]
                preview = text[:60] + ("…" if len(text) > 60 else "")
                log.info("transcriber", f"[{label}] {preview!r}")
                self.on_text_callback(text, label, start_time, end_time)
        except RuntimeError as e:
            if "cublas" in str(e) or "cuda" in str(e).lower():
                log.warn("whisper", f"CUDA runtime error - switching to CPU: {e}")
                self._switch_to_cpu()
                self._run_whisper(audio, label, use_vad,
                                 start_time=start_time, end_time=end_time)
            else:
                self._log_whisper_error(label, audio, use_vad, e)
        except Exception as e:
            self._log_whisper_error(label, audio, use_vad, e)

    def _merge_contexts(self, keep_label: str, merge_label: str) -> None:
        """Combine merge_label's prompt context into keep_label's bucket.

        Wired as the diarizer's on_merge_speakers callback so that when the
        user (or auto-merge logic) decides two diarized speakers are really
        the same person, the merged speaker's recent transcript history
        follows them into the kept label — Whisper's prompt for the kept
        speaker keeps the richer context instead of starting cold.
        """
        merge_ctx = self._contexts.pop(merge_label, "")
        if not merge_ctx:
            return
        keep_ctx = self._contexts.get(keep_label, "")
        combined = (keep_ctx + " " + merge_ctx).strip()
        self._contexts[keep_label] = combined[-self.prompt_chars * 2:]

    def _log_whisper_error(
        self,
        label: str,
        audio: np.ndarray,
        use_vad: bool,
        exc: BaseException,
    ) -> None:
        duration = len(audio) / 16000.0
        log.error(
            "transcriber",
            f"Whisper failed [{label}] ({duration:.2f}s, vad={use_vad}, "
            f"device={self.device}/{self.compute_type}): "
            f"{type(exc).__name__}: {exc}",
        )
        tb = traceback.format_exc().rstrip()
        for line in tb.splitlines():
            log.error("transcriber", f"  {line}")

    def _switch_to_cpu(self) -> None:
        """Reload the Whisper model in CPU/int8 mode after a CUDA failure.

        Only relevant on Windows/Linux — on macOS the engine is mlx-whisper
        which doesn't fail with cublas/cuda errors. We keep the method
        available for callers that don't check platform.
        """
        from ml.transcriber_engine import make_engine
        self.device = "cpu"
        self.compute_type = "int8"
        self.model_size = "small"
        log.warn("whisper", "Reloading as 'small' on CPU (int8)…")
        self.model = make_engine("small", "cpu", "int8")
        log.info("whisper", "CPU fallback ready.")

    def _loop(self) -> None:
        """Dispatch to the two-stream ("mic = Me") loop when a Me label is
        configured, else the legacy single mixed-stream loop."""
        if self.me_label:
            self._loop_two_stream()
        else:
            self._loop_legacy()

    def _loop_two_stream(self) -> None:
        """Route per-source PCM (5-tuples from the capture mixer) into two
        independent streams: the microphone stream is never diarized and is
        always labelled with ``self.me_label`` (the app user); the desktop
        stream is diarized exactly as the legacy path. Each stream flushes on
        its own silence cadence; segments interleave by timestamp downstream.

        Falls back gracefully: a non-per-source item (e.g. macOS capture that
        doesn't split, or the feature mid-toggle) is fed to the desktop stream
        so it is still transcribed/diarized."""
        chunks_per_second    = self.sample_rate / self.CHUNK_SIZE
        silence_chunk_thresh = int(self.silence_duration  * chunks_per_second)
        min_buffer_chunks    = int(self.min_buffer_seconds * chunks_per_second)
        max_buffer_chunks    = int(self.max_buffer_seconds * chunks_per_second)

        import collections
        sr = self.sample_rate or 48000
        # Rolling buffer of recent desktop (loopback) audio keyed by the mixer's
        # sample offset, used by the optional voiceprint bleed check (~20 s window).
        desktop_ring = collections.deque(maxlen=int(20 * sr / self.CHUNK_SIZE) + 8)
        # Rolling RAW (pre-gain) mic/loopback levels keyed by sample offset, used by
        # the primary level-based bleed gate to judge whole mic segments (~30 s).
        self._level_ring = collections.deque(maxlen=int(30 * sr / self.CHUNK_SIZE) + 8)

        def _mk(on_flush):
            return _StreamAccumulator(
                self.sample_rate, self.CHUNK_SIZE, self.silence_threshold,
                silence_chunk_thresh, min_buffer_chunks, max_buffer_chunks, on_flush)

        def _mic_flush(buf, s, e):
            # Drop a mic segment that is really just the desktop bleeding in. The
            # level gate is the primary, reliable check (it survives the mic/loopback
            # clock drift); the voiceprint check is an optional secondary, off unless
            # mic_bleed_threshold > 0.
            if self._mic_segment_is_bleed(s, e):
                return
            if self.mic_bleed_threshold > 0.0 and \
                    self._mic_is_desktop_bleed(buf, s, e, desktop_ring):
                return
            self._transcribe(buf, self.me_label, start_time=s, end_time=e,
                             force_no_diarize=True)

        mic = _mk(_mic_flush)
        desktop = _mk(lambda buf, s, e: self._transcribe(
            buf, "loopback", start_time=s, end_time=e))

        while self.is_running:
            try:
                item = self.audio_queue.get(timeout=0.5)
                if isinstance(item, tuple) and len(item) >= 5:
                    _src, _mixed, sample_off, mic_bytes, lb_bytes = item[:5]
                    # 6th element (macOS): (raw_mic_rms, raw_lb_rms) pre-gain levels
                    # for the bleed gate. Absent on Windows, where the gate no-ops.
                    levels = item[5] if len(item) >= 6 else None
                    if levels is not None and sample_off >= 0:
                        self._level_ring.append((sample_off, levels[0], levels[1]))
                    if lb_bytes is not None:
                        desktop_ring.append((sample_off, lb_bytes))
                        desktop.feed(lb_bytes, sample_off)
                    if mic_bytes is not None:
                        mic.feed(mic_bytes, sample_off)
                else:
                    # Defensive fallback for a non-per-source item.
                    if isinstance(item, tuple) and len(item) == 3:
                        _src, chunk, sample_off = item
                    elif isinstance(item, tuple):
                        _src, chunk = item
                        sample_off = -1
                    else:
                        chunk, sample_off = item, -1
                    desktop.feed(chunk, sample_off)
            except queue.Empty:
                # Genuine audio gap - flush both streams.
                mic.flush()
                desktop.flush()

        # Final flush on shutdown.
        mic.flush()
        desktop.flush()

    def _loop_legacy(self) -> None:
        buffer: list[bytes] = []
        source_counts: dict[str, int] = {}
        silence_chunks = 0
        first_offset = -1     # sample offset of the first chunk in this buffer
        last_offset  = -1     # sample offset of the most recent chunk

        chunks_per_second      = self.sample_rate / self.CHUNK_SIZE
        silence_chunk_thresh   = int(self.silence_duration * chunks_per_second)
        min_buffer_chunks      = int(self.min_buffer_seconds  * chunks_per_second)
        max_buffer_chunks      = int(self.max_buffer_seconds  * chunks_per_second)

        def _flush() -> None:
            nonlocal silence_chunks, first_offset, last_offset
            if not buffer:
                return
            dominant = max(source_counts, key=source_counts.get) if source_counts else "loopback"
            # Compute wall-clock times from sample offsets
            if first_offset >= 0 and self.sample_rate:
                start_t = first_offset / self.sample_rate
                end_t   = (last_offset + self.CHUNK_SIZE) / self.sample_rate
            else:
                start_t, end_t = 0.0, 0.0
            self._transcribe(buffer, dominant, start_time=start_t, end_time=end_t)
            buffer.clear()
            source_counts.clear()
            silence_chunks = 0
            first_offset = -1
            last_offset  = -1

        while self.is_running:
            try:
                item = self.audio_queue.get(timeout=0.5)
                if isinstance(item, tuple) and len(item) >= 3:
                    # 3-tuple (legacy) or 5-tuple (per-source); use the mixed
                    # chunk + offset and ignore any per-source fields here.
                    src, chunk, sample_off = item[0], item[1], item[2]
                elif isinstance(item, tuple):
                    src, chunk = item
                    sample_off = -1
                else:
                    src, chunk, sample_off = "loopback", item, -1

                buffer.append(chunk)
                source_counts[src] = source_counts.get(src, 0) + 1

                if sample_off >= 0:
                    if first_offset < 0:
                        first_offset = sample_off
                    last_offset = sample_off

                # Measure energy of this chunk to track pauses
                samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32_768.0
                rms = float(np.sqrt(np.mean(samples ** 2)))

                if rms < self.silence_threshold:
                    silence_chunks += 1
                else:
                    silence_chunks = 0

                enough_audio  = len(buffer) >= min_buffer_chunks
                long_pause    = silence_chunks >= silence_chunk_thresh
                hit_max       = len(buffer) >= max_buffer_chunks

                if (enough_audio and long_pause) or hit_max:
                    _flush()

            except queue.Empty:
                # Queue dried up (genuine audio gap) - flush whatever we have
                _flush()

        # Final flush
        _flush()
