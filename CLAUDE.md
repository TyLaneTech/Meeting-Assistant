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

## Release notes

End users read `CHANGELOG.md` (repo root) in **Settings → Changelog** and in the What's new
card after an update. It is the only source of user-facing release notes; commit messages and
pull request text never reach users.

- Every change a user could notice gets an entry, or a bullet under the entry for the release
  it ships in, in the same pull request. Infrastructure, docs, CI and tooling get no entry.
- One entry per `## ` heading: the title, then the date in parentheses. Newest first. The first
  word of the title picks the icon: Added, Fixed, Improved, Removed, Reworked.
- Under the heading: `### ` sub-headings for areas (Recording, Speakers, Settings), `- `
  bullets in plain user language. No module names, no emoji, no marketing verbs.

```
## Fixed the desktop audio device (2026-09-05)

### Recording
- The device you select is always the device captured, even when Windows reports a
  different default output
```

The parser is `core/changelog.py`; `tests/test_changelog.py` fails if the file stops parsing.

## Commit messages and pull requests

Written for developers: past-tense verb first, what changed and why. **No `Co-Authored-By:`
and no generated-with footers**, on any branch. `main` is squash-merge only (branch policy).
The completion dialog's prefilled title and description can stay as they are; nothing users
see is built from them.

## Writing style

No em dashes anywhere: code comments, docs, commit messages, PR descriptions. Use
commas, parentheses, colons, or two sentences. En dashes are not a substitute.

## Running it

`launch.bat` (Windows) or `./launch.command` (macOS) handles venv creation,
accelerator detection, dependency install, model download, and browser launch.
The app serves on http://localhost:6969.

Recordings must be started from the session page via `?autostart=1`. Starting one
any other way causes a DirectShow echo.
