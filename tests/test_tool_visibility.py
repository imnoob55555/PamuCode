from dataclasses import replace

import pytest

from agent_app.adapters.common import ModelResponse, TextBlock, ToolUseBlock
from agent_app.bootstrap import build_runtime
from agent_app.cli import run_agent_turn
from agent_app.config import AppConfig
from agent_app.core.context_manager import estimate_current_context
from agent_app.features import tasks
from agent_app.tools.visibility import FOLLOW_UP_TOOLS, offered_tools

FOLLOW_UPS = {name for names in FOLLOW_UP_TOOLS.values() for name in names}


class ScriptedModel:
    def __init__(self):
        self.responses = []
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)

    create_streaming = create


@pytest.fixture
def runtime(tmp_path):
    config = replace(AppConfig.from_env(tmp_path, environ={"MODEL_ID": "test"}), headless=True)
    rt = build_runtime(config, None, adapter=ScriptedModel())
    yield rt
    rt.sessions.close()


def offered(runtime):
    return {tool["name"] for tool in offered_tools(runtime, runtime.tools.snapshot()[0])}


def test_fresh_session_offers_entry_tools_but_not_idle_follow_ups(runtime):
    runtime.llm.responses = [ModelResponse([TextBlock("done")], "end_turn")]
    run_agent_turn(runtime, "hello")
    sent = {tool["name"] for tool in runtime.llm.requests[0]["tools"]}
    assert not FOLLOW_UPS & sent
    assert {"create_task", "schedule_cron", "create_worktree", "spawn_teammate", "task"} <= sent
    assert FOLLOW_UPS <= set(runtime.tools.snapshot()[1])
    assert "Available tools" not in runtime.llm.requests[0]["system"]


def test_state_with_something_to_act_on_offers_each_group(runtime):
    tasks.create_task(runtime.tasks, "pending work")
    runtime.scheduler.jobs["job"] = object()
    runtime.bus.send("worker", "lead", "hi")
    (runtime.worktrees.root / "feature").mkdir(parents=True)
    assert FOLLOW_UPS <= offered(runtime)


def test_group_stays_offered_once_used_in_the_conversation(runtime):
    runtime.session.history.append({"role": "assistant", "content": [ToolUseBlock("c1", "cancel_cron", {"job_id": "x"})]})
    names = offered(runtime)
    assert set(FOLLOW_UP_TOOLS["cron"]) <= names
    assert not set(FOLLOW_UP_TOOLS["tasks"]) & names


def test_uninspectable_state_keeps_the_group_offered(runtime):
    runtime.team = None
    assert set(FOLLOW_UP_TOOLS["team"]) <= offered(runtime)


def test_hidden_tool_still_runs_when_called(runtime):
    runtime.llm.responses = [ModelResponse([ToolUseBlock("c1", "list_tasks", {})], "tool_use"),
                             ModelResponse([TextBlock("done")], "end_turn")]
    run_agent_turn(runtime, "list tasks")
    result = next(block for message in runtime.session.history if isinstance(message["content"], list)
                  for block in message["content"] if isinstance(block, dict) and block.get("type") == "tool_result")
    assert not result.get("is_error")
    assert "list_tasks" in {tool["name"] for tool in runtime.llm.requests[1]["tools"]}


def test_context_estimate_counts_only_offered_tools(runtime):
    before = estimate_current_context(runtime).categories["tools"]
    tasks.create_task(runtime.tasks, "pending work")
    assert estimate_current_context(runtime).categories["tools"] > before
