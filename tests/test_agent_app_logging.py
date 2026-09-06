"""Acceptance tests for the runtime logging system."""

from __future__ import annotations

import logging
import stat
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from agent_app.config import AppConfig
from agent_app.logging_setup import (
    get_logger,
    resolve_level,
    reset_logging,
    setup_logging,
)
from agent_app.tools.hooks import HookRegistry, make_log_hook


@pytest.fixture(autouse=True)
def isolated_pamu_logger():
    reset_logging()
    yield
    reset_logging()


class FakeSDKClient:
    class Messages:
        def create(self, **_kwargs):
            raise AssertionError("no live request expected")

        def stream(self, **_kwargs):
            raise AssertionError("no live request expected")

    def __init__(self):
        self.messages = self.Messages()


def test_setup_logging_creates_log_file_under_state_logs_dir(tmp_path):
    logs_dir = tmp_path / ".pamu" / "logs"

    logger = setup_logging(logs_dir, "INFO")
    logger.info("hello pamu")
    for handler in logger.handlers:
        handler.flush()

    assert (logs_dir / "pamu.log").is_file()
    content = (logs_dir / "pamu.log").read_text(encoding="utf-8")
    assert "hello pamu" in content
    assert "INFO" in content


def test_setup_logging_is_idempotent_and_keeps_single_file_handler(tmp_path):
    first = setup_logging(tmp_path / "logs-a")
    second = setup_logging(tmp_path / "logs-b")

    assert first is second
    rotating = [
        handler
        for handler in first.handlers
        if isinstance(handler, RotatingFileHandler)
    ]
    assert len(rotating) == 1
    # 第二个目录不应被创建：handler 未重复挂载
    assert not (tmp_path / "logs-b").exists()


def test_log_level_from_settings_is_honoured(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MODEL_ID", "test-model")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    config = AppConfig.from_env(tmp_path)

    assert config.log_level == "DEBUG"
    logger = setup_logging(config.logs_dir, config.log_level)
    logger.debug("debug-line")

    for handler in logger.handlers:
        handler.flush()
    assert "debug-line" in (config.logs_dir / "pamu.log").read_text(
        encoding="utf-8"
    )


def test_invalid_log_level_falls_back_to_info():
    assert resolve_level("not-a-level") == logging.INFO
    assert resolve_level("WARNING") == logging.WARNING


def test_build_runtime_creates_logs_dir_and_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ID", "test-model")
    config = AppConfig.from_env(tmp_path)

    from agent_app.bootstrap import build_runtime

    build_runtime(config, FakeSDKClient())

    assert config.logs_dir.is_dir()
    assert (config.logs_dir / "pamu.log").is_file()


def test_build_runtime_rejects_symlinked_logs_dir(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    state = workspace / ".pamu"
    external = tmp_path / "external-logs"
    state.mkdir(parents=True)
    external.mkdir()
    (state / "logs").symlink_to(external, target_is_directory=True)
    monkeypatch.setenv("MODEL_ID", "test-model")
    config = AppConfig.from_env(tmp_path / "install", workspace)

    from agent_app.bootstrap import build_runtime

    with pytest.raises(ValueError, match="symlink"):
        build_runtime(config, FakeSDKClient())

    assert list(external.iterdir()) == []


def test_log_hook_records_tool_use_without_blocking(tmp_path, caplog):
    block = type("Block", (), {"name": "bash", "input": {"command": "ls"}})()
    hook = make_log_hook(tmp_path, logger=get_logger("pamu.hooks"))

    with caplog.at_level(logging.INFO, logger="pamu.hooks"):
        assert hook(block) is None

    assert any(
        "bash" in record.message and "tool_use" in record.message
        for record in caplog.records
    )


def test_agent_loop_logs_turn_lifecycle(tmp_path, monkeypatch, caplog):
    import types

    from agent_app.core.loop import run_agent_loop
    from agent_app.runtime import RuntimeContext, SessionState

    monkeypatch.setenv("MODEL_ID", "test-model")
    config = AppConfig.from_env(tmp_path)
    config.task_dir.mkdir(parents=True)
    config.mailbox_dir.mkdir(parents=True)
    config.worktrees_dir.mkdir(parents=True)

    class FakeAdapter:
        def create_streaming(self, **_kwargs):
            return types.SimpleNamespace(
                stop_reason="end_turn",
                content=[types.SimpleNamespace(type="text", text="ok")],
            )

        def create(self, **_kwargs):
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text="[]")]
            )

    # 与 test_agent_app_loop 相同的最小 runtime 组装
    from agent_app.features.background import BackgroundState
    from agent_app.features.mcp import MCPState
    from agent_app.features.memory import MemoryStore
    from agent_app.features.scheduler import SchedulerState
    from agent_app.features.skills import SkillState
    from agent_app.features.tasks import TaskStore
    from agent_app.features.teams.bus import MessageBus
    from agent_app.features.teams.protocol import ProtocolStore
    from agent_app.features.teams.teammates import TeamState
    from agent_app.features.worktrees import WorktreeState
    from agent_app.tools.hooks import HookRegistry
    from agent_app.tools.registry import ToolRegistry
    from agent_app.core.prompt import PromptBuilder

    runtime = RuntimeContext(
        config=config,
        llm=FakeAdapter(),
        session=SessionState(),
        prompt_builder=PromptBuilder(),
        tools=ToolRegistry(),
        hooks=HookRegistry(),
        scheduler=SchedulerState(),
        background=BackgroundState(),
        tasks=TaskStore(root=config.task_dir),
        worktrees=WorktreeState(
            workdir=config.workdir, root=config.worktrees_dir,
            run_git=lambda _args: (True, "ok"),
        ),
        skills=SkillState(root=config.skills_dir),
        memory=MemoryStore(root=config.memory_dir, index_path=config.memory_index),
        bus=MessageBus(root=config.mailbox_dir),
        protocols=ProtocolStore(),
        team=TeamState(),
        mcp=MCPState(),
    )

    with caplog.at_level(logging.INFO, logger="pamu.loop"):
        runtime.session.history.append({"role": "user", "content": "hi"})
        run_agent_loop(runtime)

    messages = [record.message for record in caplog.records]
    assert any("turn start" in message for message in messages)
    assert any("turn complete" in message for message in messages)


def test_credentials_file_permissions_are_owner_only(tmp_path):
    """凭证安全验收（auth 模块随日志一并验证权限位）。"""
    from agent_app.features.auth import save_credentials

    save_credentials({"providers": {}, "active": None}, tmp_path / "creds.json")

    mode = stat.S_IMODE((tmp_path / "creds.json").stat().st_mode)
    assert mode == 0o600
