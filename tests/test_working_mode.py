"""Behavior checks use the real runtime with scripted model responses, never credentials."""
import json
from dataclasses import replace

import pytest

from agent_app.adapters.common import ModelResponse, TextBlock, ToolUseBlock
from agent_app.bootstrap import build_runtime
from agent_app.cli import handle_slash_command, run_agent_turn
from agent_app.config import AppConfig
from agent_app.core.context import build_context
from agent_app.core.context_manager import compact, context_control
from agent_app.features.mcp import connect_mcp
from agent_app.working_mode import switch_mode, working_mode


class ScriptedModel:
    def __init__(self):
        self.responses = []
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)

    create_streaming = create


def answer(text="done"):
    return ModelResponse([TextBlock(text)], "end_turn")


@pytest.fixture
def runtime(tmp_path):
    config = replace(AppConfig.from_env(tmp_path, environ={"MODEL_ID": "test"}), headless=True)
    rt = build_runtime(config, None, adapter=ScriptedModel())
    yield rt
    rt.sessions.close()


def test_mode_command_before_first_message_is_persisted(runtime):
    output = []
    handle_slash_command(runtime, "/plan", print_fn=output.append)
    assert "规划模式" in output[0]
    assert working_mode(runtime.session) == "plan"
    assert runtime.sessions.store.load(runtime.sessions.session_id)["context_state"]["working_mode"] == "plan"
    assert not runtime.session.history
    assert not runtime.llm.requests


@pytest.mark.parametrize("name,args", [
    ("write_file", {"path": "answer.txt", "content": "changed"}),
    ("edit_file", {"path": "answer.txt", "old_text": "old", "new_text": "changed"}),
    ("bash", {"command": "printf changed > answer.txt", "run_in_background": True}),
    ("task", {"description": "modify answer.txt"}),
    ("spawn_teammate", {"name": "writer", "role": "coding", "prompt": "modify answer.txt"}),
    ("mcp__deploy__trigger", {"service": "production"}),
    ("unknown_external_action", {}),
])
def test_plan_blocks_adversarial_model_calls_before_dispatch(runtime, name, args):
    path = runtime.config.workdir / "answer.txt"
    path.write_text("old")
    connect_mcp(runtime.mcp, "deploy")
    switch_mode(runtime, "plan")
    runtime.llm.responses = [ModelResponse([ToolUseBlock("call-1", name, args)], "tool_use"), answer()]
    run_agent_turn(runtime, "Investigate only; do not modify anything.")
    assert path.read_text() == "old"
    results = [b for m in runtime.session.history if isinstance(m["content"], list)
               for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert results[0]["is_error"] is True
    assert "规划模式禁止" in results[0]["content"]
    offered = {t["name"] for t in runtime.llm.requests[0]["tools"]}
    assert not {"bash", "write_file", "task", "spawn_teammate"} & offered
    assert not runtime.background.tasks
    assert not runtime.team.active
    assert name not in {t["name"] for t in runtime.llm.requests[0]["tools"]}


def test_cached_handlers_obey_current_mode_and_execute_restores_edits(runtime):
    _, handlers = runtime.tools.snapshot()
    switch_mode(runtime, "plan")
    assert "规划模式禁止" in handlers["write_file"](path="answer.txt", content="changed")
    assert "规划模式禁止" in handlers["bash"](command="echo changed")
    switch_mode(runtime, "execute")
    handlers["write_file"](path="answer.txt", content="changed")
    assert "changed" in handlers["read_file"](path="answer.txt")


def test_plan_reads_files_and_finishes_without_modifying(runtime):
    path = runtime.config.workdir / "answer.txt"
    path.write_text("old")
    switch_mode(runtime, "plan")
    runtime.llm.responses = [ModelResponse([ToolUseBlock("read-1", "read_file", {"path": "answer.txt"})], "tool_use"), answer("Proposed plan.")]
    run_agent_turn(runtime, "Explain the file and propose a plan. Wait for approval.")
    assert path.read_text() == "old"
    assert "Working mode: plan" in runtime.llm.requests[0]["system"]
    assert "does not authorize edits" in runtime.llm.requests[0]["system"]


def test_mode_and_pinned_limits_survive_compaction_and_restore(runtime):
    switch_mode(runtime, "plan")
    context_control(runtime, "pin Do not push or deploy until I approve.")
    for i in range(6):
        runtime.session.history.extend([{"role": "user", "content": f"Investigate {i}"},
                                        {"role": "assistant", "content": "evidence " * 1000}])
    runtime.session.context_state["current_request"] = {"id": "request-1", "text": "Investigate 5"}
    summary = dict(goal="investigate", constraints=[], decisions=[], completed=[], remaining=["review"], uncertainties=[], references=[])
    # Even a summary that omits the constraint cannot remove the pinned original.
    runtime.llm.responses = [answer(json.dumps(summary)) for _ in range(8)]
    assert compact(runtime)
    saved = runtime.sessions.store.load(runtime.sessions.session_id)
    runtime.sessions.close()
    restored = build_runtime(runtime.config, None, adapter=ScriptedModel())
    try:
        restored.sessions.restore(restored, saved, restored.sessions.store.acquire(saved["id"]))
        assert working_mode(restored.session) == "plan"
        assert restored.session.context_state["current_request"]["text"] == "Investigate 5"
        context = build_context(restored, restored.tools.snapshot()[0])
        assert "Do not push" in context["pinned_constraints"]
        assert "规划模式禁止" in restored.tools.snapshot()[1]["bash"](command="git push")
    finally:
        restored.sessions.close()


@pytest.mark.parametrize("worker", ["background", "team"])
def test_mode_switch_rejects_active_workers(runtime, worker):
    if worker == "background":
        runtime.background.tasks["test"] = {"status": "running"}
    else:
        runtime.team.active["test"] = {"status": "running"}
    with pytest.raises(ValueError, match="仍在运行"):
        switch_mode(runtime, "plan")
    assert working_mode(runtime.session) == "execute"


@pytest.mark.parametrize("saved", ["unexpected", None, {}, []])
def test_unknown_saved_mode_cannot_enable_writes(runtime, saved):
    runtime.session.context_state["working_mode"] = saved
    assert "规划模式禁止" in runtime.tools.snapshot()[1]["write_file"](path="x", content="x")


def test_switch_during_agent_turn_is_rejected(runtime):
    with runtime.agent_lock:
        with pytest.raises(ValueError, match="正在执行"):
            switch_mode(runtime, "plan")
    assert working_mode(runtime.session) == "execute"


@pytest.mark.parametrize("kind", ["task", "spawn_teammate"])
def test_delegates_inherit_scope_and_cannot_use_stale_write_handler(runtime, monkeypatch, kind):
    from agent_app import bootstrap
    captured = {}
    runtime.session.context_state["current_request"] = {"text": "Fix locally; do not push."}
    context_control(runtime, "pin Preserve the public API.")
    if kind == "task":
        def spawn(description, llm, config, system, tools, handlers, hooks, **kwargs):
            captured.update(scope=system, handlers=handlers)
            return "captured"
        monkeypatch.setattr(bootstrap.subagents, "spawn_subagent", spawn)
        runtime.tools.snapshot()[1][kind]("Fix the file")
    else:
        def spawn(*args, **kwargs):
            captured.update(scope=kwargs["prompt"], handlers=kwargs["handlers"])
            return "captured"
        monkeypatch.setattr(bootstrap.teammates, "spawn_teammate_thread", spawn)
        runtime.tools.snapshot()[1][kind]("worker", "coding", "Fix the file")
    assert "do not push" in captured["scope"]
    assert "Preserve the public API" in captured["scope"]
    switch_mode(runtime, "plan")
    assert "规划模式禁止" in captured["handlers"]["write_file"](path="answer.txt", content="changed")
    assert not (runtime.config.workdir / "answer.txt").exists()


def test_live_intent_suite_loads_with_existing_eval_runner():
    from pathlib import Path
    from pamu_eval.suite import load_suite
    suite = load_suite(Path(__file__).resolve().parents[1] / "evals" / "intent-live.json")
    assert len(suite["tasks"]) == 4
