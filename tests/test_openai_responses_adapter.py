from types import SimpleNamespace as NS
import pytest

from agent_app.core.recovery import PartialStreamError

TOOL = {'name': 'read', 'description': 'Read file', 'input_schema': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}
REASON = {'type': 'reasoning', 'id': 'rs1', 'summary': [], 'encrypted_content': 'opaque'}
CALL = {'type': 'function_call', 'id': 'fc1', 'call_id': 'c1', 'name': 'read', 'arguments': '{"path":"a"}', 'status': 'completed'}

def answer(output, status='completed'):
    return {'status': status, 'output': output}

def text(value):
    return {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': value}]}

class Client:
    def __init__(self, *responses):
        self.responses = NS(create=self.create)
        self.queue = iter(responses)
        self.requests = []
    def create(self, **kw):
        self.requests.append(kw)
        return next(self.queue)

class Stream:
    def __init__(self, events): self.events, self.closed = events, False
    def __iter__(self):
        for event in self.events:
            if isinstance(event, Exception): raise event
            yield event
    def close(self): self.closed = True

def make(client, origin='p@url'):
    from agent_app.adapters.openai_responses import OpenAIResponsesAdapter
    return OpenAIResponsesAdapter(client, origin=origin)

def request(adapter, messages=None, streaming=False, model='m'):
    return (adapter.create_streaming if streaming else adapter.create)(system='rules', messages=messages or [{'role': 'user', 'content': 'read file'}], model=model, max_tokens=100, tools=[TOOL])

def history(first):
    return [{'role':'user','content':'read file'}, {'role':'assistant','content':first.content}, {'role':'user','content':[{'type':'tool_result','tool_use_id':b.id,'content':'contents'} for b in first.content if b.type == 'tool_use']}]


@pytest.mark.parametrize('code,check', [
    ('rate_limit_exceeded', 'is_rate_limit_error'),
    ('context_length_exceeded', 'is_prompt_too_long_error'),
])
@pytest.mark.parametrize('kind', ['error', 'response.failed', 'nonstream'])
def test_protocol_error_category_survives_sse_and_failed_responses(code, check, kind):
    from agent_app.core import recovery
    error = {'code': code, 'message': 'server details'}
    if kind == 'nonstream':
        response = {'status': 'failed', 'error': error, 'output': []}
    elif kind == 'error':
        from openai.types.responses import ResponseErrorEvent
        response = Stream([ResponseErrorEvent(type='error', sequence_number=1, **error)])
    else:
        response = Stream([{'type': 'response.failed', 'response': {'status': 'failed', 'error': error, 'output': []}}])
    with pytest.raises(ValueError) as caught:
        request(make(Client(response)), streaming=kind != 'nonstream')
    assert getattr(recovery, check)(caught.value)
    assert 'server details' not in str(caught.value)

def test_roundtrip_multiple_calls_replays_reasoning_without_adapter_cache():
    c2 = dict(CALL, id='fc2', call_id='c2', arguments='{}')
    client = Client(answer([REASON, CALL, c2]), answer([text('unrelated')]), answer([text('finished')]))
    adapter = make(client)
    first = request(adapter)
    assert [b.id for b in first.content] == ['c1', 'c2']
    request(adapter)
    assert request(adapter, history(first)).content[0].text == 'finished'
    assert client.requests[0]['tools'] == [{'type':'function', 'name':'read', 'description':'Read file', 'parameters':TOOL['input_schema'], 'strict':False}]
    payload = client.requests[2]['input']
    assert payload[1:4] == [REASON, CALL, c2]
    assert payload[4:] == [{'type':'function_call_output','call_id':'c1','output':'contents'}, {'type':'function_call_output','call_id':'c2','output':'contents'}]
    assert client.requests[0]['instructions'] == 'rules'
    assert client.requests[0]['max_output_tokens'] == 100

@pytest.mark.parametrize('origin,model', [('other','m'), ('p@url','other')])
def test_foreign_native_reasoning_is_not_replayed(origin, model):
    first = request(make(Client(answer([REASON, CALL]))))
    client = Client(answer([text('finished')]))
    request(make(client, origin), history(first), model=model)
    assert all(i.get('type') != 'reasoning' for i in client.requests[0]['input'])
    assert any(i.get('call_id') == 'c1' for i in client.requests[0]['input'])

@pytest.mark.parametrize('args', [None, '', '{', '[]', 'null'])
def test_malformed_arguments_never_become_executable(args):
    with pytest.raises(ValueError): request(make(Client(answer([dict(CALL, arguments=args)]))))

@pytest.mark.parametrize('response', [answer([]), answer([text('')]), answer([CALL], 'failed'), answer([{'type':'message','content':[{'type':'refusal','refusal':'no'}]}])])
def test_empty_refused_failed_are_errors(response):
    with pytest.raises(ValueError): request(make(Client(response)))

def test_responses_typed_stream_prints_and_uses_validated_terminal_response(capsys):
    stream = Stream([{'type':'response.output_text.delta','delta':'hello'}, {'type':'response.output_text.delta','delta':' world'}, {'type':'response.function_call_arguments.delta','output_index':1,'item_id':'fc1','delta':'{"path":"a"}'}, {'type':'response.completed','response':answer([text('hello world'), CALL])}])
    result = request(make(Client(stream)), streaming=True)
    assert result.content[-1].input == {'path':'a'}
    assert result.stop_reason == 'tool_use'
    assert stream.closed
    assert capsys.readouterr().out == 'hello world\n'

@pytest.mark.parametrize('tail', [[], [RuntimeError('lost')], [{'type':'response.failed'}], [{'type':'error','message':'bad'}], [{'type':'response.completed','response':answer([dict(CALL, arguments='{')])}]])
def test_incomplete_or_error_stream_after_text_is_partial(tail):
    stream = Stream([{'type':'response.output_text.delta','delta':'visible'}, *tail])
    with pytest.raises(PartialStreamError) as caught: request(make(Client(stream)), streaming=True)
    assert caught.value.partial_text == 'visible'
    assert stream.closed


def test_sdk_response_and_history_blocks_preserve_native_encrypted_replay():
    from openai.types.responses import Response
    from agent_app.adapters.common import TextBlock, ToolUseBlock
    sdk = Response.model_validate({
        'id': 'resp1', 'object': 'response', 'created_at': 1, 'model': 'm',
        'status': 'completed', 'parallel_tool_calls': True,
        'tool_choice': 'auto', 'tools': [], 'output': [REASON, CALL],
    })
    client = Client(sdk, answer([text('finished')]))
    adapter = make(client)
    first = request(adapter)
    assert 'opaque' not in str(first.content)
    assert request(adapter, history(first)).content[0].text == 'finished'
    assert client.requests[-1]['input'][1]['encrypted_content'] == 'opaque'
    assert client.requests[0]['store'] is False
    assert client.requests[0]['include'] == ['reasoning.encrypted_content']


def test_modified_history_invalidates_native_replay_instead_of_resurrecting_call():
    first = request(make(Client(answer([REASON, CALL, dict(CALL, id='fc2', call_id='c2')]))))
    first.content.pop()
    client = Client(answer([text('finished')]))
    request(make(client), history(first))
    payload = client.requests[0]['input']
    assert not any(item.get('type') == 'reasoning' for item in payload)
    assert not any(item.get('call_id') == 'c2' for item in payload)


@pytest.mark.parametrize('streaming', [False, True])
def test_incomplete_output_budget_maps_to_max_tokens_including_empty(streaming):
    response = dict(answer([], 'incomplete'), incomplete_details={'reason': 'max_output_tokens'})
    native = Stream([{'type':'response.incomplete','response':response}]) if streaming else response
    result = request(make(Client(native)), streaming=streaming)
    assert result.content == []
    assert result.stop_reason == 'max_tokens'


def test_tool_only_stream_without_terminal_does_not_return_executable_call():
    stream = Stream([{'type':'response.output_item.done','output_index':0,'item':CALL}])
    with pytest.raises(ValueError, match='结束'):
        request(make(Client(stream)), streaming=True)
    assert stream.closed


def test_responses_rejects_non_string_text():
    with pytest.raises(ValueError):
        request(make(Client(answer([text({'bad':'shape'})]))))


def test_responses_stream_assembles_argument_fragments_and_rejects_inconsistent_terminal():
    events = [
        {'type':'response.output_item.added','output_index':0,'item':dict(CALL,arguments='',status='in_progress')},
        {'type':'response.function_call_arguments.delta','output_index':0,'item_id':'fc1','delta':'{"path":'},
        {'type':'response.function_call_arguments.delta','output_index':0,'item_id':'fc1','delta':'"a"}'},
        {'type':'response.function_call_arguments.done','output_index':0,'item_id':'fc1','arguments':'{"path":"a"}'},
        {'type':'response.output_item.done','output_index':0,'item':CALL},
    ]
    result = request(make(Client(Stream(events+[{'type':'response.completed','response':answer([CALL])}]))),streaming=True)
    assert result.content[0].input == {'path':'a'}
    with pytest.raises(ValueError):
        request(make(Client(Stream(events+[{'type':'response.completed','response':answer([dict(CALL,arguments='{}')])}]))),streaming=True)


def test_responses_stream_rejects_incomplete_json_even_with_valid_final_payload():
    stream=Stream([
        {'type':'response.output_item.added','output_index':0,'item':dict(CALL,arguments='',status='in_progress')},
        {'type':'response.function_call_arguments.delta','output_index':0,'item_id':'fc1','delta':'{'},
        {'type':'response.completed','response':answer([CALL])},
    ])
    with pytest.raises(ValueError):request(make(Client(stream)),streaming=True)
    assert stream.closed


@pytest.mark.parametrize('streaming', [False, True])
def test_truncated_tool_arguments_allow_budget_recovery_without_execution(streaming):
    incomplete = dict(CALL, arguments='{"path":', status='incomplete')
    response = dict(answer([incomplete], 'incomplete'), incomplete_details={'reason': 'max_output_tokens'})
    events = [
        {'type': 'response.output_item.added', 'output_index': 0, 'item': dict(CALL, arguments='', status='in_progress')},
        {'type': 'response.function_call_arguments.delta', 'output_index': 0, 'item_id': 'fc1', 'delta': '{"path":'},
        {'type': 'response.output_item.done', 'output_index': 0, 'item': incomplete},
        {'type': 'response.incomplete', 'response': response},
    ]
    result = request(make(Client(Stream(events) if streaming else response)), streaming=streaming)
    assert result.stop_reason == 'max_tokens'
    assert result.content == []
