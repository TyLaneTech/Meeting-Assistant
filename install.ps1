# Meeting Assistant one-line installer (Windows, PowerShell).
#
#   irm https://raw.githubusercontent.com/TyLaneTech/Meeting-Assistant/main/install.ps1 | iex
#
# Clones the repo from GitHub, then hands off to launch.bat, which installs
# uv + Python, builds the virtual environment, downloads the models, and starts
# the app on http://localhost:6969 (your browser opens automatically).
#
# Override the install location with:
#   $env:MEETING_ASSISTANT_DIR = 'C:\path\to\dir'; irm <url> | iex
$ErrorActionPreference = 'Stop'

$repo = if ($env:MEETING_ASSISTANT_REPO) { $env:MEETING_ASSISTANT_REPO } else { 'https://github.com/TyLaneTech/Meeting-Assistant.git' }
$dest = if ($env:MEETING_ASSISTANT_DIR) { $env:MEETING_ASSISTANT_DIR } else { Join-Path $HOME 'Meeting Assistant' }

Write-Host '==> Meeting Assistant installer'

# 1. git is required (used for the clone and for the app's in-app updates).
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  Write-Host 'git is required but was not found.'
  Write-Host 'Install it from https://git-scm.com/download/win (or run: winget install --id Git.Git), then re-run this command.'
  return
}

# 2. Clone, or update an existing checkout in place.
if (Test-Path (Join-Path $dest '.git')) {
  Write-Host "==> Updating existing install at: $dest"
  git -C $dest pull --ff-only
} else {
  Write-Host "==> Cloning into: $dest"
  git clone $repo $dest
}

# 3. Build and launch. launch.bat bootstraps uv + Python + the venv, installs
#    dependencies, downloads the models, and starts the app.
Set-Location $dest
Write-Host '==> Starting Meeting Assistant (the first run sets up the environment and can take a few minutes)...'
& cmd /c launch.bat
