"""Real runtime/loop acceptance with protocol fixtures and no network."""

from types import SimpleNamespace

import pytest

from agent_app.bootstrap import build_runtime
from agent_app.config import AppConfig
from agent_app.core.loop import run_agent_loop, _summarize
from agent_app.adapters.common import TextBlock, ModelResponse
from tests.test_agent_app_loop import tool_results


def protocol_adapter(protocol):
    from tests.test_openai_responses_adapter import Client, Stream, answer, text, CALL
    if protocol == 'openai_responses':
        from agent_app.adapters.openai_responses import OpenAIResponsesAdapter
        client = Client(
            Stream([{'type': 'response.completed', 'response': answer([dict(CALL, name='echo_tool', arguments='{"value":"hello"}')])}]),
            Stream([{'type': 'response.output_text.delta', 'delta': 'finished'}, {'type': 'response.completed', 'response': answer([text('finished')])}]),
        )
        return OpenAIResponsesAdapter(client, origin='test'), client.requests
    if protocol == 'openai_chat':
        from tests.test_openai_chat_adapter import Client as ChatClient, delta
        from agent_app.adapters.openai_chat import OpenAIChatAdapter
        client = ChatClient(
            Stream([delta(calls=[{'index': 0, 'id': 'c1', 'function': {'name': 'echo_tool', 'arguments': '{"value":"hello"}'}}]), delta(finish='tool_calls')]),
            Stream([delta('finished'), delta(finish='stop')]),
        )
        return OpenAIChatAdapter(client, origin='test'), client.requests
    from tests.test_ollama_adapter import make, ndjson, answer as ollama_answer
    return make([ndjson(ollama_answer(calls=[{'function': {'name': 'echo_tool', 'arguments': {'value': 'hello'}}}])), ndjson(ollama_answer('finished'))])


@pytest.mark.parametrize('protocol', ['openai_responses', 'openai_chat', 'ollama'])
def test_real_loop_executes_tool_and_returns_result_through_protocol(tmp_path, protocol, monkeypatch):
    # Memory selection/extraction is separately exercised below; keep this
    # conversation focused on the two streaming turns and real tool executor.
    monkeypatch.setattr('agent_app.core.loop.extract_memories', lambda *args, **kw: None)
    monkeypatch.setattr('agent_app.core.loop.consolidate_memories', lambda *args, **kw: None)
    adapter, sent = protocol_adapter(protocol)
    runtime = build_runtime(AppConfig.from_env(tmp_path, environ={'MODEL_ID': 'local', 'PAMU_CONTEXT_WINDOW': '256000'}), object(), adapter=adapter, protocol=protocol)
    executed = []
    def echo(value):
        executed.append(value)
        return 'echo:' + value
    runtime.tools.register({'name': 'echo_tool', 'description': 'Echo', 'input_schema': {'type': 'object', 'properties': {'value': {'type': 'string'}}}}, echo)
    runtime.session.history.append({'role': 'user', 'content': 'call echo'})
    run_agent_loop(runtime)
    assert executed == ['hello']
    assert tool_results(runtime)[0]['content'] == 'echo:hello'
    assert len(sent) == 2
    assert 'echo:hello' in str(sent[1])
    assert runtime.session.history[-1]['content'][0].text == 'finished'
    if protocol == 'ollama':
        adapter.client.close()


def test_subagents_summary_and_teammates_keep_selected_adapter(tmp_path, monkeypatch):
    calls = []
    class Adapter:
        def create(self, **kwargs):
            calls.append(kwargs)
            return ModelResponse([TextBlock('result')], 'end_turn')
    adapter = Adapter()
    runtime = build_runtime(AppConfig.from_env(tmp_path, environ={'MODEL_ID': 'local', 'PAMU_CONTEXT_WINDOW': '256000'}), object(), adapter=adapter, provider='ollama', protocol='ollama')
    handlers = runtime.tools.snapshot()[1]
    assert handlers['task']('inspect') == 'result'
    assert _summarize(runtime, 'summarize') == 'result'
    assert [r['model'] for r in calls] == ['local', 'local']
    assert calls[0]['messages'][0]['content'] == 'inspect'
    teammate = []
    def spawn(team, bus, llm, **kwargs):
        teammate.append(llm)
        return 'spawned'
    monkeypatch.setattr('agent_app.bootstrap.teammates.spawn_teammate_thread', spawn)
    assert handlers['spawn_teammate']('worker', 'reviewer', 'inspect') == 'spawned'
    # A provider change must not pair an existing teammate's old model ID
    # with the new provider's client.
    runtime.model.switch('openai', 'other', llm=object())
    result = teammate[0](system=None, messages=[], max_tokens=10, tools=None)
    assert result.content[0].text == 'result'
    assert calls[-1]['model'] == 'local'


@pytest.mark.parametrize('status,expected', [(404, '模型'), (401, '认证'), (400, '工具')])
def test_http_model_errors_have_actionable_chinese_feedback(status, expected, capsys):
    import httpx
    from agent_app.core.recovery import append_unrecoverable_error
    request = httpx.Request('POST', 'http://localhost:11434/api/chat')
    response = httpx.Response(status, request=request)
    history = []
    append_unrecoverable_error(history, httpx.HTTPStatusError('raw server failure', request=request, response=response))
    assert expected in history[-1]['content'][0]['text']
    assert expected in capsys.readouterr().out


def test_local_connection_failure_mentions_service_address(capsys):
    import httpx
    from agent_app.core.recovery import append_unrecoverable_error
    history = []
    append_unrecoverable_error(history, httpx.ConnectError('connection refused'))
    assert '服务地址' in capsys.readouterr().out


def test_summary_uses_portable_content_without_native_signatures(tmp_path):
    calls = []
    adapter = SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or ModelResponse([TextBlock('summary')], 'end_turn'))
    runtime = build_runtime(AppConfig.from_env(tmp_path, environ={'MODEL_ID': 'local', 'PAMU_CONTEXT_WINDOW': '256000'}), object(), adapter=adapter, provider='ollama', protocol='ollama')
    history = [{'role': 'assistant', 'content': [
        SimpleNamespace(type='thinking', thinking='private-thought', signature='private-signature', _pamu_scope=('anthropic_messages', 'old', 'old')),
        {'type': 'redacted_thinking', 'data': 'private-data'},
        TextBlock('visible-answer'),
    ]}]
    assert _summarize(runtime, history) == 'summary'
    prompt = calls[0]['messages'][0]['content']
    assert 'visible-answer' in prompt
    assert 'private-' not in prompt
    assert '_pamu_scope' not in prompt
