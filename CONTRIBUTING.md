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

- **A pull request.** Direct pushes to `main` are rejected.
- **At least one approval.** Your own approval counts, so you are never blocked
  waiting on someone. Get a real review anyway when the change is not trivial.
- **Squash merge.** Basic merge, rebase, and rebase-with-merge-commit are all
  disabled.

Squash is not cosmetic here. The in-app **Settings → Changelog** tab is built from
`git log` on `main` (see `_build_changelog` in `app.py`). It already skips merge
commits, so a non-squashed PR would spill every WIP commit into the changelog as
separate user-facing entries. One squash per PR gives one clean entry.

Delete your branch when the PR completes. Azure DevOps offers this in the
completion dialog and it is pre-ticked on PRs created from the CLI.

## Commit messages

End users read these in the Changelog tab, so write for them, not for yourself.

**[AGENT.md](AGENT.md) has the full specification.** In short: past-tense verb,
user-friendly noun phrases, no emoji, no marketing verbs, body split into
blank-line-separated sections with a sub-heading and bullets.

```
Improved how quickly recordings stop and sidebar items move

Recording
- Stopping a recording now takes effect immediately instead of waiting for
  the current audio chunk to finish.
```

Never add `Co-Authored-By:` or generated-with footers. The changelog parser
strips them defensively, but they should not be written in the first place.

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
2. **Never push straight to `main`.** Branch policy rejects it. Open a PR.
3. **Never add branch protection to GitHub's `main`.** The mirror force-pushes and
   protection would block it.
4. **Never commit `.env`, keys, or anything under `storage/`.** All gitignored.
   Keep it that way.
5. **Read AGENT.md first.** Especially "Key Behaviors to Preserve" and "Common
   Pitfalls". Several subsystems have non-obvious invariants that look like bugs
   and are not.
