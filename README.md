# PamuCode

PamuCode is a minimal‑baseline Agent implementation similar to Codex and Claude Code.

## Setup

PamuCode requires [Git](https://git-scm.com/) and
[UV](https://docs.astral.sh/uv/getting-started/installation/). Choose the
instructions for your operating system.

### macOS / Linux

Install UV if it is not already available:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new terminal if the installer asks you to refresh `PATH`, then run:

```bash
git clone https://github.com/imnoob55555/PamuCode.git
cd PamuCode
uv sync
mkdir -p ~/.pamu
if [ ! -e ~/.pamu/.settings ]; then
  cp .settings.example ~/.pamu/.settings
fi
uv tool install --editable .
uv tool update-shell
```

Edit `~/.pamu/.settings` and set `ANTHROPIC_API_KEY` and `MODEL_ID`.
Restart the terminal after `uv tool update-shell` before running `pamu`.

### Windows (PowerShell)

Install UV if it is not already available:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Open a new PowerShell window if the installer asks you to refresh `PATH`, then
run:

```powershell
git clone https://github.com/imnoob55555/PamuCode.git
Set-Location PamuCode
uv sync
New-Item -ItemType Directory -Force -Path "$HOME\.pamu" | Out-Null
if (!(Test-Path "$HOME\.pamu\.settings")) {
    Copy-Item -Path .settings.example -Destination "$HOME\.pamu\.settings"
}
uv tool install --editable .
uv tool update-shell
```

Edit `$HOME\.pamu\.settings` and set `ANTHROPIC_API_KEY` and `MODEL_ID`.
Restart PowerShell after `uv tool update-shell` before running `pamu`.

The editable install means later changes to this checkout are used directly by
the global `pamu` command.

Configuration is loaded in this priority order:

1. Environment variables already set in the process.
2. `<project>/.pamu/.settings`, for project-specific overrides.
3. `~/.pamu/.settings`, for user-wide defaults.

The first value found wins, so a project configuration can override the global
default while an explicitly exported environment variable overrides both.

### Migrating from `.env`

PamuCode no longer reads `~/.config/pamucode/.env` or
`<project>/.pamu/.env`. Move an existing configuration explicitly before
upgrading. Do not overwrite a `.settings` file that already exists.

On macOS or Linux:

```bash
mkdir -p ~/.pamu
if [ -e ~/.pamu/.settings ]; then
  echo "Refusing to overwrite ~/.pamu/.settings" >&2
else
  mv ~/.config/pamucode/.env ~/.pamu/.settings
fi
# Run inside a project only when it has a project-specific configuration:
if [ -e .pamu/.settings ]; then
  echo "Refusing to overwrite .pamu/.settings" >&2
else
  mv .pamu/.env .pamu/.settings
fi
```

On Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force -Path "$HOME\.pamu" | Out-Null
if (Test-Path "$HOME\.pamu\.settings") {
    throw "Refusing to overwrite $HOME\.pamu\.settings"
}
Move-Item "$HOME\.config\pamucode\.env" "$HOME\.pamu\.settings"
# Run inside a project only when it has a project-specific configuration:
if (Test-Path ".pamu\.settings") {
    throw "Refusing to overwrite .pamu\.settings"
}
Move-Item ".pamu\.env" ".pamu\.settings"
```

## Run

```bash
cd /path/to/project
pamu
```

While waiting for a model response, interactive terminals show an animated
`Working` status. Redirected and other non-interactive output stays clean.

PamuCode uses the current directory as the project workspace. Its local runtime
state is stored beneath `.pamu/`, including `memory/`, `transcripts/`,
`task_outputs/`, `tasks/`, `mailboxes/`, `worktrees/`, and
`scheduled_tasks.json`. The state directory contains its own protective
`.gitignore`; it should remain untracked by the project repository.

Static symlink and path validation prevents accidental state escape, but
PamuCode is not a sandbox against the workspace owner concurrently mutating
paths while it runs. This boundary does not weaken file-tool workspace
containment.

Legacy top-level state directories and files are not migrated automatically.
They are left in place so you can review or remove them deliberately.

## Test

```bash
uv run pytest
```

The implementation is organized under `agent_app/adapters`, `core`, `features`,
and `tools`. `agent_app/bootstrap.py` is the composition root and
`agent_app/cli.py` owns the command-line lifecycle.
