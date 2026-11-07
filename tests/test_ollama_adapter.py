import json
import pytest
import httpx
from agent_app.core.recovery import PartialStreamError
from tests.test_openai_responses_adapter import TOOL


def answer(content='',calls=None,thinking='',done=True,reason='stop'):
    return {'message':{'role':'assistant','content':content,'thinking':thinking,'tool_calls':calls or []},'done':done,'done_reason':reason}

def call(args=None): return {'function':{'name':'read','arguments': {'path':'a'} if args is None else args}}

def make(responses, origin='ollama@url'):
    from agent_app.adapters.ollama import OllamaAdapter
    requests=[]
    def handler(req):
        requests.append(json.loads(req.content))
        assert req.url.path == '/api/chat'
        value=responses.pop(0)
        return httpx.Response(200,json=value) if isinstance(value,dict) else httpx.Response(200,content=value)
    client=httpx.Client(base_url='http://localhost:11434', transport=httpx.MockTransport(handler))
    return OllamaAdapter(client,origin=origin),requests

def request(adapter,messages=None,streaming=False,model='m'):
    return (adapter.create_streaming if streaming else adapter.create)(system='rules',messages=messages or [{'role':'user','content':'read'}],model=model,max_tokens=100,tools=[TOOL])

def test_native_roundtrip_keeps_thinking_and_tool_names():
    adapter, sent = make([answer(calls=[call(),call({})],thinking='private'),answer('finished')])
    first=request(adapter)
    assert len({b.id for b in first.content}) == 2
    messages=[{'role':'assistant','content':first.content},{'role':'user','content':[{'type':'tool_result','tool_use_id':b.id,'content':'contents'} for b in first.content]}]
    assert request(adapter,messages).content[0].text == 'finished'
    assert sent[1]['messages'][1]['thinking'] == 'private'
    assert sent[1]['messages'][2:] == [{'role':'tool','tool_name':'read','content':'contents'}]*2
    assert sent[0]['options']['num_predict'] == 100
    assert sent[0]['stream'] is False

@pytest.mark.parametrize('origin,model', [('other','m'),('ollama@url','other')])
def test_foreign_thinking_is_removed(origin,model):
    a,_=make([answer(calls=[call()],thinking='private')])
    first=request(a)
    b,sent=make([answer('finished')],origin)
    request(b,[{'role':'assistant','content':first.content}],model=model)
    assert 'thinking' not in sent[0]['messages'][1]

@pytest.mark.parametrize('value', [answer(), answer(calls=[call('{')]), answer(calls=[{'function':{'name':'read'}}]), {'error':'model missing'}, answer('x',done=False)])
def test_invalid_response_rejected(value):
    a,_=make([value])
    with pytest.raises(ValueError):request(a)

def ndjson(*values): return '\n'.join(json.dumps(v) if not isinstance(v,str) else v for v in values)

def test_ndjson_stream_accumulates_text_thinking_and_calls(capsys):
    a,sent=make([ndjson(answer('hi',thinking='private',done=False), answer(calls=[call()],done=False), answer(' there'))])
    result=request(a,streaming=True)
    assert result.content[0].text == 'hi there'
    assert result.content[1].input == {'path':'a'}
    assert result.stop_reason == 'tool_use'
    assert capsys.readouterr().out == 'hi there\n'
    assert sent[0]['stream'] is True

@pytest.mark.parametrize('tail', ['', '\n{', '\n{"error":"bad"}', '\n'+ndjson(answer(calls=[call('{')]))])
def test_broken_ndjson_stream_reports_partial(tail):
    a,_=make([ndjson(answer('visible',done=False))+tail])
    with pytest.raises(PartialStreamError) as caught:request(a,streaming=True)
    assert caught.value.partial_text == 'visible'


def test_stream_disconnect_closes_connection_and_keeps_visible_partial():
    from agent_app.adapters.ollama import OllamaAdapter
    class BrokenBody(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            yield (ndjson(answer('visible',done=False))+'\n').encode()
            raise httpx.ReadError('lost')
        def close(self):
            self.closed = True
    body = BrokenBody()
    client = httpx.Client(base_url='http://localhost:11434', transport=httpx.MockTransport(lambda req: httpx.Response(200,stream=body)))
    with pytest.raises(PartialStreamError) as caught:
        request(OllamaAdapter(client),streaming=True)
    assert caught.value.partial_text == 'visible'
    assert isinstance(caught.value.cause,httpx.ReadError)
    assert body.closed


@pytest.mark.parametrize('streaming', [False,True])
def test_http_errors_preserve_status_and_retry_headers(streaming):
    from agent_app.adapters.ollama import OllamaAdapter
    from agent_app.core.recovery import get_status_code, extract_retry_after
    client = httpx.Client(base_url='http://localhost:11434',transport=httpx.MockTransport(lambda req: httpx.Response(429,headers={'retry-after':'2'},json={'error':'busy'})))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        request(OllamaAdapter(client),streaming=streaming)
    assert get_status_code(caught.value) == 429
    assert extract_retry_after(caught.value) == 2
    assert caught.value.response.is_closed


@pytest.mark.parametrize('streaming', [False,True])
def test_length_is_mapped_to_max_tokens(streaming):
    value = answer(reason='length')
    adapter, _ = make([ndjson(value) if streaming else value])
    assert request(adapter,streaming=streaming).stop_reason == 'max_tokens'
