# Roadmap

Postponed ideas and feature plans. Not yet scheduled for implementation.

---

## Idle cooldown / VRAM unload

**Goal:** When the app sits idle (no webpage or tray interaction) for a configurable duration, unload the Whisper + pyannote models from VRAM and enter an idle state to conserve GPU resources. Toggleable from the System tab of the settings pane.

### Context
- Whisper + pyannote stay resident in VRAM for the full app lifetime (loaded at startup in `app.py:790-791`).
- This produces ~10% phantom GPU utilization in Task Manager (CUDA context heartbeats), and holds hundreds of MB of VRAM even when the app isn't being used.
- Reload cost from cached weights: Whisper ~2–5s, pyannote ~3–8s, sentence-transformers ~1–2s. Full cold wake ≈ 5–12s before first transcription is possible.

### Tradeoffs
- **Pro:** frees VRAM and eliminates idle GPU overhead when the app is forgotten.
- **Con:** 5–12s stall on the next Record click if the user wakes suddenly. Mitigated by pre-warming on UI/tray activity before Record is pressed.

### Proposed implementation
1. **Settings** — add `idle_unload_enabled` (bool, default off) and `idle_unload_minutes` (int, default 30) to `settings.py`; expose in the System tab of the settings pane.
2. **Activity tracker** — `_last_activity_ts` updated by:
   - Any Flask request (via `@app.before_request`)
   - SSE client connects
   - Tray menu interactions
   - Recording start/stop
3. **Idle watchdog thread** — wakes every ~30s, checks `now - _last_activity > threshold and not is_recording and not is_testing and no chat/reanalysis in flight`. If idle, calls `_unload_models()` which drops the Whisper model, diarizer, (optionally text embeddings), then `gc.collect()` + `torch.cuda.empty_cache()`.
4. **Wake path** — any activity while idle kicks off the existing `_load_model` / `_load_diarizer` threads. UI shows a "waking up…" banner reusing the existing `diarizer_ready` status mechanism.
5. **Guardrails** — never unload mid-recording, mid-reanalysis, or while a chat request is in flight. Consider pre-warming when the web UI is opened or the tray menu opens, so the user doesn't hit the stall on their first Record click.

### Open questions
- Should the text-embedding model (MiniLM, ~80 MB) also be unloaded, or left alone since it's small and loaded on demand anyway?
- Default threshold: 30 min feels safe. Off by default so existing users aren't surprised.

---

## macOS: per-source ("mic = Me") capture

**Goal:** Mirror the Windows "microphone is you" source-aware diarization on macOS so mic audio is always the app user and only desktop audio is diarized there too.

### Context
- The feature shipped Windows-first. The cross-cutting parts (the `me_speaker_global_id` setting + purge, the two-stream `Transcriber._loop_two_stream`, the `BatchTranscriber` per-source reanalysis path, the onboarding popup + "(You)" badge, export/import safety) are platform-agnostic and already done.
- Only the capture side is Windows-only: `capture_audio/windows.py` writes the per-source `{sid}_mic.wav` / `{sid}_desktop.wav` temp tracks in `_mixer_loop`, encodes them to Opus on `stop()`, and enqueues 5-tuples `(src, mixed, offset, mic_bytes, lb_bytes)`.
- `capture_audio/mac.py` still enqueues the legacy mixed tuples, so on macOS `start_recording` sees `getattr(capture, "_per_source_active", False) == False` and leaves `me_label` unset — the app cleanly falls back to the legacy single-stream behavior (no regression, just no feature).

### Proposed implementation
1. Add the same `mic_is_me_enabled` / `_per_source_active` attributes + `_open_per_source_writers` / `_encode_per_source_opus` / resume-decode helpers to `capture_audio/mac.py` (they can largely be lifted from `windows.py`).
2. At mac's mix point, write the loopback-only and mic-only chunks (sample-aligned, zeros included) to the two temp WavWriters and enqueue the 5-tuple.
3. Reuse `capture_video/ffmpeg_util.find_ffmpeg()` for the Opus encode (the bundled macOS ffmpeg already supports libopus).
4. Verify end-to-end on macOS hardware (BlackHole loopback + a real mic).

---

## ~~Notes pane: export/import bundling~~ — done

The export zip now bundles `notes_attachments/<file>` for every file in `storage/data/notes/<session_id>/`, gated on the new `notes` checkbox in the export modal. On import the directory is restored under the new session id and the notes Delta has its `/api/sessions/<old>/notes/attachments/` URLs rewritten to the new id, mirroring the existing screenshot-URL rewrite.
