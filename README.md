# PamuCode

PamuCode is a layered coding-agent runtime extracted into an independent project.

## Setup

PamuCode keeps your user-level defaults outside of individual projects. Create
the global configuration once, then install the command as an editable UV tool:

```bash
mkdir -p ~/.config/pamucode
cp .env.example ~/.config/pamucode/.env
uv tool install --editable /Users/wh/python/PamuCode
uv tool update-shell
```

Edit `~/.config/pamucode/.env` to set `ANTHROPIC_API_KEY` and `MODEL_ID`.
The editable install means later changes to this checkout are used directly by
the global `pamu` command.

Configuration is loaded in this priority order:

1. Environment variables already set in the process.
2. `<project>/.pamu/.env`, for project-specific overrides.
3. `~/.config/pamucode/.env`, for user-wide defaults.

The first value found wins, so a project configuration can override the global
default while an explicitly exported environment variable overrides both.

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

Legacy top-level state directories and files are not migrated automatically.
They are left in place so you can review or remove them deliberately.

## Test

```bash
uv run pytest
```

The implementation is organized under `agent_app/adapters`, `core`, `features`,
and `tools`. `agent_app/bootstrap.py` is the composition root and
`agent_app/cli.py` owns the command-line lifecycle.
