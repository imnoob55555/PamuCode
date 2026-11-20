import types

from agent_app.config import AppConfig
from agent_app.features.subagents import spawn_subagent
from agent_app.tools.hooks import HookRegistry


class FakeAdapter:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)


def text_response(text):
    return types.SimpleNamespace(
        stop_reason="end_turn",
        content=[types.SimpleNamespace(type="text", text=text)],
    )


def test_subagent_uses_fresh_history(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ID", "test-model")
    config = AppConfig.from_env(tmp_path)
    fake_adapter = FakeAdapter([text_response("done")])

    result = spawn_subagent(
        description="inspect parser",
        llm=fake_adapter,
        config=config,
        system="subagent system",
        tools=[],
        handlers={},
        hooks=HookRegistry(),
    )

    assert result == "done"
    assert fake_adapter.calls[0]["messages"] == [
        {"role": "user", "content": "inspect parser"}
    ]


def test_subagent_stops_repeating_an_identical_tool(tmp_path):
    from agent_app.adapters.common import ToolUseBlock
    from agent_app.tools.executor import TOOL_REPEAT_ERROR, TOOL_REPEAT_STOP

    config = AppConfig.from_env(tmp_path, environ={"MODEL_ID": "test-model"})
    replies = [types.SimpleNamespace(content=[ToolUseBlock(str(i), "inspect", {})]) for i in range(5)]
    adapter = FakeAdapter(replies)
    calls = []
    result = spawn_subagent("inspect", adapter, config, "system", [],
                            {"inspect": lambda: calls.append(True) or "ok"}, HookRegistry())

    assert result == TOOL_REPEAT_STOP
    assert calls == [True] * 3
    assert adapter.calls[-1]["messages"][-1]["content"][0]["content"] == TOOL_REPEAT_ERROR


def test_subagent_can_retrieve_own_externalized_result_but_not_sibling(tmp_path):
    import json
    import re
    from agent_app.adapters.common import ToolUseBlock
    from agent_app.core.delegated_context import DelegatedContext
    from agent_app.core.context_manager import archive_for
    config = AppConfig.from_env(tmp_path, environ={'MODEL_ID': 'test-model'})
    delegate = DelegatedContext(config, lambda **kw: None, 'test-model')
    sibling = DelegatedContext(config, lambda **kw: None, 'test-model')
    sibling_ref = archive_for(sibling.runtime).save_artifact('SIBLING PRIVATE')
    calls = []
    executions = []
    def llm(**kwargs):
        calls.append(kwargs)
        names = {tool['name'] for tool in kwargs['tools']}
        assert {'history_read', 'history_search'} <= names
        if len(calls) == 1:
            return types.SimpleNamespace(content=[ToolUseBlock('one', 'produce', {})])
        if len(calls) == 2:
            result = kwargs['messages'][-1]['content'][0]['content']
            ref = re.search(r'artifact:[0-9a-f]{64}', result).group()
            return types.SimpleNamespace(content=[ToolUseBlock('read', 'history_read', {'ref': ref, 'offset': 4900, 'limit': 200}),
                                                  ToolUseBlock('deny', 'history_read', {'ref': sibling_ref})])
        values = kwargs['messages'][-1]['content']
        assert 'MIDDLE EVIDENCE' in json.loads(values[0]['content'])['text']
        assert 'SIBLING PRIVATE' not in str(values)
        assert values[1]['content'].startswith('Error:')
        return text_response('retrieved')
    def produce():
        executions.append(True)
        return 'x'*5000 + 'MIDDLE EVIDENCE' + 'y'*5000
    assert spawn_subagent('inspect', llm, config, 'system', [{'name': 'produce', 'input_schema': {'type': 'object', 'properties': {}}}],
                          {'produce': produce}, HookRegistry(), context=delegate) == 'retrieved'
    assert executions == [True]
