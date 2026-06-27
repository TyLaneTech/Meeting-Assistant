#!/bin/bash
# Meeting Assistant launcher (macOS)
# Double-clickable in Finder. Mirrors launch.bat: ensure uv exists,
# create venv if missing, then hand off to launch.py.

set -e

# ── Force native arm64 on Apple Silicon ───────────────────────────────────────
# If launched under Rosetta (x86_64) on Apple Silicon - e.g. the Terminal is set
# to "Open using Rosetta" - re-exec natively as arm64. The python.org interpreter
# is a universal2 build, so under Rosetta it runs its x86_64 slice; uv then
# resolves x86_64 wheel tags and mlx (which ships arm64-only wheels) becomes
# unsatisfiable, while torch is pinned to its last x86_64 macOS wheel. The env
# var guards against a re-exec loop; a genuine Intel Mac is left untouched.
if [ "$(uname -m)" = "x86_64" ] \
   && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = "1" ] \
   && [ -z "$_MA_NATIVE_ARM64" ]; then
    export _MA_NATIVE_ARM64=1
    exec arch -arm64 /bin/bash "${BASH_SOURCE[0]}" "$@"
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"

# ── Ensure uv is available ───────────────────────────────────────────────────
UV=""
if command -v uv >/dev/null 2>&1; then
    UV="uv"
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
    export PATH="$HOME/.local/bin:$PATH"
else
    # Try Homebrew location.
    if [ -x "/opt/homebrew/bin/uv" ]; then
        UV="/opt/homebrew/bin/uv"
        export PATH="/opt/homebrew/bin:$PATH"
    elif [ -x "/usr/local/bin/uv" ]; then
        UV="/usr/local/bin/uv"
        export PATH="/usr/local/bin:$PATH"
    fi
fi

if [ -z "$UV" ]; then
    echo
    echo "  Installing uv package manager..."
    echo
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    if command -v uv >/dev/null 2>&1; then
        UV="uv"
    elif [ -x "$HOME/.local/bin/uv" ]; then
        UV="$HOME/.local/bin/uv"
    else
        echo
        echo "  Failed to install uv. Install manually:"
        echo "    https://docs.astral.sh/uv/getting-started/installation/"
        echo "    or: brew install uv"
        echo
        read -n 1 -s -r -p "Press any key to exit..."
        exit 1
    fi
fi

# ── Create venv if needed (uv auto-downloads Python 3.12 if not found) ──────
if [ ! -x "$VENV/bin/python" ]; then
    echo "  Creating Python environment..."
    "$UV" venv "$VENV" --python 3.12 --seed
fi

# ── Hand off to launch.py ──────────────────────────────────────────────────
exec "$VENV/bin/python" "$ROOT/launch.py"
