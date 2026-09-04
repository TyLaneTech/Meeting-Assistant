# Meeting Assistant

Local Flask app that records meetings, transcribes and diarizes them, and layers
an AI assistant on top. Runs on Windows (CUDA) and macOS Apple Silicon (Metal).

## Read these first

| Doc | What it covers |
|---|---|
| [AGENT.md](AGENT.md) | **Authoritative.** Architecture, file map, threading model, SSE events, state management, behaviors that must not regress, commit message spec. Read it before changing code. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Repo topology, environment setup, branching, pull request policy. |

## Git workflow

Two repos, not equal. Azure DevOps `HiggDAC/Meeting-Assistant` is the source of
truth and holds all development. GitHub `TyLaneTech/Meeting-Assistant` is a
read-only public mirror used only for distribution.

Merging to `main` fires a pipeline that force-pushes `main` and tags to GitHub.

1. **Never push to the GitHub remote.** The mirror force-pushes, so anything landed
   there directly is erased on the next merge to `main`.
2. **Do not push straight to `main`.** Branch policy requires a pull request and is
   enforced for every contributor. The repo owner holds "Bypass policies when pushing"
   and is the sole exception. Work on `feature/<name>` or `fix/<name>`, then open a pull
   request in Azure DevOps. Open a pull request even if you are working as the owner,
   unless you are explicitly told to push directly.
3. **Squash merge only.** Enforced by policy.
4. **Never commit `.env`, API keys, or anything under `storage/`.** All gitignored.
5. **Do not remove the bundled HuggingFace token** from `core/config.py`. It is
   deliberate and the app depends on it.
6. **Do not commit unless asked.** Leave changes in the working tree by default.

## Commit messages

End users read every commit in the app's **Settings → Changelog** tab, which parses
`git log` directly. Write for them, not for yourself. The full spec is in AGENT.md;
the rules that matter most:

- **Past-tense verb first.** "Added Notes pane", not "Add Notes pane". The leading
  verb picks the icon shown next to the entry.
- **User-friendly noun phrases**, not module names. "Fixed Whisper hallucinations
  during long meetings", not "Fixed `_collapse_word_periods` regression".
- **Body is blank-line-separated sections**, each with a sub-heading line then
  `- ` bullets.
- **No emoji. No marketing verbs** (`Introducing`, `Meet`, `Ship`, `Level up`).
- **No `Co-Authored-By:` and no generated-with footers.** Repo policy, every
  branch, no exceptions. The parser strips them, but do not write them.

```
Improved how quickly recordings stop and sidebar items move

Recording
- Stopping a recording now takes effect immediately instead of waiting for
  the current audio chunk to finish.
```

## Pull requests

`main` is squash-merge only, so the squash commit **is** the changelog entry users read.
Azure DevOps prefills it as `Merged PR <n>: <title>` followed by your whole PR description
in markdown. Both are wrong. Replace both fields in the completion dialog, every time:

- **Delete the `Merged PR <n>: ` prefix.** The first word of the subject picks the icon, and
  `merged` matches no category, so the entry loses its icon and users see an internal PR
  number in a user-facing widget.
- **Replace the description with the changelog body format.** Nothing is markdown-rendered
  (the widget uses `textContent`), so `##`, `**bold**`, backticks, and numbered lists all
  appear literally on screen. Only `- `, `* `, and `• ` are bullets. A blank line ends a
  section; the first line of a section is its sub-heading.

The PR description is for your reviewer. The squash commit body is for end users. They are
different documents with different readers. AGENT.md has the full spec and a worked example.

## Keeping internal work out of the changelog

Every non-merge commit on `main` shows up in the user-facing Changelog tab. Prefix the
subject with `[internal]` when the change is infrastructure, CI, docs, or tooling:

```
[internal] Documented pull request and changelog conventions
```

`_build_changelog()` in `app.py` filters those out. Never use the marker to hide a real
user-facing change.

## Writing style

No em dashes anywhere: code comments, docs, commit messages, PR descriptions. Use
commas, parentheses, colons, or two sentences. En dashes are not a substitute.

## Running it

`launch.bat` (Windows) or `./launch.command` (macOS) handles venv creation,
accelerator detection, dependency install, model download, and browser launch.
The app serves on http://localhost:6969.

Recordings must be started from the session page via `?autostart=1`. Starting one
any other way causes a DirectShow echo.
