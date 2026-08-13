# PamuCode Current-Directory Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `pamu` operate on the directory from which it is invoked, store all generated project state under `.pamu/`, and load provider configuration from global and project dotenv files without overriding explicit process variables.

**Architecture:** Keep the package installation root separate from the invocation workspace. `AppConfig` remains the single path-mapping owner, `bootstrap.build_default_runtime()` captures `Path.cwd()` and owns dotenv loading, and runtime storage initialization creates a protective `.pamu/.gitignore` without overwriting user content.

**Tech Stack:** Python 3.13, pathlib, dataclasses, python-dotenv, Anthropic SDK, pytest, uv, Git.

## Global Constraints

- `Path.cwd().resolve()` at CLI startup is the workspace root for tools, Git, hooks, prompts, teammates, tasks, worktrees, and scheduler activity.
- All generated project state lives under `<workspace>/.pamu`; `skills/` remains `<workspace>/skills`.
- Configuration priority is explicit process environment, then `<workspace>/.pamu/.env`, then `~/.config/pamucode/.env`.
- Dotenv files never overwrite variables already present in the process environment.
- `build_runtime(config, sdk_client)` remains injectable and does not read dotenv files or `Path.cwd()`.
- Imports create no directories, files, SDK clients, or threads.
- `.pamu/.gitignore` contains `*` and `!.gitignore`, is created only when absent, and an existing file is never overwritten.
- Existing legacy state directories are not migrated automatically.
- Never print or commit API key values.
- Preserve all unrelated runtime behavior and keep the full offline suite green.

---

### Task 1: Separate installation, workspace, and state paths

**Files:**
- Modify: `agent_app/config.py`
- Modify: `agent_app/bootstrap.py`
- Modify: `tests/test_agent_app_foundation.py`
- Modify: `tests/test_agent_app_bootstrap.py`
- Modify: `tests/test_entrypoint.py`

**Interfaces:**
- Consumes: installation root `Path`, optional workspace root `Path`
- Produces: `AppConfig.from_env(repo_root: Path, workdir: Path | None = None) -> AppConfig`, `AppConfig.state_dir`, and non-destructive state-root initialization

- [ ] **Step 1: Write path-mapping and state-ignore regression tests**

Replace the path assertions in `tests/test_agent_app_foundation.py` with:

```python
def test_app_config_separates_installation_workspace_and_state_paths(
    tmp_path, monkeypatch
):
    install_root = tmp_path / "install"
    workspace = tmp_path / "project"
    monkeypatch.setenv("MODEL_ID", "primary")
    monkeypatch.setenv("FALLBACK_MODEL_ID", "fallback")

    config = AppConfig.from_env(install_root, workspace)

    assert config.repo_root == install_root.resolve()
    assert config.workdir == workspace.resolve()
    assert config.state_dir == workspace.resolve() / ".pamu"
    assert config.skills_dir == workspace.resolve() / "skills"
    assert config.memory_dir == config.state_dir / "memory"
    assert config.memory_index == config.state_dir / "memory" / "MEMORY.md"
    assert config.transcripts_dir == config.state_dir / "transcripts"
    assert config.tool_result_dir == config.state_dir / "task_outputs" / "tool-results"
    assert config.task_dir == config.state_dir / "tasks"
    assert config.mailbox_dir == config.state_dir / "mailboxes"
    assert config.scheduled_tasks_path == config.state_dir / "scheduled_tasks.json"
    assert config.worktrees_dir == config.state_dir / "worktrees"
    assert not config.state_dir.exists()
```

Add to `tests/test_agent_app_bootstrap.py`:

```python
def test_build_runtime_creates_protective_state_gitignore(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ID", "test-model")
    config = AppConfig.from_env(tmp_path / "install", tmp_path / "project")

    from agent_app.bootstrap import build_runtime

    build_runtime(config, FakeSDKClient())

    assert (config.state_dir / ".gitignore").read_text(encoding="utf-8") == (
        "*\n!.gitignore\n"
    )


def test_build_runtime_preserves_existing_state_gitignore(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ID", "test-model")
    config = AppConfig.from_env(tmp_path / "install", tmp_path / "project")
    config.state_dir.mkdir(parents=True)
    ignore = config.state_dir / ".gitignore"
    ignore.write_text("custom\n", encoding="utf-8")

    from agent_app.bootstrap import build_runtime

    build_runtime(config, FakeSDKClient())

    assert ignore.read_text(encoding="utf-8") == "custom\n"
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider \
  tests/test_agent_app_foundation.py tests/test_agent_app_bootstrap.py -q
```

Expected: FAIL because `state_dir`, the two-root mapping, and the protective
ignore file do not exist.

- [ ] **Step 3: Implement the pure path mapping**

Add `state_dir: Path` to `AppConfig`. Change the constructor to:

```python
@classmethod
def from_env(
    cls, repo_root: Path, workdir: Path | None = None
) -> "AppConfig":
    root = repo_root.resolve()
    workspace = (workdir or repo_root).resolve()
    state_dir = workspace / ".pamu"
    memory_dir = state_dir / "memory"
    tool_result_dir = state_dir / "task_outputs" / "tool-results"
    return cls(
        repo_root=root,
        workdir=workspace,
        state_dir=state_dir,
        skills_dir=workspace / "skills",
        memory_dir=memory_dir,
        memory_index=memory_dir / "MEMORY.md",
        transcripts_dir=state_dir / "transcripts",
        tool_result_dir=tool_result_dir,
        task_dir=state_dir / "tasks",
        mailbox_dir=state_dir / "mailboxes",
        scheduled_tasks_path=state_dir / "scheduled_tasks.json",
        worktrees_dir=state_dir / "worktrees",
        primary_model=os.environ["MODEL_ID"],
        fallback_model=os.getenv("FALLBACK_MODEL_ID"),
    )
```

The optional second argument preserves the explicit single-root test/embedding
contract while production passes both roots.

- [ ] **Step 4: Add non-destructive state-root initialization**

At the start of `_create_storage_roots(config)`:

```python
config.state_dir.mkdir(parents=True, exist_ok=True)
ignore = config.state_dir / ".gitignore"
if not ignore.exists():
    ignore.write_text("*\n!.gitignore\n", encoding="utf-8")
```

Keep the existing feature-directory creation loop, now using paths already
mapped below `state_dir`.

- [ ] **Step 5: Rename the installation-root constant and update its test**

Rename `DEFAULT_REPO_ROOT` to `INSTALL_ROOT` in `agent_app/bootstrap.py`. In
`tests/test_entrypoint.py`, import `INSTALL_ROOT` and assert:

```python
def test_install_root_is_the_standalone_project_root():
    assert INSTALL_ROOT == Path(__file__).resolve().parents[1]
```

- [ ] **Step 6: Run focused and complete tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider \
  tests/test_agent_app_foundation.py tests/test_agent_app_bootstrap.py \
  tests/test_entrypoint.py -q
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider -q
```

Expected: focused tests pass and the complete suite passes.

- [ ] **Step 7: Commit**

```bash
git add agent_app/config.py agent_app/bootstrap.py \
  tests/test_agent_app_foundation.py tests/test_agent_app_bootstrap.py \
  tests/test_entrypoint.py
git commit -m "refactor: centralize PamuCode project state"
```

---

### Task 2: Load layered configuration and capture the invocation workspace

**Files:**
- Modify: `agent_app/bootstrap.py`
- Modify: `tests/test_agent_app_bootstrap.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `INSTALL_ROOT`, `Path.cwd()`, `~/.config/pamucode/.env`, `<workspace>/.pamu/.env`
- Produces: `_load_environment(workspace: Path, global_env: Path, load: Callable) -> None` and a default runtime whose `config.workdir` is the invocation directory

- [ ] **Step 1: Write dotenv-priority tests**

Add to `tests/test_agent_app_bootstrap.py`:

```python
def test_environment_loads_project_before_global_without_override(tmp_path):
    import agent_app.bootstrap as bootstrap

    calls = []
    workspace = tmp_path / "project"
    global_env = tmp_path / "config" / ".env"

    bootstrap._load_environment(
        workspace,
        global_env,
        lambda path, **kwargs: calls.append((path, kwargs)),
    )

    assert calls == [
        (workspace / ".pamu" / ".env", {"override": False}),
        (global_env, {"override": False}),
    ]
```

Add an integration test using real `dotenv.load_dotenv` and `monkeypatch.delenv`
for `MODEL_ID`: write `MODEL_ID=global` in the global file and
`MODEL_ID=project` in the project file, call `_load_environment`, and assert
`os.environ["MODEL_ID"] == "project"`. Set `MODEL_ID=process` before a second
call and assert it remains `process`.

- [ ] **Step 2: Update the default-runtime test for current-directory capture**

In `test_build_default_runtime_owns_environment_and_sdk_creation`, create
separate `install_root`, `workspace`, and `global_env` paths. Monkeypatch
`bootstrap.INSTALL_ROOT`, `bootstrap.GLOBAL_ENV_PATH`, and `Path.cwd` indirectly
with `monkeypatch.chdir(workspace)`. The fake `load_dotenv` accepts `path` and
records two calls. Assert:

```python
assert calls == [
    ("dotenv", workspace / ".pamu" / ".env", {"override": False}),
    ("dotenv", global_env, {"override": False}),
    ("anthropic", {"base_url": "https://example.invalid"}),
]
assert runtime.config.repo_root == install_root.resolve()
assert runtime.config.workdir == workspace.resolve()
```

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider \
  tests/test_agent_app_bootstrap.py tests/test_cli.py -q
```

Expected: FAIL because `_load_environment`, `GLOBAL_ENV_PATH`, and cwd-based
configuration are absent.

- [ ] **Step 4: Implement layered environment loading**

In `agent_app/bootstrap.py`, add:

```python
GLOBAL_ENV_PATH = Path.home() / ".config" / "pamucode" / ".env"


def _load_environment(workspace: Path, global_env: Path, load) -> None:
    load(workspace / ".pamu" / ".env", override=False)
    load(global_env, override=False)
```

Change `build_default_runtime()` to capture and use the workspace once:

```python
workspace = Path.cwd().resolve()
_load_environment(workspace, GLOBAL_ENV_PATH, load_dotenv)
base_url = os.getenv("ANTHROPIC_BASE_URL")
if base_url:
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
config = AppConfig.from_env(INSTALL_ROOT, workspace)
return build_runtime(config, Anthropic(base_url=base_url))
```

- [ ] **Step 5: Add a subprocess workspace contract**

In `tests/test_cli.py`, extend the existing dependency stubs so fake
`Anthropic.__init__` writes its current working directory to the file named by
`PAMU_TEST_CWD_FILE`. Run `[sys.executable, "-m", "agent_app"]` with
`cwd=tmp_path / "project"`, an explicit `MODEL_ID`, and the project root on
`PYTHONPATH`. After bounded `q`, assert the recorded cwd is the project path and
that `<project>/.pamu` exists while `<install-root>/.pamu` does not.

- [ ] **Step 6: Run focused and complete tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider \
  tests/test_agent_app_bootstrap.py tests/test_cli.py -q
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider -q
```

Expected: focused tests pass and the complete suite passes without live API
calls.

- [ ] **Step 7: Commit**

```bash
git add agent_app/bootstrap.py tests/test_agent_app_bootstrap.py tests/test_cli.py
git commit -m "feat: run PamuCode in the current directory"
```

---

### Task 3: Document, install, and verify the global command

**Files:**
- Modify: `README.md`
- Modify: `.gitignore`
- Test: complete offline suite and temporary-directory command smoke tests

**Interfaces:**
- Consumes: `uv tool install --editable`, global dotenv path, current-directory runtime
- Produces: user instructions and a globally available `pamu` executable using the latest editable source

- [ ] **Step 1: Update README setup and workspace documentation**

Document these commands:

```bash
mkdir -p ~/.config/pamucode
cp .env.example ~/.config/pamucode/.env
uv tool install --editable /Users/wh/python/PamuCode
uv tool update-shell
```

Explain that users edit `~/.config/pamucode/.env`, may override it with
`<project>/.pamu/.env`, and can then run:

```bash
cd /path/to/project
pamu
```

Document the configuration priority and `.pamu/` state layout. State explicitly
that legacy top-level state directories are not migrated automatically.

- [ ] **Step 2: Simplify repository ignore rules**

Add `.pamu/` and `*.egg-info/` to the repository `.gitignore`. Keep legacy
state ignore entries so local remnants remain untracked.

- [ ] **Step 3: Run documentation and security checks**

Run:

```bash
! rg -n "cp \.env\.example \.env|uv run pamu" README.md
rg -n "~/.config/pamucode/.env|\.pamu/\.env|uv tool install|cd /path/to/project" README.md
git check-ignore .pamu/.env pamucode.egg-info/PKG-INFO .env
! git grep -nE 'ANTHROPIC_API_KEY=(sk-[A-Za-z0-9_-]{12,})' -- ':!.env.example'
```

Expected: obsolete setup commands are absent, new commands are present, all
secret/build paths are ignored, and no real key is tracked.

- [ ] **Step 4: Run final Python verification**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run python -m compileall -q agent_app
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider -q
MODEL_ID=dummy uv run python -c \
'import os,tempfile; p=tempfile.mkdtemp(); os.chdir(p); import agent_app.bootstrap,agent_app.cli; assert not os.listdir(p); print("import-safe")'
```

Expected: compile succeeds, all tests pass, and output contains `import-safe`.

- [ ] **Step 5: Reinstall the editable global command**

Run:

```bash
uv tool install --force --editable /Users/wh/python/PamuCode
uv tool update-shell
command -v pamu
```

Expected: `pamu` resolves under the uv tool bin directory.

- [ ] **Step 6: Smoke-test two independent invocation directories**

Create two temporary directories. In each directory, run the absolute `pamu`
executable with an explicit `MODEL_ID=dummy`, pipe `q`, and impose a 10-second
timeout. Assert both exit 0, each creates its own `.pamu/`, neither writes
`.pamu/` beneath the PamuCode installation root, and the two state directories
are distinct.

- [ ] **Step 7: Commit documentation**

```bash
git add README.md .gitignore
git commit -m "docs: explain global PamuCode workspace usage"
```

- [ ] **Step 8: Verify clean final state**

Run:

```bash
git diff --check
git status --short
git log -4 --oneline
```

Expected: diff check succeeds, status is empty, and the three implementation
commits follow the approved design commit.

---

### Task 4: Add an interactive Working spinner

**Files:**
- Create: `agent_app/cli_progress.py`
- Modify: `agent_app/adapters/anthropic.py`
- Modify: `agent_app/bootstrap.py`
- Modify: `tests/test_agent_app_anthropic_adapter.py`
- Create: `tests/test_cli_progress.py`
- Modify: `tests/test_agent_app_bootstrap.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: interactive `stderr`, streaming model request lifecycle, production bootstrap
- Produces: `TerminalSpinner`, `terminal_progress(label: str)`, no-op adapter default, and `AnthropicAdapter(..., progress=...)`

- [ ] **Step 1: Write deterministic terminal progress tests**

Create `tests/test_cli_progress.py` with fakes for stream, event, and thread.
The fake stream records writes and exposes configurable `isatty()`. The fake
event returns `False` once and `True` on its next `wait()` so the animation
target emits a bounded number of frames. Assert:

```python
def test_tty_spinner_animates_then_clears_and_joins():
    stream = FakeStream(tty=True)
    created = []
    spinner = TerminalSpinner(
        "Working",
        stream=stream,
        frames=("A", "B"),
        interval=0.01,
        event_factory=FakeEvent,
        thread_factory=lambda **kwargs: FakeThread(created, **kwargs),
    )

    with spinner:
        pass

    assert "\rA Working" in "".join(stream.writes)
    assert stream.writes[-1] == "\r\033[2K"
    assert created[0].daemon is True
    assert created[0].joined == [1.0]
```

Add a second test calling `spinner.stop()` twice and asserting only one clear
write, and a non-TTY test asserting no thread and no output.

- [ ] **Step 2: Write adapter progress-order tests**

In `tests/test_agent_app_anthropic_adapter.py`, use a recording progress
context and monkeypatch `builtins.print` to append events. Add three tests:

```python
def test_streaming_stops_progress_before_first_visible_chunk(monkeypatch):
    # Stream yields "visible" then returns a final message.
    # Expected event order begins: enter, stop, print:visible, exit.


def test_streaming_exception_exits_progress(monkeypatch):
    # Stream raises before yielding text.
    # Assert the exception propagates and events end with stop/exit cleanup.


def test_tool_only_stream_keeps_progress_until_final_message(monkeypatch):
    # text_stream is empty and get_final_message records "final".
    # Assert final occurs before the context cleanup event.
```

Keep the existing adapter tests constructing `AnthropicAdapter(client)`; they
prove the default progress implementation is silent and backward compatible.

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider \
  tests/test_cli_progress.py tests/test_agent_app_anthropic_adapter.py -q
```

Expected: FAIL because `cli_progress` and adapter progress injection do not
exist.

- [ ] **Step 4: Implement no-op progress at the adapter boundary**

In `agent_app/adapters/anthropic.py`, add a private idempotent no-op context
handle and factory. Extend the dataclass compatibly:

```python
@dataclass(frozen=True, slots=True)
class AnthropicAdapter:
    client: Any
    progress: Callable[[str], ContextManager] = _null_progress
```

Wrap only `create_streaming()`:

```python
with self.progress("Working") as progress:
    with self.client.messages.stream(...) as stream:
        for chunk in stream.text_stream:
            if not chunk:
                continue
            progress.stop()
            chunks.append(chunk)
            print(chunk, end="", flush=True)
        return stream.get_final_message()
```

The existing outer `try`/`except PartialStreamError` behavior and newline
`finally` remain unchanged. Do not show progress for `create()`.

- [ ] **Step 5: Implement the TTY-only spinner**

Create `agent_app/cli_progress.py` with `TerminalSpinner`. It uses the frame
sequence `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`, defaults to `sys.stderr` and a `0.08` second
interval, starts one daemon thread named `pamu-working-spinner`, and writes:

```python
stream.write(f"\r{frame} {label}")
```

`stop()` sets its event, joins the worker for `1.0` second unless invoked by
that worker, then writes `"\r\033[2K"` and flushes. Guard stop and writes with
a lock so repeated stops clear once and cannot race the animation. For
`isatty() == False`, `__enter__` returns without starting a thread and `stop()`
does not write. `__exit__` always calls `stop()` and returns `False`.

Expose:

```python
def terminal_progress(label: str) -> TerminalSpinner:
    return TerminalSpinner(label)
```

- [ ] **Step 6: Wire progress only into the production CLI runtime**

Extend `build_runtime` with a backward-compatible keyword-only argument:

```python
def build_runtime(config, sdk_client, *, progress_factory=None):
    llm = (
        AnthropicAdapter(sdk_client, progress_factory)
        if progress_factory is not None
        else AnthropicAdapter(sdk_client)
    )
```

Import `terminal_progress` in bootstrap and have `build_default_runtime()` call:

```python
return build_runtime(
    config,
    Anthropic(base_url=base_url),
    progress_factory=terminal_progress,
)
```

Update the default-runtime bootstrap test to assert
`runtime.llm.progress is bootstrap.terminal_progress`. Explicit
`build_runtime(config, FakeSDKClient())` tests remain silent.

- [ ] **Step 7: Run focused and full regression tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider \
  tests/test_cli_progress.py tests/test_agent_app_anthropic_adapter.py \
  tests/test_agent_app_bootstrap.py tests/test_cli.py -q
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider -q
```

Expected: focused and full suites pass with no spinner noise in captured output.

- [ ] **Step 8: Document and commit the spinner**

Add a short README note: interactive model waits display an animated `Working`
status; redirected and non-interactive output stays clean. Then run:

```bash
git add agent_app/cli_progress.py agent_app/adapters/anthropic.py \
  agent_app/bootstrap.py tests/test_cli_progress.py \
  tests/test_agent_app_anthropic_adapter.py tests/test_agent_app_bootstrap.py \
  README.md
git commit -m "feat: show interactive PamuCode working status"
```

- [ ] **Step 9: Verify thread and output cleanup**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider -q
git diff --check
git status --short
```

Expected: all tests pass, diff check succeeds, and status is empty.
