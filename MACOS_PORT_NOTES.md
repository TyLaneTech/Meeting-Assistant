# macOS Port Notes

Working notes from porting Meeting Assistant from Windows to macOS Apple
Silicon. Captures the non-obvious findings and routing model so future
sessions don't have to re-discover them.

## Status

| Pass | Scope | State |
|---|---|---|
| 1 | Inventory of files, deps, audio libs | done |
| 2 | Environment (Python, arch, brew, TCC, ffmpeg) | done |
| 3 | Audio I/O via ScreenCaptureKit | **done** |
| 4 | macOS audio wiring, preferences, launcher, and UI review | done on 2026-06-01 |
| 5 | Transcription pipeline verification (16 kHz mono float32 path) | **done — live SCK→WAV→Whisper smoke passed 2026-06-12** |
| 6 | Self-test script | updated for SCK on 2026-06-01 |

## 2026-06-12 hardening pass (commit ccd41b9)

A 109-agent adversarial review confirmed 31 macOS bugs; all are fixed in
`ccd41b9` (see that commit message for the full list). The two blockers:
the menu-bar tray was 100% dead (pystray must run on the main thread on
darwin — AppKit aborts NSStatusItem creation off-main; `main()` now inverts
the threading per-platform) and `/api/restart`//api/update/apply died in
Windows-only relaunch code (now `_relaunch_app()` with a darwin branch via
`launch.command`; verified self-relaunching live).

**Runtime home moved:** the app now lives and runs from `~/meeting-assistant`
(local disk). Running it from a OneDrive/File Provider path
(`~/Library/CloudStorage/...`, which `~/Documents` resolves into on this
machine) made imports take 6+ minutes and stalled the pyannote diarizer load
indefinitely; the launcher now warns about CloudStorage paths. The OneDrive
checkout remains as a synced backup only.

Known papercut: the first ~2–4 s after Record may be missed while SCK warms
up; this machine's `replayd` was observed taking up to 20 s to start
delivering buffers, which `start()` now tolerates (polls for flowing audio,
20 s cap) and the watchdog supervises thereafter.

## 2026-06-12 Claude Code update — live capture fixed and verified

The SCK pipeline was delivering audio callbacks but every buffer was being
silently discarded: PyObjC's `CMBlockBufferCopyDataBytes` returns a tuple
`(OSStatus, filled_buffer)` for the out-parameter variant, not a bare
OSStatus. The guard `if status != 0: return None` in
`_extract_int16_from_sample_buffer` compared a tuple to 0 — always truthy —
so the extractor returned `None` for 100% of sample buffers. Fixed by
unpacking the tuple before the status check.

Verification on Ryan's Mac (2026-06-12, Corsair Virtuoso XT as default output):

- Default output playback works again (`afplay` returns normally), so the
  blocked smoke tests from 2026-06-01 could finally run.
- Live end-to-end RMS smoke **passed**: `AudioCapture.start()` → SCK delegate
  → `_loopback_q` → `_mixer_loop` → consumer queue, while `afplay` played
  Glass.aiff 3×: 657 chunks, rms=710, peak=5744.
- Live transcription smoke **passed**: `say` spoke a phrase during SCK
  capture, mixer output written to WAV at 48 kHz mono int16, transcribed with
  `mlx-community/whisper-large-v3-mlx` → exact transcript
  `Meeting Assistant screen capture test passed.`
- TCC note: the first SCK run from this session hit `-3801 declined`; macOS
  flipped `kTCCServiceScreenCapture` for `com.anthropic.claude-code` to
  allowed at that moment and a fresh process then worked — consistent with
  gotcha 1 (per-process TCC caching; restart after grant).

## 2026-06-01 Codex update

The authoritative project now uses ScreenCaptureKit as the macOS system-audio
backend. BlackHole is not used by the SCK capture path and is filtered out of
the app's microphone/input picker so it is not accidentally selected as a mic.

Changes applied during this update:

- `audio_capture_mac.py` now comes from the SCK branch, with the ffmpeg 8.x
  `-audio_buffer_size` regression removed from the avfoundation mic path.
- `requirements.txt` includes `pyobjc-framework-ScreenCaptureKit` and
  `pyobjc-framework-CoreMedia`.
- `launch.py` no longer tries to build or select the old `audiotee`/BlackHole
  loopback stack at startup.
- The legacy loopback files were removed from the active tree:
  `mac_system_audio.py`, `mac_audio_output_switch.py`, `tools/audiotee-bin`,
  `tools/audiotee/`, and `tools/build_audiotee.sh`.
- `selftest.py` now checks ScreenCaptureKit dependencies instead of
  `audiotee`.
- Saved macOS mic preferences are normalized before audio test/record start.
  Stale values such as `-2`, missing CoreAudio indexes, BlackHole,
  `Meeting Assistant Output`, and missing/stale `ffmpeg:` mic names migrate to
  the first safe physical avfoundation/CoreAudio mic, or `None` if no real mic
  is available.

Verification on Ryan's Mac (macOS 26.5):

- `git diff --check` passed.
- Full project compile check passed:
  `.venv/bin/python -m compileall -q -x '(^|/)(\\.venv|__pycache__|\\.git)(/|$)' .`
- Device enumeration returns one loopback entry:
  `System Audio (ScreenCaptureKit)`, plus real mic inputs. `BlackHole 2ch` is
  intentionally hidden from both CoreAudio and ffmpeg/avfoundation mic lists.
- Dispatcher import check returns the macOS backend:
  `audio_capture.AudioCapture.__module__ == "audio_capture_mac"`.
- A direct SCK capture start succeeds from the project venv with mic disabled.
- The live RMS/transcription smoke still needs a clean output-playback run:
  Python `sounddevice.play()` failed with `Internal PortAudio error`, and
  `afplay` timed out against the current default output. That is separate from
  the SCK loopback wiring and should be rerun once the Mac's default output is
  producing audio normally.
- Offline transcription smoke passed using the cached
  `mlx-community/whisper-large-v3-mlx` model: macOS `say` generated a local
  phrase file and MLX Whisper returned `Meeting Assistant Screen Capture Test.`
  in 39.64 seconds.

System-audio loopback uses Apple's ScreenCaptureKit (macOS 13+). No virtual
driver, no aggregate device, no system-output reroute. Mic capture uses
sounddevice/CoreAudio. Whisper on Metal and diarization on MPS confirmed
working in earlier passes.

## Audio architecture (macOS)

ScreenCaptureKit captures the system audio mix directly. The same OS-level
TCC permission used by screen recording (Screen & System Audio Recording)
gates audio loopback. Apps like Zoom, Loom, OBS, and Audio Hijack use this
API today.

```
                    System audio mix
                          │
                          ▼
                  ┌───────────────────┐
                  │ ScreenCaptureKit  │
                  │   SCStream        │
                  │  (audio enabled)  │
                  └─────────┬─────────┘
                            │  CMSampleBuffer (Float32 planar)
                            ▼
                ┌─────────────────────┐
                │  _SCKAudioDelegate  │  (audio_capture_mac.py)
                │  Float32 → Int16    │
                └─────────┬───────────┘
                          │
                          ▼
                    _loopback_q  ──►  _mixer_loop  ──►  audio_queue
```

`AudioCapture.start()` instantiates `_SCKLoopbackStream`, which:
1. Calls `SCShareableContent.getShareableContentWithCompletionHandler_` —
   this is what triggers the TCC prompt on first run.
2. Builds an `SCContentFilter` over the main display with no app
   exclusions (captures the full system audio mix).
3. Configures `SCStreamConfiguration` with `capturesAudio=True`,
   `sampleRate=48000`, `channelCount=2`, video set to a tiny 2×2 frame
   capped at 1 fps (SCK still drives a video pipeline even for audio-only
   use, so we minimize that work).
4. Adds an `_SCKAudioDelegate` to receive `CMSampleBuffer`s on the audio
   output type, extracts planar Float32 from the underlying `CMBlockBuffer`
   via `CMBlockBufferCopyDataBytes` (ctypes-friendly), interleaves the
   channels, clamps, and quantizes to Int16.

`AudioCapture.stop()` calls `stopCaptureWithCompletionHandler_` and
releases the stream — there is no system-output state to restore.

## Critical gotchas (for the next person reading this code)

### 1. SCK requires Screen & System Audio Recording permission

The TCC prompt fires the first time `getShareableContentWithCompletionHandler_`
runs. After granting, **the app must be restarted once** before capture
will work — TCC caches per-process auth at launch. We surface this in the
`RuntimeError` raised by `_SCKLoopbackStream.start()` when shareable
content enumeration fails or returns empty.

### 2. SCK sample buffers are planar Float32 by default

Layout is `[c0_s0..c0_sN, c1_s0..c1_sN]`, not interleaved. The
`_extract_int16_from_sample_buffer` helper reshapes to `(channels, samples)`,
transposes, and flattens before quantizing. Skipping the transpose produces
audible channel-bleeding artifacts.

### 3. SCK still drives a video pipeline

Even with `capturesAudio=True` and no video output added, SCK runs its
display capture machinery. Setting `width=2`, `height=2`, and a minimum
frame interval of 1 fps keeps that overhead negligible. Don't omit those
config fields — leaving them at defaults captures full-resolution display
frames that get discarded.

### 4. PyObjC selector arity

PyObjC selectors are tied to argument count via underscores. The audio
delegate uses `stream_didOutputSampleBuffer_ofType_` (3 args) and
`configureWithQueue_channels_` (2 args). Renaming or adding/removing
trailing underscores silently breaks the ObjC bridge.

### 5. CoreMedia / ScreenCaptureKit need explicit pyobjc deps

`pyobjc-framework-ScreenCaptureKit` and `pyobjc-framework-CoreMedia` are
required. The umbrella `pyobjc` package does *not* pull these in; they
must be listed individually. Both are in `requirements.txt`.

### 6. evermeet.cx ffmpeg serves x86_64 by default

Unrelated to audio capture but worth flagging:
`https://evermeet.cx/ffmpeg/getrelease/zip` returns an x86_64 binary that
runs through Rosetta on Apple Silicon — slow and a footgun. Prefer
`brew install ffmpeg`, which gives a native arm64 build. `find_ffmpeg()`
already prefers `tools/ffmpeg` then PATH, so brew "wins" if no project-local
download exists.

### 7. PyObjC out-parameter functions return tuples, not OSStatus

CoreMedia/CoreAudio C functions with out-parameters (e.g.
`CMBlockBufferCopyDataBytes`) come back through PyObjC as
`(OSStatus, filled_buffer)` tuples. Comparing the raw return value to 0
(`if status != 0`) is always truthy for a tuple, which silently rejects
every buffer with no exception and no log line. Unpack first. This bit us
in `_extract_int16_from_sample_buffer` — capture "worked" (stream started,
callbacks fired) but zero PCM ever reached the queue.

## Files changed in this port

| File | Change | Why |
|---|---|---|
| `audio_capture_mac.py` | SCK-based loopback; removed BlackHole/aggregate routing; `enumerate_audio_devices` returns a single synthetic loopback entry; `auto_detect_devices` only ranks mics | System audio capture now uses Apple's official API — no virtual driver, no install dance, no system-output reroute |
| `mac_system_audio.py` | **Deleted** | Removed the old Process Tap/audiotee Python wrapper |
| `mac_audio_output_switch.py` | **Deleted** | Removed the old output-device switching fallback |
| `tools/audiotee-bin`, `tools/audiotee/`, `tools/build_audiotee.sh` | **Deleted** | Removed the bundled Swift audiotee stack |
| `launch.py` | Removed the `if sys.platform == "darwin":` BlackHole/audiotee bootstrap block; added SCK permission guidance | SCK needs no install-time setup, but users need restart-after-grant guidance |
| `requirements.txt` | Added `pyobjc-framework-ScreenCaptureKit` and `pyobjc-framework-CoreMedia` for darwin | New SCK-based capture path |
| `README.md` | Marked macOS 13+ supported; documented Screen Recording TCC permission | User-facing reqs changed |

## Run from a clean checkout (macOS Apple Silicon)

```bash
# One-time system prerequisite (only ffmpeg now — no BlackHole)
brew install ffmpeg

# Project setup (idempotent)
cd <repo>
./launch.command                      # or: python launch.py
```

The launcher will:
1. Build/refresh the venv via `uv` (Python 3.12 arm64).
2. Install all deps from `requirements.txt` — torch arm64 with MPS,
   mlx-whisper, sounddevice, pyannote, pyobjc-frameworks (including
   ScreenCaptureKit + CoreMedia), etc.
3. Pre-download Whisper (mlx-community/whisper-large-v3-mlx) and pyannote
   models.
4. Find ffmpeg from PATH (brew) and skip the broken evermeet.cx download.
5. Start `app.py` on `http://localhost:6969`.

When the user clicks Record:
- `AudioCapture.start()` instantiates `_SCKLoopbackStream`, which kicks off
  the SCK content query (TCC prompt on first run).
- After the user grants permission and restarts once, SCK delivers system
  audio sample buffers; mic streams from sounddevice in parallel.
- On stop, both streams release; nothing else to restore.

## Verification snippets

These are paste-ready into a shell from the project root.

### Live loopback capture test
```bash
.venv/bin/python <<'EOF'
import queue, time, numpy as np
from audio_capture_mac import AudioCapture

q = queue.Queue()
cap = AudioCapture(q)
cap.echo_cancel_enabled = False
cap.agc_loopback_enabled = False
cap.agc_mic_enabled = False
cap.start(loopback_index=None, mic_index=-1)
print("Play a YouTube tab with audible content for 5 seconds...")
time.sleep(5)

chunks = []
try:
    while True:
        chunks.append(q.get_nowait())
except queue.Empty:
    pass
cap.stop()

if chunks:
    pcm = b"".join(c[1] for c in chunks if c[0] == "loopback")
    s = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    print(f"frames={len(chunks)} peak={np.max(np.abs(s)):.3f} RMS={np.sqrt(np.mean(s**2)):.4f}")
else:
    print("FAIL: no audio captured")
EOF
```

Expected: `peak > 0.05`, `RMS > 0.005` for typical speech/music. First run
will trigger the TCC prompt; grant it and restart Python before re-running.

### Auto-detect end-to-end
```bash
.venv/bin/python -c "
from audio_capture_mac import auto_detect_devices
r = auto_detect_devices()
print('best_loopback:', r['best_loopback'])
print('best_mic:    ', r['best_mic'])
"
```

Expected: `best_loopback = {'index': 0, 'name': 'System Audio (ScreenCaptureKit)'}`
and a real mic entry with non-zero RMS as `best_mic`.

## Known issues / open work

- **`PaMacCore err='-50'` on multi-stream open** during `auto_detect_devices`.
  paramErr from PortAudio when several input streams open simultaneously.
  Capture still works; no longer compounded by aggregate-device routing.
- **`ffmpeg_util.download_ffmpeg`** still hard-codes the x86_64 evermeet.cx
  URL. Harmless because brew is preferred, but should branch on
  `platform.machine() == "arm64"` for clean-checkout reliability.
- **Per-app exclusion UI** — SCK's `SCContentFilter` supports excluding
  specific applications from the audio mix. A future Settings panel could
  let users exclude (e.g.) the assistant's own playback when replaying
  sessions, or background apps like Spotify. v1 captures everything.
- **Live SCK/WAV transcription smoke** still needs one post-output-fix run:
  start capture, play a short phrase, verify nonzero WAV RMS/peak, then
  confirm the full recorded-file workflow transcribes the phrase. The cached
  MLX engine itself already passed a local phrase-file transcription.

## Historical: pre-SCK BlackHole approach

Before the SCK rewrite, system audio loopback used BlackHole 2ch or the
bundled `audiotee` Process Tap CLI depending on which macOS port attempt was
active. BlackHole required a custom output route so audio reached both the
user's speakers and the virtual input; audiotee avoided the virtual driver but
left another binary and permission surface to supervise. Both approaches are
now removed from the active tree. The git history preserves them if needed.

## Memory pointer

A short-form note for cross-session recall lives at:

```
~/.claude/projects/-Users-higg-Documents-Claude-Projects-Higg/memory/meeting_assistant_macos.md
```
