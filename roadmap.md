# Roadmap

Postponed ideas and feature plans. Not yet scheduled for implementation.

---

## Speaker attribution: follow-ons from the speaker_lab findings

The "link v2" auto-apply policy (exclusion removal + margin/streak routes,
validated in the gitignored `speaker_lab/` replay harness against 10
hand-corrected meetings) shipped in `app.py` / `ml/speaker_db.py` behind the
`speaker_link_v2` settings key (default on). Deferred follow-ups, in impact
order:

- ~~**Library hygiene pass.**~~ Done 2026-08-27: `SpeakerFingerprintDB` grew a
  maintenance pipeline (`library_health` / `run_maintenance`: same-name
  duplicate merges, foreign-embedding sweep via leave-one-out vs rival
  centroids, 2-means pollution purges, full centroid canonicalization),
  exposed at `/api/fingerprint/library/{health,maintenance,auto}`, in the
  Voice Library "Health" tab, and as a weekly idle-time scheduler
  (`library_maintenance_*` settings). Validated on a DB copy
  (`speaker_lab/test_maintenance.py`), then applied live: 141 -> 131
  profiles, 11,102 -> 10,331 embeddings, changelog + pre-cleanup backup in
  the data folder's `backups/`. Replay showed the cleanup is
  accuracy-NEUTRAL for attribution (the linking-policy change carries the
  accuracy win); its value is consolidated identities and stopping further
  pollution. NOTE: S-norm magnet score normalization was tested in the lab
  and REJECTED (kills depressed-similarity meetings); the remaining
  confusions (Kristen Maddox <-> Sarah Elliott 0.845, Snehitha <-> Sireesha
  0.767) are surfaced as Health-tab warnings instead. A possible future
  refinement: audio-snippet preview for review-class split profiles (Rich
  Nelson, Riley Preiss, Amber OReilly) so the user can adjudicate the second
  voice by ear.
- **Per-meeting similarity calibration.** One meeting regime (e.g. Carrier
  Contact Tool Review 76da00bd, likely narrowband/phone-bridge audio) has
  every profile similarity depressed below ~0.66 for the whole meeting, so
  even the new floors never fire (35 correct suggestions, 0 auto-links).
  Idea: track the session's running max-sim distribution and lower the
  margin/streak floors when the whole distribution is shifted down (z-score
  the top-match sims within the session instead of using absolute floors).
- **Within-key confusion (the oracle gap).** Even with perfect per-key
  naming, ~19% of speech time is misattributed because the online clusterer
  assigns windows of one voice to another voice's key (worst on 7+ person
  calls). This is the diarizer layer itself; candidate ideas: multiple
  anchors per session speaker, overlap-aware embedding exclusion, or
  replacing max(centroid, anchor) with a small per-speaker embedding pool.
  Test any of these in speaker_lab before touching the live path (oracle
  accuracy is the metric to move; it is insensitive to the linking layer).
- **Settings UI checkbox** for `speaker_link_v2` in the Diarization settings
  section (the backend honors the key today; there is no UI control yet).
- **Suggestion popup ranking.** With exclusion removed, suggestions can
  include a profile already linked to another key; consider annotating those
  in the picker ("also matched Speaker 3") rather than hiding them.

---

## Agent API: follow-on ideas

The Agent API (agent_api/ + mcp_server.py, docs/AGENT_API.md) shipped with REST + stdio MCP. Deferred extensions:

- **Streamable-HTTP MCP transport** so remote MCP clients (claude.ai connectors) could attach without spawning a process. Needs auth hardening first since it implies non-loopback exposure.
- **Agent event stream**: an SSE or long-poll feed of app events (recording started/stopped, new segment, summary updated) so agents can react instead of polling `/live`.
- **Trigger AI actions via agents**: let an agent kick off summary regeneration, reanalysis, or chapter generation (currently read-only over AI outputs by design; would need a job/status pattern since those run minutes-long).
- **Speaker-scoped audio clips**: stitch per-speaker WAV excerpts (the per-source Opus tracks exist already) so an agent can pull "everything Alice said".
- **MCP write-guard mode**: per-tool allow/deny toggles in the Agent API settings panel for users who want read-only agents.

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

## ~~macOS: per-source ("mic = Me") capture~~ — done

`capture_audio/mac.py` now mirrors `capture_audio/windows.py`: the per-source helpers (`_open_per_source_writers` / `_close_per_source_writers` / `_encode_per_source_opus` / resume-decode) are byte-identical, and the mixer is the same dual-clocked, always-sum loop that writes the sample-aligned mic-only / desktop-only temp WAVs (encoded to Opus on `stop()`) and enqueues the 5-tuple `(src, mixed, offset, mic_bytes, lb_bytes)`. `mic_is_me_enabled` / `_per_source_active` are wired exactly as on Windows (app.py drives both backends through the same attributes), so source-aware diarization now works on macOS. The only Windows-side code not ported is the `INPUT_DEBUG` verbose tracing, a dev aid that does not affect output. Still wants one end-to-end check on Apple Silicon hardware (SCK loopback + a real mic).

---

## ~~Notes pane: export/import bundling~~ — done

The export zip now bundles `notes_attachments/<file>` for every file in `storage/data/notes/<session_id>/`, gated on the new `notes` checkbox in the export modal. On import the directory is restored under the new session id and the notes Delta has its `/api/sessions/<old>/notes/attachments/` URLs rewritten to the new id, mirroring the existing screenshot-URL rewrite.
