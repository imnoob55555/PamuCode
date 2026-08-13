# PamuCode Standalone Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the validated layered agent application from source commit `cae7729` into an independent `/Users/wh/python/PamuCode` repository with no `homework` namespace and no root `BaseAgent.py`.

**Architecture:** Preserve the existing `agent_app` layers and behavior by mechanically copying the validated package, then change only repository-boundary concerns: package location, module entry point, test imports, packaging metadata, and standalone documentation. The application starts through `agent_app.__main__` or the `pamu` console script; `agent_app.cli.main` remains the single CLI owner.

**Tech Stack:** Python 3.13, dataclasses, pathlib, threading, Anthropic Python SDK, python-dotenv, PyYAML, pytest, uv, Git.

## Global Constraints

- Source files come from `/Users/wh/python/learn-claude-code/.worktrees/baseagent-layered` at commit `cae7729`.
- Do not modify `/Users/wh/python/learn-claude-code` or its uncommitted files.
- Do not copy the source repository's `.git`, course lessons, legacy requirements, runtime state, or unrelated documentation.
- Do not add a root-level `BaseAgent.py` or `homework` package.
- Preserve agent behavior; only repository-boundary imports, entry points, filenames, and documentation may change.
- Never commit `.env`, API keys, memory, transcripts, mailboxes, tasks, scheduled jobs, worktrees, or tool outputs.
- All tests remain offline and must not make live API calls.

---

### Task 1: Extract the application package and define standalone packaging

**Files:**
- Create: `agent_app/**/*.py` by copying `homework/agent_app/**/*.py`
- Create: `agent_app/__main__.py`
- Create: `pyproject.toml`
- Test: `tests/test_entrypoint.py`

**Interfaces:**
- Consumes: validated `agent_app` package from source commit `cae7729`
- Produces: `python -m agent_app`, console script `pamu = "agent_app.cli:main"`, and an installable top-level `agent_app` package

- [ ] **Step 1: Write the entry-point contract test**

Create `tests/test_entrypoint.py`:

```python
import runpy


def test_module_entrypoint_delegates_to_cli(monkeypatch):
    calls = []
    monkeypatch.setattr("agent_app.cli.main", lambda: calls.append("main"))

    runpy.run_module("agent_app", run_name="__main__")

    assert calls == ["main"]
```

- [ ] **Step 2: Run the test and verify the package is absent**

Run: `uv run --with pytest pytest -p no:cacheprovider tests/test_entrypoint.py -q`

Expected: FAIL because `agent_app` has not been migrated.

- [ ] **Step 3: Copy the validated package without regenerating its implementation**

Copy the complete contents of
`/Users/wh/python/learn-claude-code/.worktrees/baseagent-layered/homework/agent_app/`
to `agent_app/`. Verify the source commit before copying:

```bash
git -C /Users/wh/python/learn-claude-code/.worktrees/baseagent-layered \
  rev-parse --verify cae7729
```

Create `agent_app/__main__.py`:

```python
from .cli import main


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Add project metadata**

Create `pyproject.toml`:

```toml
[project]
name = "pamucode"
version = "0.1.0"
description = "A layered coding-agent runtime"
readme = "README.md"
requires-python = ">=3.13"
dependencies = [
  "anthropic>=0.52",
  "python-dotenv>=1.0",
  "PyYAML>=6.0",
]

[project.scripts]
pamu = "agent_app.cli:main"

[dependency-groups]
dev = ["pytest>=8.0"]

[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["agent_app*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 5: Run the focused entry-point test**

Run: `uv run pytest -p no:cacheprovider tests/test_entrypoint.py -q`

Expected: `1 passed`.

- [ ] **Step 6: Commit the standalone application boundary**

```bash
git add agent_app pyproject.toml tests/test_entrypoint.py uv.lock
git commit -m "refactor: extract standalone PamuCode application"
```

---

### Task 2: Migrate and rename the offline test suite

**Files:**
- Create: `tests/agent_app_fakes.py`
- Create: `tests/test_agent_app_*.py`
- Create: `tests/test_agent_teams.py`
- Create: `tests/test_background_tasks.py`
- Create: `tests/test_cli.py`
- Create: `tests/test_compact_tool.py`
- Create: `tests/test_error_recovery.py`
- Create: `tests/test_task_system.py`
- Create: `tests/test_todo_resume.py`
- Modify: `tests/test_entrypoint.py`

**Interfaces:**
- Consumes: top-level `agent_app` package and the 20 validated source test files
- Produces: an offline suite with no `homework` or `BaseAgent` path dependency

- [ ] **Step 1: Copy the exact test bodies and rename repository-specific files**

Copy `tests/homework_agent_app_fakes.py`, every
`tests/test_homework_agent_app_*.py`, and every
`tests/test_homework_baseagent_*.py` from the source worktree. Rename them as follows:

```text
homework_agent_app_fakes.py                  -> agent_app_fakes.py
test_homework_agent_app_<feature>.py         -> test_agent_app_<feature>.py
test_homework_baseagent_agent_teams.py       -> test_agent_teams.py
test_homework_baseagent_background_tasks.py  -> test_background_tasks.py
test_homework_baseagent_cli.py               -> test_cli.py
test_homework_baseagent_compact_tool.py       -> test_compact_tool.py
test_homework_baseagent_error_recovery.py     -> test_error_recovery.py
test_homework_baseagent_task_system.py        -> test_task_system.py
test_homework_baseagent_todo_resume.py        -> test_todo_resume.py
```

- [ ] **Step 2: Mechanically rewrite package imports**

Across `tests/*.py`, apply these exact substitutions:

```text
homework.agent_app -> agent_app
tests.homework_agent_app_fakes -> tests.agent_app_fakes
```

Run: `rg -n "homework|BaseAgent" tests agent_app`

Expected: only the old CLI script test still refers to `BASE_AGENT`.

- [ ] **Step 3: Replace the legacy script subprocess test with the module entry point**

In `tests/test_cli.py`, remove `BASE_AGENT` and replace
`test_baseagent_script_starts_without_repository_on_pythonpath` with:

```python
def test_module_cli_starts_without_installed_dependencies(tmp_path):
    (tmp_path / "anthropic.py").write_text(
        "class Anthropic:\n"
        "    def __init__(self, **kwargs):\n"
        "        self.messages = object()\n",
        encoding="utf-8",
    )
    (tmp_path / "dotenv.py").write_text(
        "def load_dotenv(**kwargs):\n    return None\n",
        encoding="utf-8",
    )
    (tmp_path / "yaml.py").write_text(
        "class YAMLError(Exception):\n    pass\n"
        "def safe_load(_text):\n    return {}\n",
        encoding="utf-8",
    )
    project_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment.update(
        {
            "MODEL_ID": "test-model",
            "PYTHONPATH": f"{project_root}{os.pathsep}{tmp_path}",
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "agent_app"],
        input="q\n",
        text=True,
        capture_output=True,
        cwd=project_root,
        env=environment,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
```

- [ ] **Step 4: Run the migrated suite**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider -q`

Expected: `171 passed, 30 subtests passed` (the previous 170 tests plus the new direct `__main__` contract).

- [ ] **Step 5: Enforce namespace removal**

Run:

```bash
! rg -n "homework\.agent_app|BaseAgent\.py|BASE_AGENT" agent_app tests
test ! -e BaseAgent.py
test ! -e homework
```

Expected: exit status 0.

- [ ] **Step 6: Commit the standalone tests**

```bash
git add tests
git commit -m "test: migrate PamuCode runtime coverage"
```

---

### Task 3: Add standalone operator documentation and ignore rules

**Files:**
- Create: `README.md`
- Create: `.env.example`
- Create: `.gitignore`

**Interfaces:**
- Consumes: `python -m agent_app`, `pamu`, and `AppConfig.from_env(Path.cwd())`
- Produces: complete local setup instructions and secret/runtime-state exclusions

- [ ] **Step 1: Add the environment template**

Create `.env.example` without real credentials:

```dotenv
ANTHROPIC_API_KEY=sk-ant-xxx
MODEL_ID=claude-sonnet-4-6
# FALLBACK_MODEL_ID=claude-haiku-4-5
# ANTHROPIC_BASE_URL=https://api.anthropic.com
```

- [ ] **Step 2: Add repository ignore rules**

Create `.gitignore`:

```gitignore
.env
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.DS_Store
.memory/
.transcripts/
.task_outputs/
.tasks/
.mailboxes/
.worktrees/
.scheduled_tasks.json
.todo.json
```

- [ ] **Step 3: Document installation, execution, layout, and testing**

Create `README.md` with these exact operational commands:

````markdown
# PamuCode

PamuCode is a layered coding-agent runtime extracted into an independent project.

## Setup

```bash
cp .env.example .env
uv sync
```

Set `ANTHROPIC_API_KEY` and `MODEL_ID` in `.env`.

## Run

```bash
uv run python -m agent_app
# or, after installation
uv run pamu
```

## Test

```bash
uv run pytest
```

The implementation is organized under `agent_app/adapters`, `core`, `features`,
and `tools`. `agent_app/bootstrap.py` is the composition root and
`agent_app/cli.py` owns the command-line lifecycle.
````

- [ ] **Step 4: Verify tracked files do not contain credentials or runtime state**

Run:

```bash
git check-ignore .env .memory/MEMORY.md .transcripts/example.jsonl \
  .tasks/task.json .mailboxes/lead.jsonl .scheduled_tasks.json
! git grep -n "sk-ant-[A-Za-z0-9]" -- ':!.env.example'
```

Expected: every generated path is ignored and no real-looking Anthropic key is tracked.

- [ ] **Step 5: Commit project documentation**

```bash
git add README.md .env.example .gitignore
git commit -m "docs: document standalone PamuCode usage"
```

---

### Task 4: Perform independent-project acceptance verification

**Files:**
- Modify only if a verification exposes a migration defect

**Interfaces:**
- Consumes: complete standalone repository
- Produces: verified clean `main` branch with no dependency on `learn-claude-code`

- [ ] **Step 1: Prove the package has no source-repository references**

Run:

```bash
! rg -n "learn-claude-code|homework\.agent_app|BaseAgent\.py|BASE_AGENT" \
  agent_app tests README.md pyproject.toml
test ! -e BaseAgent.py
test ! -e homework
```

Expected: exit status 0.

- [ ] **Step 2: Compile and run the complete offline suite**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run python -m compileall -q agent_app
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider -q
```

Expected: compile succeeds and `171 passed, 30 subtests passed`.

- [ ] **Step 3: Verify import-time side-effect safety**

Run:

```bash
MODEL_ID=dummy uv run python -c \
'import os,tempfile; p=tempfile.mkdtemp(); os.chdir(p); import agent_app.bootstrap,agent_app.cli; assert not os.listdir(p); print("import-safe")'
```

Expected: `import-safe`.

- [ ] **Step 4: Smoke-test both supported entry points**

Run each command and enter `q`:

```bash
MODEL_ID=dummy uv run python -m agent_app
MODEL_ID=dummy uv run pamu
```

Expected: both display the prompt and exit with status 0 without an API request.

- [ ] **Step 5: Verify independent Git identity and clean state**

Run:

```bash
test "$(git rev-parse --show-toplevel)" = "/Users/wh/python/PamuCode"
test "$(git rev-list --max-parents=0 HEAD | wc -l | tr -d ' ')" = "1"
git diff --check
git status --short
```

Expected: the repository root is PamuCode, there is one independent root commit,
the diff check succeeds, and status is empty.

- [ ] **Step 6: Record acceptance if verification required fixes**

If Steps 1-5 required any tracked correction, commit only those corrections:

```bash
git add agent_app tests README.md pyproject.toml uv.lock .env.example .gitignore
git commit -m "fix: complete standalone PamuCode migration"
```

Otherwise make no empty commit.
