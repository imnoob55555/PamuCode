# PamuCode

[中文](README.md) | English

PamuCode is a terminal coding agent written in Python. It puts a model-driven agent loop, file and shell tools, sessions, context management, skills, and delegated work into a layered runtime where each layer can be read and changed on its own.

The directory you start `pamu` in is the workspace, and runtime state is stored in its `.pamu/` directory. The interface messages are in Chinese.

Version 0.1.0 is experimental and requires Python 3.13 or newer.

## Features

- Streaming responses with retry, fallback model, continuation, and context recovery.
- Adapters for Anthropic Messages, OpenAI Responses, OpenAI Chat Completions, and native Ollama.
- `/login` saves several services locally; `/model` switches between the models of every configured service.
- A rounded prompt box with slash suggestions, `@` file completion, and input history. The footer shows workspace, git branch, context usage, model, thinking level, and work mode.
- Tool calls render as `● Read(path)` with a `⎿` summary line, and edits show `+added −removed`. Streamed Markdown is formatted line by line with syntax-highlighted code blocks.
- Planning and execution modes, `/undo` for file changes, resumable sessions, context compaction, and pinned constraints.
- Cross-session memory with offline consolidation, todos, shared tasks, scheduled jobs, background commands, subagents, teammates, and Git worktrees.
- Tool hooks, an MCP discovery scaffold, bounded headless execution, and an evaluation harness.

## Quick start

Install Python 3.13+, Git, and [uv](https://docs.astral.sh/uv/), then:

```bash
git clone https://github.com/imnoob55555/PamuCode.git
cd PamuCode
uv sync
uv tool install --editable .
uv tool update-shell
```

Open a new shell if `pamu` is not on your PATH yet. The same commands work in PowerShell. From the checkout, `uv run python -m agent_app` also starts the CLI.

Then start it from the project you want to work on:

```bash
cd /path/to/your/project
pamu
```

On first launch PamuCode asks you to run `/login`. Pick a provider, enter its endpoint and API key if needed (input is masked), choose a model, and start chatting.

## Connecting models

### Providers

| Name | Service | Protocol |
| --- | --- | --- |
| `anthropic` | Anthropic | Messages |
| `deepseek` | DeepSeek | Messages |
| `moonshot` | Moonshot Kimi | Messages |
| `zhipu` | Zhipu GLM | Messages |
| `openrouter` | OpenRouter | Messages |
| `openai` | OpenAI | Responses |
| `openai-compatible` | Any OpenAI-compatible service | Chat Completions or Responses, chosen at login |
| `ollama` | Local Ollama | Native `/api/chat` |

Models need native tool calling to do coding work. A failed request never switches protocols on its own. The five `/thinking` levels only apply to Messages backends; other protocols accept `off`.

### `/login`, `/model`, and `/provider`

`/login` saves a service's endpoint, protocol, API key, and default model to `~/.pamu/credentials.json`. You can log in to several providers and all of them stay saved. Interactive login is offline: it does not check that the key or model works, so you find out on the first real request. The `pamu login` command sends one verification request unless you pass `--no-verify`.

`/model` lists the models of every service that has API access configured locally, one line per "service · model". A service counts as configured if you logged in with `/login` or set its key in `.settings` or the environment. Each service contributes its saved model plus a built-in list of common models, and the last entry lets you type a custom model ID for the current service.

Choosing a model from another service rebuilds the connection with that service's saved key and endpoint. The change applies to the current session immediately, and that service becomes the default for the next launch. You can also type `/model <model-id>`. PamuCode stays on the current service if it offers that ID or if no service does, and only switches when another configured service lists it.

`/provider` switches by service and uses that service's saved model.

Each provider has a single saved record, so two OpenAI-compatible services with different endpoints cannot be saved at the same time. The later login replaces the earlier one.

### Local model services

PamuCode does not install or serve models. It connects to a service that is already running:

```bash
PAMU_PROVIDER=ollama MODEL_ID=your-installed-model pamu

PAMU_PROVIDER=openai-compatible MODEL_ID=your-served-model \
  OPENAI_COMPATIBLE_BASE_URL=http://localhost:8000/v1 \
  OPENAI_COMPATIBLE_PROTOCOL=openai_chat pamu
```

Ollama needs no key. Compatible services accept an optional `OPENAI_COMPATIBLE_API_KEY`. Endpoints must not contain credentials or query parameters.

## Commands

| Command | Purpose |
| --- | --- |
| `pamu` | Start an interactive session |
| `pamu continue` / `pamu -c` | Restore the latest session in this workspace |
| `pamu resume [ID]` / `pamu -r [ID]` | Pick a session, or restore one by ID |
| `pamu login [provider] [--no-verify]` | Save provider credentials and model |
| `pamu logout [provider]` | Remove saved credentials |
| `pamu status` | Show the effective configuration and login status |
| `pamu use <provider>` | Set the provider for future sessions |
| `pamu run ...` | Run one bounded headless task |

| Slash command | Purpose |
| --- | --- |
| `/login` | Configure a model service |
| `/model` | Choose a model from any configured service |
| `/provider` | Switch providers |
| `/thinking` | Choose the thinking level |
| `/status` | Show the current session, provider, and model |
| `/plan` / `/execute` | Enter planning mode / execution mode |
| `/skill [name] [task]` | Pick or invoke a skill; `/skill trust` trusts the workspace's skills |
| `/context` | Show the context budget; subcommands `window <tokens>`, `pin <text>`, `pins`, `unpin <id>` |
| `/compact [focus]` | Compact older history and keep recent work |
| `/resume [--last \| ID]` | Switch to another saved session |
| `/delete` | Delete the current session after confirmation and exit |
| `/undo` | Undo the previous turn's file-tool changes |
| `/memory [forget <path>]` | List or delete memory notes |
| `/dream` | Review recent sessions and consolidate memory |
| `/background` / `/stop <ID \| all>` | List or stop background commands |
| `/cron [cancel <ID \| all>]` | List or cancel scheduled jobs |
| `/help` / `/h` | Show the command list |
| `/quit` | Exit |

Arrow keys move through suggestions, Tab completes, Enter submits, and Esc closes menus. Menus also accept the number of an entry. Type `@` to browse workspace files; paths with spaces are quoted for you. Exit with `/quit` or by pressing Ctrl+C twice within a second.

## Usage

### Approvals and interruption

Read-only commands run without asking: `ls`, `cat`, `rg`, `find` without `-delete`/`-exec`, `git status`/`diff`/`log`/`show`, and similar. Everything else asks first and says why, for example an unknown program, a `>` redirect, command substitution, a subshell, or a command that cannot be parsed. Answer `a` to stop asking about the same program and subcommand (such as `uv run pytest`) for the rest of the session.

Commands that delete, move, or send data (`rm`, `mv`, `cp`, `curl`, `ssh`, `git push`, `git reset`, and so on) and commands that touch sensitive paths always ask. `sudo`, `su`, `shutdown`, `mkfs`, `dd` to a device, and recursive removal of `/` or the home directory are refused, including disguised forms such as `s\udo` or `/usr/bin/sudo`. Proposed file changes show a diff before you approve them.

Ctrl+C while the agent is working interrupts the current turn and returns to the same session. Completed edits and printed output are kept. Interrupted tool calls get explicit error results and are not replayed when you continue. An interrupted operation may already have had side effects, so check the actual state before retrying.

A turn allows 50 model calls, 200 tool calls, and 2,000,000 tokens by default. When a limit is reached PamuCode asks whether to continue.

### Planning and execution

New sessions start in execution mode. `/plan` restricts the agent to reading files, searching, loading skills, retrieving history, and managing todos; edits, shell commands, MCP calls, scheduling, and delegation are blocked. Leaving planning mode requires typing `/execute`. Saying "go ahead" in the conversation does not switch modes.

Execution mode does not authorize unrelated changes, pushes, or deployments. Limits you state in conversation depend on the model following them, so use `/plan` when you need read-only access enforced.

### Undo

Before a turn first changes a file through `write_file` or `edit_file`, PamuCode saves the original under `.pamu/checkpoints/`. `/undo` lists the files from the most recent turn and restores them after confirmation (files created in that turn are removed). Run it again to go further back. If a file was edited again afterwards, you get a warning first. Changes made by shell commands cannot be undone. Each session keeps the last 20 turns.

### Project instructions

Every request carries the project's instruction files. PamuCode walks from the git repository root down to the current directory and reads one `AGENTS.md` per level. A level without a usable `AGENTS.md` (missing, empty, or a symlink) falls back to its `CLAUDE.md`. The files are joined outermost first, each labelled with its relative path, so the ones closest to the current directory come last. Outside a git repository only the current directory is read. Instruction files in subdirectories below the current directory are not read.

The files go into the prompt in full and are never truncated. When they add up to more than 32 KB, PamuCode warns once at startup. Edits take effect on the next turn.

PamuCode also records a git snapshot at startup: current branch, default branch, `git status --short --branch` (clipped at about 2000 characters) and the last 5 commits. It is taken once per launch, never stored with the session, and taken again when you resume. The snapshot runs git with hooks, fsmonitor and signature checks disabled and a 5-second timeout. Confined mode reads only the current directory's instruction file and takes no snapshot.

### Sessions and context

Sessions are stored per workspace in `.pamu/sessions/`. Restoring a session replays its messages in order; press Ctrl+C to skip the replay. Past commands are never re-run, and background processes and teammates are not resumed.

`Context xx%` in the footer estimates how much of the model's context window the retained conversation, system instructions, and tool definitions take up. The estimate can go past 100%. `/context` shows the window size and where it came from, and `/context window <tokens>` sets it for models PamuCode does not know. `/compact` summarizes older conversation and keeps the current request and recent tool calls. Constraints pinned with `/context pin` keep their exact text through compaction and session restore.

### Memory

Memory has three layers, following Anthropic's [The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory):

- **Transcripts**: past sessions in `.pamu/sessions/`. The model can look them up with `transcript_search` and `transcript_read`. It sees what you typed, what it answered and which tools it called, but never tool output.
- **Notes**: Markdown files under `.pamu/memory/`, optionally grouped into topic folders (up to three levels). The model reads and writes them with the `memory` tool and decides for itself what is worth keeping: things you ask it to remember, or things clearly useful later, such as preferences, decisions and their reasons, verified rules, and pitfalls. Anything the code or git history already says does not belong there. Content that looks like a credential is refused. Every write shows up in the tool call log.
- **`MEMORY.md`**: the index of the notes. The first 200 lines (at most 25 KB) are loaded at the start of every session; other notes are read on demand. Until a `MEMORY.md` exists, a listing of the notes is loaded instead.

Notes written in the middle of a task can be wrong or go stale. `/dream` runs an offline consolidation pass: it checks notes against recent transcripts, fixes wrong ones, adds what was missed, merges duplicates, and trims `MEMORY.md` back to a short index. Notes are backed up to `.pamu/memory/.backup/` first (the five most recent copies are kept); if the pass fails or you press Ctrl+C, the notes are restored. When you leave the REPL, PamuCode consolidates automatically if three or more sessions with real dialogue have accumulated since the last pass, or if the last pass was more than 24 hours ago and there are new sessions. Press Ctrl+C to skip it.

`/memory` lists the notes and `/memory forget <path>` deletes one. With `PAMU_AUTO_MEMORY=off` the model can only read memory and nothing is consolidated on exit; `/dream` still works on request. Memory is read-only in plan mode, and the memory tools are not available in `confined` sandbox mode.

### Skills

A skill is a directory containing `SKILL.md`. PamuCode searches these directories, highest priority first:

| Directory | Scope |
| --- | --- |
| `<workspace>/.pamu/skills/` | This project, private |
| `<workspace>/.agents/skills/` | This project, shared through the repository |
| `<workspace>/.claude/skills/` | This project, shared with Claude Code |
| `~/.pamu/skills/` | All of your projects |
| `~/.agents/skills/` | All projects, shared with other agent tools |
| `~/.claude/skills/` | All projects, shared with Claude Code |

```markdown
---
name: Review
description: Review the current changes
---
Read the diff, identify defects, and report findings before editing.
```

The directory name is the skill's identifier, and the frontmatter `name` works as an alias. Skills are picked up when added, edited, or removed, without a restart. Invoke one with `/skill review Check the authentication changes`.

Skills that come with the workspace (the three project directories above) go into the system prompt, so they load only after you trust them. PamuCode lists them and asks the first time it finds them, or you can run `/skill trust` later. Trust is stored per workspace in `~/.pamu/trusted_workspaces.json`, and adding or editing a project skill asks again. User-level skills are always trusted.

### Background and scheduled work

Background commands and teammates run independently of the conversation. Use `/background` to list them and `/stop <ID>` or `/stop all` to stop them.

The model can schedule prompts with cron expressions. Jobs are one-shot and last only for the current session by default. Recurring and durable jobs need your approval; recurring jobs expire after seven days, and durable jobs are saved in `.pamu/scheduled_tasks.json`. A scheduled turn cannot raise approval prompts, so operations that need approval are refused. Use `/cron` to list jobs and `/cron cancel <ID>` to cancel one.

## Configuration

Precedence, highest first:

1. Process environment
2. `<workspace>/.pamu/.settings`
3. `~/.pamu/.settings`
4. Provider configuration saved in `~/.pamu/credentials.json`

[.settings.example](.settings.example) is a template; copy it to one of the `.settings` locations and fill it in if needed. Legacy `.env` files are not migrated. `MODEL_ID` overrides the model saved at login, so clear it to use the saved one.

| Setting | Purpose |
| --- | --- |
| `PAMU_PROVIDER` | Provider selection |
| `MODEL_ID` | Model ID, optional when the saved provider configuration has one |
| `FALLBACK_MODEL_ID` | Fallback model within the same provider |
| `<PROVIDER>_API_KEY` / `<PROVIDER>_BASE_URL` | Key and optional endpoint for that service |
| `OPENAI_COMPATIBLE_PROTOCOL` | `openai_chat` or `openai_responses` |
| `PAMU_CONTEXT_WINDOW` | Context window of the startup model |
| `PAMU_SANDBOX` | `off` (default) or `confined` |
| `PAMU_MAX_TURN_MODEL_CALLS` / `_TOOL_CALLS` / `_TOKENS` | Per-turn limits, defaults 50 / 200 / 2,000,000 |
| `PAMU_MAX_TEAMMATES` / `PAMU_MAX_BACKGROUND_TASKS` | Concurrent teammates and background commands, defaults 4 / 8 |
| `PAMU_TRUST_PROJECT_SKILLS` | `1` loads workspace skills without asking |
| `PAMU_AUTO_MEMORY` | `on` (default) or `off`: the model can only read memory and nothing is consolidated on exit |
| `PAMU_SHELL_APPROVAL` | `ask` (default) or `auto`. `auto` runs every shell command except the refused ones without asking, which is risky |
| `LOG_LEVEL` | Log verbosity |

## Headless execution and evaluation

`pamu run` runs one task through the same loop as the interactive session, with explicit budgets. Its tools are limited to `bash`, `read_file`, `write_file`, `edit_file`, and `glob`, and `--allow-tool` narrows them further. It reads provider configuration from the process environment only, not from saved credentials or `.settings`.

```bash
pamu run --task-file /tmp/task.txt --output /tmp/pamu-run \
  --model your-model --protocol anthropic_messages \
  --max-model-calls 30 --max-tool-calls 100 --max-tokens 100000 --timeout 300
```

Results go to `events.jsonl` and `result.json` in the output directory. A finished run does not prove the task was done correctly, and token accounting is not a precise cost cap.

The checkout includes `pamu_eval`, an evaluation harness that is not shipped with the runtime:

```bash
uv run python -m pamu_eval selftest --output /tmp/pamu-selftest
uv run python -m pamu_eval run evals/regression.json \
  --output /tmp/pamu-regression --repetitions 3 --jobs 2
uv run python -m pamu_eval report /tmp/pamu-regression
uv run python -m pamu_eval compare /tmp/pamu-baseline /tmp/pamu-candidate
```

`evals/regression.json` replays offline and measures runtime mechanisms. `evals/live.json` and `evals/intent-live.json` need a real model, selected with `--model` and `--protocol`. There are also replay export, performance sampling, SWE-bench prediction export, and a Harbor/Terminal-Bench bridge. These are adapters only and do not mean an official benchmark has been run.

## Security

- Credentials are kept outside the repository in `~/.pamu/credentials.json`, readable only by your user. Key input is masked and kept out of input history and logs.
- The default `PAMU_SANDBOX=off` has no OS-level file or network isolation. Shell children get an allowlisted environment, a temporary HOME, and a fixed PATH, and do not inherit provider credentials.
- Even without the sandbox, file tools refuse `.pamu`, `.env`, `.settings`, `.ssh`, `.aws`, `.gnupg`, and `.codex` in the workspace, and refuse writes to `.git/hooks` and `.git/config`. Shell commands that mention these paths always ask, but that is a text check; only `PAMU_SANDBOX=confined` actually stops a shell command from reading them.
- `confined` uses macOS Seatbelt to restrict shell file and network access. An unsupported platform or unavailable backend fails with an error instead of falling back to unisolated execution.
- The local boundary is not multi-tenant isolation and has no complete CPU, memory, process-count, or disk quotas. Run untrusted tasks in a container or VM.
- Never commit `.env`, `.settings`, credential files, `.pamu/`, logs, or evaluation output. Sessions and tool output can contain source code or private data, so review them before sharing.

## Architecture

```text
CLI and terminal UI
  → agent loop and context management
    → provider adapters and tool execution
      → workspace features and execution policy
```

| Path | Responsibility |
| --- | --- |
| `agent_app/cli.py`, `terminal.py` | Input, commands, status, and lifecycle |
| `agent_app/bootstrap.py` | Runtime composition |
| `agent_app/core/` | Loop, prompts, budgets, compaction, and recovery |
| `agent_app/adapters/` | Provider protocols and streaming |
| `agent_app/tools/` | Tool schemas, registry, dispatch, and hooks |
| `agent_app/security/` | Execution policy, file access, and shell boundary |
| `agent_app/features/` | Sessions, skills, memory, tasks, and collaboration |
| `pamu_eval/`, `evals/` | Evaluation harness and cases, checkout only |

The logo, title, and welcome hints live in `agent_app/branding.py`.

## Development

```bash
uv sync
uv run pytest -q
uv run pytest tests/test_cli_pty.py -q
uv tool install --editable .
```

Tests use fake model clients and temporary directories, with no real keys or network. PTY tests need a Unix terminal. There is no linter, type checker, or CI configured yet.

## License

[MIT](LICENSE). Built with [prompt-toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit), the [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python), the [OpenAI Python SDK](https://github.com/openai/openai-python), and [uv](https://github.com/astral-sh/uv). Interface and architecture ideas draw from Codex, Claude Code, and pi.
