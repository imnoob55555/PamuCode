from types import SimpleNamespace as NS

import pytest


def test_project_owned_blocks_and_anthropic_serialization():
    from agent_app.adapters.common import ModelResponse, TextBlock, ToolUseBlock
    from agent_app.adapters.anthropic import AnthropicAdapter
    blocks = [TextBlock('hello'), ToolUseBlock('c1', 'read', {'path': 'a'})]
    response = ModelResponse(blocks, 'tool_use')
    captured = {}
    client = NS(messages=NS(create=lambda **kw: captured.update(kw)))
    AnthropicAdapter(client).create(system=None, messages=[{'role': 'assistant', 'content': response.content}], model='m', max_tokens=10, tools=[])
    assert captured['messages'][0]['content'] == [
        {'type': 'text', 'text': 'hello'},
        {'type': 'tool_use', 'id': 'c1', 'name': 'read', 'input': {'path': 'a'}},
    ]


@pytest.mark.parametrize('module,name', [('openai_responses', 'OpenAIResponsesAdapter'), ('openai_chat', 'OpenAIChatAdapter'), ('ollama', 'OllamaAdapter')])
def test_non_off_thinking_rejected_before_client_use(module, name):
    from importlib import import_module
    adapter = getattr(import_module('agent_app.adapters.' + module), name)(object())
    for method in (adapter.create, adapter.create_streaming):
        with pytest.raises(ValueError, match='思考'):
            method(system=None, messages=[], model='m', max_tokens=10, tools=[], thinking={'type': 'enabled', 'budget_tokens': 1024})


@pytest.mark.parametrize('origin,model,keep', [('p@url','m',True), ('other','m',False), ('p@url','other',False)])
def test_anthropic_signature_replay_is_origin_and_model_scoped(origin, model, keep):
    from agent_app.adapters.anthropic import AnthropicAdapter
    first = NS(content=[NS(type='thinking', thinking='private', signature='signed'), NS(type='text', text='hello'), NS(type='tool_use', id='c1', name='read', input={})])
    captured = []
    client = NS(messages=NS(create=lambda **kw: captured.append(kw) or first))
    assert AnthropicAdapter(client, origin='p@url').create(system=None,messages=[],model='m',max_tokens=10,tools=[]) is first
    AnthropicAdapter(client, origin=origin).create(system=None,messages=[{'role':'assistant','content':first.content}],model=model,max_tokens=10,tools=[])
    sent = captured[-1]['messages'][0]['content']
    assert any(b['type'] == 'thinking' for b in sent) is keep
    assert sent[-1] == {'type':'tool_use','id':'c1','name':'read','input':{}}
    assert '_pamu' not in str(sent)


def test_native_replay_survives_copy_and_off_thinking_filter_without_repr_leaks():
    from copy import deepcopy
    from agent_app.thinking import strip_thinking_blocks
    from agent_app.adapters.common import TextBlock, ToolUseBlock, attach_replay, native_replay
    blocks = [TextBlock('hello'),ToolUseBlock('c1','read',{})]
    attach_replay(blocks,'ollama','service','m',{'thinking':'secret'})
    messages = strip_thinking_blocks(deepcopy([{'role':'assistant','content':blocks}]))
    assert native_replay(messages[0]['content'],'ollama','service','m') == {'thinking':'secret'}
    assert 'secret' not in str(messages)


def test_cross_protocol_conversion_omits_native_reasoning_and_metadata():
    from agent_app.adapters.common import ToolUseBlock, attach_replay, chat_history, anthropic_history
    blocks = [ToolUseBlock('c1','read',{})]
    attach_replay(blocks,'openai_responses','service','m',[{'type':'reasoning','encrypted_content':'secret'}])
    messages = [{'role':'assistant','content':blocks},{'role':'user','content':[{'type':'tool_result','tool_use_id':'c1','content':'contents'}]}]
    for wire in (chat_history(None,messages), anthropic_history(messages,origin='service',model='m')):
        assert 'secret' not in str(wire)
        assert 'replay' not in str(wire)
        assert 'c1' in str(wire)


@pytest.mark.parametrize('protocol', ['responses','chat','ollama'])
def test_stream_progress_stops_before_print_and_exits_on_completion(protocol,monkeypatch):
    from tests.test_agent_app_anthropic_adapter import RecordingProgress
    from tests.test_openai_responses_adapter import Client as ResponsesClient, Stream, answer, text
    from tests.test_openai_chat_adapter import Client as ChatClient, delta
    from agent_app.adapters.openai_responses import OpenAIResponsesAdapter
    from agent_app.adapters.openai_chat import OpenAIChatAdapter
    from tests.test_ollama_adapter import make, ndjson, answer as ollama_answer
    events=[]
    if protocol == 'responses':
        adapter = OpenAIResponsesAdapter(ResponsesClient(Stream([{'type':'response.output_text.delta','delta':'visible'}, {'type':'response.completed','response':answer([text('visible')])}])),progress=lambda _:RecordingProgress(events))
    elif protocol == 'chat':
        adapter = OpenAIChatAdapter(ChatClient(Stream([delta('visible'),delta(finish='stop')])),progress=lambda _:RecordingProgress(events))
    else:
        original,_ = make([ndjson(ollama_answer('visible'))])
        adapter = type(original)(original.client,progress=lambda _:RecordingProgress(events))
    monkeypatch.setattr('builtins.print',lambda value='',**kw:events.append('print:'+value))
    adapter.create_streaming(system=None,messages=[],model='m',max_tokens=10,tools=[])
    assert events == ['enter','stop','print:visible','exit','print:']
