# Meeting Assistant Agent API

The machine-facing interface to Meeting Assistant. It gives AI agents (Claude
Desktop, Claude Code, Codex, or any script) direct, structured access to
everything the app knows: recorded meetings with diarized transcripts, AI
summaries, chapters, user notes, screen-recording frames, audio, hybrid
search, folders, speakers, app settings, logs, and live recording state.

There are two front doors to the same capability set:

| Door | Best for | Where |
|---|---|---|
| **MCP server** | Claude Desktop, Claude Code, Codex, any MCP client | `mcp_server.py` at the repo root (stdio) |
| **REST API** | curl, scripts, custom integrations | `http://localhost:6969/api/agent/v1` |

Everything runs locally. The web server binds to `127.0.0.1` only and never
accepts remote connections. The MCP server is a thin, dependency-free proxy
to the REST API, so the app must be running for tools to return data.

**If you are an agent reading this with no prior context:** call the MCP tool
`get_started`, or `GET /api/agent/v1/` for the endpoint catalog. Both are
self-describing. The rest of this document is the full reference.

---

## 1. Connecting

### Is the app running?

```bash
curl http://localhost:6969/api/agent/v1/system/health
```

Returns `{"ok": true, "recording": false, ...}`. If nothing answers, the app
is not running: start it with `launch.bat` (Windows) or `launch.command`
(macOS). A non-default port is set via `PORT` in the repo's `.env`.

### Authentication

By default there is none (the server is loopback-only). The user can set a
token in **Settings > Agent API** inside the app, after which every request
needs:

```
Authorization: Bearer <token>
```

Exceptions that stay open: `/`, `/docs`, `/openapi.json`, `/system/health`.
The bundled MCP server picks the token up automatically from the app's
`settings.json`, so MCP clients need no extra configuration.

The API can also be disabled entirely from the same settings panel
(`agent_api_enabled`), in which case every endpoint returns `503`.

---

## 2. MCP setup

**One-click:** open **Settings > Agent API** in the app and press **Run
setup** next to Claude Desktop, Claude Code, or Codex. The app writes the
client's config for you (existing configs are merged and backed up, never
clobbered; Claude Code is registered through its own CLI at user scope).
The same action is scriptable: `POST /api/agent/v1/setup/{claude_desktop|
claude_code|codex}` with no body.

Manual setup follows. `mcp_server.py` is a pure-stdlib stdio MCP server. Any
Python 3.10+ works; the app's venv interpreter is the safest choice. Replace
the paths below with your install location.

**Claude Desktop** (`claude_desktop_config.json`, via Settings > Developer):

```json
{
  "mcpServers": {
    "meeting-assistant": {
      "command": "C:\\Users\\you\\Meeting Assistant\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\you\\Meeting Assistant\\mcp_server.py"]
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add meeting-assistant -- "C:\Users\you\Meeting Assistant\.venv\Scripts\python.exe" "C:\Users\you\Meeting Assistant\mcp_server.py"
```

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.meeting-assistant]
command = "C:\\Users\\you\\Meeting Assistant\\.venv\\Scripts\\python.exe"
args = ["C:\\Users\\you\\Meeting Assistant\\mcp_server.py"]
```

The app's **Settings > Agent API** panel renders these snippets with your
real paths filled in, ready to copy.

**Verify the bridge without an MCP client:**

```bash
python mcp_server.py --selftest
```

**Environment overrides:** `MEETING_ASSISTANT_URL` points the MCP server at a
non-default app URL (e.g. `http://127.0.0.1:7000`).

### MCP tool catalog

| Tool | What it does |
|---|---|
| `get_started` | Orientation: live status, library shape, tool map, workflows. Call first. |
| `list_meetings` | Browse newest-first with folder/date/speaker filters and pagination. |
| `search_meetings` | Hybrid keyword + semantic search; snippets labelled with speaker + time. |
| `search_text` | Exact substring scan (punctuation-safe), incl. summaries/notes/chat. |
| `get_meeting` | One meeting bundle: metadata, summary, chapters, notes, speakers, media. |
| `get_transcript` | Diarized transcript; text/markdown/json/srt/vtt; sliceable + paginated. |
| `export_meeting` | Whole meeting as one markdown doc; optionally saved to a file. |
| `get_frame` | One screen-recording frame at a timestamp, returned as an image. Works live too (`timestamp='now'`). |
| `get_frames` | Up to 8 frames: explicit timestamps or an even sweep of the recording. |
| `get_audio_clip` | Cut a WAV clip (max 15 min) to a local file. |
| `get_meeting_media` | Audio/video tech info, screenshots, note attachments with URLs. |
| `update_meeting` | Rename or move to a folder (never deletes). |
| `append_meeting_notes` | Append a block to the Notes pane (strictly additive). |
| `add_chapter` | Add a titled timestamp marker. |
| `list_folders` / `create_folder` | Folder tree with paths + counts; create new folders. |
| `list_speakers` / `get_speaker_meetings` | Voice-library roster; every meeting a person appears in. |
| `get_ai_chats` | Global Chat conversations (list or one conversation's messages). |
| `get_live_status` | Recording state; live transcript tail with an incremental cursor. |
| `get_app_info` | Version, uptime, models, AI config, library counts, storage usage. |
| `get_logs` | App logs with level/tag/substring filters. |
| `get_settings` / `update_settings` | Read settings + schema; validated writes. |
| `start_recording` / `stop_recording` | Opt-in only (off by default; see section 8). |

The server also exposes this guide as an MCP resource
(`meeting-assistant://docs/agent-guide`).

---

## 3. REST conventions

- Base URL: `http://localhost:6969/api/agent/v1`
- `GET /` is a machine-readable index; `GET /openapi.json` is the OpenAPI 3.1
  spec; `GET /docs` is this document.
- Responses are JSON unless an endpoint serves media (JPEG/WAV) or a text
  format (transcripts, exports, docs). Errors are always
  `{"error": "..."}` with a 4xx/5xx status.
- **Timestamps** in params (`t`, `start`, `end`, `start_time`) accept seconds
  (`90.5`) or clock strings (`1:30`, `01:02:03`). They are on the *meeting
  timeline* (the same clock as transcript `start_time` values).
- **Shared filters** on every meeting-scoped listing/search endpoint, all
  combinable: `folder` (id, partial name, or path like
  `Engineering / Backend`), `include_subfolders` (default true), `speaker`
  (partial participant name), `within_days`, `start_date`, `end_date`
  (ISO dates).
- An ambiguous `folder` value is not guessed: the response carries
  `candidates` so you can retry with an exact id or path.

---

## 4. Endpoint reference

### Discovery and system

| Endpoint | Notes |
|---|---|
| `GET /` | Index: endpoint catalog, docs links, live status. |
| `GET /docs` | This guide (markdown). |
| `GET /openapi.json` | OpenAPI 3.1. |
| `GET /system/health` | Liveness; answers even when the API is disabled. |
| `GET /system/status` | Recording flag, active session, model/diarizer readiness, audio levels. `ml_sleeping: true` means the models were unloaded after idle and reload on the next recording start (`recording_ready` stays true). |
| `GET /system/info` | Version (git), platform, uptime, data folder, models, AI config, counts. |
| `GET /system/stats` | Analytics (talk time, activity, top speakers) + storage usage breakdown. |
| `GET /system/logs` | Ring buffer since app start. Params: `limit`, `level` (info/warn/error), `tag`, `contains`, `after_id` (incremental polling). |
| `GET /system/logs/files` | Persisted rotating log files under `<data>/logs/`. |
| `GET /system/logs/files/{name}` | Tail one file (`?lines=500`). |
| `GET /system/changelog` | Recent app updates from CHANGELOG.md (`?limit=15`): id, date, title, body as markdown, category. |

Log tags you will see: `app`, `recording`, `whisper`, `transcriber`,
`diarizer`, `ai`, `summary`, `storage`, `audio`, `fingerprint`, `settings`,
`tray`, `agent` (this API's own request log).

### Meetings

| Endpoint | Notes |
|---|---|
| `GET /meetings` | Newest first. Shared filters + `limit` (default 50) / `offset`. Items carry `session_id`, title, times, `duration_min`, folder path, speakers, summary preview, `has_audio` / `has_video` / `has_notes`. |
| `GET /meetings/{id}` | Bundle. Default parts: summary, chapters, speakers (talk-time stats), notes (markdown), media flags. `?include=transcript,chat,summary_history` or `?include=all`. |
| `PATCH /meetings/{id}` | Body: `{"title": "...", "folder": "id|name|path|null"}`. |
| `GET /meetings/{id}/transcript` | See section 5. |
| `GET /meetings/{id}/summary` | Latest AI summary + revision count. |
| `GET /meetings/{id}/notes` | Notes as markdown + plain text (+ `?raw=1` for the Quill Delta) and note attachments. |
| `POST /meetings/{id}/notes/append` | Body: `{"text": "...", "heading": "..."}`. Additive only. |
| `GET /meetings/{id}/chapters` | `[{id, start_time, title}]`. |
| `POST /meetings/{id}/chapters` | Body: `{"title": "...", "start_time": 90}`. |
| `GET /meetings/{id}/chat` | Per-meeting AI chat history (tool calls parsed). |
| `GET /meetings/{id}/speakers` | Per-speaker `segment_count`, `talk_seconds`, `word_count`, voice-library link. |
| `GET /meetings/{id}/export` | Whole meeting as markdown (`?format=json` for the raw package; `?save_to_file=1` writes to `<data>/tmp/agent_exports/` and returns the path). |

### Media

| Endpoint | Notes |
|---|---|
| `GET /meetings/{id}/media` | Inventory: WAV duration/rate/size, MP4 duration/size + `video_offset_sec`, screenshots, note attachments (all with URLs). |
| `GET /meetings/{id}/frame` | One JPEG frame. Params: `t` (required; `now` is valid while the meeting records), `width` (160-1920, default 1280), `format` (`jpeg` binary default, `base64`, `data_uri`), `raw` (skip offset handling). The `source` field / `X-Frame-Source` header reports `video`, `live_file`, or `live_screen`. |
| `GET /meetings/{id}/frames` | Batch (JSON, base64, max 12): `at=30,1:30,240` or `count=6` with optional `start`/`end` sweep bounds; `width` default 640. During a live recording the default sweep covers recording start through the live head. |
| `GET /meetings/{id}/audio` | Full WAV (supports HTTP range). |
| `GET /meetings/{id}/audio/clip` | `start` (required), `end` (default +60s, max 15 min), `format=wav|mp3` (mp3 needs ffmpeg). |
| `GET /meetings/{id}/screenshots` | Frames previously captured into AI chat. |
| `GET /meetings/{id}/screenshots/{name}` | One screenshot JPEG. |

**The video offset, explained once:** a screen recording can start later than
the audio (e.g. screen capture toggled on mid-meeting). The app stores that
gap as `video_offset_sec`. Frame endpoints take *meeting-timeline* times (the
ones in transcripts and chapters) and convert internally, so you can pass a
transcript timestamp straight to `/frame?t=`. Pass `raw=1` only if you want
video-file seconds.

**Live frames:** while a meeting is being recorded (with screen recording
on), the frame endpoints read the in-progress recording file directly, so
agents can see the screen mid-meeting. `t=now` returns the current instant;
any earlier timestamp up to the live head works too. If the requested moment
is so fresh it has not been flushed to disk yet, the endpoint falls back to a
screenshot of the recorded display and labels it `source: live_screen`
(historical timestamps never silently degrade to a current screenshot).
`GET /live` reports `live_video: true` plus a ready-made `frame_url` when
this is available.

### Search

| Endpoint | Notes |
|---|---|
| `GET\|POST /search` | `q` + `mode=hybrid|keyword|semantic` (default hybrid). Keyword matches carry up to `max_snippets` snippets, each labelled with the speaker and `start_time`. Semantic results carry `semantic_score`; hybrid fuses both rankings (`score`). Extra params: `match=all|any|phrase`, `limit`, `min_score`. Shared filters apply. |
| `GET /search/text` | `contains=` exact substring (punctuation-safe, case toggle), `scope=` any of `transcript,titles,summaries,notes,chat,global_chat`. Finds what FTS tokenization cannot. |

Choosing a search:

- Exact words someone said, names, project terms: `mode=keyword`.
- Conceptual questions ("meetings about hiring"): `mode=semantic`.
- Unsure: `hybrid` (the default) fuses both.
- Error codes, versions, `snake_case`, partial words, or text in
  summaries/notes/chat: `/search/text`.

Semantic mode needs the embedding model, which loads in the background after
app start; `semantic_ready: false` in responses (or a 503 for
`mode=semantic`) means retry shortly or use keyword mode meanwhile.

### Folders, speakers, chats

| Endpoint | Notes |
|---|---|
| `GET /folders` | Full tree: id, name, parent, path, direct + recursive session counts. |
| `POST /folders` | `{"name": "...", "parent": "id|name|path"}`. |
| `GET /folders/resolve?q=` | Resolve fuzzy folder wording; returns the match or candidates. |
| `GET /speakers` | Voice-library roster with per-speaker session counts and last-seen. |
| `GET /speakers/{id_or_name}/meetings` | All meetings featuring the person (+ `segments_by_speaker`). Ambiguous names return candidates with counts. |
| `GET /chats` | Global Chat conversations (cross-meeting AI chats in the app). |
| `GET /chats/{conversation_id}` | One conversation's messages. |

### Settings

| Endpoint | Notes |
|---|---|
| `GET /settings` | All values (secrets masked) + API-key status + data folder. |
| `GET /settings/schema` | Every key: type, default, description, `locked_while_recording`, `restart_required`. |
| `PATCH /settings` | Body `{"updates": {key: value}}`. Validated. Response reports `applied`, `skipped` (with reasons), `restart_required`. |

Notes:

- `ai_provider` / `ai_model` changes apply immediately (the AI client
  reloads), matching the settings UI.
- Capture/model keys (`whisper_preset`, devices, ...) are locked during a
  recording and take effect on next app start.
- API keys (Anthropic/OpenAI/HuggingFace) are **not** readable or writable
  here; they live in the app UI only.

### Live and recording

| Endpoint | Notes |
|---|---|
| `GET /live` | If recording: session id/title, `elapsed_sec`, transcript tail (`after_segment_id` cursor + `limit`), `last_segment_id`, chapters, current summary, audio levels. If idle: `latest_session_id`. |
| `POST /recording/start` | Opt-in (section 8). Body `{"confirm": true}`. Sends a start command to the app window (the page performs the start, so a headless start would silently lose the device selection); a window is opened only if none takes the command. |
| `POST /recording/stop` | Opt-in. Body `{"confirm": true}`. Finalization (cleanup, auto-title) continues asynchronously. |

---

## 5. Transcripts in depth

`GET /meetings/{id}/transcript` parameters:

| Param | Meaning |
|---|---|
| `format` | `json` (default), `text`, `markdown`, `srt`, `vtt`. |
| `start` / `end` | Time window; returns segments overlapping it. |
| `speaker` | Only segments whose resolved speaker name contains this. |
| `after_segment_id` | Only segments newer than this id (live tailing). |
| `offset` / `limit` | Pagination over the matched segments. |
| `timestamps` / `speakers` | Toggle `[M:SS]` stamps / names in text formats. |
| `chapters` | Interleave chapter headings in markdown (default on). |
| `envelope` | Wrap text formats in JSON (`{"content": ...}`) instead of raw. |

`json` rows look like:

```json
{"id": 185664, "start": 6.22, "end": 28.72, "start_hms": "0:06",
 "speaker": "Snehitha Alliburapu", "source": "Speaker 1",
 "text": "guys confirm on this ..."}
```

**Speaker resolution** (identical to the app UI): a per-segment manual label
override wins; otherwise the session's speaker label for the (possibly
reassigned) diarization key; otherwise the built-in source label (`Desktop`,
`Mic`, `Me`); otherwise the raw key. `source` keeps the raw key so you can
tell what diarization originally decided.

Sizing rule of thumb: one hour of meeting is roughly 600-800 segments. For
whole-meeting analysis prefer `GET /meetings/{id}/export?save_to_file=1` and
read the file, or slice with `start`/`end`.

---

## 6. Data model glossary

- **Meeting / session**: one recording. `session_id` is a UUID and the key
  for every per-meeting endpoint. `ended_at` null means in progress.
- **Segment**: one transcribed utterance with `start_time`/`end_time`
  (seconds from meeting start), a `source` (speaker key), and optional
  manual overrides.
- **Speaker keys vs names**: diarization produces keys like `Speaker 1`;
  users label them with real names per session, and the voice library links
  them to global speaker profiles that persist across meetings. `Mic` audio
  is attributed to the app user ("Me") when that feature is on.
- **Folders**: user-defined hierarchy; meetings live in at most one folder.
- **Summary**: AI-maintained markdown, updated incrementally during the
  meeting; only the latest revision is current.
- **Chapters**: AI- or user-created `{start_time, title}` topic markers.
- **Notes**: the user's rich-text pane (stored as Quill Delta; the API
  converts to markdown and appends safely).
- **Global Chat**: cross-meeting AI conversations the user runs inside the
  app (distinct from per-meeting chat).

---

## 7. Recipes

**"What did we decide about X?"**

1. `GET /search?q=X` (hybrid).
2. Take the top result's `session_id`; snippets already say who said it and
   when.
3. `GET /meetings/{id}` for the summary; quote with speaker + `[M:SS]`.

**"Catch me up on project Y from last week"**

1. `GET /folders/resolve?q=Y` (or `GET /folders`).
2. `GET /meetings?folder=<id>&within_days=7`.
3. Summaries are in the listing; drill in with `GET /meetings/{id}`.

**"Show me what was on screen when they demoed the dashboard"**

1. `GET /search?q=dashboard demo` and read the snippet `start_time` (say 754s).
2. `GET /meetings/{id}/frame?t=754` (or `t=12:34`). Offset handled for you.
3. Nearby moments: `GET /meetings/{id}/frames?at=744,754,764`.

**Analyze a 2-hour meeting without flooding context**

1. `GET /meetings/{id}/export?save_to_file=1` returns a local `.md` path.
2. Read or grep the file locally; fetch frames for the interesting moments.

**Follow a meeting that is happening right now**

1. `GET /live` -> note `last_segment_id` (and `live_video`).
2. Poll `GET /live?after_segment_id=<cursor>` every few seconds.
3. See the screen at any point: `GET /meetings/{id}/frame?t=now` for this
   instant, or pass any timestamp from the live transcript.

**Write findings back**

- `POST /meetings/{id}/notes/append` with `{"heading": "Action items",
  "text": "- ..."}` (shows up in the app's Notes pane).
- `POST /meetings/{id}/chapters` to bookmark moments.

**Diagnose "why did transcription stop?"**

1. `GET /system/status` (model/diarizer readiness).
2. `GET /system/logs?level=warn&limit=100`, then filter by `tag=whisper`,
   `tag=diarizer`, or `tag=recording`.

---

## 8. Recording control (opt-in)

Agents must not be able to silently record people. Therefore:

- `POST /recording/start|stop` return `403` unless the user has enabled
  **Allow recording control** in Settings > Agent API
  (`agent_api_allow_recording_control`).
- Both require `{"confirm": true}`.
- Start asks an app window to begin rather than starting headlessly: device
  selection and the readiness gate live on the page, so this is the only start
  mode that captures everything. The request goes to the start coordinator
  (core/recording_request.py), which offers it to the window that is already
  open, then opens the app window, and only then falls back to a fresh
  autostart window. Poll `GET /live` for the outcome.

Agent etiquette: only call these when the user explicitly asks, and say out
loud that recording is starting/stopping.

---

## 9. Security and privacy

- Loopback only: the Flask server binds `127.0.0.1`; nothing is exposed to
  the network.
- Optional bearer token (`agent_api_token`) and a master kill switch
  (`agent_api_enabled`) in Settings > Agent API.
- The Agent API performs no destructive operations: no endpoint deletes
  meetings, folders, notes, or files. Notes writes are append-only.
- Secrets (API keys, the token itself) are masked in every response.
- Meeting content is personal data: keep it local, quote only what the task
  needs.

---

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| Connection refused | App not running. `launch.bat` / `launch.command`, or check `PORT` in `.env`. MCP: set `MEETING_ASSISTANT_URL` if non-default. |
| Every endpoint 503 | Agent API disabled. Settings > Agent API > enable. |
| 401 | Token set but not sent. `Authorization: Bearer <token>`; the MCP server reads it automatically from `settings.json`. |
| `semantic_ready: false` / 503 on semantic | Embedding model still loading (starts shortly after app launch). Use keyword mode meanwhile. |
| Frame 404 | Meeting has no screen recording (`has_video` false and not currently recording with screen capture on). |
| Frame 422 | Timestamp beyond the end of the video (response includes the video duration). |
| Empty transcript | Meeting recorded before transcription was working, or still being reanalyzed; check `GET /system/logs?tag=reanalysis`. |
| MCP tools missing in client | Run `python mcp_server.py --selftest`; verify the client config paths and that the command uses an absolute interpreter path. |

---

*Version 1.0.0. Served live (with your real base URL substituted) at
`GET /api/agent/v1/docs`. Implementation: `agent_api/` package +
`mcp_server.py`; developer notes in `AGENT.md`.*
