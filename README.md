# PamuCode

A Python terminal coding agent with an inspectable, layered runtime.

PamuCode combines a model-driven agent loop with file and shell tools, workspace
sessions, context management, skills, and delegated work. Run it inside a project:
**the current directory is the workspace**, and runtime state lives in `.pamu/`.
The interface currently uses Chinese messages; this guide is primarily in English.

Experimental release: **0.1.0**, Python **3.13+**.

## Features

- Streaming model responses with retry, fallback, continuation, and context recovery.
- Anthropic Messages, OpenAI Responses, Chat Completions, and native Ollama adapters.
- Inline terminal input in a rounded prompt box with slash suggestions, `@` file
  completion, input history, and a single footer showing workspace, git branch,
  context usage, model, thinking level, and work mode.
- Styled transcript: `>` marks your messages, `●` marks model replies and shell
  statuses, and streamed Markdown (headings, lists, inline code, fenced code with
  syntax highlighting) is formatted line by line.
- Tool calls appear as `● Read(path)` with a `⎿` summary line (lines read, files
  found, `+added −removed` for edits, a checklist for todos). Shell commands keep
  their `Running`/`Ran` status and only get a tool line when they were denied,
  blocked, or sent to the background. Resuming a session replays these lines too; redirected
  output keeps the plain `Shell（status）` records.
- Explicit planning and execution modes, plus direct skill invocation.
- Durable sessions, interrupted tool-call repair, context budgets, compaction,
  pinned constraints, and retrievable history evidence.
- Memory, todos, shared tasks, scheduled work, background commands, synchronous
  subagents, teammates, and Git worktrees.
- Tool hooks and an MCP discovery scaffold with bundled mock servers.
- Bounded headless execution and a checkout-only evaluation harness.

## Quick start

Install Python 3.13+, Git, and [uv](https://docs.astral.sh/uv/), then:

```bash
git clone --branch feat/eval-harness https://github.com/imnoob55555/PamuCode.git
cd PamuCode
uv sync
uv tool install --editable .
uv tool update-shell
pamu
```

This README describes the `feat/eval-harness` development branch. Open a new shell
if `pamu` is not yet on your PATH. The same `uv` commands work in PowerShell.
From the checkout, `uv run python -m agent_app` also starts the CLI.

Use `/login` to choose a provider, configure its endpoint and model, and enter a
key through masked input when required. Interactive setup is offline; it does not
verify that the model or credentials work. Non-interactive `pamu login` performs
verification unless `--no-verify` is supplied.

After installation, start `pamu` from the project you want to work on:

```bash
cd /path/to/your/project
pamu
```

The startup screen contains the logo and basic controls. Workspace and model
information appear below the input box. Menus accept arrow keys or the number
of an entry. Cancelled or unsuccessful commands show
feedback above the input box for three seconds; successful results stay in the
terminal transcript. Slash commands can be recalled through input history.
Exit with `/quit` or two presses of Ctrl+C within one second at the REPL.

The terminal shows an animated `Working…` status with elapsed time while
waiting for the model. Shell commands show a temporary `● Running <command>`
status that stays visible briefly for fast commands and is replaced by
`● Ran <command>` on exit; the dot turns red for a non-zero exit code.
Redirected output records the final status as plain text without colors or
glyphs. Set `NO_COLOR` to keep the layout but drop colors.
Operations requiring approval show the permission prompt and wait for your
answer. Proposed file changes include a diff.

While the agent is working, Ctrl+C interrupts the current turn and returns to the
same session's prompt. It keeps completed edits, captured command output, and
visible partial model text. Interrupted tool calls receive explicit error results;
continuing or resuming does not automatically replay them. Cancelling a permission
prompt leaves that tool unexecuted. Incomplete operations may have side effects:
inspect their actual state before retrying.

Each agent has up to three attempts per turn for the same tool and arguments.
The model can use the error from a blocked attempt to change approach. Two
consecutive batches containing only blocked repeats end the turn.

Background commands and teammates run independently of the foreground turn.
Use `/background` to list this session's background commands, `/stop bg_0001` to
stop one, or `/stop all` to stop all background commands. A cancellation request
is not reported as finished until the worker acknowledges it. Exiting requests
cleanup of background commands too. Shell cleanup covers the process group;
detached descendants and remote jobs may survive. These controls do not roll back
changes or stop independent teammates. Mid-turn text steering and Esc-to-interrupt
are not implemented; use Ctrl+C, then enter your new instruction.

## Commands

| Command | Purpose |
| --- | --- |
| `pamu` | Start an interactive session |
| `pamu continue` or `pamu -c` | Restore the most recently updated workspace session |
| `pamu resume [ID]` or `pamu -r [ID]` | Pick a workspace session or restore it by ID |
| `pamu login [provider] [--no-verify]` | Save provider credentials and model |
| `pamu logout [provider]` | Remove saved credentials |
| `pamu status` | Show effective configuration and login status |
| `pamu use <provider>` | Set the provider for future sessions |
| `pamu run ...` | Execute one bounded headless task |

| Slash command | Purpose |
| --- | --- |
| `/login` | Configure provider access |
| `/status` | Inspect current session and provider configuration |
| `/model` | Select a model |
| `/provider` | Switch providers |
| `/thinking` | Select thinking effort |
| `/plan` | Enter read-only exploration and planning mode |
| `/execute` | Permit implementation within the user's request |
| `/skill [name] [task]` | Select or explicitly invoke a skill |
| `/context` | Inspect the context budget |
| `/context window <tokens>` | Set a session window override |
| `/context pin <text>` | Preserve a constraint verbatim |
| `/context pins` / `/context unpin <id>` | List or remove pinned constraints |
| `/compact [focus]` | Compact earlier conversation |
| `/resume [--last \| ID]` | Switch to another saved session |
| `/delete` | Confirm deletion of the current session and exit |
| `/background` | List this session's background commands |
| `/stop <ID \| all>` | Stop selected background commands |
| `/help` or `/h` | Show a concise command list |
| `/quit` | Exit the session |

Use arrow keys to select suggestions, Tab to complete, Enter to submit, and Esc
to dismiss menus. Type `@` to browse workspace files and directories; paths with
spaces are quoted automatically.

### Planning and execution

New sessions default to execution mode. Model instructions distinguish questions,
investigation, planning, and authorized implementation. Execution mode itself does
not authorize unrelated changes, pushes, or deployments.

`/plan` enforces a restricted tool set: file reading, glob search, skill loading,
history retrieval, compaction, and session todos. It blocks source edits, shell
commands, MCP calls, scheduling, and delegation. Runtime checkpoints still write
inside `.pamu/`. Finish active background work and teammates before switching modes.
Use `/execute` explicitly to leave planning mode; a conversational confirmation
alone does not switch it.

Modes, pinned constraints, and the current request survive compaction and session
restore. Delegates receive the original request and pinned constraints. In execution
mode, natural-language limits still depend on model compliance; pinned text is not
an execution deny rule. Use `/plan` when you need enforced read-only tool access.

### Skills

A skill is a directory containing `SKILL.md`. PamuCode searches these
directories, highest priority first:

| Directory | Scope |
| --- | --- |
| `<workspace>/.pamu/skills/` | This project, private (ignored by `.pamu/.gitignore`) |
| `<workspace>/.agents/skills/` | This project, shareable through the repository |
| `<workspace>/.claude/skills/` | This project, shared with Claude Code |
| `~/.pamu/skills/` | All projects for the current user |
| `~/.agents/skills/` | All projects, shared with other agent tools |
| `~/.claude/skills/` | All projects, shared with Claude Code |

When two directories define the same skill name, the higher-priority one wins,
so project skills override user skills. A visible `skills/` folder in the
project root is not read, so ordinary project folders are never mistaken for
skills.

```markdown
---
name: Review
description: Review the current changes
---
Read the diff, identify defects, and report findings before editing.
```

The directory name (for example `review` in `.pamu/skills/review/SKILL.md`) is
the identifier; the frontmatter `name` is accepted as an alias, case-insensitively.
Without a `description`, the first line of the body is used. Skills are picked up
when they are added, edited, or removed, without a restart. An unreadable
`SKILL.md` is skipped and reported instead of stopping PamuCode.

```text
/skill review Check the authentication changes
```

`/skill` opens the picker and shows where each skill comes from. The skill body
and task are supplied to the current model. Work mode and security policy remain
in force. Without an interactive UI, `/skill` lists available names.
You can also ask PamuCode which skills are available. It reads the registry
through `list_skills`, which also names the searched directories when it is empty.
Skill loading is unavailable in the confined sandbox.

## Configuration

Configuration precedence is:

1. Process environment.
2. `<workspace>/.pamu/.settings`.
3. `~/.pamu/.settings`.
4. Saved provider configuration in `~/.pamu/credentials.json`.

The committed [.settings.example](.settings.example) has empty values. Copy it to
one of the settings locations if needed. Legacy `.env` files are not automatically
migrated. An explicit `MODEL_ID` overrides a saved model; clear it to use the model
selected during login. `/model` and `/provider` take effect in the current session.

| Setting | Purpose |
| --- | --- |
| `PAMU_PROVIDER` | Provider selection |
| `MODEL_ID` | Model ID, unless supplied by saved provider configuration |
| `FALLBACK_MODEL_ID` | Optional fallback within the same provider |
| `<PROVIDER>_API_KEY` | Credential for the selected service |
| `<PROVIDER>_BASE_URL` | Optional endpoint override |
| `OPENAI_COMPATIBLE_PROTOCOL` | `openai_chat` or `openai_responses` |
| `PAMU_CONTEXT_WINDOW` | Explicit context window for the startup model |
| `PAMU_SANDBOX` | `off` (default) or `confined` |
| `LOG_LEVEL` | Runtime log verbosity |

Provider entries: `anthropic`, `deepseek`, `moonshot`, `zhipu`, `openrouter`,
`openai`, `openai-compatible`, and `ollama`. The first five use Messages;
`openai` uses Responses; compatible services use the explicitly chosen protocol;
Ollama uses native `/api/chat`. Failed requests do not automatically switch protocols.
Thinking controls currently apply to Messages backends; other protocols accept `off`.
Models need native tool-calling support to perform coding tasks.

For an already-running local model service:

```bash
PAMU_PROVIDER=ollama MODEL_ID=your-installed-model pamu

PAMU_PROVIDER=openai-compatible MODEL_ID=your-served-model \
  OPENAI_COMPATIBLE_BASE_URL=http://localhost:8000/v1 \
  OPENAI_COMPATIBLE_PROTOCOL=openai_chat pamu
```

Ollama requires no key; compatible services can use an optional
`OPENAI_COMPATIBLE_API_KEY`. PamuCode does not install or serve models.
Endpoints must not embed credentials in their URL.

## Sessions and context

Sessions are scoped to the workspace and stored in `.pamu/sessions/`. Use
`pamu continue` (or `pamu -c`) to restore the latest one. Use `pamu resume`
(or `pamu -r`) to choose a session, or append an ID to restore it directly.
On restore, PamuCode prints saved messages in order, including messages archived
during context compaction. Shell commands have their own `Shell` entries,
separate from PamuCode's replies. Replay preserves line breaks
and code indentation, then opens the prompt in the same session. Press Ctrl+C
during replay to skip to the prompt. Historical commands are never re-executed.
Restoring also retains the model selection and repairs interrupted tool-result
pairs. Background processes and teammates are not automatically resumed.

In the interactive session, use `/delete` and confirm to permanently remove its
snapshot and session archive, then exit. If work is still running or queued,
finish it before deleting the session. Workspace files are outside the scope
of session deletion.

`Context xx%` in the footer estimates the share of the active model's
context window occupied by the retained conversation, system instructions, and
tools. It refreshes before each prompt. The window comes from the current
provider and model configuration or a provisional default. Use `/context` to
check its source and `/context window <tokens>` to set a session override.
The percentage can exceed 100% when the estimate exceeds the window.

`/context` reports estimated usage, window source, and output reserve. Usage
reported by the provider is tracked separately. `/compact` summarizes older
conversation, preserves the current request and recent tool batches, and stores
references to historical evidence. Pinned constraints retain their original text.
Unknown model windows use a provisional budget; set the actual window when needed.

## Headless execution and evaluation

`pamu run` uses the production loop with explicit budgets and a reduced tool set.
It reads provider configuration from the process environment, not saved credentials
or `.settings`. Supply credentials through your environment or secret manager;
never put them in task files, command arguments, or suite manifests.

```bash
pamu run --task-file /tmp/task.txt --output /tmp/pamu-run \
  --model your-model --protocol anthropic_messages \
  --max-model-calls 30 --max-tool-calls 100 --max-tokens 100000 --timeout 300
```

Headless execution supports Messages, Chat Completions, and Responses. Configure
the provider and endpoint to match the requested protocol. Tools are limited to
`bash`, `read_file`, `write_file`, `edit_file`, and `glob`; repeat `--allow-tool NAME`
to narrow the set. Background work, teams, subagents, and automatic memory extraction
are disabled. Events and results are written to `events.jsonl` and `result.json`.
A completed run is not proof of task correctness. Missing usage stays unknown;
token accounting does not provide a precise cost cap, and a response can cross a
budget before its usage is reported.

The source checkout includes `pamu_eval`; it is not shipped in the runtime wheel:

```bash
uv run python -m pamu_eval selftest --output /tmp/pamu-selftest
uv run python -m pamu_eval run evals/regression.json \
  --output /tmp/pamu-regression --repetitions 3 --jobs 2
uv run python -m pamu_eval report /tmp/pamu-regression
uv run python -m pamu_eval compare /tmp/pamu-baseline /tmp/pamu-candidate
```

Regression replay is offline and measures runtime mechanisms. Real-model cases are
in `evals/live.json` and `evals/intent-live.json`; use `--model` and `--protocol` with
the runner. Intent cases cover explanation, diagnosis, planning, and authorized
editing. File checks do not assess explanation quality or every side effect;
inspect the answer and trace too. No live-model success rate is claimed here.

Each trial uses a fresh workspace, external grading, and evidence receipts. Resume
only reuses matching, complete trials; incomplete trials require an explicit rerun
in a new directory. Receipts detect accidental changes, not malicious tampering.
The CLI also includes replay export, performance sampling, SWE-bench prediction
export, and a Harbor/Terminal-Bench bridge. These adapters do not imply a completed
official benchmark run or full ATIF support.

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
| `agent_app/tools/` | Schemas, registry, dispatch, and hooks |
| `agent_app/security/` | Execution policy, file access, and shell boundary |
| `agent_app/features/` | Sessions, skills, memory, tasks, and collaboration |
| `agent_app/working_mode.py` | User-selected planning/execution scope |
| `pamu_eval/`, `evals/` | Checkout-only evaluation runner and fixtures |

Customize the logo, title, and welcome hints in `agent_app/branding.py`.

## Development

```bash
uv sync
uv run pytest -q
uv run pytest tests/test_cli_pty.py -q
uv tool install --editable .
```

Tests use fake model clients and temporary workspaces without real keys or network.
PTY tests require a Unix terminal environment. There is currently no configured
linter, type checker, or CI workflow. Live provider, container, and official benchmark
validation require their own configured environments.

## Security and repository hygiene

- Credentials are stored outside the repository in `~/.pamu/credentials.json`
  with owner-only permissions. Key entry is masked and excluded from input history.
- Keep `.env`, `.settings`, credential files, `.pamu/`, logs, transcripts, and
  evaluation artifacts private. Review staged changes before publishing.
- `docs/` and `.superpowers/` contain local/internal documentation and are ignored.
  Public project documentation belongs in this README. `.settings.example` must
  keep all values empty.
- Sessions, tool traces, and evaluation outputs can contain source code or private
  task data even when credential patterns have been redacted. Review them before sharing.

Execution defaults to `PAMU_SANDBOX=off`, which has no OS filesystem or network
isolation. Shell children still receive an allowlisted environment, a temporary
HOME, and a fixed executable search path instead of inherited provider credentials.
Commands that depend on the original shell environment may need explicit paths.

`PAMU_SANDBOX=confined` uses macOS Seatbelt for shell file/network restrictions
and descriptor-based file tools. Unsupported platforms or unavailable backends
fail closed. Credential paths and runtime control code are protected; worktree
and skill operations without a confined implementation are disabled. Team approval
authority stays in process memory and cannot be granted by forged mailbox messages.

The local boundary is not hostile multi-tenant isolation. Process-group cleanup
cannot guarantee termination of detached descendants, and local execution has no
complete CPU, memory, process-count, or disk quotas. Use an external container or VM
for untrusted tasks. The optional Docker evaluation backend supports offline replay
and file graders only, requires a preinstalled digest-pinned image and enforceable
quotas, and does not automatically pull images or expose model credentials.

## License and acknowledgments

[MIT](LICENSE). Built with [prompt-toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit),
[Anthropic's Python SDK](https://github.com/anthropics/anthropic-sdk-python),
[OpenAI's Python SDK](https://github.com/openai/openai-python), and
[uv](https://github.com/astral-sh/uv). Interface and architecture ideas draw from
Codex, Claude Code, and pi.
