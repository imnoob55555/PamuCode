# AGENTS.md

PamuCode is a Python 3.13+ terminal coding-agent runtime (layered: CLI → agent loop → adapters/tools/features). The repository root is the install root, but the CLI treats **the current working directory as the workspace** and creates runtime state in `<cwd>/.pamu/`.

## Commands

- Setup: `uv sync` — installs the project plus the `dev` dependency group (pytest only).
- Full suite: `uv run pytest -q` — self-contained; uses fake model clients and temp dirs, no API key or network required.
- Focused run: `uv run pytest tests/test_cli_commands.py -q`; terminal/PTY check: `uv run pytest tests/test_cli_pty.py -q`.
- Try changes through the real command: `uv tool install --editable .` then `pamu` (README "Development"). Running the REPL inside this repo creates a local `.pamu/` state directory.
- There is no linter, formatter, or type-checker configuration in `pyproject.toml`, and no CI workflow in the repository. Do not assume lint/type-check gates exist.

## Repository structure

- `agent_app/cli.py:main` is the entry point (`python -m agent_app` delegates to it). Commands: `login`, `logout`, `status`, `use`; otherwise it starts the REPL.
- `agent_app/bootstrap.py` is the composition root (`build_default_runtime`) that wires config, adapters, tools, hooks, and features.
- `agent_app/core/` holds the agent loop, prompt/context, compaction, recovery. `agent_app/adapters/` holds provider/model adapters and provider specs. `agent_app/features/` holds stateful features (auth, memory, tasks, teams, scheduler, MCP, subagents, worktrees, todos). `agent_app/tools/` holds tool schemas, registry, executor, hooks. `agent_app/terminal.py` is the prompt-toolkit UI.
- `agent_app/config.py` distinguishes `repo_root` (install root) from `workdir` (current directory) and derives all `.pamu/` state paths from the workspace.

## Configuration and credentials

Priority order (README "Configuration"): process environment > `<workspace>/.pamu/.settings` > `~/.pamu/.settings` > `~/.pamu/credentials.json`.

- `.settings.example` is the committed template; keep its values empty. `.env` is legacy and is not migrated automatically.
- Credentials live in `~/.pamu/credentials.json` with owner-only permissions; API keys are masked during input and excluded from input history and logs. Never read, print, or commit real values.
- `MODEL_ID` is required unless a logged-in provider supplies a model; a missing configuration exits with status 2 rather than crashing.

## Tests

- There is no `conftest.py`; helpers such as `tests/agent_app_fakes.py` and fixtures are defined per file. Tests set `MODEL_ID` via `monkeypatch.setenv` where needed.
- Tests that build a runtime or call `cli.main` inject `credentials_path=...` or monkeypatch `bootstrap.CREDENTIALS_PATH`. Follow that pattern — the default `auth.CREDENTIALS_PATH` points at the developer's real home directory.
- `cli.main`'s keyword injection surface (`runtime_factory`, `run_turn`, `start_threads`, `stop_threads`, `ui`, `credentials_path`, `configuration_check`) is exercised by tests; keep it compatible.
- `tests/test_cli_pty.py` drives a real PTY (`pty`, `termios`, `fcntl`) and cannot run on Windows.
- Terminal styling (`agent_app/theme.py`, `agent_app/rendering.py`) only applies when stdout is a TTY; captured output stays plain, so most tests are unaffected. PTY tests set `NO_COLOR=1` for exact-byte assertions and wait for the `❯` prompt marker (the welcome panel also draws `─`). pytest re-captures `sys.stdout` per test phase, so patch it inside the test body, not in a fixture. Tool call lines (`agent_app/tool_display.py`) hang off the `ToolStart`/`ToolResult` hook events, which only the main loop (`core/loop.py`) fires — subagents and teammates never trigger them.
- Tests assert exact user-facing strings. CLI/REPL output is written in Chinese (`agent_app/cli.py`, `agent_app/branding.py`, `agent_app/onboarding.py`); change messages and their assertions together.

## Generated and untracked files

- `.pamu/` (runtime state, has its own `.gitignore` ignoring everything except itself), `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `*.egg-info/`, `.settings`, `.env` are generated or ignored — do not edit or commit them.
- `docs/` is ignored by `.gitignore` (`git ls-files docs` is empty), including the local acceptance matrix `docs/ACCEPTANCE.md`. Anything written under `docs/` is not tracked; tracked documentation belongs in `README.md`.

## Completion criteria

- `uv run pytest -q` passes.
- Inspect the staged diff and confirm no credential values, `.settings`/`.env` files, or `.pamu/` state are included, per README "Security and repository hygiene".
