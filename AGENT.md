# Meeting Assistant — Developer Agent Guide

This document is the authoritative reference for AI agents working on this codebase. Read it before making changes.

---

## Repository & Git Workflow

**Read this before your first commit.** Full detail lives in [CONTRIBUTING.md](CONTRIBUTING.md).

The project lives in two repos and they are not equal:

| | Azure DevOps | GitHub |
|---|---|---|
| Repo | `HiggDAC/Meeting-Assistant` | `TyLaneTech/Meeting-Assistant` |
| Role | Source of truth, all development | Read-only public mirror, distribution only |
| Branches | `main` plus short-lived work branches | `main` only |
| Push to it? | Yes, via pull request | **Never** |

`origin` in a dev checkout is Azure DevOps. Merging to `main` fires the
`mirror-to-github` pipeline (`.azuredevops/mirror-to-github.yml`), which
force-pushes `main` and tags to GitHub. GitHub is what `install.sh` / `install.ps1`
clone and what `/api/update/check` pulls from, so an *installed* copy has GitHub as
its `origin` while a *dev* checkout has Azure DevOps. `_UPDATE_REMOTES` in `app.py`
tries `origin` first and falls back to the GitHub URL, which is why both work.

### Hard rules

1. **Never push to the GitHub remote.** The mirror force-pushes, so anything landed
   there directly is erased on the next merge to `main`.
2. **Do not push straight to `main`.** Branch policy requires a pull request and is
   enforced for every contributor. The repo owner holds the `PolicyExempt` permission
   ("Bypass policies when pushing") and is the sole exception. Branch, push, then open a
   pull request in Azure DevOps. Do this even when working as the owner, unless explicitly
   told to push directly.
3. **Squash merge only.** Enforced by policy. The Changelog tab is built from
   `git log` on `main`, so one PR must collapse to one commit or the changelog
   fills with WIP noise. See [Commit Messages](#commit-messages) below.
4. **Never commit `.env`, API keys, or anything under `storage/`.** All gitignored.
5. **Do not remove the bundled HuggingFace token** from `core/config.py`. It is
   deliberate and the app depends on it.

### Branch naming

`feature/<short-name>` or `fix/<short-name>`, branched off `main`, deleted when the
pull request completes.

---

## Architecture Overview

Meeting Assistant is a **Flask web app** that runs locally on Windows and macOS (Apple Silicon). It captures system audio and microphone input, transcribes speech using Whisper (faster-whisper on Windows/CUDA, mlx-whisper on macOS/Metal), performs speaker diarization with pyannote, and uses Claude (via the Anthropic API) or OpenAI for live summarization and Q&A.

The app is a **single-page application**. Flask serves an HTML template; all subsequent communication is via REST API calls and **Server-Sent Events (SSE)** for real-time streaming.

```
Browser (3-column SPA)
    │  REST + SSE
    ▼
Flask (app.py) ──► core/ (log, config, paths, settings, network, compute_device, storage)
    │
    ├── ml.transcriber              ──► ml.transcriber_engine (faster-whisper / mlx-whisper)
    │       └── ml.diarizer          (streaming pyannote)
    ├── ml.batch_transcriber         (full-file reanalysis pipeline)
    ├── ml.speaker_db                (cross-session voice library)
    ├── capture_audio                ──► capture_audio.windows | capture_audio.mac
    │       └── capture_audio.wav_writer
    ├── capture_video                ──► capture_video.windows | capture_video.mac
    │       └── capture_video.ffmpeg_util
    ├── ai.assistant                 (Anthropic / OpenAI streaming summary + chat)
    └── ui_desktop.tray              (main thread only)
            └── ui_desktop.notifications
```

Platform-specific backends live behind the `capture_audio` and `capture_video` package dispatchers (`__init__.py`). Callers always do `from capture_audio import AudioCapture` and stay platform-agnostic.

---

## File Map

Code is organized into seven packages plus root-level entry points (`app.py`, `launch.py`).

### Entry points (root)

| File | Responsibility |
|---|---|
| `app.py` | Flask server, all API routes, session orchestration, SSE dispatch. Configured with `template_folder="ui_web/templates"`, `static_folder="ui_web/static"`. |
| `launch.py` | Setup automation — venv creation, GPU/Metal probe, dependency install (picks `requirements-macos.txt` on darwin), model predownload, app launch. |
| `launch.bat` / `launch.command` | OS-specific shells that invoke `launch.py`. |
| `mcp_server.py` | Stdio MCP server for external AI agents (Claude Desktop/Code, Codex). Pure stdlib, zero project imports — proxies to the Agent REST API over localhost HTTP, so it works with any Python and never loads app modules. |
| `watchdog.py` | External freeze watchdog, opt-in via `freeze_watchdog_enabled`. Polls `/api/status` from outside the process and reads `<data>/heartbeat.json` to tell a frozen or crashed app (restart) from a clean quit (leave alone). Started by `launch.py`, never by the app. |
| `launch_hidden.vbs` | Tray-only Windows start: runs `launch.bat` with no console and sends the startup output to `storage/logs/launch-startup-<stamp>.log`, one file per launch, pruned after a week. `_relaunch_app()` prefers it for restarts and updates. |
| `app_launcher.vbs` | Click-to-open launcher for pins and shortcuts: opens the app window if the server is already up, otherwise starts it hidden and opens the window once it answers. |
| `tests/` | pytest suite, no hardware needed: unit tests for the pure modules plus static assertions over the templates, scripts and stylesheets. `python -m pytest tests -q` runs in about ten seconds. See CONTRIBUTING.md. |

### `core/` — foundational utilities

| File | Responsibility |
|---|---|
| `core/log.py` | Structured console logging + capture: ring buffer and rotating `<data>/logs/app.log`, queryable via `log.recent()` and the Agent API |
| `core/config.py` | `.env` file management, bundled HF token, API key status |
| `core/paths.py` | Data directory resolution (configurable via `.data_location` pointer) |
| `core/settings.py` | JSON user preferences (device selections, model choices, UI prefs) |
| `core/network.py` | HuggingFace token + pipeline download helpers |
| `core/compute_device.py` | `best_torch_device()` — single source of truth for CUDA/MPS/CPU choice |
| `core/storage.py` | SQLite CRUD — sessions, segments, summaries, chat, speaker labels, calendar matches, expected speaker counts |
| `core/attention.py` | The Needs attention queue: recordings whose speakers are still unnamed, against the calendar's expected count |
| `core/browser.py` | Opens the UI as an app window (Chrome/Edge `--app=` or the installed PWA) instead of a browser tab |
| `core/calendar_feed.py` | Published-calendar (ICS) download, RRULE expansion, time zones, attendee parsing, URL masking |
| `core/calendar_sync.py` | Matches recordings to calendar instances, stores the match and expected speaker count, feeds attendee candidates to the Speakers Cleanup tab, runs the hourly refresh |
| `core/calendar_events_api.py` | `/api/calendar/events` blueprint behind the Calendar view |
| `core/dashboard_api.py` | `/api/dashboard` blueprint: the Home dashboard's stats, charts and people queries |
| `core/heartbeat.py` | `<data>/heartbeat.json`, refreshed while alive and removed on a clean quit; read by `watchdog.py` |
| `core/icons.py`, `core/icons_api.py` | Icon sets (Settings > Icons): per-state slots, tinting, PNG/ICO rendering, custom uploads, the tray and shortcut icons; `/api/icons/*` and the web manifest |
| `core/obsidian_export.py` | Optional Markdown export of finished meetings into an Obsidian vault |
| `core/recording_request.py` | Start-recording coordinator: offers the start to an already-open window over SSE, then the installed PWA, then a fresh `?autostart=1` window |
| `core/shortcut.py` | Windows `.lnk` helpers shared by the launcher and the icon sync; only ever touches shortcuts that launch this checkout |

### `capture_audio/` — audio input

| File | Responsibility |
|---|---|
| `capture_audio/__init__.py` | Platform dispatcher — re-exports `AudioCapture`, `enumerate_audio_devices`, `auto_detect_devices` based on `sys.platform`. |
| `capture_audio/windows.py` | WASAPI loopback + microphone capture, mixer, RMS levels, AGC, FFmpeg mic ingestion. |
| `capture_audio/mac.py` | ScreenCaptureKit loopback (system audio) + CoreAudio mic capture via sounddevice. |
| `capture_audio/mac_bootstrap.py` | Retired no-op shim. ScreenCaptureKit needs no virtual driver, aggregate device, or output reroute, so the BlackHole machinery this held is gone. Kept only so a stale import gets no-ops instead of an `ImportError`. |
| `capture_audio/wav_writer.py` | Minimal WAV file writer with sample-offset tracking; append mode walks the RIFF chunks so an ffmpeg-written header survives |
| `capture_audio/render_probe.py` | Out-of-process Core Audio probe (pycaw/comtypes, run only as a subprocess) reporting which output endpoint is actually playing; used by the opt-in follow-output watchdog |
| `capture_audio/params.py` | Default audio parameters, AGC settings, recording presets |
| `capture_audio/audio/test_sample.mp3` | Tone played during input-device auto-detection (read directly off disk by `windows.py`/`mac.py`) |
| `capture_audio/audio/complete.mp3` | Recording-complete chime |

### `capture_video/` — screen recording + media editing

| File | Responsibility |
|---|---|
| `capture_video/__init__.py` | Platform dispatcher. Also home of `PRESETS`, `H264_PRESETS`, `extract_frame()` (cross-platform). |
| `capture_video/windows.py` | gdigrab capture, DPI-aware `EnumDisplayMonitors`, kill_stale_ffmpeg |
| `capture_video/mac.py` | AVFoundation screen capture |
| `capture_video/ffmpeg_util.py` | `find_ffmpeg()`, `download_ffmpeg()`, `_LOCAL_FFMPEG` constants |
| `capture_video/media_edit.py` | Trim, split, concatenate audio/video files |

### `ml/` — transcription, diarization, speakers

| File | Responsibility |
|---|---|
| `ml/transcriber.py` | Streaming Whisper — model management, audio queue consumer, pause-based flush. `WHISPER_PRESETS` filtered by `sys.platform`. |
| `ml/transcriber_engine.py` | Engine factory — `make_engine()` returns `FasterWhisperEngine` (CUDA) or `MLXWhisperEngine` (Metal). |
| `ml/batch_transcriber.py` | Reanalysis pipeline — full-file pyannote diarization + batched Whisper transcription. |
| `ml/diarizer.py` | pyannote streaming diarization, speaker profile tracking, embedding merges |
| `ml/speaker_db.py` | Voice library: embeddings, centroids, cross-session speaker matching |
| `ml/text_embeddings.py` | Text embedding helpers (chat memory / RAG) |
| `ml/eval_diarization.py` | Standalone diarization evaluation script |
| `ml/optimize_diarization.py` | Hyperparameter tuning script |

### `ai/` — LLM assistant

| File | Responsibility |
|---|---|
| `ai/assistant.py` | Anthropic/OpenAI integration — streaming summary, incremental patch, Q&A, title generation. Per-tool model selection and prompt caching. |
| `ai/speaker_relabel.py` | Bulk speaker relabel agent for the global chat: plans, confirms and applies speaker reassignments (`plan_speaker_relabel`, `apply_speaker_relabel`, `cancel_speaker_relabel`). The only chat tools that write. |

### `agent_api/` — external agent interface (REST)

| File | Responsibility |
|---|---|
| `agent_api/rest.py` | Flask blueprint mounted at `/api/agent/v1` — meetings, transcripts (5 formats), frames, audio clips, hybrid search, folders, speakers, settings, logs, live tail, opt-in recording control. Auth gate (`agent_api_enabled` / `agent_api_token` settings). |
| `agent_api/helpers.py` | Pure converters: timestamp parsing, transcript renderers (json/text/markdown/srt/vtt), Quill Delta → markdown/text, note-append ops, media probing, settings schema/validation. |
| `agent_api/context.py` | `AgentContext` — callables injected by app.py (status payload, `_scope_filters`, `_describe_session`, model/AI snapshots, SSE push) so the blueprint never imports app.py. |
| `agent_api/openapi.py` | OpenAPI 3.1 spec builder, served at `/api/agent/v1/openapi.json`. |
| `docs/AGENT_API.md` | The agent-facing guide; served live (base URL substituted) at `GET /api/agent/v1/docs`. Keep it in sync when adding endpoints/tools. |

### `ui_desktop/` — OS integration

| File | Responsibility |
|---|---|
| `ui_desktop/tray.py` | System tray icon (pystray + Pillow), dynamic menu, status indicators. Loads icons from `ui_web/static/images/`. |
| `ui_desktop/notifications.py` | Toast/banner notifications — `winotify` on Windows, `osascript` on macOS. |

### `ui_web/` — Flask web UI assets

| File | Responsibility |
|---|---|
| `ui_web/templates/index.html` | The one app shell: header, sidebar, the view containers and every dialog. Served for `/`, `/session/<id>`, `/calendar`, `/attention` and `/speakers`; the client router picks the view. |
| `ui_web/templates/_header.html`, `_sidebar.html`, `_settings.html`, `_ask_rail.html` | Shell partials: header controls, the sidebar (page links, recordings, footer), the Settings dialog, the global "Ask your meetings" rail |
| `ui_web/templates/_view_home.html`, `_view_calendar.html`, `_view_attention.html`, `_view_speakers.html` | The view bodies the router swaps between without a reload |
| `ui_web/static/app.js` | Router (`Views`, `navigateTo`), the `AppData` cache, SSE handling, the session view, Settings, preferences |
| `ui_web/static/home.js`, `calendar.js`, `attention.js` | Renderers for the Home dashboard, the Calendar view and the Needs attention view |
| `ui_web/static/ui-dialog.js`, `ui-combobox.js` | `uiToast`, `uiConfirm`, `uiAlert`, `uiPrompt` (native dialogs are not used anywhere) and the combobox widget |
| `ui_web/static/style.css`, `home.css`, `calendar.css` | Styles; the two view sheets belong to their views |
| `ui_web/static/images/` | Logo, the bundled icon set under `sets/wave/`, fontAwesome assets. Custom sets live under `<data>/icons/`. |

### `storage/` — runtime data and bundled binaries (not a Python package)

`storage/` is fully gitignored (`**/storage/` in `.gitignore`) and auto-created by its consumers on first use — it does not need to exist at checkout. Files inside are runtime/cache, not code.

**Auto-migration on update:** `launch.py` runs `_migrate_legacy_layout()` early in `main()` (right after `_ensure_venv()`). On users who pull a version with the storage/ layout, any pre-existing `<project>/{tools,models}/` get moved into `storage/` automatically. `<project>/data/` is moved as well **only if it's still at the default location** (no `.data_location` pointer, or pointer points exactly at `<project>/data/`). After moving the default-located data folder, the pointer file is deleted since the new default IS `storage/data/`. If the pointer redirects to a relocated custom dir, both the data folder and the pointer are left alone. Migration is idempotent and silent on no-op runs; collisions (target already exists) are skipped without clobber.

| Path | Responsibility |
|---|---|
| `storage/data/` | SQLite DB, `settings.json`, recorded WAV/video files, attachments, screenshots, voice profiles, backups, tmp. **Location is overridable** — the `.data_location` pointer file at the project root can redirect this to any absolute path (see `core/paths.py`). |
| `storage/models/` | HuggingFace model cache (`HF_HOME` is pointed here at import time by `core/config.py`). |
| `storage/tools/` | Bundled binaries — currently just `ffmpeg(.exe)`, auto-downloaded by `launch.py` if not on PATH. |

### Import conventions

- Always import packages via their qualified path: `from core import log`, `from ml.transcriber import Transcriber`, `from capture_audio import AudioCapture`.
- Never reach into a platform backend directly (`from capture_audio.windows import ...`) from app code — go through the dispatcher so macOS/Windows behavior stays symmetric.
- New shared utilities default to `core/`. New ML/AI features split between `ml/` (models, embeddings) and `ai/` (LLM-facing prompts, summarization, chat).

---

## Threading Model

**Critical:** pystray requires the Win32 message pump on the **main thread**. Everything else is a daemon thread.

```
Main thread:    MeetingTray.run()  →  blocks until quit
Daemon threads: Flask server
                Transcriber._loop()
                AudioCapture mixer loop
                _level_push_loop()  (12 fps SSE push)
                Model/diarizer loading (on startup, on change)
                Chat/summary generation
                Recording cleanup
```

**Thread safety rules:**
- All `_state` reads/writes → acquire `_state_lock`
- All `_client_queues` reads/writes → acquire `_cq_lock`
- `core/settings.py` → has its own internal `_lock`
- `core/storage.py` → uses thread-local SQLite connections via `_conn()` context manager
- Tray refresh (`_refresh_tray()`) is safe to call from any thread (wrapped in try/except)

Never call `_state` without the lock. Never do slow I/O (model loading, DB writes, HTTP) while holding the lock — snapshot the values you need first, release the lock, then do the work.

---

## State Management

### Server state (`_state` in app.py)

In-memory, protected by `_state_lock`. Holds the **current live session** only.

```python
_state = {
    "session_id": str | None,
    "is_recording": bool,
    "segments": list[dict],        # {text, source, start_time, end_time}
    "summary": str,
    "chat_history": list[dict],    # {role, content}
    "pending_segments": int,       # since last auto-summary
    "summarized_seg_count": int,   # segments included in current summary
    "audio_capture": AudioCapture | None,
    "test_capture": AudioCapture | None,
    "is_testing": bool,
    "model_ready": bool,
    "model_info": str,
    "diarizer_ready": bool,
    "speaker_labels": dict,        # speaker_key → display name
    "custom_prompt": str,
}
```

Past sessions are stored in SQLite and loaded on demand by the frontend.

### Client state (`state` in app.js)

```javascript
const state = {
  sessionId, isRecording, isTesting, isViewingPast,
  aiChatBusy, modelInfo, chatCursor, chatBuffer,
  summaryBuffer, summaryCursor, sidebarOpen
}
```

`isViewingPast` is critical — when `true`, live transcript appends are suppressed so loading a past session doesn't get polluted by ongoing transcription.

### User preferences (`_prefs` in app.js, `data/settings.json` on server)

Loaded from `GET /api/preferences` on page load. Writes are debounced 400ms via `savePref(key, value)`, which sends only the keys changed since the last flush and flushes on `pagehide`. It must never send the whole `_prefs` object: every open page holds the copy it loaded, and an older copy written back undoes changes made elsewhere (enabling the calendar in one tab and resizing the sidebar in another switched the calendar off again, 2026-09-05). The server-side module is `core/settings.py`; `PUT /api/preferences` merges. `calendar_ics_url` is a credential with its own route (`POST /api/calendar/link`) and is stripped from every preferences write.

---

## SSE Event System

The browser connects to `GET /api/events` and receives a stream. All real-time updates flow through here.

**Pushing an event (server):**
```python
_push("event_name", {"key": "value"})
```

**Receiving an event (browser):**
```javascript
es.addEventListener("event_name", e => {
    const data = JSON.parse(e.data);
    // handle it
});
```

### Event catalog

| Event | Payload | Meaning |
|---|---|---|
| `status` | `recording`, `session_id`, `model_ready`, `model_info`, `diarizer_ready` | Any state change |
| `transcript` | `text`, `source`, `session_id`, `start_time`, `end_time`, `seg_id` | New segment transcribed |
| `summary_start` | `session_id` | First summary beginning to stream |
| `summary_chunk` | `text` | Token from first summary stream |
| `summary_done` | _(empty)_ | First summary complete |
| `summary_replace` | `content`, `session_id` | Incremental patch complete (full replacement) |
| `chat_start` | `request_id`, `question` | Chat response starting |
| `chat_chunk` | `request_id`, `text` | Chat response token |
| `chat_done` | `request_id` | Chat response complete |
| `audio_level` | `loopback`, `mic`, `has_mic` | RMS levels for visualizer |
| `audio_test_status` | `testing` | Test mode toggled |
| `session_title` | `session_id`, `title` | Auto-generated title ready |
| `speaker_label` | `session_id`, `speaker_key`, `name` | Speaker rename broadcast |
| `chapters_updated` | `session_id`, `chapters` | Chapter list changed (generated, edited, added, or deleted) |
| `chapters_busy` | `busy`, `session_id` | Chapter generation started/finished |
| `calendar_refresh_done` | refresh summary (`matched`, `cleared`, `updated`, ...) | The published calendar was re-read and recordings re-matched |
| `calendar_match_changed` | `session_id`, `confirmed` | A recording's calendar match was confirmed or cleared |
| `attention_changed` | `session_id` | The Needs attention queue changed for a recording |
| `capture_alert` | `kind`, `message`, ... | The desktop capture has produced no signal (loopback silence watchdog) |
| `smart_cleanup_done` | `session_id`, ... | A smart cleanup reanalysis finished |
| `recording_command` | `nonce`, `source`, `reason` | A start-recording request offered to open windows; one acks it via `/api/recording/ack_command` (`core/recording_request.py`) |

**Adding a new event:** Call `_push("new_event", {...})` anywhere in app.py. Add an `es.addEventListener("new_event", ...)` in app.js.

---

## API Design Patterns

All API routes follow these conventions:

- `GET` routes return `jsonify(data)` — never raise, return error dict with 4xx status
- `POST`/`PATCH` routes accept `request.get_json(silent=True) or {}` — never crash on missing body
- Error responses: `return jsonify({"error": "message"}), 4xx`
- Success responses: `return jsonify({"ok": True, ...})`
- Slow operations (model loading, AI calls) always dispatched to a `daemon=True` thread — routes return immediately
- Routes that must not run during recording check `_state["is_recording"]` under lock first

### Existing route groups

| Prefix | Purpose |
|---|---|
| `/api/events` | SSE stream |
| `/api/status` | Current app state |
| `/api/audio/*` | Device enumeration, test start/stop, mic chunk injection |
| `/api/recording/*` | Start/stop recording |
| `/api/sessions/*` | CRUD + audio playback |
| `/api/segments/*` | Per-segment label overrides |
| `/api/summarize` | Manual summary trigger |
| `/api/custom-prompt` | Session-level summary context |
| `/api/chapters/*` | Chapter generation + built-in default prompt |
| `/api/sessions/<id>/chapters*` | Chapter CRUD + per-session chapters prompt |
| `/api/chat` | Q&A streaming |
| `/api/models` | Whisper/diarizer config |
| `/api/models/whisper` | Change Whisper preset |
| `/api/models/diarizer` | Change diarizer device |
| `/api/settings/keys` | API key management |
| `/api/settings/status` | Combined setup status |
| `/api/preferences` | User preferences (JSON settings) |
| `/api/shutdown` | Graceful exit |
| `/api/restart` | Graceful stop, then relaunch through the launcher (`_relaunch_app()`) |
| `/api/update/check`, `/api/update/apply` | Self-update: fetch `main`, compare, pull, relaunch |
| `/`, `/session/<id>`, `/calendar`, `/attention`, `/speakers` | The one app shell (`index.html`); the client router picks the view |
| `/api/dashboard` | Home dashboard data (`core/dashboard_api.py`) |
| `/api/calendar/*`, `/api/sessions/<id>/calendar_match` | Published-calendar status, link, test, refresh, events; the per-recording match |
| `/api/attention/summary`, `/api/sessions/<id>/expected_speakers` | Needs attention queue; the expected speaker count |
| `/api/sessions/<id>/resolution_candidates`, `/api/sessions/<id>/smart_cleanup` | Calendar attendees offered in the Cleanup picker; the smart cleanup reanalysis |
| `/api/speakers/relabel/*`, `PATCH /api/sessions/<id>/speakers` | Bulk relabel agent confirm/cancel; speaker reassignment |
| `/api/recording/request_start`, `/api/recording/ack_command` | Start coordinator (`core/recording_request.py`) |
| `/api/icons/*`, `/manifest.webmanifest` | Icon sets (`core/icons_api.py`) |
| `/api/obsidian/*` | Optional Obsidian export |
| `/api/agent/v1/*` | Agent API blueprint (`agent_api/rest.py`) — REST interface for external AI agents; self-documenting via `/docs` + `/openapi.json` |

---

## Adding Features

### New user preference

1. Add the key + default to `DEFAULTS` in `core/settings.py`
2. Save it from JS: `savePref('my_pref', value)` — this debounces and PUTs to `/api/preferences`
3. Read it from JS: `_prefs.my_pref` (available after `loadPreferences()` resolves)
4. If it needs to be restored on startup, read it in `loadPreferences().then(...)` in the init block

### New API endpoint

```python
@app.route("/api/my-feature", methods=["POST"])
def my_feature():
    data = request.get_json(silent=True) or {}
    with _state_lock:
        if _state["is_recording"]:
            return jsonify({"error": "Not allowed while recording"}), 400
    # ... do work ...
    return jsonify({"ok": True})
```

### New SSE event (server → browser)

1. In app.py: `_push("my_event", {"field": value})`
2. In app.js: Add `es.addEventListener("my_event", e => { ... })`

### New storage (persistent data)

Add to `core/storage.py`. Use the `_conn()` context manager — it auto-commits on exit and handles rollback on error. SQLite is thread-safe here because each thread gets its own connection via `threading.local()`.

### New model configuration option

1. Add a new preset dict to `WHISPER_PRESETS` or `DIARIZER_OPTIONS` in `ml/transcriber.py`. Whisper presets carry a `platforms` tuple (e.g. `("win32", "linux")` or `("darwin",)`) so each OS only sees backends it can actually run.
2. The `/api/models` endpoint returns these automatically
3. The frontend renders them automatically in the model config dropdowns

### New Agent API endpoint + MCP tool

1. Add the route to `agent_api/rest.py`. Storage/paths/settings are imported directly; anything app-owned (live `_state`, shared search helpers) goes through `AgentContext` — extend the dataclass in `agent_api/context.py` and the `register_agent_api(...)` call near the bottom of app.py if you need a new capability.
2. Register the path in `agent_api/openapi.py` (`build_spec`).
3. Mirror it as a tool in `mcp_server.py`: add a `_tool(...)` entry to `TOOLS` and a dispatch branch in `call_tool()`. Keep `mcp_server.py` stdlib-only — it must never import project modules (it runs under whatever Python the MCP client spawns).
4. Document it in `docs/AGENT_API.md` (served at `/api/agent/v1/docs`).
5. Conventions: JSON errors as `{"error": ...}` with 4xx status; timestamps through `helpers.parse_timestamp` (accepts seconds or `M:SS`); meeting-scoped listings accept the shared folder/date/speaker filters via `_filters_input` + `ctx.scope_filters`; nothing in the Agent API may delete user data.

### Log capture (core/log.py)

`log.info/warn/error` still print to the console, but every line is also kept in an in-memory ring buffer (4000 entries) and appended to `<data>/logs/app.log` (5 MB rotation, 3 files kept). `GET /api/agent/v1/system/logs` reads the ring; use `log.recent()` / `log.log_files()` / `log.read_log_file()` from code. File-write failures are swallowed — logging must never crash the app.

---

## Key Behaviors to Preserve

**Pause-based Whisper flushing:** The transcriber uses RMS energy to detect pauses. Flush occurs when: `(buffer ≥ 0.5s AND silence ≥ 0.4s) OR buffer ≥ 8s`. The 8s hard cap is intentionally short to prevent large audio chunks from causing Whisper inference spikes that compete with system audio playback. Do not raise `MAX_BUFFER_SECONDS` significantly — 30s chunks were the original value and caused audio buffering issues under load. `beam_size=2` is also intentional for speed; do not raise it to 5.

**Incremental summary patching:** After the first summary, `ai.patch_summary()` sends only the `new_transcript` (segments since last summary) and asks Claude to return a JSON patch of only changed sections. This avoids rewriting the whole summary and keeps it stable. The `summarized_seg_count` tracks the split point.

**Auto-chapters cadence (full replace):** Chapters are regenerated wholesale via `storage.replace_chapters()`, not patched. During a live recording `_run_chapters(is_auto=True)` fires only when BOTH `pending_chapter_segments >= AUTO_CHAPTERS_EVERY` (12) AND `AUTO_CHAPTERS_MIN_GAP_SEC` (90s) have elapsed — this dual gate is what keeps chapters from being added too often. Auto-runs pass the existing chapters to the model to keep early ones stable and never wipe to an empty list; the manual `/api/chapters/generate` regenerate is authoritative and may clear. AI timestamps are snapped to the nearest transcript segment start (`_prepare_chapters`), and chapters feed the summary + chat via `meta["chapters"]` rendered in `_format_meta_block`. Serialized by `_chapters_lock`.

**Speaker label merging:** When a user renames two speakers to the same display name, `_state["speaker_labels"]` is checked for collision and `diarizer.merge_speakers(keep, merge)` is called to combine their embedding pools. This should always happen atomically under `_state_lock`.

**Recording cleanup is always async:** `stop_recording()` returns immediately and dispatches `_cleanup()` to a daemon thread. This thread stops streams, finalizes WAV, ends the DB session, and runs auto-title. Never move this back to the request handler — the operations can take up to 12s (thread join timeout).

**Audio stream graveyard:** `capture_audio/windows.py` retires closed streams to a `_stream_graveyard` list rather than deleting them immediately. This avoids a PortAudio bug on WASAPI loopback that triggers `ExitProcess()` if a stream is cleaned up too early. Don't remove this pattern.

**CUDA DLL registration:** `ml/transcriber.py` registers nvidia pip-package DLL directories at import time (before ctranslate2 loads). This must happen before any CUDA library is imported. Do not move this code.

**macOS loopback watchdog:** `_sck_watchdog_loop()` supervises the ScreenCaptureKit stream and restarts it through `_restart_sck_loopback()` if it dies mid-recording. SCK delivers sample buffers continuously even during silence, so a gap longer than `_SCK_WATCHDOG_TIMEOUT` (5 s) means a dead stream (TCC permission revoked, display reconfigured), not a quiet room. Do not "optimize" that check away by treating silence as normal. Restart backoff is `_SCK_RESTART_BACKOFF` = (0.5, 1.0, 2.0) seconds; if all three attempts fail, `loopback_error` is set and the recording continues mic-only rather than dying.

**Desktop device choice (Windows):** `_resolve_loopback()` uses the device the user selected: the saved index when it still carries the saved name, otherwise the live device with that name, and the default output only when the saved device is gone. Following the default output, at start and mid-recording through `render_probe.py`, is behind `loopback_follow_output` (default off). Windows keeps two output roles and PortAudio reports only one, so "the default output" is routinely not the device the user hears; following it captured an idle endpoint for a whole call (2026-09-05). The word-level name tier ignores the `[Loopback]` suffix, which every loopback device carries.

**Loopback silence watchdog:** `_loopback_silence_watchdog()` raises `capture_alert` when the desktop capture never produces signal or drops out. It only ever switches devices when following is on and the probe shows a different endpoint actually playing; silence alone never moves the capture.

**Preference writes are partial:** `savePref()` sends only the changed keys (see User preferences). Do not reintroduce a whole-object `PUT`.

**WAV append walks the RIFF chunks:** `WavWriter(append=True)` locates the data chunk instead of patching offset 40, and the resume path decodes Opus parts with `-fflags +bitexact`. Pause/resume with per-source tracks corrupted both tracks without this.

**Console logging never raises:** `core/log.py` reconfigures stdout/stderr with `errors="replace"` and echoes through `_echo()`. Under `launch_hidden.vbs` stdout is a cp1252 file, and a `→` in a log line used to raise inside the screen recorder at record start. `launch.py` does the same for its own output.

**Shortcut ownership:** `launch.py` only rewrites a Start Menu shortcut that already launches this checkout (or when none exists), and `core/icons.py` only re-icons shortcuts that do. A second clone or a git worktree must never take over the user's shortcut while the other checkout still exists.

**One launcher log per launch:** `launch_hidden.vbs` names its redirect target with a timestamp. `cmd` opens a redirect target without write sharing, so a fixed name could not be reopened while the chain being replaced (an in-app restart or update) still held it, and the second launcher died before running anything: every restart from a hidden-launched app silently never came back (2026-09-05). `launch.py` always prints how the app exited so a quiet exit leaves a trace.

**Speakers dialog lands on Cleanup:** the Resolve tab was folded into Cleanup, whose picker lists the calendar invite's attendees ahead of the Voice Library. Every entry point (post-recording auto-open, `?speakers=cleanup`, the Home, Needs attention and Calendar buttons) opens Cleanup.

**Sidebar page links are preferences:** `sidebar_nav_items` and `sidebar_nav_compact` drive `applySidebarNavPrefs()`, which moves the nav anchors (not copies) into the brand row when folded, so ids and the router's current-page marking keep working. The collapsed rail always shows the icons and pins Settings and the status dot to its bottom.

---

## Platform Notes

The app supports Windows (CUDA) and macOS Apple Silicon (Metal/MPS). Platform branches live behind the `capture_audio` and `capture_video` package dispatchers and behind `core.compute_device.best_torch_device()`. Linux is not officially supported but is unblocked at the import level (CPU-only).

### Device selection

`core.compute_device.best_torch_device()` is the single source of truth for accelerator choice and returns `"cuda"`, `"mps"`, or `"cpu"`. Every component (transcriber, diarizer, batch transcriber, app settings layer) consults it. User-saved device strings from another machine are revalidated and auto-fall-back through this same probe — never trust a raw string from `settings.json`.

### Whisper backends

| Platform | Backend | Engine class | Models |
|---|---|---|---|
| Windows / Linux + CUDA | `faster-whisper` (CTranslate2) | `FasterWhisperEngine` | Systran/`faster-whisper-*` |
| macOS Apple Silicon | `mlx-whisper` (Metal) | `MLXWhisperEngine` | `mlx-community/whisper-*-mlx` |

`ml.transcriber_engine.make_engine()` is the factory. `WHISPER_PRESETS` rows in `ml/transcriber.py` carry a `platforms` tuple so the model picker UI only shows backends the current OS can actually run.

### macOS audio architecture

macOS has no WASAPI-style loopback. System audio comes from Apple's
**ScreenCaptureKit** (macOS 13+), the same API Zoom, Loom, and OBS use. SCK captures the
system audio mix directly, so there is no virtual audio driver, no aggregate device, and no
system-output reroute:

```
                 ┌────────────────────────────────┐
System audio ───►│ ScreenCaptureKit SCStream      │──► _loopback_q ──┐
                 │ 48 kHz stereo, always emitting │                  │
                 └────────────────────────────────┘                  ├─► _mixer_loop
                                                                     │   (mono downmix,
Microphone ─────► sounddevice / CoreAudio input ────► _mic_q ────────┘    always sums)
```

`_SCKLoopbackStream` wraps the stream. PyObjC imports are deferred into `start()` so the
module still imports on a machine without ScreenCaptureKit. The stream runs at
`_SCK_SAMPLE_RATE` (48 kHz) and `_SCK_CHANNELS` (2), downmixed to mono in `_mixer_loop`.

The only setup requirement is the **Screen & System Audio Recording** permission under
System Settings > Privacy & Security. Both failure paths in `_SCKLoopbackStream.start()` (a
10 s timeout enumerating shareable content, or an explicit denial) raise with that
instruction.

The BlackHole implementation this replaced is still in git history if it is ever needed.

### macOS gotchas (do not regress)

- **Screen recording permission is cached per process:** macOS grants Screen & System Audio Recording to the running binary. After the user grants it, the app must be restarted once before SCK returns audio. Do not treat the first post-grant failure as a bug.
- **PyObjC imports must stay inside `_SCKLoopbackStream.start()`:** importing `ScreenCaptureKit` at module scope breaks `import capture_audio` on any machine without it. The deferred import is what lets the dispatcher load on older macOS.
- **Loopback is not a CoreAudio device on macOS:** `enumerate_audio_devices()` returns a single synthetic loopback entry, and `auto_detect_devices()` never probes it. Code that assumes loopback has a real device index is Windows-only thinking.
- **Virtual sinks must stay hidden from the mic list:** `_HIDDEN_INPUT_NAME_PARTS` filters BlackHole, Teams, and Zoom virtual devices out of input selection, because picking one feeds system audio back in labelled as mic and corrupts speaker attribution. `aggregate` is deliberately not in that list, since user-built aggregates often wrap a real microphone.
- **ffmpeg arch:** `evermeet.cx/ffmpeg/getrelease/zip` serves x86_64, which runs through Rosetta on Apple Silicon. `download_ffmpeg()` therefore refuses to auto-download on Apple Silicon and raises with a `brew install ffmpeg` instruction instead. `find_ffmpeg()` prefers the project-local copy in `storage/tools/` and otherwise falls back to PATH, so the native arm64 brew binary is picked up on the next launch.

---

## Data Files

```
storage/data/
├── meetings.db       # SQLite — sessions, segments, summaries, chat, speaker labels
├── settings.json     # User preferences (auto-created, human-readable JSON)
├── audio/
│   └── <session_id>.wav    # Recorded audio per session
├── video/
│   └── <session_id>.mp4    # Recorded screen captures per session
├── attachments/             # User-uploaded files attached to sessions
├── screenshots/             # Captured frames pushed to chat context
├── audio_profiles/          # Voice library: per-speaker centroids and embeddings
├── backups/                 # Automatic DB backups
└── tmp/                     # Scratch directory for in-progress work
```

All data files are created automatically. `meetings.db` is initialized by `core.storage.init_db()` on startup; the rest are created on first use. The data root itself is configurable via `.data_location` (see `core/paths.py`); it defaults to `storage/data/` next to `app.py`.

### SQLite schema

```sql
sessions (id TEXT PK, title TEXT, started_at TEXT, ended_at TEXT)
transcript_segments (id INT PK, session_id TEXT, text TEXT, source TEXT,
                     start_time REAL, end_time REAL, created_at TEXT, label_override TEXT)
summaries (id INT PK, session_id TEXT, content TEXT, created_at TEXT)
chapters (id INT PK, session_id TEXT, start_time REAL, title TEXT, created_at TEXT)
chat_messages (id INT PK, session_id TEXT, role TEXT, content TEXT, created_at TEXT)
speaker_labels (session_id TEXT, speaker_key TEXT, name TEXT, PRIMARY KEY (session_id, speaker_key))
```

**Live migrations** run at startup — missing columns are added automatically. When extending the schema, add a migration in `storage.init_db()` using the existing `_add_column_if_missing()` pattern.

---

## Frontend Conventions

**Markdown rendering pipeline** (for summary and chat):
1. `renderMd(text)` → marked.js (gfm + breaks)
2. `highlightCode(container)` → highlight.js on code blocks
3. `linkifyTimestamps(container)` → wraps `[M:SS]` in clickable links

Always apply all three steps when rendering AI-generated content. Do not skip linkification — users rely on timestamp links to navigate audio playback.

**Escaping:** Always use `escapeHtml()` before inserting user-provided strings into innerHTML. Speaker names, session titles, and chat questions all need escaping.

**`isViewingPast` guard:** Before appending live data to the transcript, check `state.isViewingPast`. When `true`, the user is reviewing a historical session — suppress live appends to avoid corrupting the view.

**Preferences loading order:** `loadPreferences()` must resolve before calling `loadAudioDevices()` or `loadModelConfig()`, because those functions read `_prefs` to restore saved selections. This is enforced in the init block:

```javascript
loadPreferences().then(() => {
    loadAudioDevices();
    loadModelConfig();
});
```

**Debounced saves:** Use the 400ms debounce in `savePref()` for any preference that changes frequently (device selection, playback speed). Do not call `PUT /api/preferences` directly from event handlers. A handler that saves several related keys at once (`saveCalendarSettings()`, `saveObsidianSettings()`) sends exactly those keys.

**Dialogs and toasts:** use `uiToast({ message, kind })`, `uiConfirm({...})`, `uiAlert`, `uiPrompt` from `ui-dialog.js`. The static tests fail on any native `alert`, `confirm` or `prompt`.

**Views:** each view has a renderer (`home.js`, `calendar.js`, `attention.js`) that reads through `AppData` (a cache invalidated by the SSE events above) and sizes its layout with container queries (`@container view (...)`), never viewport media queries, because the view can be narrower than the window when the Ask rail is docked. Charts are painted at pixel size from the box they sit in and repainted by a `ResizeObserver`.

**Opacity:** the static tests forbid `opacity` outside `:disabled` rules and keyframes. Hide with `hidden`, `visibility`, or `.visually-hidden-input`.

---

## Environment & Configuration

### Required
- `ANTHROPIC_API_KEY` — Claude API key. Validated at startup; app enters setup mode if missing.

### Optional
- `HUGGING_FACE_KEY` — Enables speaker diarization (pyannote models from HuggingFace Hub)
- `PORT` — HTTP server port (default: `6969`)

Keys are stored in `.env` and hot-reloadable — `POST /api/settings/keys` calls `config.save_key()` which writes to `.env` and updates `os.environ`, then `ai.reload_client()` re-instantiates the Anthropic client.

### First-run detection
`config.needs_setup()` returns `True` if `ANTHROPIC_API_KEY` is unset. On first run, the browser opens to `?settings=1` which auto-triggers the settings modal.

---

## Performance Architecture

The audio pipeline is designed to avoid progressive slowdown during long sessions. These patterns exist for specific performance reasons — do not regress them.

**Pre-allocated diarization ring buffer:** `ml/transcriber.py` uses a fixed-size numpy array (`_diar_buf`) for the rolling diarization window instead of `np.concatenate()`. The old approach allocated a new 30-second array (~1.9 MB) on every flush cycle (~62 times/sec), causing severe GC pressure and memory fragmentation over time. The ring buffer writes in-place with zero allocations.

**List-based mixer accumulation:** `capture_audio/windows.py`'s `_mixer_loop()` collects chunks in a Python list (`lb_parts`, `mic_parts`) and only calls `np.concatenate()` once per emit cycle (bounded to a few chunks). The old approach called `np.concatenate()` inside the drain loop — O(n²) copies over many iterations. The mixer also caps internal buffers at 3 seconds to prevent unbounded growth when the downstream transcriber is slow.

**Speaker profile cap and garbage collection:** `ml/diarizer.py` limits speaker profiles to `_MAX_SPEAKERS = 12`. Without this, acoustic noise (coughing, laughter, environmental sounds) gradually creates dozens of phantom speaker profiles, and every new embedding requires an O(n_profiles) cosine similarity scan. Immature profiles (< 5 embeddings) not seen in 5 minutes are automatically pruned by `_cleanup_stale_profiles()`.

**Pre-normalized centroids:** `_SpeakerProfile` stores centroids as unit-normalized vectors. The query embedding is also normalized once per `_resolve()` call. Cosine similarity then reduces to a single `np.dot()` instead of two `np.linalg.norm()` calls plus a division — roughly 3x faster per comparison.

**Backpressure-aware transcription:** When `audio_queue.qsize() > 50` (~1.6s backed up), the transcriber skips diarization for that cycle and falls through to plain Whisper. This circuit-breaker prevents the cascade failure where a slow diarizer causes queue buildup → chunk drops → "no new speech" → apparent freeze. Diarization resumes automatically when the queue drains.

**Do not introduce `np.concatenate()` in hot loops.** If you need to accumulate numpy arrays, use a list of arrays and concatenate once at the end, or use a pre-allocated buffer with index tracking.

---

## Commit Messages

Commits surface to the user via the in-app **Settings → Changelog** tab. The tab parses git history client-side: it splits the body on blank lines into sections, treats the first non-bullet line of each section as a sub-heading, and treats `- ` / `* ` / `• ` lines as bullets (indented continuation lines fold into the preceding bullet). Compose every commit so it reads well there, not just in `git log`.

### Subject — past-tense verb + descriptive user-friendly noun phrases

The first alpha word of the subject drives the icon shown next to the entry. Subjects are written in **past tense** ("Added", "Fixed"), not imperative ("Add", "Fix"):

| Leading verb(s) — past tense | Category | Icon |
|---|---|---|
| `Added`, `Created`, `Built`, `New` | feature | green plus |
| `Fixed`, `Guarded`, `Hardened`, `Self-healed` | fix | red wrench |
| `Updated`, `Improved`, `Enhanced`, `Polished`, `Tightened`, `Tuned`, `Reworked`, `Replaced`, `Switched`, `Made` | improvement | blue up-arrow |
| `Refactored`, `Rewrote`, `Restructured`, `Reorganized`, `Consolidated` | refactor | purple shuffle |
| `Removed`, `Dropped`, `Killed`, `Stripped` | removal | yellow minus |
| anything else | other | neutral dot |

(The categorizer also accepts the imperative forms — `Add`, `Fix`, etc. — so older commits in git history still get the right icon. New commits use past tense.)

The "user-friendly" part comes from the **noun phrases that follow the verb**, not from substituting the verb itself with marketing language. Use phrases the user actually understands — names of features they'll see in the UI, plain descriptors of how the change feels — rather than internal module names or engineering jargon.

| ✓ User-friendly noun phrases | ✗ Internal jargon |
|---|---|
| Added Notes pane, Changelog tab, and folder-aware sidebar filtering | Added `_notesEditor` module, `/api/changelog` route, and `_renderFolderSubtree` filtering |
| Fixed Whisper hallucinations during long meetings | Fixed `_collapse_word_periods` regression in transcriber prompt context |
| Updated OpenAI summaries to use the Responses API | Refactored `_complete_structured` to call `responses.create` |

Multi-feature commits get one subject naming the headline ("Added Notes pane, Changelog tab, and folder-aware sidebar filtering") and split the body into per-area sections.

### Three firm rules

- **Past tense, not imperative.** "Added Notes pane", not "Add Notes pane".
- **No emoji in commit subjects.** Flair from glyphs is off. The categorizer strips leading non-letter chars defensively, but it's not the convention here.
- **No marketing-speak verbs.** `Introducing`, `Meet`, `Presenting`, `Ship`, `Level up`, `Sunset`, etc. overshoot — the user has explicitly said they read as too much flair. Stay with the past-tense engineering verb table above.

### Body — sections separated by blank lines

```
Subject (verb-first, ≤72 chars)

Section heading
- First user-facing change in this area.
- Second change. Continuation lines indent two spaces and are
  folded back into the same bullet by the parser.

Another section heading
- …
```

Section headings are 2-4 words naming the user-visible area (e.g. *Notes pane*, *Sidebar*, *Settings: Changelog tab*). Keep ~6 bullets max per section — split if longer.

### Tone — user-facing, not implementation jargon

The audience is the user reading the Changelog tab. Prefer plain wording over internal terminology. Save technical detail for the second clause if it informs behaviour.

| ✗ Avoid | ✓ Prefer |
|---|---|
| POST to `/api/sessions/<sid>/notes/attachments` | Drop files into the Notes pane |
| Refactor `_renderSidebar` to early-return on `filterActive` | Filtering preserves your folder hierarchy |
| Set `_toolOverrides.summary_provider` from picker click | Pick an AI model per-column from the inline picker |

### Forbidden trailers

**Never** include `Co-Authored-By:` or `🤖 Generated with` lines. This is repo policy on every branch, for every contributor, and it is restated in [CLAUDE.md](CLAUDE.md). The parser strips them defensively, but the rule is: don't write them in the first place.

### Tightening pass

When the user asks for a tighter message, cut in this order:
1. The catch-all "Other" miscellany section.
2. Sub-bullets describing internals the user can't action on (e.g. transcriber-internal mechanisms when the user-visible behaviour is captured by the lead bullet).
3. Wordy clause-chains — split into two short bullets or pick the more important one.

Don't cut: section headings, bullets that describe a *new* user-visible capability, or the verb in the subject line.

### Worked example

```
Added Notes pane, Changelog tab, and folder-aware sidebar filtering

Notes pane
- New rich-text Notes column alongside Transcript, Summary, and Chat.
- Drop or paste images and files anywhere in the pane.
- Drag any image or file from Notes into the Chat panel to attach it
  as context for the AI.

Settings: Changelog tab
- New tab pinned to the bottom of the settings nav. Lists recent
  updates parsed from git history with date headings.
- Cached locally; only rebuilt when you click Refresh or after an
  update is applied.

Sidebar
- Filtering by date now preserves your folder hierarchy. Folders
  with no matching sessions are hidden until the filter clears.
```

The subject names the three headline user-visible features in plain language (`Notes pane`, `Changelog tab`, `folder-aware sidebar filtering`) — descriptive without being marketing-y. Body sub-headings (`Notes pane`, `Settings: Changelog tab`, `Sidebar`) stay neutral so the parser renders them as proper section labels rather than competing with the subject for attention.

---

### Keeping internal work out of the changelog

Every non-merge commit on `main` becomes a user-facing Changelog entry, infrastructure,
CI, docs and tooling included. Those mean nothing to an end user.

Prefix the subject with `[internal]` to suppress one:

```
[internal] Documented pull request and changelog conventions
```

`_build_changelog()` drops any commit whose subject contains the marker, matched
case-insensitively, so it never reaches `/api/changelog` or the tab. Use it for anything a
user cannot see or act on. Do **not** use it to bury a user-facing change you would rather
not explain.

`_CHANGELOG_EXCLUDE_HASHES` sits next to it and drops specific commits by full hash. That
is for damage already merged and no longer rewritable, not a routine tool. Anything added
to it needs a comment saying why the commit is unsalvageable.

---

## Pull Requests

`main` is squash-merge only, so **the squash commit is the changelog entry**. Everything in
[Commit Messages](#commit-messages) applies to it. The trap is that Azure DevOps does not
produce that commit correctly on its own.

### What Azure DevOps prefills, and why it is wrong

Completing a pull request prefills the commit as `Merged PR <n>: <PR title>` followed by the
entire PR description verbatim. Both halves break the Changelog tab:

- **The `Merged PR <n>: ` prefix** defeats `_changelog_category()`, which matches on the first
  word of the subject. `merged` appears in no category table, so the entry falls back to the
  neutral "other" dot instead of its real icon, and users see an internal PR number in a
  user-facing widget.
- **The PR description is markdown.** `_renderChangelogEntry()` assigns subject and body with
  `textContent`, so nothing is ever markdown-rendered: `##`, `**bold**`, and backticks all
  appear literally on screen.

A squash commit has a single parent, so the `--no-merges` flag in `_build_changelog()` does
**not** filter it out. It always reaches users.

### What to do at completion

Replace both fields in the completion dialog. Never accept the prefill.

**Subject.** Exactly what the commit subject would have been. Delete the `Merged PR <n>: `
prefix. Past-tense verb first, user-friendly noun phrases, no PR number, no branch name.

**Body.** The changelog format, not the PR description. `_parseChangelogBody()` in `app.js`
recognises exactly this:

| Input line | Renders as |
|---|---|
| Blank line | Ends the current section |
| First non-blank line of a section | Sub-heading |
| Line starting `- `, `* `, or `• ` | Bullet |
| Indented line following a bullet | Folded into that bullet |
| Non-bullet line after bullets have started | Plain paragraph row |
| `1. `, `##`, `**bold**`, backticks | Literal text. Never use them. |

Numbered lists are **not** bullets: the matcher is `/^[-*•]\s+/`, so `1.` lines become stray
sub-headings.

### Direct pushes bypass this entirely

The repo owner can push straight to `main`. That path never opens the completion dialog, so
there is no `Merged PR <n>: ` prefix to strip and no description to replace: the commit
message you write **is** the changelog entry, verbatim. It is the cleanest route to a good
entry, and it is how every commit before PR 904 was made.

The cost is that nothing squashes for you. Push five commits directly and users see five
changelog entries. Squash locally before pushing, or push one self-contained commit at a
time.

### Two audiences, two documents

| | PR description | Squash commit body |
|---|---|---|
| Read by | Your reviewer | Every end user, in Settings > Changelog |
| Lives for | The life of the pull request | Forever, on `main` |
| Style | Markdown, file paths, technical rationale | Plain text sections and bullets, user language |

Write the description for review. At completion, replace it with the user-facing body. Letting
the description leak into the changelog is the single easiest way to ship a broken entry.

### Worked example

Prefilled by Azure DevOps:

```
Merged PR 912: Fixed speaker drift

## Problem
`_assign_speaker()` compared against a stale centroid after long silences.

## Fix
1. Rebuild the centroid on re-entry
2. Widen the merge window
```

Rewritten before completing:

```
Fixed speakers being renamed part way through long meetings

Speakers
- Kept a speaker's name attached for the whole meeting instead of letting it
  drift onto another voice after a long silence
- Stopped brief crosstalk from creating a duplicate speaker that had to be
  merged back by hand afterwards
```

---

## Common Pitfalls

- **Don't hold `_state_lock` during I/O.** Snapshot values, release the lock, then do DB/network/file operations.
- **Don't call `_push()` while holding `_state_lock`.** `_push` acquires `_cq_lock` and could deadlock if another thread holds `_cq_lock` and tries to acquire `_state_lock`.
- **Don't touch `_state["segments"]` after stop.** The cleanup thread calls `storage.end_session()` asynchronously. Use the snapshot taken at stop time.
- **pystray menu items must be rebuilt, not mutated.** Call `_tray.refresh()` which rebuilds the full menu. Don't try to update individual menu items in place.
- **pyannote is not in requirements.txt.** It's installed separately or pulled in by the HuggingFace pipeline on first diarizer load. Don't add it to requirements.txt — it has complex CUDA-version-dependent dependencies.
- **WAV writer tracks sample offsets.** These are used to compute `start_time`/`end_time` for segments. If you change the audio pipeline, maintain the `sample_offset` tracking in `WavWriter.write()`.
- **SSE queues have a max size of 200.** If a client falls behind (slow browser, many events), old events are silently dropped when the queue fills. Don't rely on SSE for durability — use the DB or REST for historical data.
- **Don't raise `_MAX_SPEAKERS` above ~15.** Each speaker profile adds an O(1) cosine similarity check per embedding. At 30+ profiles the diarizer thread can't keep up with real-time audio, triggering the backpressure cascade described above.
