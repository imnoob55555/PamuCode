import importlib
import io
import os
import stat
import sys
import types
from dataclasses import replace
from pathlib import Path

import pytest

from agent_app.config import AppConfig


class FakeSDKClient:
    class Messages:
        def create(self, **_kwargs):
            raise AssertionError("no live request expected")

        def stream(self, **_kwargs):
            raise AssertionError("no live request expected")

    def __init__(self):
        self.messages = self.Messages()


def test_environment_isolated_mapping_uses_process_then_project_then_global(
    tmp_path,
):
    import agent_app.bootstrap as bootstrap

    calls = []
    workspace = tmp_path / "project"
    global_settings = tmp_path / "home" / ".pamu" / ".settings"
    values = {
        workspace / ".pamu" / ".settings": {
            "MODEL_ID": "project",
            "ANTHROPIC_API_KEY": "project-key",
        },
        global_settings: {
            "MODEL_ID": "global",
            "FALLBACK_MODEL_ID": "global-fallback",
        },
    }

    environment = bootstrap._load_environment(
        workspace,
        global_settings,
        {"MODEL_ID": "process"},
        lambda path: (calls.append(path), values[path])[1],
    )

    assert calls == [
        workspace / ".pamu" / ".settings",
        global_settings,
    ]
    assert environment == {
        "MODEL_ID": "process",
        "FALLBACK_MODEL_ID": "global-fallback",
        "ANTHROPIC_API_KEY": "project-key",
    }


def test_environment_loading_does_not_mutate_process_and_allows_missing_files(
    tmp_path, monkeypatch
):
    from dotenv import dotenv_values

    import agent_app.bootstrap as bootstrap

    workspace = tmp_path / "project"
    global_settings = tmp_path / "home" / ".pamu" / ".settings"
    monkeypatch.setenv("MODEL_ID", "process")
    before = dict(os.environ)

    environment = bootstrap._load_environment(
        workspace, global_settings, os.environ, dotenv_values
    )

    assert environment["MODEL_ID"] == "process"
    assert dict(os.environ) == before


def test_global_settings_path_uses_home_pamu_directory():
    import agent_app.bootstrap as bootstrap

    assert bootstrap.GLOBAL_SETTINGS_PATH == Path.home() / ".pamu" / ".settings"


def test_environment_loader_never_requests_legacy_env_paths(tmp_path):
    import agent_app.bootstrap as bootstrap

    workspace = tmp_path / "project"
    global_settings = tmp_path / "home" / ".pamu" / ".settings"
    requested = []

    environment = bootstrap._load_environment(
        workspace,
        global_settings,
        {},
        lambda path: (requested.append(path), {})[1],
    )

    assert environment == {}
    assert requested == [
        workspace / ".pamu" / ".settings",
        global_settings,
    ]
    assert workspace / ".pamu" / ".env" not in requested
    assert tmp_path / ".config" / "pamucode" / ".env" not in requested


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


def test_failed_initial_gitignore_write_is_removed_and_retry_regenerates(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MODEL_ID", "test-model")
    config = AppConfig.from_env(tmp_path / "install", tmp_path / "project")
    ignore = config.state_dir / ".gitignore"

    from agent_app.bootstrap import build_runtime

    real_open = io.open
    failed = False

    class FailingWrite:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.handle.close()

        def write(self, value):
            self.handle.write(value[:1])
            self.handle.flush()
            raise OSError("simulated gitignore write failure")

    def fail_first_write(file, *args, **kwargs):
        nonlocal failed
        handle = real_open(file, *args, **kwargs)
        if not failed:
            failed = True
            return FailingWrite(handle)
        return handle

    with monkeypatch.context() as scoped:
        scoped.setattr(io, "open", fail_first_write)
        with pytest.raises(OSError, match="simulated gitignore write failure"):
            build_runtime(config, FakeSDKClient())

    assert not ignore.exists()

    build_runtime(config, FakeSDKClient())

    assert ignore.read_text(encoding="utf-8") == "*\n!.gitignore\n"


def test_build_runtime_preserves_existing_state_gitignore(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ID", "test-model")
    config = AppConfig.from_env(tmp_path / "install", tmp_path / "project")
    config.state_dir.mkdir(parents=True)
    ignore = config.state_dir / ".gitignore"
    original = b"\xffcustom\r\n"
    ignore.write_bytes(original)

    from agent_app.bootstrap import build_runtime

    build_runtime(config, FakeSDKClient())

    assert ignore.read_bytes() == original


def test_build_runtime_rejects_external_state_symlink_before_writing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MODEL_ID", "test-model")
    workspace = tmp_path / "project"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("untouched\n", encoding="utf-8")
    (workspace / ".pamu").symlink_to(external, target_is_directory=True)
    config = AppConfig.from_env(tmp_path / "install", workspace)

    from agent_app.bootstrap import build_runtime

    with pytest.raises(ValueError, match="symlink"):
        build_runtime(config, FakeSDKClient())

    assert sorted(path.name for path in external.iterdir()) == ["sentinel"]
    assert sentinel.read_text(encoding="utf-8") == "untouched\n"


def test_build_runtime_rejects_symlinked_state_component_before_mutating_state(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MODEL_ID", "test-model")
    workspace = tmp_path / "project"
    state = workspace / ".pamu"
    external = tmp_path / "external-tasks"
    state.mkdir(parents=True)
    external.mkdir()
    (state / "tasks").symlink_to(external, target_is_directory=True)
    config = AppConfig.from_env(tmp_path / "install", workspace)

    from agent_app.bootstrap import build_runtime

    with pytest.raises(ValueError, match="symlink"):
        build_runtime(config, FakeSDKClient())

    assert sorted(path.name for path in state.iterdir()) == ["tasks"]
    assert list(external.iterdir()) == []


def test_build_runtime_rejects_dangling_gitignore_symlink_without_following_it(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MODEL_ID", "test-model")
    workspace = tmp_path / "project"
    state = workspace / ".pamu"
    external = tmp_path / "external"
    state.mkdir(parents=True)
    external.mkdir()
    external_ignore = external / "created-by-symlink"
    (state / ".gitignore").symlink_to(external_ignore)
    config = AppConfig.from_env(tmp_path / "install", workspace)

    from agent_app.bootstrap import build_runtime

    with pytest.raises(ValueError, match="symlink"):
        build_runtime(config, FakeSDKClient())

    assert not external_ignore.exists()
    assert sorted(path.name for path in state.iterdir()) == [".gitignore"]


def test_build_runtime_rejects_non_regular_state_gitignore(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MODEL_ID", "test-model")
    workspace = tmp_path / "project"
    ignore = workspace / ".pamu" / ".gitignore"
    ignore.mkdir(parents=True)
    config = AppConfig.from_env(tmp_path / "install", workspace)

    from agent_app.bootstrap import build_runtime

    with pytest.raises(ValueError, match="regular file"):
        build_runtime(config, FakeSDKClient())

    assert sorted(path.name for path in config.state_dir.iterdir()) == [
        ".gitignore"
    ]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is POSIX-only")
def test_build_runtime_rejects_fifo_state_gitignore(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ID", "test-model")
    workspace = tmp_path / "project"
    ignore = workspace / ".pamu" / ".gitignore"
    ignore.parent.mkdir(parents=True)
    os.mkfifo(ignore)
    config = AppConfig.from_env(tmp_path / "install", workspace)

    from agent_app.bootstrap import build_runtime

    with pytest.raises(ValueError, match="regular file"):
        build_runtime(config, FakeSDKClient())

    assert stat.S_ISFIFO(ignore.lstat().st_mode)


def test_build_runtime_rejects_dotdot_state_path_before_creating_it(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MODEL_ID", "test-model")
    workspace = tmp_path / "project"
    config = AppConfig.from_env(tmp_path / "install", workspace)
    escaped = workspace / "escaped-memory"
    config = replace(
        config,
        memory_dir=config.state_dir / ".." / escaped.name,
        memory_index=config.state_dir / ".." / escaped.name / "MEMORY.md",
    )

    from agent_app.bootstrap import build_runtime

    with pytest.raises(ValueError, match="state path"):
        build_runtime(config, FakeSDKClient())

    assert not escaped.exists()


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
    fake_dotenv.dotenv_values = lambda path: (
        calls.append(("dotenv", path)),
        {},
    )[1]
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    install_root = tmp_path / "install"
    workspace = tmp_path / "workspace"
    global_settings = tmp_path / "home" / ".pamu" / ".settings"
    workspace.mkdir()
    monkeypatch.setattr(bootstrap, "INSTALL_ROOT", install_root)
    monkeypatch.setattr(bootstrap, "GLOBAL_SETTINGS_PATH", global_settings)
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("MODEL_ID", "test-model")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "remove-me")
    before = dict(os.environ)

    runtime = bootstrap.build_default_runtime()

    assert calls == [
        ("dotenv", workspace / ".pamu" / ".settings"),
        ("dotenv", global_settings),
        (
            "anthropic",
            {"api_key": "", "base_url": "https://example.invalid"},
        ),
    ]
    assert dict(os.environ) == before
    assert runtime.config.repo_root == bootstrap.INSTALL_ROOT.resolve()
    assert runtime.config.workdir == workspace.resolve()
    assert runtime.llm.progress is bootstrap.terminal_progress


def test_default_runtimes_do_not_leak_project_environment_between_workspaces(
    tmp_path, monkeypatch
):
    import agent_app.bootstrap as bootstrap

    client_calls = []

    class FakeAnthropic:
        def __init__(self, **kwargs):
            client_calls.append(kwargs)
            self.messages = FakeSDKClient.Messages()

    def read_values(path):
        if not path.exists():
            return {}
        return dict(
            line.split("=", 1)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        )

    fake_anthropic = types.ModuleType("anthropic")
    fake_dotenv = types.ModuleType("dotenv")
    fake_anthropic.Anthropic = FakeAnthropic
    fake_dotenv.dotenv_values = read_values
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    monkeypatch.setattr(bootstrap, "GLOBAL_SETTINGS_PATH", tmp_path / "missing-global")
    for name in (
        "MODEL_ID",
        "FALLBACK_MODEL_ID",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    before = dict(os.environ)
    projects = []
    for name, model, api_key in (
        ("a", "alpha", "alpha-key"),
        ("b", "beta", "beta-key"),
    ):
        project = tmp_path / name
        settings_file = project / ".pamu" / ".settings"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(
            f"MODEL_ID={model}\nANTHROPIC_API_KEY={api_key}\n",
            encoding="utf-8",
        )
        projects.append(project)

    monkeypatch.chdir(projects[0])
    alpha = bootstrap.build_default_runtime()
    monkeypatch.chdir(projects[1])
    beta = bootstrap.build_default_runtime()

    assert alpha.config.primary_model == "alpha"
    assert beta.config.primary_model == "beta"
    assert client_calls == [
        {"api_key": "alpha-key"},
        {"api_key": "beta-key"},
    ]
    assert dict(os.environ) == before


def test_default_runtime_passes_both_supported_credentials_without_base_url(
    tmp_path, monkeypatch
):
    import agent_app.bootstrap as bootstrap

    client_calls = []

    class FakeAnthropic:
        def __init__(self, **kwargs):
            client_calls.append(kwargs)
            self.messages = FakeSDKClient.Messages()

    fake_anthropic = types.ModuleType("anthropic")
    fake_dotenv = types.ModuleType("dotenv")
    fake_anthropic.Anthropic = FakeAnthropic
    fake_dotenv.dotenv_values = lambda _path: {}
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    monkeypatch.setattr(bootstrap, "GLOBAL_SETTINGS_PATH", tmp_path / "missing-global")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MODEL_ID", "test-model")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "auth-token")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    bootstrap.build_default_runtime()

    assert client_calls == [
        {"api_key": "api-key", "auth_token": "auth-token"}
    ]
