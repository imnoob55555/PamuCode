import json
import types
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import agent_app.bootstrap as bootstrap
from agent_app.bootstrap import build_runtime
from agent_app.config import AppConfig
from agent_app.features.teams import teammates
from agent_app.features.teams.bus import MessageBus
from agent_app.features.teams.protocol import (
    ProtocolState,
    ProtocolStore,
    match_response,
    process_permission_request,
)
from agent_app.tools.hooks import HookRegistry


class FakeSDKClient:
    def __init__(self):
        self.messages = types.SimpleNamespace(create=lambda **_kwargs: text_response())


def text_response(text="done"):
    return types.SimpleNamespace(stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text=text)])


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ID", "test-model")
    config = AppConfig.from_env(tmp_path)
    config = replace(config, idle_timeout=0, permission_timeout=0)
    return build_runtime(config, FakeSDKClient())


@pytest.fixture
def bus(tmp_path):
    return MessageBus(tmp_path / ".mailboxes")


def test_bus_protocol_and_team_states_are_isolated(bus):
    bus.send("lead", "worker", "hello")
    assert [item["content"] for item in bus.read_inbox("worker")] == ["hello"]
    assert bus.read_inbox("worker") == []
    first, second = ProtocolStore(), ProtocolStore()
    first.pending["req"] = ProtocolState("req", "shutdown", "lead", "worker", "pending", "")
    assert second.pending == {}
    assert teammates.TeamState().active == {}


def test_teammate_history_window_keeps_tool_results_with_their_calls(bus, tmp_path):
    from agent_app.adapters.common import ToolUseBlock, chat_history

    class ImmediateThread:
        def __init__(self, target, **kwargs): self.target = target
        def start(self): self.target()

    idle_calls = []
    requests = []
    def idle(name, messages, *args):
        if idle_calls:
            return 'timeout'
        idle_calls.append(True)
        messages.extend([
            {'role': 'assistant', 'content': [ToolUseBlock('previous', 'read_file', {'path': 'a'})]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'previous', 'content': 'contents'}]},
            *[{'role': 'user', 'content': f'update {i}'} for i in range(19)],
        ])
        return 'work'

    def llm(**kwargs):
        requests.append(kwargs)
        chat_history(kwargs['system'], kwargs['messages'])
        return text_response('done')

    teammates.spawn_teammate_thread(
        teammates.TeamState(), bus, llm, name='worker', role='reviewer', prompt='inspect',
        workdir=tmp_path, handlers={}, hooks=HookRegistry(), validate_name=lambda *a, **kw: None,
        guarded_tools=set(), guarded_tool=lambda *a: ('', False), idle=idle, max_tokens=100,
        thread_factory=ImmediateThread,
    )
    assert len(requests) == 2
    assert bus.read_inbox('lead')[-1]['content'] == 'done'


def test_protocol_routes_only_matching_response_and_permission(bus, tmp_path):
    store = ProtocolStore()
    store.pending["shutdown"] = ProtocolState("shutdown", "shutdown", "lead", "worker", "pending", "")
    assert not match_response(store, "plan_approval_response", "shutdown", True)
    assert match_response(store, "shutdown_response", "shutdown", True)
    bus.authority.register("permit", "worker", {"tool_use_id": "tool", "tool_name": "bash", "tool_input": {"command": "echo safe"}, "cwd": str(tmp_path)})
    process_permission_request(
        bus, {"from": "worker", "content": {"request_id": "permit", "tool_use_id": "tool", "tool_name": "bash", "tool_input": {"command": "echo safe"}, "cwd": str(tmp_path)}},
        hook=lambda *_args: None, cwd_resolver=Path, guarded_tools={"bash"},
    )
    assert bus.read_inbox("worker")[0]["content"] == {"request_id": "permit", "approved": True, "reason": ""}


def test_runtime_registers_team_tools_and_uses_state_mailboxes(runtime):
    schemas, handlers = runtime.tools.snapshot()
    names = {schema["name"] for schema in schemas}
    assert {"task", "spawn_teammate", "send_message", "check_inbox"} <= names
    assert {"task", "spawn_teammate", "send_message", "check_inbox"} <= set(handlers)
    assert runtime.config.mailbox_dir.name == "mailboxes"


@pytest.mark.parametrize("name", ["a", "Researcher_1", "worker-name", "x" * 32])
def test_mailbox_safe_names_stay_below_runtime_root(runtime, name):
    runtime.bus.send("lead", name, "safe")
    path = runtime.config.mailbox_dir / f"{name}.jsonl"
    assert path.resolve().is_relative_to(runtime.config.mailbox_dir.resolve())
    assert runtime.bus.read_inbox(name)[0]["content"] == "safe"


@pytest.mark.parametrize("name", ["", "../evil", "a/b", "a\\b", "1worker", "has space", "x" * 33])
def test_mailbox_rejects_unsafe_names(runtime, name):
    with pytest.raises((TypeError, ValueError)):
        runtime.bus.send("lead", name, "unsafe")
    assert all(path.resolve().is_relative_to(runtime.config.mailbox_dir.resolve()) for path in runtime.config.mailbox_dir.rglob("*"))


def test_concurrent_messages_and_lead_inbox_consumption(runtime):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: runtime.bus.send(f"worker-{index}", "lead", f"message-{index}"), range(40)))
    lines = (runtime.config.mailbox_dir / "lead.jsonl").read_text().splitlines()
    assert len(lines) == 40 and all(isinstance(json.loads(line), dict) for line in lines)
    assert {item["content"] for item in runtime.bus.read_inbox("lead")} == {f"message-{index}" for index in range(40)}


def test_corrupt_mailbox_line_does_not_hide_valid_messages(bus, capsys):
    bus.bootstrap()
    bus.mailbox_path("worker").write_text("not-json\n", encoding="utf-8")
    bus.send("lead", "worker", "valid")

    messages = bus.read_inbox("worker")

    assert [message["content"] for message in messages] == ["valid"]
    assert "ignored corrupt line" in capsys.readouterr().out


def test_teammate_owner_validates_lifecycle_and_nonrecursive_tools(bus, tmp_path):
    state = teammates.TeamState()
    created = []
    class Thread:
        def __init__(self, *, target, daemon): self.target, self.daemon = target, daemon
        def start(self): created.append(self)
    kwargs: dict = {"workdir": tmp_path, "handlers": {}, "hooks": HookRegistry(), "validate_name": lambda name, **_kwargs: (_ for _ in ()).throw(ValueError("reserved")) if name == "lead" else name, "guarded_tools": set(), "guarded_tool": lambda *_args: ("", False), "idle": lambda *_args: "timeout", "max_tokens": 100, "thread_factory": Thread}
    assert "Invalid" in teammates.spawn_teammate_thread(state, bus, lambda **_kwargs: text_response(), name="lead", role="r", prompt="p", **kwargs)
    kwargs["validate_name"] = lambda name, **_kwargs: name
    result = teammates.spawn_teammate_thread(state, bus, lambda **_kwargs: text_response(), name="worker", role="reviewer", prompt="inspect", **kwargs)
    assert "spawned" in result and created[0].daemon and "worker" in state.active
    assert {tool["name"] for tool in teammates.TEAM_TOOLS}.isdisjoint({"task", "spawn_teammate", "schedule_cron"})
    created[0].target()
    assert "worker" not in state.active
    assert bus.read_inbox("lead")[0]["content"] == "done"


def test_teammate_owner_injects_inbox_and_post_hook_once(bus, tmp_path):
    state, events, calls = teammates.TeamState(), [], []
    bus.send("lead", "worker", "Focus parser tests")
    block = types.SimpleNamespace(type="tool_use", id="guarded", name="bash", input={"command": "echo guarded"})
    responses = iter([types.SimpleNamespace(content=[block]), text_response("complete")])
    class Immediate:
        def __init__(self, *, target, daemon): self.target = target
        def start(self): self.target()
    hooks = HookRegistry()
    hooks.register("PostToolUse", lambda block, output: events.append((block, output)))
    teammates.spawn_teammate_thread(state, bus, lambda **kwargs: (calls.append(kwargs), next(responses))[1], name="worker", role="reviewer", prompt="inspect", workdir=tmp_path, handlers={"bash": lambda command, cwd=None: "guarded output"}, hooks=hooks, validate_name=lambda name, **_kwargs: name, guarded_tools={"bash"}, guarded_tool=lambda _agent, used, _inbox, handler, _cwd: (handler(**used.input), False), idle=lambda *_args: "timeout", max_tokens=100, thread_factory=Immediate)
    assert "Focus parser tests" in json.dumps(calls[0]["messages"])
    assert events == [(block, "guarded output")]
    assert bus.read_inbox("lead")[0]["content"] == "complete"


def test_teammate_waiting_for_plan_approval_sleeps_between_polls(bus, tmp_path):
    state = teammates.TeamState()
    plan = types.SimpleNamespace(
        type="tool_use",
        id="plan",
        name="submit_plan",
        input={"plan": "inspect first"},
    )
    responses = iter([types.SimpleNamespace(content=[plan]), text_response("done")])
    sleeps = []

    class ImmediateThread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            self.target()

    def sleep(seconds):
        sleeps.append(seconds)
        bus.send(
            "lead",
            "worker",
            "approved",
            "plan_approval_response",
            {"request_id": "req_1", "approve": True},
        )

    teammates.spawn_teammate_thread(
        state,
        bus,
        lambda **_kwargs: next(responses),
        name="worker",
        role="reviewer",
        prompt="inspect",
        workdir=tmp_path,
        handlers={"submit_plan": lambda plan: "Plan submitted (req_1)"},
        hooks=HookRegistry(),
        validate_name=lambda name, **_kwargs: name,
        guarded_tools=set(),
        guarded_tool=lambda *_args: ("", False),
        idle=lambda *_args: "timeout",
        max_tokens=100,
        thread_factory=ImmediateThread,
        sleep=sleep,
        plan_poll_interval=0.25,
    )

    assert sleeps == [0.25]
    assert bus.read_inbox("lead")[0]["content"] == "done"


def test_bootstrap_teammate_idle_adapter_drops_role_argument(
    runtime, monkeypatch
):
    class ImmediateThread:
        def __init__(self, *, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            self.target()

    monkeypatch.setattr(bootstrap.threading, "Thread", ImmediateThread)
    _, handlers = runtime.tools.snapshot()

    result = handlers["spawn_teammate"]("worker", "reviewer", "inspect")
    messages = runtime.bus.read_inbox("lead")

    assert "spawned" in result
    assert messages[-1]["content"] == "done"


def test_bootstrap_teammate_retries_transient_errors(runtime, monkeypatch):
    from agent_app.core import recovery

    calls = []

    class ImmediateThread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            self.target()

    class RateLimited(Exception):
        status_code = 429

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RateLimited()
        return text_response("retried")

    runtime.llm.client.messages.create = create
    monkeypatch.setattr(bootstrap.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(recovery.time, "sleep", lambda _: None)
    _, handlers = runtime.tools.snapshot()

    handlers["spawn_teammate"]("worker", "reviewer", "inspect")

    assert [call["model"] for call in calls] == ["test-model", "test-model"]
    assert runtime.bus.read_inbox("lead")[-1]["content"] == "retried"
    assert runtime.team.active == {}


def test_teammate_error_and_round_limit_send_result_and_cleanup(bus, tmp_path):
    class ImmediateThread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            self.target()

    common: dict = {
        "role": "reviewer",
        "prompt": "inspect",
        "workdir": tmp_path,
        "handlers": {},
        "hooks": HookRegistry(),
        "validate_name": lambda name, **_kwargs: name,
        "guarded_tools": set(),
        "guarded_tool": lambda *_args: ("", False),
        "idle": lambda *_args: "timeout",
        "max_tokens": 100,
        "thread_factory": ImmediateThread,
    }

    error_state = teammates.TeamState()
    teammates.spawn_teammate_thread(
        error_state,
        bus,
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        name="error-worker",
        **common,
    )
    assert error_state.active == {}
    assert "Teammate error: RuntimeError: boom" in bus.read_inbox("lead")[0][
        "content"
    ]

    blocks = iter(types.SimpleNamespace(type="tool_use", id=str(i), name="missing", input={"step": i})
                  for i in range(10))
    round_state = teammates.TeamState()
    teammates.spawn_teammate_thread(
        round_state,
        bus,
        lambda **_kwargs: types.SimpleNamespace(content=[next(blocks)]),
        name="round-worker",
        **common,
    )
    assert round_state.active == {}
    assert bus.read_inbox("lead")[0]["content"] == (
        "Stopped after 10 teammate tool rounds."
    )

    from agent_app.adapters.common import ToolUseBlock
    from agent_app.tools.executor import TOOL_REPEAT_STOP
    calls = []
    repeats = iter(ToolUseBlock(str(i), "list_tasks", {}) for i in range(5))
    repeat_state = teammates.TeamState()
    teammates.spawn_teammate_thread(
        repeat_state, bus, lambda **_: types.SimpleNamespace(content=[next(repeats)]),
        name="repeat-worker", **{**common, "handlers": {"list_tasks": lambda: calls.append(True) or "ok"}},
    )
    assert repeat_state.active == {}
    assert calls == [True] * 3
    assert bus.read_inbox("lead")[-1]["content"] == TOOL_REPEAT_STOP


def test_teammate_retrieval_tools_use_only_delegate_archive(bus, tmp_path):
    from agent_app.adapters.common import ToolUseBlock
    from agent_app.core.delegated_context import DelegatedContext
    from agent_app.core.context_manager import archive_for
    config = AppConfig.from_env(tmp_path, environ={'MODEL_ID': 'test-model'})
    delegate = DelegatedContext(config, lambda **kw: None, 'test-model')
    other = DelegatedContext(config, lambda **kw: None, 'test-model')
    own = archive_for(delegate.runtime).save_artifact('own evidence')
    forbidden = archive_for(other.runtime).save_artifact('other private evidence')
    calls = []
    class ImmediateThread:
        def __init__(self, target, **kw): self.target = target
        def start(self): self.target()
    def llm(**kwargs):
        calls.append(kwargs)
        assert {'history_read', 'history_search'} <= {tool['name'] for tool in kwargs['tools']}
        if len(calls) == 1:
            return types.SimpleNamespace(content=[ToolUseBlock('one', 'history_read', {'ref': own}),
                                                  ToolUseBlock('two', 'history_read', {'ref': forbidden})])
        results = kwargs['messages'][-1]['content']
        assert json.loads(results[0]['content'])['text'] == 'own evidence'
        assert results[1]['content'].startswith('Error:')
        assert 'other private evidence' not in str(results)
        return text_response('retrieved safely')
    teammates.spawn_teammate_thread(
        teammates.TeamState(), bus, llm, name='reader', role='reviewer', prompt='inspect evidence',
        workdir=tmp_path, handlers={'history_read': lambda **kw: 'parent leak'}, hooks=HookRegistry(),
        validate_name=lambda *a, **kw: None, guarded_tools=set(), guarded_tool=lambda *a: ('', False),
        idle=lambda *a: 'timeout', max_tokens=100, thread_factory=ImmediateThread, context=delegate,
    )
    assert len(calls) == 2
    assert bus.read_inbox('lead')[-1]['content'] == 'retrieved safely'
