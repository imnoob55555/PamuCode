# PamuCode

PamuCode is a terminal based coding agent runtime inspired by tools such as
Codex and Claude Code. It gives an AI model a practical project workspace with
file tools, an interactive REPL, persistent memory, tasks, background work,
provider switching, and recovery for long running conversations.

The project is intentionally small and inspectable. The runtime is layered so
you can follow the path from a terminal prompt to model streaming, tool calls,
state updates, and the final response.

```text
  ____    _    __  __ _   _
 |  _ \  / \  |  \/  | | | |
 | |_) |/ _ \ | |\/| | | | |
 |  __// ___ \| |  | | |_| |
 |_|  /_/   \_\_|  |_|\___/

 PamuCode · AI Coding Agent
```

## What it provides

- Interactive terminal startup screen with project, provider, model, and configuration status.
- Anthropic-compatible model adapters for Anthropic, DeepSeek, Moonshot, Zhipu, and OpenRouter.
- Interactive provider and model onboarding through `/login`, with local credential storage.
- Streaming responses with working indicators, retries, continuation, compaction, and recovery.
- Project tools for files, shell commands, patches, search, and other agent operations.
- Persistent project memory, task tracking, transcripts, scheduled work, background tasks, and worktrees.
- MCP integration, hooks, team messaging, and synchronous subagents.
- Slash command completion and session controls that stay out of the model conversation.

## Quick start

PamuCode requires Python 3.13 or newer, Git, and [uv](https://docs.astral.sh/uv/).

### macOS and Linux

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
pamu
```

### Windows PowerShell

```powershell
git clone https://github.com/imnoob55555/PamuCode.git
Set-Location PamuCode
uv sync

New-Item -ItemType Directory -Force -Path "$HOME\.pamu" | Out-Null
if (!(Test-Path "$HOME\.pamu\.settings")) {
    Copy-Item .settings.example "$HOME\.pamu\.settings"
}

uv tool install --editable .
uv tool update-shell
pamu
```

If `uv` is not installed, follow the official [installation guide](https://docs.astral.sh/uv/getting-started/installation/).

## First run

Start `pamu` from the project directory. The current directory is PamuCode's
workspace. If no local credentials are available, the REPL shows a setup hint:

```text
PamuCode · AI Coding Agent

Working directory: /path/to/your/project
Provider: not configured
Model: not configured
Status: configure a provider with /login

Type a request to start coding
Type /help for commands
Type /quit to exit
```

Run `/login` to choose a provider, save an API key locally, and select a model.
The interactive setup is offline: it uses built-in model choices and does not
send a verification request. Non-interactive `pamu login <provider>` keeps the
existing verification workflow; add `--no-verify` when you explicitly want to
save credentials without verification.

## Configuration

PamuCode accepts environment-style settings from these locations, in priority
order:

1. Environment variables already present in the process.
2. Project settings at `<project>/.pamu/.settings`.
3. User settings at `~/.pamu/.settings`.
4. Saved provider credentials in `~/.pamu/credentials.json`.

Higher-priority values win. A project settings file can override user-wide
defaults, while an explicitly exported environment variable overrides both.

The template contains no secret:

```dotenv
ANTHROPIC_API_KEY=
MODEL_ID=claude-sonnet-4-6
# FALLBACK_MODEL_ID=claude-haiku-4-5
# ANTHROPIC_BASE_URL=https://api.anthropic.com
# LOG_LEVEL=INFO
```

You can also configure a provider interactively:

```text
/login
```

Credentials are stored in `~/.pamu/credentials.json` with owner-only file
permissions. API keys are masked during input and excluded from input history
and application logs.

## CLI commands

```bash
pamu                         # Start the interactive agent
pamu login [provider]        # Configure a provider and model
pamu logout [provider]       # Remove one provider or all saved providers
pamu status                  # Show the active provider and saved accounts
pamu use <provider>          # Set the provider used on the next startup
```

Inside the REPL:

```text
/login                       Configure an API key and model
/status                      Show the active provider and model
/model                       Choose or switch the current model
/provider                    Choose or switch the provider
/help                        Show available commands
/quit                        Exit the current session
```

Slash commands are local controls. They do not enter the model context or the
up/down input history. Successful command output remains visible in the
terminal; cancelled or unsuccessful commands show temporary feedback above the
input area that disappears automatically after three seconds. Empty input is
ignored, `q` and `exit` are ordinary prompts, and pressing `Ctrl+C` twice within
one second shuts down the session.

## Workspace and state

PamuCode stores project runtime state under `.pamu/` in the current workspace.
Depending on the enabled features, this directory can contain:

```text
.pamu/
├── logs/
├── memory/
├── transcripts/
├── task_outputs/
├── tasks/
├── mailboxes/
├── worktrees/
└── scheduled_tasks.json
```

The state directory is ignored by Git and has its own protective `.gitignore`.
PamuCode validates paths and symlinks to keep tool state inside the workspace,
but it is not a security sandbox against a workspace owner changing files at
the same time.

Legacy `.env` files are not migrated automatically. Move them to `.settings`
deliberately and do not overwrite an existing settings file.

## Project layout

```text
agent_app/
├── adapters/       Model and provider adapters
├── core/           Agent loop, prompt context, compaction, recovery
├── features/       Memory, tasks, teams, MCP, scheduling, worktrees
├── tools/          Tool schemas, registry, execution, and hooks
├── branding.py     Startup Logo and welcome screen rendering
├── terminal.py     prompt-toolkit input and menu UI
└── cli.py          CLI lifecycle and slash command routing
```

The composition root is `agent_app/bootstrap.py`; the command entry point is
`agent_app.cli:main`.

## Development

Install the development environment and run the complete test suite:

```bash
uv sync
uv run pytest -q
```

The test suite uses fake model clients, temporary credentials, and PTYs where
needed. It does not require a real API key or a live model request.

For a focused terminal check:

```bash
uv run pytest tests/test_cli_pty.py -q
```

To try local changes through the installed command, use the editable install:

```bash
uv tool install --editable .
pamu
```

The automated acceptance coverage lives in the `tests/` directory.

## Security and repository hygiene

Do not commit API keys, access tokens, credential files, or local `.pamu/`
state. The repository ignores `.settings`, `.env`, `.pamu/`, virtual
environments, caches, and generated state. Before publishing changes, inspect
the staged diff and confirm that all credential values are empty or redacted.

PamuCode is an experimental coding-agent runtime. Review tool permissions and
provider configuration before using it in a sensitive workspace.
