"""
Batch reanalysis pipeline using HuggingFace transformers + pyannote.

Uses batched Whisper inference (transformers.pipeline) and full-file pyannote
SpeakerDiarization for higher accuracy than the real-time streaming path.
This module is completely independent of transcriber.py / diarizer.py.

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

import log

# ── Hallucination detection (mirrors transcriber.py) ──────────────────────────
_HALLUCINATION_NGRAM     = 4
_HALLUCINATION_THRESHOLD = 0.35  # unique-ratio below this → discard


def _repetition_ratio(text: str, n: int = _HALLUCINATION_NGRAM) -> float:
    """Return the fraction of unique n-grams (1.0 = no repetition)."""
    words = text.lower().split()
    if len(words) < n * 2:
        return 1.0
    ngrams = [tuple(words[i:i + n]) for i in range(len(words) - n + 1)]
    return len(set(ngrams)) / len(ngrams)


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


class BatchTranscriber:
    """Batch reanalysis pipeline: full-file diarization + batched Whisper."""

    def __init__(
        self,
        on_text_callback: Callable[[str, str, float, float], None],
        fingerprint_callback: Callable[[str, np.ndarray, float, float], None] | None = None,
        hf_token: str = "",
        on_progress_callback: Callable[[float], None] | None = None,
    ):
        self.on_text_callback = on_text_callback
        self.fingerprint_callback = fingerprint_callback
        self.hf_token = hf_token
        self.on_progress_callback = on_progress_callback

    def process_wav_file(self, wav_path: str, params: dict) -> None:
        """Run the full batch pipeline on a WAV file (blocking)."""
        import torch

        # ── Resolve device ────────────────────────────────────────────────────
        device_pref = params.get("reanalysis_device", "auto")
        if device_pref == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            device = device_pref
        if device == "cuda" and not torch.cuda.is_available():
            log.warn("batch", "CUDA requested but not available — falling back to CPU")
            device = "cpu"
        torch_device = torch.device(device)

        log.info("batch", f"Device: {device}")

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
            # No diarization results — transcribe the whole file as one segment
            log.warn("batch", "No diarization segments — transcribing full file")
            segments = [("Speaker 1", 0.0, total_duration)]

        # ── Fire fingerprint callbacks ────────────────────────────────────────
        # Pass ALL segments to the callback (even short ones) — the accumulator
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
            from pyannote.audio import Pipeline as PyannotePipeline
        except ImportError:
            log.error("batch", "pyannote.audio not installed — skipping diarization")
            return []

        log.info("batch", "Loading diarization pipeline...")
        try:
            from network import ensure_warp_disconnected
            ensure_warp_disconnected()
            pipeline = PyannotePipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=self.hf_token,
            )
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

        log.info("batch", f"Running diarization on {total_duration:.1f}s of audio...")
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
                )
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

        # Release diarization pipeline to free VRAM before transcription
        del pipeline
        del waveform
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        # Re-enable TF32 — pyannote disables it for reproducibility but
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
        """Batch-transcribe diarized segments using HuggingFace transformers pipeline."""
        import torch
        from transformers import pipeline as hf_pipeline
        from transformers.utils import is_flash_attn_2_available

        model_name = params.get("reanalysis_whisper_model", "openai/whisper-large-v3")
        batch_size = params.get("reanalysis_batch_size", 16)

        attn_impl = "flash_attention_2" if is_flash_attn_2_available() else "sdpa"
        log.info("batch", f"Loading Whisper model: {model_name} (attn: {attn_impl})")

        from network import ensure_warp_disconnected
        ensure_warp_disconnected()

        torch_dtype = torch.float16 if device == "cuda" else torch.float32
        whisper_pipe = hf_pipeline(
            "automatic-speech-recognition",
            model=model_name,
            torch_dtype=torch_dtype,
            device=device,
            model_kwargs={"attn_implementation": attn_impl},
        )

        # Prepare audio chunks for each diarized segment
        chunks = []
        chunk_meta = []  # (speaker, start, end) parallel to chunks
        for speaker, start, end in segments:
            start_i = int(start * TARGET_RATE)
            end_i = int(end * TARGET_RATE)
            seg_audio = audio[start_i:end_i]
            if len(seg_audio) < int(0.1 * TARGET_RATE):
                continue  # skip tiny segments
            seg_duration = len(seg_audio) / TARGET_RATE
            if seg_duration > 60:
                log.warn("batch", f"Long segment: {speaker} {start:.1f}s-{end:.1f}s "
                         f"({seg_duration:.1f}s) — will be internally chunked")
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
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        log.info("batch", f"Transcription complete: {processed} segments processed")

    def _report_progress(self, fraction: float) -> None:
        if self.on_progress_callback:
            try:
                self.on_progress_callback(round(min(1.0, max(0.0, fraction)), 3))
            except Exception:
                pass
