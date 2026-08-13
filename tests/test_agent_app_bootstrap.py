import importlib
import os
import sys
import types
from pathlib import Path

from agent_app.config import AppConfig


class FakeSDKClient:
    class Messages:
        def create(self, **_kwargs):
            raise AssertionError("no live request expected")

        def stream(self, **_kwargs):
            raise AssertionError("no live request expected")

    def __init__(self):
        self.messages = self.Messages()


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


def test_environment_keeps_project_and_process_values(tmp_path, monkeypatch):
    from dotenv import load_dotenv

    import agent_app.bootstrap as bootstrap

    workspace = tmp_path / "project"
    global_env = tmp_path / "config" / ".env"
    project_env = workspace / ".pamu" / ".env"
    project_env.parent.mkdir(parents=True)
    global_env.parent.mkdir(parents=True)
    global_env.write_text("MODEL_ID=global\n", encoding="utf-8")
    project_env.write_text("MODEL_ID=project\n", encoding="utf-8")
    monkeypatch.delenv("MODEL_ID", raising=False)

    bootstrap._load_environment(workspace, global_env, load_dotenv)

    assert os.environ["MODEL_ID"] == "project"

    monkeypatch.setenv("MODEL_ID", "process")
    bootstrap._load_environment(workspace, global_env, load_dotenv)

    assert os.environ["MODEL_ID"] == "process"


def test_import_does_not_create_runtime_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sys.modules.pop("agent_app.bootstrap", None)

    bootstrap = importlib.import_module("agent_app.bootstrap")

    assert bootstrap is not None
    assert list(tmp_path.iterdir()) == []


def test_build_runtime_creates_independent_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ID", "test-model")
    config = AppConfig.from_env(tmp_path)
    sdk_client = FakeSDKClient()

    from agent_app.bootstrap import build_runtime

    first = build_runtime(config, sdk_client)
    second = build_runtime(config, sdk_client)
    first.session.todos.append({"content": "one", "status": "pending"})
    first.tasks.lock.acquire()
    first.tasks.lock.release()

    assert second.session.todos == []
    assert first.tasks is not second.tasks
    assert first.tools is not second.tools
    assert first.hooks is not second.hooks
    assert config.task_dir.is_dir()
    assert config.mailbox_dir.is_dir()


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


def test_build_runtime_registers_every_owner_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ID", "test-model")

    from agent_app.bootstrap import build_runtime

    runtime = build_runtime(AppConfig.from_env(tmp_path), FakeSDKClient())
    tools, handlers = runtime.tools.snapshot()
    names = [tool["name"] for tool in tools]

    assert names == [
        "bash", "read_file", "write_file", "edit_file", "glob", "load_skill",
        "compact", "todo_write", "create_task", "list_tasks", "get_task",
        "claim_task", "complete_task", "schedule_cron", "list_crons",
        "cancel_cron", "spawn_teammate", "send_message", "check_inbox",
        "request_shutdown", "request_plan", "review_plan", "create_worktree",
        "remove_worktree", "keep_worktree", "task", "connect_mcp",
    ]
    assert "compact" not in handlers
    assert set(names) - {"compact"} == set(handlers)


def test_build_default_runtime_owns_environment_and_sdk_creation(
    tmp_path, monkeypatch
):
    import agent_app.bootstrap as bootstrap

    calls = []

    class FakeAnthropic:
        def __init__(self, **kwargs):
            calls.append(("anthropic", kwargs))
            self.messages = FakeSDKClient.Messages()

    fake_anthropic = types.ModuleType("anthropic")
    fake_dotenv = types.ModuleType("dotenv")
    fake_anthropic.Anthropic = FakeAnthropic
    fake_dotenv.load_dotenv = lambda path, **kwargs: calls.append(
        ("dotenv", path, kwargs)
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    install_root = tmp_path / "install"
    workspace = tmp_path / "workspace"
    global_env = tmp_path / "config" / ".env"
    workspace.mkdir()
    monkeypatch.setattr(bootstrap, "INSTALL_ROOT", install_root)
    monkeypatch.setattr(bootstrap, "GLOBAL_ENV_PATH", global_env)
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("MODEL_ID", "test-model")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "remove-me")

    runtime = bootstrap.build_default_runtime()

    assert calls == [
        ("dotenv", workspace / ".pamu" / ".env", {"override": False}),
        ("dotenv", global_env, {"override": False}),
        ("anthropic", {"base_url": "https://example.invalid"}),
    ]
    assert "ANTHROPIC_AUTH_TOKEN" not in __import__("os").environ
    assert runtime.config.repo_root == bootstrap.INSTALL_ROOT.resolve()
    assert runtime.config.workdir == workspace.resolve()
    assert runtime.llm.progress is bootstrap.terminal_progress
