"""Process-like snapshot roundtrips through every real protocol adapter."""
import json
from types import SimpleNamespace as NS

import pytest

from agent_app.bootstrap import build_runtime
from agent_app.cli import run_agent_turn
from agent_app.config import AppConfig
from agent_app.features.session_store import SessionStore
from agent_app.tools.hooks import HookRegistry
from tests.test_openai_responses_adapter import Client as ResponsesClient, answer, CALL, REASON, text
from tests.test_openai_chat_adapter import Client as ChatClient, completion, call
from tests.test_ollama_adapter import make as ollama_adapter, answer as ollama_answer, call as ollama_call


def adapter_for(protocol, *, first=False, origin='provider@http://current.local'):
    if protocol == 'anthropic_messages':
        from agent_app.adapters.anthropic import AnthropicAdapter
        responses = []
        if first:
            responses.append(NS(stop_reason='tool_use', content=[
                {'type': 'thinking', 'thinking': 'PRIVATE_NATIVE', 'signature': 'sig'},
                {'type': 'text', 'text': 'before tool'},
                {'type': 'tool_use', 'id': 'c1', 'name': 'read', 'input': {'path': 'a'}},
            ]))
        responses.append(NS(stop_reason='end_turn', content=[{'type': 'text', 'text': 'finished'}]))
        requests = []
        def create(**kw):
            requests.append(kw)
            response = responses.pop(0)
            response.content = [NS(**block) for block in response.content]
            return response
        adapter = AnthropicAdapter(NS(messages=NS(create=create)), origin=origin)
    elif protocol == 'openai_responses':
        from agent_app.adapters.openai_responses import OpenAIResponsesAdapter
        responses = [answer([dict(REASON, encrypted_content='PRIVATE_NATIVE'), text('before tool'), CALL])] if first else []
        client = ResponsesClient(*responses, answer([text('finished')]))
        adapter, requests = OpenAIResponsesAdapter(client, origin=origin), client.requests
    elif protocol == 'openai_chat':
        from agent_app.adapters.openai_chat import OpenAIChatAdapter
        responses = [completion('before tool', [call()], 'tool_calls')] if first else []
        client = ChatClient(*responses, completion())
        adapter, requests = OpenAIChatAdapter(client, origin=origin), client.requests
    else:
        responses = [ollama_answer('before tool', [ollama_call()], thinking='PRIVATE_NATIVE')] if first else []
        adapter, requests = ollama_adapter([*responses, ollama_answer('finished')], origin=origin)
    # Exercise the same real request/response codec without stream transport fixtures.
    return NS(create=lambda **kw: NS(content=[NS(type='text', text='[]')]),
              create_streaming=adapter.create), requests


@pytest.mark.parametrize('protocol', ['anthropic_messages', 'openai_responses', 'openai_chat', 'ollama'])
@pytest.mark.parametrize('change', ['none', 'endpoint', 'model', 'protocol'])
def test_fresh_runtime_next_turn_preserves_results_without_reexecuting_tools(tmp_path, protocol, change):
    config = AppConfig.from_env(tmp_path, environ={'MODEL_ID': 'm', 'PAMU_CONTEXT_WINDOW': '256000'})
    adapter, first_requests = adapter_for(protocol, first=True)
    original = build_runtime(config, None, adapter=adapter, protocol=protocol,
                             thinking_level='high' if protocol == 'anthropic_messages' else 'off')
    original.hooks = HookRegistry()
    executions = []
    schema = {'name': 'read', 'description': 'read', 'input_schema': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}
    original.tools.register(schema, lambda **kw: executions.append(kw) or 'PRESERVED_TOOL_RESULT')
    run_agent_turn(original, 'original question')
    assert len(executions) == 1 and len(first_requests) == 2
    session_id = original.sessions.session_id
    original.sessions.close()

    target = ('openai_responses' if protocol == 'openai_chat' else 'openai_chat') if change == 'protocol' else protocol
    origin = 'provider@http://other.local' if change == 'endpoint' else 'provider@http://current.local'
    adapter, requests = adapter_for(target, origin=origin)
    fresh_config = AppConfig.from_env(tmp_path, environ={'MODEL_ID': 'other' if change == 'model' else 'm', 'PAMU_CONTEXT_WINDOW': '256000'})
    restored = build_runtime(fresh_config, None, adapter=adapter, protocol=target,
                             thinking_level='high' if target == 'anthropic_messages' else 'off')
    restored.hooks = HookRegistry()
    restored.tools.register(schema, lambda **kw: executions.append(kw) or 'UNEXPECTED_EXECUTION')
    store = SessionStore(tmp_path)
    lease = store.acquire(session_id)
    restored.sessions.restore(restored, store.load(session_id), lease)
    assert not requests and len(executions) == 1
    run_agent_turn(restored, 'continue now')
    assert len(executions) == 1 and len(requests) == 1
    payload = json.dumps(requests[0], ensure_ascii=False)
    assert 'PRESERVED_TOOL_RESULT' in payload
    assert 'original question' in payload and 'continue now' in payload and 'before tool' in payload
    assert ('PRIVATE_NATIVE' in payload) == (change == 'none' and protocol != 'openai_chat')
    assert restored.sessions.store.load(session_id)['history'][-1]['role'] == 'assistant'
    restored.sessions.close()
