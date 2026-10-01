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

- **One entry per update.** Everything that ships together goes under one `## Title (YYYY-MM-DD)` heading, dated the day it ships, with `### ` area sub-headings (Recording, Speakers, Settings) when it covers more than one. The What's new card shows only the newest entry, so a second entry in the same push is never seen there. Newest first; the title's first word picks the icon (Added, Fixed, Improved, Removed, Reworked).
- **Keep it tight.** One bullet per change, one short sentence: what the user will notice. Not how it works, not why it was hard, not the history of the bug. Cut anything a user would not miss. No module names, no emoji, no marketing verbs.
- **One line per bullet.** The notes render with `breaks: true`, so a newline inside a sentence shows on screen as a break. Never hard-wrap; the page wraps text itself.
- `**bold**` on the thing that changed, the bullet's opening words and never a whole sentence. `` `code` `` for text the user sees or types in the app (`Apply`, `Settings > Calendar`), never a module or file path. Nested bullets and a `> ` note only for a caveat the user has to act on.
- Infrastructure, docs, CI and tooling get no entry.

```
## Fixed the desktop audio device (2026-09-05)

### Recording
- **The device you select** is always the one recorded, even when Windows reports a different default output.
```

The parser is `core/changelog.py`. `tests/test_changelog.py` fails if the file stops parsing, a bullet is hard-wrapped, a bullet runs past 25 words, or the newest entry passes 250.

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
