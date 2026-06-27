#!/usr/bin/env bash
# Meeting Assistant one-line installer (macOS / Linux).
#
#   curl -fsSL https://raw.githubusercontent.com/TyLaneTech/Meeting-Assistant/main/install.sh | bash
#
# Clones the repo from GitHub, then hands off to launch.command, which installs
# uv + Python, builds the virtual environment, downloads the models, and starts
# the app on http://localhost:6969 (your browser opens automatically).
#
# Override the install location with:
#   MEETING_ASSISTANT_DIR="/path/to/dir" bash install.sh
set -euo pipefail

REPO="${MEETING_ASSISTANT_REPO:-https://github.com/TyLaneTech/Meeting-Assistant.git}"
DEST="${MEETING_ASSISTANT_DIR:-$HOME/Meeting Assistant}"

echo "==> Meeting Assistant installer"

# 1. git is required (used for the clone and for the app's in-app updates).
if ! command -v git >/dev/null 2>&1; then
  echo "git is required but was not found."
  if [ "$(uname)" = "Darwin" ]; then
    echo "Install Apple's command line tools with:  xcode-select --install"
  fi
  exit 1
fi

# 2. macOS needs a native arm64 ffmpeg; the launcher will not auto-download one
#    on Apple Silicon (it would fetch an x86_64 build). Install it via Homebrew
#    when missing.
if [ "$(uname)" = "Darwin" ] && ! command -v ffmpeg >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "==> Installing ffmpeg via Homebrew..."
    brew install ffmpeg
  else
    echo "Note: ffmpeg was not found and Homebrew is not installed."
    echo "      Install Homebrew from https://brew.sh then run:  brew install ffmpeg"
  fi
fi

# 3. Clone, or update an existing checkout in place.
if [ -d "$DEST/.git" ]; then
  echo "==> Updating existing install at: $DEST"
  git -C "$DEST" pull --ff-only || echo "  (could not fast-forward; continuing with the current checkout)"
else
  echo "==> Cloning into: $DEST"
  git clone "$REPO" "$DEST"
fi

# 4. Build and launch. launch.command bootstraps uv + Python + the venv,
#    installs dependencies, downloads the models, and starts the app.
cd "$DEST"
chmod +x launch.command 2>/dev/null || true
echo "==> Starting Meeting Assistant (the first run sets up the environment and can take a few minutes)..."
exec ./launch.command
