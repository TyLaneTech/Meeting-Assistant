"""
Batch reanalysis pipeline using HuggingFace transformers + pyannote.

Uses batched Whisper inference (transformers.pipeline) and full-file pyannote
SpeakerDiarization for higher accuracy than the real-time streaming path.
This module is completely independent of transcriber.py / diarizer.py.

Currently uses pyannote/speaker-diarization-3.1 on pyannote.audio 3.x.
When pyannote 4.x is adopted, upgrade to speaker-diarization-community-1
for 10-17% DER improvement via VBx clustering (see pyannote 4.0 release notes).

Usage:
    bt = BatchTranscriber(on_text_callback=..., fingerprint_callback=..., hf_token=...)
    bt.process_wav_file("path/to/file.wav", params)
"""
import os
import sys
import traceback
import wave
from typing import Callable

import numpy as np
from scipy import signal as scipy_signal

from core import log as log

# ── Hallucination detection (shared with transcriber.py) ─────────────────────
from ml.transcriber import (
    _HALLUCINATION_THRESHOLD,
    _repetition_ratio,
    _clean_ellipses,
    _clean_hallucinations,
    _collapse_word_periods,
    _dedup_sentences,
)


# ── Windows: register nvidia DLL directories ─────────────────────────────────
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

TARGET_RATE = 16_000


def _load_audio(wav_path: str) -> tuple[np.ndarray, int]:
    """Load a WAV file and return (float32 mono audio at 16 kHz, original_rate)."""
    with wave.open(wav_path, "rb") as wf:
        n_channels = wf.getnchannels()
        file_rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32_768.0
    if n_channels > 1:
        audio = audio.reshape(-1, n_channels).mean(axis=1)
    if file_rate != TARGET_RATE:
        audio = scipy_signal.resample_poly(audio, TARGET_RATE, file_rate)
    return audio, file_rate


def desktop_speaker_params(params: dict) -> dict:
    """Speaker-count hints for the desktop track of a source-aware pass.

    The count from the reanalyze dial, Smart Cleanup or the calendar is the
    number of people in the meeting, Me included. Me is on the mic track, so
    the desktop diarizer is asked for one less (never below one)."""
    out = dict(params)
    for key in ("reanalysis_num_speakers", "reanalysis_min_speakers",
                "reanalysis_max_speakers"):
        value = int(out.get(key) or 0)
        if value > 0:
            out[key] = max(1, value - 1)
    return out


# A speaker is a short-reply fragment when their typical turn is this short,
# none of their turns is longer than SHORT_REPLY_MAX_TURN_S, and they talk
# less than SHORT_REPLY_MAX_SHARE of the main speaker; or when their whole
# talk time is under SHORT_REPLY_MIN_TOTAL_S. Measured on 40 meetings
# (2026-10-01): the phantom "speakers" were "mm-hmm", "yeah", "thank you"
# clusters with a median turn of 0.2 to 1.2 s and no turn over about 6 s,
# while real participants had longer typical turns, at least one long turn,
# or a large talk share.
SHORT_REPLY_MAX_MEDIAN_S = 1.2
SHORT_REPLY_MAX_TURN_S = 8.0
SHORT_REPLY_MAX_SHARE = 0.10
SHORT_REPLY_MIN_TOTAL_S = 5.0
# A fragment whose voice is less similar than this (cosine, speaker centroids)
# to every real speaker is somebody else who only spoke briefly, not one of
# them saying "mm-hmm", so it is never folded. Duration alone folded a
# different voice saying one four-second sentence into another person (review
# of PR 1086, 2026-10-07). Chosen conservatively, not calibrated: the voice
# library's own "same person" bar is 0.70 on these embeddings, and a different
# voice sits far below 0.25. Without a usable embedding there is no evidence
# either way, and the fragment goes to the main talker as before.
SHORT_REPLY_MIN_SIMILARITY = 0.25


def absorb_short_replies(
    segments: list[tuple[str, float, float]],
    centroids: dict | None = None,
    min_speakers: int = 0,
) -> tuple[list[tuple[str, float, float]], dict[str, str]]:
    """Fold short-reply fragment speakers into the closest real speaker.

    Short replies carry too little voice for a reliable embedding, so the
    diarizer files them as extra people. Each fragment speaker is relabeled to
    the remaining speaker whose centroid is most similar (cosine), or to the
    speaker who talks most when no usable centroid exists. A fragment that
    sounds like none of them (SHORT_REPLY_MIN_SIMILARITY) is kept, and folding
    stops before fewer than ``min_speakers`` remain, smallest talkers folded
    first. The speaker who talks most is never folded.
    Returns (segments, {fragment: target}).
    """
    durations: dict[str, list[float]] = {}
    for speaker, start, end in segments:
        durations.setdefault(speaker, []).append(max(0.0, end - start))
    if len(durations) < 2:
        return segments, {}
    totals = {s: sum(d) for s, d in durations.items()}
    top = max(totals.values())
    fragments = {
        s for s, total in totals.items()
        if total < top and (
            total < SHORT_REPLY_MIN_TOTAL_S
            or (float(np.median(durations[s])) <= SHORT_REPLY_MAX_MEDIAN_S
                and max(durations[s]) <= SHORT_REPLY_MAX_TURN_S
                and total < SHORT_REPLY_MAX_SHARE * top)
        )
    }
    keepers = [s for s in totals if s not in fragments]
    if not fragments or not keepers:
        return segments, {}

    def _unit(vec):
        if vec is None:
            return None
        v = np.asarray(vec, dtype=np.float64).ravel()
        n = float(np.linalg.norm(v))
        return v / n if n > 0 and np.isfinite(n) else None

    centroids = centroids or {}
    keeper_units = {k: _unit(centroids.get(k)) for k in keepers}
    loudest = max(keepers, key=lambda k: totals[k])
    floor = max(1, int(min_speakers or 0))
    mapping: dict[str, str] = {}
    for frag in sorted(fragments, key=lambda s: (totals[s], s)):
        if len(totals) - len(mapping) <= floor:
            break   # the user asked for at least this many people
        target = loudest
        fu = _unit(centroids.get(frag))
        if fu is not None:
            scored = [(float(fu @ ku), k) for k, ku in keeper_units.items() if ku is not None]
            if scored:
                similarity, target = max(scored)
                if similarity < SHORT_REPLY_MIN_SIMILARITY:
                    continue
        mapping[frag] = target
    return [(mapping.get(s, s), a, b) for s, a, b in segments], mapping


def renumber_speakers(
    segments: list[tuple[str, float, float]],
) -> list[tuple[str, float, float]]:
    """Relabel speakers "Speaker 1..N" by first appearance, closing the gaps
    folding leaves (Speaker 1, Speaker 4 becomes Speaker 1, Speaker 2)."""
    order: dict[str, str] = {}
    out = []
    for speaker, start, end in sorted(segments, key=lambda x: x[1]):
        if speaker not in order:
            order[speaker] = f"Speaker {len(order) + 1}"
        out.append((order[speaker], start, end))
    return out


class ReanalysisCancelled(Exception):
    """Raised inside the batch pipeline once its cancel event is set. Checked
    between transcription windows and before every emitted segment, so a
    cancelled pass never writes another segment after the caller moved on."""


class BatchTranscriber:
    """Batch reanalysis pipeline: full-file diarization + batched Whisper."""

    def __init__(
        self,
        on_text_callback: Callable[[str, str, float, float], None],
        fingerprint_callback: Callable[[str, np.ndarray, float, float], None] | None = None,
        hf_token: str = "",
        on_progress_callback: Callable[[float], None] | None = None,
        cancel_event=None,
    ):
        self._user_on_text = on_text_callback
        self.on_text_callback = self._guarded_on_text
        # The fingerprint callback gets the same cancel guard as the text
        # callback: it reads the app's current session from shared state, so a
        # cancelled pass that kept emitting fingerprints would attach the
        # previous meeting's voices to the recording that replaced it.
        self._user_fingerprint = fingerprint_callback
        self.fingerprint_callback = self._guarded_fingerprint if fingerprint_callback else None
        self.hf_token = hf_token
        self.on_progress_callback = on_progress_callback
        # threading.Event (or None). Set by the app to abandon the pass, e.g.
        # when a new recording starts while a post-meeting transcription runs.
        # Diarization is one uninterruptible call; the Whisper phase stops at
        # the next window.
        self.cancel_event = cancel_event

    def _check_cancel(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise ReanalysisCancelled()

    def _guarded_on_text(self, *args, **kwargs):
        self._check_cancel()
        return self._user_on_text(*args, **kwargs)

    def _guarded_fingerprint(self, *args, **kwargs):
        self._check_cancel()
        return self._user_fingerprint(*args, **kwargs)

    @staticmethod
    def _resolve_device(pref: str) -> str:
        """"auto" picks the best device; a requested accelerator this machine
        lacks falls back to the best one it has."""
        import torch
        from core.compute_device import best_torch_device
        if pref == "cpu":
            # Decided without asking torch about CUDA at all: a CPU job on
            # battery must not initialize the NVIDIA driver.
            return "cpu"
        if pref == "auto":
            return best_torch_device()
        best = best_torch_device()
        if pref == "cuda" and not torch.cuda.is_available():
            log.warn("batch", f"CUDA requested but not available - falling back to {best}")
            return best
        if pref == "mps" and not (
            getattr(torch.backends, "mps", None) is not None
            and torch.backends.mps.is_available()
        ):
            log.warn("batch", f"MPS requested but not available - falling back to {best}")
            return best
        return pref

    def process_wav_file(self, wav_path: str, params: dict,
                         tracks_root: str | None = None) -> None:
        """Run the full batch pipeline on a WAV file (blocking).

        ``tracks_root`` is where the per-source tracks live, as ``audio/{sid}``
        with no suffix. It used to be derived from ``wav_path``, which stopped
        holding the day the WAV could be a decode in tmp/ of an Opus session
        (core/media.py); the caller knows the session, so it says.
        """
        import torch

        # ── Resolve devices ───────────────────────────────────────────────────
        # Whisper runs on reanalysis_device. Diarization runs on
        # reanalysis_diarizer_device when the caller set one (the app passes
        # the user's Diarizer device setting through it), else on the same
        # device as Whisper.
        device = self._resolve_device(params.get("reanalysis_device", "auto"))
        diar_pref = params.get("reanalysis_diarizer_device") or ""
        diar_device = self._resolve_device(diar_pref) if diar_pref else device
        torch_device = torch.device(diar_device)

        log.info("batch", f"Device: diarization {diar_device}, transcription {device}")

        # ── Source-aware path ("mic = Me") ────────────────────────────────────
        # When per-source tracks exist (recorded with the feature on), diarize
        # ONLY the desktop track and attribute the whole mic track to the Me
        # speaker. Falls back to the mixed path for old recordings.
        me_label = params.get("me_label")
        if me_label:
            ps = self._per_source_inputs(wav_path, tracks_root)
            if ps is not None:
                desktop_audio, mic_audio = ps
                total_duration = max(len(desktop_audio), len(mic_audio)) / TARGET_RATE
                log.info("batch", f"Per-source reanalysis: desktop "
                         f"{len(desktop_audio)/TARGET_RATE:.1f}s + mic "
                         f"{len(mic_audio)/TARGET_RATE:.1f}s")
                self._report_progress(0.05)
                self._process_per_source(
                    desktop_audio, mic_audio, params, me_label,
                    torch_device, device, total_duration)
                self._report_progress(1.0)
                log.info("batch", "Reanalysis complete (source-aware).")
                return
            log.info("batch", "No per-source tracks found - using mixed audio "
                     "(this recording predates source-aware capture).")

        # ── Load audio ────────────────────────────────────────────────────────
        log.info("batch", f"Loading audio: {wav_path}")
        audio, original_rate = _load_audio(wav_path)
        total_duration = len(audio) / TARGET_RATE
        log.info("batch", f"Audio loaded: {total_duration:.1f}s @ {TARGET_RATE} Hz")

        self._report_progress(0.05)

        # ── Run diarization ───────────────────────────────────────────────────
        segments = self._run_diarization(audio, params, torch_device, total_duration)
        self._report_progress(0.40)

        if not segments:
            # No diarization results - transcribe the whole file as one segment
            log.warn("batch", "No diarization segments - transcribing full file")
            segments = [("Speaker 1", 0.0, total_duration)]

        # ── Fire fingerprint callbacks ────────────────────────────────────────
        # Pass ALL segments to the callback (even short ones) - the accumulator
        # in _on_fingerprint_audio handles the minimum duration threshold.
        if self.fingerprint_callback:
            for speaker, start, end in segments:
                start_i = int(start * TARGET_RATE)
                end_i = int(end * TARGET_RATE)
                seg_audio = audio[start_i:end_i]
                if len(seg_audio) > 0:
                    try:
                        self.fingerprint_callback(speaker, seg_audio, start, end)
                    except Exception as e:
                        log.warn("batch", f"Fingerprint callback failed for {speaker}: {e}")

        self._report_progress(0.45)

        # ── Run batched Whisper transcription ─────────────────────────────────
        self._run_transcription(audio, segments, params, device, total_duration)
        self._report_progress(1.0)

        log.info("batch", "Reanalysis complete.")

    # ── Source-aware ("mic = Me") reanalysis ──────────────────────────────────

    @staticmethod
    def _tracks_root(wav_path: str, tracks_root: str | None = None) -> str:
        if tracks_root:
            return tracks_root
        return wav_path[:-4] if wav_path.lower().endswith(".wav") else wav_path

    @classmethod
    def _per_source_opus_paths(cls, wav_path: str,
                               tracks_root: str | None = None) -> tuple[str, str]:
        root = cls._tracks_root(wav_path, tracks_root)
        return root + "_desktop.opus", root + "_mic.opus"

    def _per_source_inputs(self, wav_path: str, tracks_root: str | None = None):
        """If both per-source Opus tracks exist at ``tracks_root`` (or beside
        ``wav_path`` when no root is given), decode them to 16 kHz mono numpy
        and return (desktop_audio, mic_audio); else None. Also accepts leftover
        temp WAVs (e.g. an interrupted encode)."""
        desktop_opus, mic_opus = self._per_source_opus_paths(wav_path, tracks_root)
        root = self._tracks_root(wav_path, tracks_root)
        desktop_src = desktop_opus if os.path.isfile(desktop_opus) else (
            root + "_desktop.wav" if os.path.isfile(root + "_desktop.wav") else None)
        mic_src = mic_opus if os.path.isfile(mic_opus) else (
            root + "_mic.wav" if os.path.isfile(root + "_mic.wav") else None)
        if not desktop_src or not mic_src:
            return None
        desktop_audio = self._decode_to_array(desktop_src)
        mic_audio = self._decode_to_array(mic_src)
        if desktop_audio is None or mic_audio is None:
            return None
        return desktop_audio, mic_audio

    @staticmethod
    def _decode_to_array(path: str):
        """Decode any ffmpeg-readable audio file to a 16 kHz mono float32 array."""
        if path.lower().endswith(".wav"):
            try:
                audio, _ = _load_audio(path)
                return audio
            except Exception:
                return None
        from capture_video.ffmpeg_util import find_ffmpeg, subprocess_no_window_flag
        import subprocess
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            log.warn("batch", "ffmpeg not found - cannot decode per-source Opus")
            return None
        tmp = path + ".dec16k.wav"
        try:
            r = subprocess.run(
                [ffmpeg, "-y", "-i", path, "-acodec", "pcm_s16le",
                 "-ar", str(TARGET_RATE), "-ac", "1", tmp],
                capture_output=True, timeout=600,
                creationflags=subprocess_no_window_flag(),
            )
            if r.returncode != 0 or not os.path.isfile(tmp):
                return None
            audio, _ = _load_audio(tmp)
            return audio
        except Exception:
            traceback.print_exc()
            return None
        finally:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    @staticmethod
    def _energy_segments(audio: np.ndarray, rms_thresh: float,
                         frame_sec: float = 0.03, merge_gap_sec: float = 0.6,
                         min_seg_sec: float = 0.3, pad_sec: float = 0.15
                         ) -> list[tuple[float, float]]:
        """Split mono audio into voiced (start, end) spans by frame energy. Used
        to give the microphone "Me" track real per-utterance timestamps without
        diarizing it."""
        n = len(audio)
        fr = max(1, int(frame_sec * TARGET_RATE))
        if n == 0:
            return []
        spans: list[tuple[float, float]] = []
        cur_start = None
        idx = 0
        for i in range(0, n, fr):
            chunk = audio[i:i + fr]
            rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
            t = idx * frame_sec
            if rms >= rms_thresh and cur_start is None:
                cur_start = t
            elif rms < rms_thresh and cur_start is not None:
                spans.append((cur_start, t))
                cur_start = None
            idx += 1
        if cur_start is not None:
            spans.append((cur_start, idx * frame_sec))
        # Merge small gaps
        merged: list[tuple[float, float]] = []
        for s, e in spans:
            if merged and s - merged[-1][1] <= merge_gap_sec:
                merged[-1] = (merged[-1][0], e)
            else:
                merged.append((s, e))
        # Pad + drop too-short + clamp
        dur = n / TARGET_RATE
        out = []
        for s, e in merged:
            if e - s < min_seg_sec:
                continue
            out.append((max(0.0, s - pad_sec), min(dur, e + pad_sec)))
        return out

    def _process_per_source(
        self,
        desktop_audio: np.ndarray,
        mic_audio: np.ndarray,
        params: dict,
        me_label: str,
        torch_device,
        device: str,
        total_duration: float,
    ) -> None:
        """Diarize the desktop track, attribute the mic track to the Me speaker,
        merge by time, and transcribe."""
        # Desktop: full diarization. The speaker count the user or calendar
        # gives counts everyone in the meeting, Me included, but Me is on the
        # mic track and never in the desktop audio, so the desktop gets one less.
        desktop_segs = self._run_diarization(
            desktop_audio, desktop_speaker_params(params), torch_device, total_duration)
        self._report_progress(0.40)

        # Fingerprint only desktop speakers (Me is never fingerprinted).
        if self.fingerprint_callback:
            for speaker, start, end in desktop_segs:
                seg = desktop_audio[int(start * TARGET_RATE):int(end * TARGET_RATE)]
                if len(seg) > 0:
                    try:
                        self.fingerprint_callback(speaker, seg, start, end)
                    except Exception as e:
                        log.warn("batch", f"Fingerprint callback failed for {speaker}: {e}")
        self._report_progress(0.45)

        # Mic: energy-segment into Me utterances (never diarized).
        rms_thresh = float(params.get("silence_threshold", 0.008) or 0.008)
        me_spans = self._energy_segments(mic_audio, rms_thresh)
        log.info("batch", f"Mic (Me) utterances: {len(me_spans)}")

        # Build the combined, time-sorted segment list with per-source audio.
        seg_list: list[tuple[str, float, float, np.ndarray]] = []
        for speaker, start, end in desktop_segs:
            seg_list.append((speaker, start, end,
                             desktop_audio[int(start * TARGET_RATE):int(end * TARGET_RATE)]))
        for start, end in me_spans:
            seg_list.append((me_label, start, end,
                             mic_audio[int(start * TARGET_RATE):int(end * TARGET_RATE)]))
        seg_list.sort(key=lambda x: x[1])

        if not seg_list:
            log.warn("batch", "No segments after source-aware split")
            return
        self._run_transcription_multi(seg_list, params, device, total_duration)

    def _run_diarization(
        self,
        audio: np.ndarray,
        params: dict,
        torch_device,
        total_duration: float,
    ) -> list[tuple[str, float, float]]:
        """Run full-file pyannote speaker diarization. Returns [(speaker, start, end), ...]."""
        import torch

        try:
            from core import config as _cfg
            _cfg.apply_torchaudio_shims()
            from pyannote.audio import Pipeline as PyannotePipeline
        except ImportError:
            log.error("batch", "pyannote.audio not installed - skipping diarization")
            return []

        log.info("batch", "Loading diarization pipeline...")
        try:
            from core.network import _load_hf_pipeline
            pipeline = _load_hf_pipeline(
                "pyannote/speaker-diarization-3.1", self.hf_token,
            )
            if pipeline is None:
                raise RuntimeError("Pipeline download failed (check network / HF token)")
            pipeline.to(torch_device)
        except Exception as e:
            log.error("batch", f"Failed to load diarization pipeline: {e}")
            traceback.print_exc()
            return []

        # Build waveform tensor for pyannote (shape: 1 x samples)
        waveform = torch.from_numpy(audio).unsqueeze(0).float()

        # Resolve speaker count hints (0 = auto)
        num_speakers = params.get("reanalysis_num_speakers", 0) or None
        min_speakers = params.get("reanalysis_min_speakers", 0) or None
        max_speakers = params.get("reanalysis_max_speakers", 0) or None

        # Apply diarization hyperparameters.
        # pyannote/speaker-diarization-3.1 uses powerset segmentation, so
        # segmentation.threshold does NOT exist. The tunable params are:
        #   segmentation: {min_duration_off}
        #   clustering:   {threshold, method, min_cluster_size, ...}
        cluster_threshold = params.get("reanalysis_clustering_threshold", 0.45)
        seg_min_dur_off = params.get("reanalysis_min_duration_off", 0.0)

        try:
            pipeline.instantiate({
                "segmentation": {
                    "min_duration_off": seg_min_dur_off,
                },
                "clustering": {
                    "threshold": cluster_threshold,
                    "method": "centroid",
                },
            })
            log.info("batch", f"Diarization params: clustering.threshold={cluster_threshold}, "
                     f"segmentation.min_duration_off={seg_min_dur_off}")
        except Exception as e:
            log.warn("batch", f"Could not set diarization hyperparameters: {e}")

        # Folding short-reply speakers needs the speaker centroids. It is
        # opt-in (Settings > Reanalysis), skipped for a forced exact count (the
        # user asked for N speakers) and when at most one speaker is allowed
        # (nothing to fold, and asking for embeddings would make pyannote
        # extract them for no reason).
        absorb = (bool(params.get("reanalysis_absorb_short_replies", 0))
                  and not num_speakers
                  and (max_speakers is None or max_speakers >= 2))

        log.info("batch", f"Running diarization on {total_duration:.1f}s of audio...")
        centroids = None
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=".*TensorFloat-32.*",
                                        category=UserWarning)
                # pyannote also uses a custom ReproducibilityWarning
                warnings.filterwarnings("ignore", message=".*TensorFloat-32.*")
                diarization = pipeline(
                    {"waveform": waveform, "sample_rate": TARGET_RATE},
                    num_speakers=num_speakers,
                    min_speakers=min_speakers,
                    max_speakers=max_speakers,
                    return_embeddings=absorb,
                )
            if absorb:
                diarization, centroids = diarization
        except Exception as e:
            log.error("batch", f"Diarization failed: {e}")
            traceback.print_exc()
            return []

        # Convert pyannote Annotation to list of (speaker_label, start, end)
        # Map pyannote's internal labels (SPEAKER_00, etc.) to "Speaker 1", "Speaker 2"
        speaker_map: dict[str, str] = {}
        raw_segments: list[tuple[str, float, float]] = []

        for segment, _track, speaker in diarization.itertracks(yield_label=True):
            if speaker not in speaker_map:
                speaker_map[speaker] = f"Speaker {len(speaker_map) + 1}"
            raw_segments.append((speaker_map[speaker], segment.start, segment.end))

        log.info("batch", f"Diarization complete: {len(raw_segments)} raw segments, "
                 f"{len(speaker_map)} speakers")

        # Sort by start time to guarantee chronological order
        raw_segments.sort(key=lambda x: x[1])

        # Merge consecutive same-speaker segments with small gaps
        merge_gap = params.get("reanalysis_merge_gap", 0.5)
        merged = self._merge_segments(raw_segments, merge_gap)
        log.info("batch", f"After merging: {len(merged)} segments")

        if absorb:
            # centroids rows follow diarization.labels() order.
            by_name: dict[str, np.ndarray] = {}
            if centroids is not None:
                for i, label in enumerate(diarization.labels()):
                    if label in speaker_map and i < len(centroids):
                        by_name[speaker_map[label]] = np.asarray(centroids[i])
            folded, mapping = absorb_short_replies(merged, by_name,
                                                   min_speakers=min_speakers or 0)
            if mapping:
                folded = renumber_speakers(folded)
                merged = self._merge_segments(folded, merge_gap)
                kept = len({s for s, _a, _b in merged})
                log.info("batch", f"Folded {len(mapping)} short-reply speaker(s) into "
                         f"real speakers: {len(speaker_map)} -> {kept} speakers, "
                         f"{len(merged)} segments")

        # Release diarization pipeline to free VRAM before transcription
        del pipeline
        del waveform
        from core.compute_device import empty_cache as _empty_cache
        _empty_cache(torch_device.type)

        # Re-enable TF32 - pyannote disables it for reproducibility but
        # it significantly speeds up Whisper inference on RTX GPUs.
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True

        return merged

    def _merge_segments(
        self,
        segments: list[tuple[str, float, float]],
        max_gap: float,
    ) -> list[tuple[str, float, float]]:
        """Merge consecutive same-speaker segments with gaps smaller than max_gap."""
        if not segments:
            return []
        merged = [segments[0]]
        for speaker, start, end in segments[1:]:
            prev_speaker, prev_start, prev_end = merged[-1]
            if speaker == prev_speaker and (start - prev_end) <= max_gap:
                merged[-1] = (speaker, prev_start, end)
            else:
                merged.append((speaker, start, end))
        return merged

    def _run_transcription(
        self,
        audio: np.ndarray,
        segments: list[tuple[str, float, float]],
        params: dict,
        device: str,
        total_duration: float,
    ) -> None:
        """Batch-transcribe diarized segments (sliced from a single ``audio``
        array) using HuggingFace transformers pipeline."""
        seg_list = []
        for speaker, start, end in segments:
            start_i = int(start * TARGET_RATE)
            end_i = int(end * TARGET_RATE)
            seg_list.append((speaker, start, end, audio[start_i:end_i]))
        self._run_transcription_multi(seg_list, params, device, total_duration)

    def _run_transcription_multi(
        self,
        seg_list: list[tuple[str, float, float, np.ndarray]],
        params: dict,
        device: str,
        total_duration: float,
    ) -> None:
        """Batch-transcribe segments where each carries its own audio slice.

        ``seg_list`` is [(speaker, start, end, seg_audio), ...] and should be
        sorted by start so callbacks fire chronologically. This lets the
        source-aware path mix desktop-diarized segments (sliced from the desktop
        track) with microphone "Me" segments (sliced from the mic track)."""
        import torch
        from transformers import pipeline as hf_pipeline
        from transformers.utils import is_flash_attn_2_available

        model_name = params.get("reanalysis_whisper_model", "openai/whisper-large-v3")
        batch_size = params.get("reanalysis_batch_size", 16)

        # flash-attn-2 only ships CUDA kernels; on MPS or CPU pipeline auto-falls
        # back to scaled_dot_product_attention (sdpa).
        attn_impl = "flash_attention_2" if (device == "cuda" and is_flash_attn_2_available()) else "sdpa"
        log.info("batch", f"Loading Whisper model: {model_name} (attn: {attn_impl})")

        # MPS supports fp16; CUDA prefers fp16; CPU sticks with fp32.
        if device == "cuda":
            torch_dtype = torch.float16
        elif device == "mps":
            torch_dtype = torch.float16
        else:
            torch_dtype = torch.float32
        whisper_pipe = hf_pipeline(
            "automatic-speech-recognition",
            model=model_name,
            torch_dtype=torch_dtype,
            device=device,
            model_kwargs={"attn_implementation": attn_impl},
        )

        # Prepare audio chunks for each segment (audio already sliced per source)
        chunks = []
        chunk_meta = []  # (speaker, start, end) parallel to chunks
        for speaker, start, end, seg_audio in seg_list:
            if len(seg_audio) < int(0.1 * TARGET_RATE):
                continue  # skip tiny segments
            seg_duration = len(seg_audio) / TARGET_RATE
            if seg_duration > 60:
                log.warn("batch", f"Long segment: {speaker} {start:.1f}s-{end:.1f}s "
                         f"({seg_duration:.1f}s) - will be internally chunked")
            chunks.append({"raw": seg_audio, "sampling_rate": TARGET_RATE})
            chunk_meta.append((speaker, start, end))

        if not chunks:
            log.warn("batch", "No segments to transcribe")
            return

        log.info("batch", f"Transcribing {len(chunks)} segments (batch_size={batch_size})...")

        # Process in batches
        progress_base = 0.45
        progress_range = 0.50  # 0.45 -> 0.95
        processed = 0

        for batch_start in range(0, len(chunks), batch_size):
            batch_end = min(batch_start + batch_size, len(chunks))
            batch_chunks = chunks[batch_start:batch_end]
            batch_meta = chunk_meta[batch_start:batch_end]

            try:
                results = whisper_pipe(
                    batch_chunks,
                    chunk_length_s=30,
                    batch_size=len(batch_chunks),
                    generate_kwargs={
                        "language": "en",
                        "task": "transcribe",
                        "compression_ratio_threshold": 2.0,
                        "no_repeat_ngram_size": 4,
                    },
                    return_timestamps=False,
                )
            except Exception as e:
                log.error("batch", f"Transcription batch failed: {e}")
                traceback.print_exc()
                processed += len(batch_chunks)
                continue

            # Fire callbacks in chronological order, filtering hallucinations
            for result, (speaker, start, end) in zip(results, batch_meta):
                text = result.get("text", "").strip()
                if not text:
                    continue
                text = _clean_ellipses(text)
                collapsed = _collapse_word_periods(text)
                if collapsed != text:
                    log.warn("batch", f"[{speaker}] Per-word-period pattern detected - cleaned")
                    text = collapsed
                text = _clean_hallucinations(text)
                if not text:
                    continue
                text = _dedup_sentences(text)
                if not text:
                    continue
                if _repetition_ratio(text) < _HALLUCINATION_THRESHOLD:
                    log.warn("batch", f"[{speaker}] Hallucination loop discarded: "
                             f"{text[:80]}…" if len(text) > 80 else
                             f"[{speaker}] Hallucination loop discarded: {text}")
                    continue
                try:
                    self.on_text_callback(text, speaker, start, end)
                except Exception:
                    traceback.print_exc()

            processed += len(batch_chunks)
            progress = progress_base + progress_range * (processed / len(chunks))
            self._report_progress(progress)

        # Release Whisper model
        del whisper_pipe
        from core.compute_device import empty_cache as _empty_cache
        _empty_cache(device)

        log.info("batch", f"Transcription complete: {processed} segments processed")

    def _report_progress(self, fraction: float) -> None:
        self._check_cancel()
        if self.on_progress_callback:
            try:
                self.on_progress_callback(round(min(1.0, max(0.0, fraction)), 3))
            except Exception:
                pass
