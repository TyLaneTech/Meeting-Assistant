<div align="center">
  <img src="static/images/logo.png" width="88" alt="Meeting Assistant"><br><br>

  # Meeting Assistant

  **Local, private, GPU-accelerated transcription + AI for every meeting.**

  ![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?logo=python&logoColor=white)
  ![Windows](https://img.shields.io/badge/Windows-10%2F11-0078D4?logo=windows&logoColor=white)
  ![CUDA](https://img.shields.io/badge/CUDA-optional-76b900?logo=nvidia&logoColor=white)
  ![Claude](https://img.shields.io/badge/Powered%20by-Claude-d97706?logoColor=white)

</div>

---

> Captures your meetings. Transcribes in real time. Summarizes without sending your audio anywhere. Ask it anything — during the call or after. Runs on your machine, on your GPU, with your data staying put.

---

## What it does

| | |
|---|---|
| 🎙️ **Live transcription** | Captures desktop audio + mic simultaneously via Windows WASAPI |
| 🗣️ **Speaker diarization** | Identifies and labels who's talking, rename labels by clicking them |
| 🤖 **AI summary** | Auto-updates as the meeting progresses, shaped by your custom prompt |
| 💬 **Chat interface** | Ask questions about the meeting at any time — *"What did we decide about X?"* |
| 📼 **Session history** | All sessions saved locally; replay audio synced to the transcript |
| ⏱️ **Timestamp linking** | Click any timestamp pill in the summary to jump to that moment in the recording |
| 🔁 **Reanalyze** | Re-run transcription and diarization on the saved audio after a session ends |
| 🖥️ **System tray** | Start/stop recording and check status without touching the browser |

---

## Requirements

- **Windows 10 or 11** — WASAPI loopback audio capture is Windows-only
- **Python 3.10+** — [python.org/downloads](https://www.python.org/downloads/) — check *"Add python.exe to PATH"* during install
- **Anthropic API key** — for summaries and chat ([get one here](https://console.anthropic.com/settings/keys))
- **HuggingFace token** *(optional)* — for speaker identification ([get one here](https://huggingface.co/settings/tokens))
- **NVIDIA GPU** *(optional)* — CUDA-accelerated transcription; falls back to CPU automatically

---

## Quick start

```
1. Install Python 3.10+ (add to PATH)
2. Double-click launch.bat
3. Paste your API key when prompted
4. Hit Record
```

`launch.bat` handles everything on first run — pip, dependencies, model downloads, and opening the browser. Subsequent launches are fast.

---

## Interface

The app is split into three panels:

<details>
<summary><strong>📝 Transcript (left)</strong></summary>
<br>

Live text of everything said, labeled by speaker. Each segment carries a timestamp — click any **timestamp pill** in the summary to seek the audio playback to that exact moment.

- **Rename a speaker** — click their label in the transcript; the name applies globally to all their segments
- **Merge speakers** — give two labels the same name to combine their voice profiles
- **Copy transcript** — button in the panel header grabs the full text

</details>

<details>
<summary><strong>📋 Summary (center)</strong></summary>
<br>

An AI-generated summary that updates automatically as the meeting progresses. The structure adapts to the content — no rigid template.

- **Custom prompt** — click <kbd>⚙</kbd> to add context like *"This is a job interview — summarize each candidate answer"*. This shapes the entire summary.
- **Manual refresh** — click <kbd>↻</kbd> to regenerate at any time
- The AI is intentionally disciplined: it only updates sections when genuinely new high-level concepts come up, so it won't bloat on every run

</details>

<details>
<summary><strong>💬 Chat (right)</strong></summary>
<br>

Ask questions about the meeting in plain English. The AI has full context of the transcript and summary, live or after the fact.

**Examples:**
- *"What action items were assigned to Sarah?"*
- *"What was the final decision on the budget?"*
- *"Give me a one-paragraph executive summary"*
- *"Did anyone push back on the timeline?"*

</details>

---

## Recording

1. Select your **Desktop** and **Mic** sources from the sidebar dropdowns
2. Click <kbd>▶ Start Recording</kbd> — or use the system tray
3. Transcription begins within seconds; the summary appears after the first few minutes
4. Click <kbd>⏹ Stop Recording</kbd> — the session saves automatically and gets an AI-generated title

Use <kbd>▶ Test Audio</kbd> to verify your audio sources are working before committing to a recording.

---

## Models

<details>
<summary><strong>Whisper (transcription)</strong></summary>
<br>

| Option | Speed | Quality | Requires |
|---|---|---|---|
| GPU — large-v3 | ⚡ Fastest | ★★★★★ | CUDA GPU |
| GPU — medium | ⚡ Fast | ★★★★☆ | CUDA GPU |
| GPU — small | ⚡ Fast | ★★★☆☆ | CUDA GPU |
| CPU — medium | 🐢 Moderate | ★★★★☆ | — |
| CPU — small | 🐢 Moderate | ★★★☆☆ | — |

</details>

<details>
<summary><strong>Diarizer (speaker identification)</strong></summary>
<br>

| Option | Speed | Requires |
|---|---|---|
| GPU | Faster | CUDA GPU + HuggingFace token |
| CPU | Slower | HuggingFace token |

Speaker identification requires accepting the [pyannote model terms](https://huggingface.co/pyannote/speaker-diarization-3.1) on HuggingFace — the app will link you there on first use.

</details>

Model selections persist between sessions. You can't swap models mid-recording.

---

## System tray

A tray icon lives in the Windows notification area while the app is running. Right-click for recording controls and settings — no browser required.

| Icon | State |
|---|---|
| 🔵 Blue | Ready |
| 🔴 Red | Recording |
| ⚫ Gray | Models loading |
| 🟡 Amber | Setup required (missing API key) |

---

## Privacy

Everything runs locally. No audio ever leaves your machine.

The only outbound calls are:
- **Anthropic API** — transcript text for summaries and chat responses
- **HuggingFace Hub** — diarization model files, downloaded once and cached

Your data lives in `data/` next to the app:

```
data/
├── meetings.db     ← SQLite — all sessions, transcripts, summaries, chat
├── settings.json   ← Preferences
└── audio/          ← Recorded WAV files
```

---

## Troubleshooting

<details>
<summary><strong>"Loading model…" never goes away</strong></summary>
<br>

The Whisper model downloads on first run — large-v3 is ~3 GB. Check the terminal window that launched with `launch.bat` for download progress.

</details>

<details>
<summary><strong>No audio / flat visualizer</strong></summary>
<br>

- Click <kbd>↻</kbd> next to the desktop device selector to re-scan audio devices
- Make sure something is actually playing on your PC — WASAPI loopback only captures active audio
- Use <kbd>▶ Test Audio</kbd> to confirm your sources before recording

</details>

<details>
<summary><strong>Speaker labels not appearing</strong></summary>
<br>

Speaker diarization requires a HuggingFace token. Add one in <kbd>⚙ Settings</kbd>. On first use, pyannote model files download automatically — this can take a few minutes.

</details>

<details>
<summary><strong>GPU not detected despite having an NVIDIA card</strong></summary>
<br>

- Ensure your NVIDIA drivers are up to date
- The GPU Whisper options will be greyed out automatically if CUDA can't be detected; CPU fallback is used instead
- Check the terminal output on startup for `[whisper] CUDA OK` or `[whisper] CUDA unavailable`

</details>

<details>
<summary><strong>Transcription looks like a broken record (repeated phrases)</strong></summary>
<br>

This is Whisper's hallucination loop — it can happen with microphone input in noisy conditions. The app has multi-layer detection to catch and discard these loops automatically. If it persists, try switching to a smaller model or reducing background noise.

</details>

<details>
<summary><strong>Port conflict on startup</strong></summary>
<br>

Port `6969` is used by default. To change it, create a `.env` file next to `launch.bat`:

```
PORT=7000
```

</details>

---

## Project structure

<details>
<summary>Show</summary>
<br>

```
Meeting Assistant/
├── launch.bat            ← Entry point — double-click to run
├── app.py                ← Flask server, SSE event stream, session state
├── transcriber.py        ← faster-whisper integration, hallucination detection
├── diarizer.py           ← diart / pyannote speaker diarization
├── ai_assistant.py       ← Summarization and chat via Anthropic / OpenAI
├── audio_capture.py      ← WASAPI loopback + mic capture
├── wav_writer.py         ← WAV recording with sample-accurate timestamps
├── storage.py            ← SQLite persistence layer
├── settings.py           ← Settings read/write
├── config.py             ← App-wide constants
├── tray.py               ← System tray icon and menu
├── requirements.txt      ← Python dependencies
├── data/                 ← Created automatically on first run (gitignored)
│   ├── meetings.db
│   ├── settings.json
│   └── audio/
├── templates/
│   └── index.html
└── static/
    ├── app.js
    ├── style.css
    └── images/
```

</details>
