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

Squash is not cosmetic here. The in-app **Settings → Changelog** tab is built from
`git log` on `main` (see `_build_changelog` in `app.py`). A non-squashed PR would
spill every WIP commit into the changelog as separate user-facing entries. One
squash per PR gives one clean entry.

### Writing the completion message

Because the squash commit is what users read, **the completion dialog is the most
user-facing thing you will write.** Azure DevOps prefills it badly and you must
replace both fields:

| Field | Prefilled as | Should be |
|---|---|---|
| Subject | `Merged PR 904: Added a contributor guide` | `Added a contributor guide` |
| Body | Your entire PR description, in markdown | Plain-text sections and bullets |

The `Merged PR <n>: ` prefix costs the entry its icon, because the categoriser
matches the first word and `merged` is in no category. It also shows an internal
PR number to end users.

The body is never markdown-rendered. `##`, `**bold**`, backticks, and numbered
lists all appear on screen exactly as typed. Only `- `, `* `, and `• ` are treated
as bullets. A blank line ends a section, and a section's first line becomes its
sub-heading.

Note that a squash commit has a single parent, so it is not filtered out as a
merge commit. Whatever you leave in that dialog ships to every user.

Your PR description is written for your reviewer and can be as technical as you
like. The completion message is a different document for a different reader.
[AGENT.md](AGENT.md) has the full specification and a worked example.

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

### Internal commits

Every non-merge commit on `main` becomes a Changelog entry that end users read.
Infrastructure, CI, docs, and tooling work has no meaning for them, so prefix the
subject with `[internal]` to keep it out:

```
[internal] Documented pull request and changelog conventions
```

The marker is matched case-insensitively against the subject. Use it honestly:
it is for work users cannot see, not for changes you would rather not explain.

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
