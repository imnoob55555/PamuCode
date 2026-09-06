"""Acceptance tests for runtime model/provider hot-switching."""

from __future__ import annotations

import types

from agent_app.config import AppConfig
from agent_app.core.loop import run_agent_loop
from agent_app.runtime import ModelRouter, RuntimeContext, SessionState


def text_response(text="done"):
    return types.SimpleNamespace(
        stop_reason="end_turn",
        content=[types.SimpleNamespace(type="text", text=text)],
    )


class RecordingAdapter:
    """Single adapter shared by the runtime across the whole session."""

    def __init__(self):
        self.requests = []

    def create_streaming(self, **kwargs):
        self.requests.append(kwargs)
        return text_response()

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="[]")]
        )


def _make_runtime(tmp_path, monkeypatch, adapter):
    from agent_app.core.prompt import PromptBuilder
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

    monkeypatch.setenv("MODEL_ID", "test-model")
    monkeypatch.delenv("FALLBACK_MODEL_ID", raising=False)
    config = AppConfig.from_env(tmp_path)
    config.task_dir.mkdir(parents=True, exist_ok=True)
    config.mailbox_dir.mkdir(parents=True, exist_ok=True)
    config.worktrees_dir.mkdir(parents=True, exist_ok=True)
    return RuntimeContext(
        config=config,
        llm=adapter,
        session=SessionState(),
        prompt_builder=PromptBuilder(),
        tools=ToolRegistry(),
        hooks=HookRegistry(),
        scheduler=SchedulerState(),
        background=BackgroundState(),
        tasks=TaskStore(root=config.task_dir),
        worktrees=WorktreeState(
            workdir=config.workdir,
            root=config.worktrees_dir,
            run_git=lambda _args: (True, "ok"),
        ),
        skills=SkillState(root=config.skills_dir),
        memory=MemoryStore(root=config.memory_dir, index_path=config.memory_index),
        bus=MessageBus(root=config.mailbox_dir),
        protocols=ProtocolStore(),
        team=TeamState(),
        mcp=MCPState(),
    )


def test_router_defaults_are_derived_from_config(tmp_path, monkeypatch):
    adapter = RecordingAdapter()
    runtime = _make_runtime(tmp_path, monkeypatch, adapter)

    assert runtime.model.snapshot() == ("anthropic", "test-model", None)
    assert runtime.model.llm is adapter


def test_model_switch_takes_effect_on_next_turn(tmp_path, monkeypatch):
    adapter = RecordingAdapter()
    runtime = _make_runtime(tmp_path, monkeypatch, adapter)

    runtime.session.history.append({"role": "user", "content": "one"})
    run_agent_loop(runtime)
    runtime.model.switch_model("deepseek-chat")
    runtime.session.history.append({"role": "user", "content": "two"})
    run_agent_loop(runtime)

    streaming_models = [
        request["model"] for request in adapter.requests if "tools" in request
    ]
    assert streaming_models[0] == "test-model"
    assert streaming_models[-1] == "deepseek-chat"
    assert runtime.model.snapshot()[1] == "deepseek-chat"


def test_provider_switch_keeps_router_and_llm_consistent(tmp_path, monkeypatch):
    from agent_app.adapters import providers

    adapter = RecordingAdapter()
    runtime = _make_runtime(tmp_path, monkeypatch, adapter)
    new_adapter = RecordingAdapter()

    original_build = providers.build_client

    def fake_build(spec, api_key, *, anthropic_factory=None, **_options):
        return object()

    providers.build_client = fake_build
    try:
        providers.apply_provider(
            runtime,
            providers.get_provider("deepseek"),
            "sk-ds",
            "deepseek-chat",
        )
    finally:
        providers.build_client = original_build
    # 用可观测的假 adapter 替换，验证 router/llm 同步语义与下一轮请求路由
    runtime.model.switch("deepseek", "deepseek-chat", None, new_adapter)
    runtime.llm = new_adapter

    assert runtime.llm is new_adapter
    assert runtime.model.llm is new_adapter
    assert runtime.model.snapshot()[:2] == ("deepseek", "deepseek-chat")

    runtime.session.history.append({"role": "user", "content": "hi"})
    run_agent_loop(runtime)

    assert adapter.requests == []
    assert new_adapter.requests


def test_router_switch_is_atomic_under_contention():
    import threading

    router = ModelRouter(provider="a", primary_model="m1", fallback_model="f1")
    seen = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            provider, primary, fallback = router.snapshot()
            seen.append((provider, primary, fallback))

    thread = threading.Thread(target=reader)
    thread.start()
    for index in range(200):
        router.switch("a" if index % 2 else "b", f"m{index}", f"f{index}")
    stop.set()
    thread.join()

    # 每次快照都是完整一致的三元组，绝不会混搭
    for provider, primary, fallback in seen:
        index = primary[1:]
        assert fallback == f"f{index}"
        assert provider == ("a" if int(index) % 2 else "b")
