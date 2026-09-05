# Contributing

## Where the code lives

Development happens in **Azure DevOps**. The GitHub repo is a read-only mirror
used for distribution: it is what `install.sh` / `install.ps1` clone and what the
in-app updater pulls from.

| | Azure DevOps | GitHub |
|---|---|---|
| Role | Source of truth, all development | Read-only distribution mirror |
| Repo | `HiggDAC/Meeting-Assistant` | `TyLaneTech/Meeting-Assistant` |
| Visibility | Private, org access required | Public |
| Branches | `main` plus short-lived work branches | `main` only |
| Push to it? | Yes, via pull request | **Never** |
| Issues and PRs | Here | Not monitored |

Pushing directly to GitHub does not work. The mirror force-pushes `main` on every
merge, so anything committed there is erased on the next run.

```
Azure DevOps (source of truth)
  main ──┬──────────────────────────────────► mirror-to-github pipeline
         └── feature/my-change ──┘ PR, squash                │
                                                             ▼
                                          GitHub (public, read-only mirror)
                                                             │
                                          install.sh / install.ps1 clone it
                                          /api/update/check pulls from it
```

## What you need

### Access

- A seat on the `HiggDAC` Azure DevOps org with **Basic** access. Stakeholder is
  not enough to push code.
- Contributor on the `Meeting-Assistant` project.
- No GitHub account required. Nothing in this workflow touches GitHub by hand.

### Tooling

| | |
|---|---|
| Git | Any recent version |
| Python | 3.10 or higher, on PATH |
| OS | Windows 10/11, or macOS 13+ on Apple Silicon |
| ffmpeg | Auto-downloaded into `storage/tools/` by `launch.py` if not on PATH. On macOS prefer `brew install ffmpeg` for a native arm64 build. |

Optional GPU acceleration (NVIDIA CUDA on Windows, Metal on Apple Silicon) is
detected automatically and falls back to CPU. See the full end-user requirements
table in [README.md](README.md).

### Keys

Copy `.env.example` to `.env` and fill it in. `.env` is gitignored and must stay
that way.

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Summaries and chat. The app boots into setup mode without it. |
| `OPENAI_API_KEY` | No | Alternative AI provider. |
| `HUGGING_FACE_KEY` | No | Speaker diarization. A working token is already bundled, so set this only to override it. |
| `PORT` | No | HTTP port, defaults to `6969`. |

Never commit a key. Do not remove the bundled HuggingFace token from
`core/config.py`; it is deliberate and the app depends on it.

## First run

```bash
git clone https://dev.azure.com/HiggDAC/Meeting-Assistant/_git/Meeting-Assistant
cd Meeting-Assistant
```

Then `launch.bat` on Windows, or `./launch.command` on macOS. The launcher creates
the virtual environment, detects CUDA or Metal, installs the matching Whisper
backend, downloads models, and opens the app at http://localhost:6969. The first
run is slow. Later ones are not.

**Read [AGENT.md](AGENT.md) before changing anything.** It is the authoritative
reference for the architecture, threading model, SSE event system, state
management, and the behaviors that must not regress.

## Tests

`tests/` holds fast, hardware-free checks: unit tests for the pure modules
(calendar feed parsing and matching, the dashboard queries, WAV handling, device
resolution) and static assertions that read the templates, scripts and
stylesheets and fail when a convention regresses (no native `alert`, `confirm`
or `prompt`; no `opacity` outside disabled states and keyframes; the ids the
scripts bind to; the Speakers dialog landing on Cleanup). Run them before
opening a pull request:

```bash
uv pip install pytest --python .venv/Scripts/python.exe
.venv/Scripts/python.exe -m pytest tests -q
```

They finish in about ten seconds. The Windows-only capture tests skip themselves
on macOS.

## Branching

One long-lived branch, `main`, always releasable. Everything else is short-lived
and branches off it. Name work branches `feature/<short-name>` or
`fix/<short-name>`.

```bash
git switch main
git pull
git switch -c feature/my-change
# ... work ...
git push -u origin feature/my-change
```

Then open a pull request into `main` in Azure DevOps.

## Pull requests

`main` is protected by branch policy. It requires:

- **A pull request.** Direct pushes to `main` are rejected for everyone except the
  repo owner, who holds the "Bypass policies when pushing" permission. If you are not
  sure whether that is you, it is not.
- **At least one approval.** Your own approval counts, so you are never blocked
  waiting on someone. Get a real review anyway when the change is not trivial.
- **Squash merge.** Basic merge, rebase, and rebase-with-merge-commit are all
  disabled.

Write the title and description for your reviewer. Azure DevOps prefills the squash
commit from them and that is fine: users never see commits (see Release notes).
Delete your branch when the PR completes; it is pre-ticked on PRs created from the CLI.

## Release notes

End users read `CHANGELOG.md` in the app's Settings → Changelog tab and in the What's
new card after an update. It is the only source of user-facing release notes, and it
is edited by hand, in the same pull request as the change:

```
## Fixed the desktop audio device (2026-09-05)

### Recording
- The device you select is always the device captured, even when Windows reports a
  different default output
```

- One entry per `## ` heading: title, then the date in parentheses. Newest first.
- The first word of the title picks the icon: Added, Fixed, Improved, Removed, Reworked.
- Under it, `### ` sub-headings for areas and `- ` bullets in plain user language. No
  module names, no emoji, no marketing verbs.
- Infrastructure, CI, docs and tooling work gets no entry.

The file documents its own format at the top, and `tests/test_changelog.py` fails if it
stops parsing.

## Commit messages

Written for developers: past-tense verb first, what changed and why. Never add
`Co-Authored-By:` or generated-with footers.

## Releasing

There is no separate release step. Merging to `main` triggers the
`mirror-to-github` pipeline ([.azuredevops/mirror-to-github.yml](.azuredevops/mirror-to-github.yml)),
which force-pushes `main` and any tags to the public GitHub repo. Installed copies
pick the change up the next time a user runs the in-app update check.

The pipeline needs a `GH_PAT` secret variable: a fine-grained GitHub token scoped
to `TyLaneTech/Meeting-Assistant` only, with `Contents: Read and write`. These
expire after at most a year, so it needs rotating.

Only `main` and tags are mirrored. Work branches stay internal to Azure DevOps.

## Rules that will bite you

1. **Never push to GitHub.** It is a mirror. Your commit will be erased.
2. **Never push straight to `main`.** Branch policy rejects it. Open a PR. (The repo
   owner holds a bypass permission and is the only exception.)
3. **Never add branch protection to GitHub's `main`.** The mirror force-pushes and
   protection would block it.
4. **Never commit `.env`, keys, or anything under `storage/`.** All gitignored.
   Keep it that way.
5. **Read AGENT.md first.** Especially "Key Behaviors to Preserve" and "Common
   Pitfalls". Several subsystems have non-obvious invariants that look like bugs
   and are not.
