from types import SimpleNamespace as NS
import pytest
from agent_app.core.recovery import PartialStreamError
from tests.test_openai_responses_adapter import TOOL, Stream


def completion(content='finished', calls=None, reason='stop'):
    return {'choices':[{'message':{'role':'assistant','content':content,'tool_calls':calls or []},'finish_reason':reason}]}

def call(id='c1', args='{"path":"a"}'):
    return {'id':id, 'type':'function', 'function':{'name':'read','arguments':args}}

class Client:
    def __init__(self, *responses):
        self.chat = NS(completions=NS(create=self.create))
        self.queue, self.requests = iter(responses), []
    def create(self, **kw):
        self.requests.append(kw)
        return next(self.queue)

def make(client):
    from agent_app.adapters.openai_chat import OpenAIChatAdapter
    return OpenAIChatAdapter(client, origin='chat@url')

def request(adapter, messages=None, streaming=False):
    return (adapter.create_streaming if streaming else adapter.create)(system='rules', messages=messages or [{'role':'user','content':'read'}],model='m', max_tokens=100, tools=[TOOL])

def delta(content=None, calls=None, finish=None):
    return {'choices':[{'index':0,'delta':{'content':content, 'tool_calls':calls or []},'finish_reason':finish}]}

def test_multiple_call_roundtrip_sdk_and_dict_history():
    client = Client(completion(None,[call(),call('c2','{}')], 'tool_calls'), completion())
    adapter = make(client)
    first = request(adapter)
    messages = [{'role':'assistant','content':first.content}, {'role':'user','content':[{'type':'tool_result','tool_use_id':b.id,'content':'contents'} for b in first.content]}]
    assert request(adapter,messages).content[0].text == 'finished'
    sent = client.requests[1]['messages']
    assert sent[1]['tool_calls'] == [call(),call('c2','{}')]
    assert sent[2:] == [{'role':'tool','tool_call_id':'c1','content':'contents'}, {'role':'tool','tool_call_id':'c2','content':'contents'}]
    assert client.requests[0]['tools'][0]['function']['parameters'] == TOOL['input_schema']
    assert client.requests[0]['max_completion_tokens'] == 100

@pytest.mark.parametrize('args', [None,'','{','[]'])
def test_invalid_arguments_rejected(args):
    with pytest.raises(ValueError): request(make(Client(completion(None,[call(args=args)],'tool_calls'))))

@pytest.mark.parametrize('response', [completion(''), completion('no', reason='content_filter'), {'choices':[]}, {'choices':[{'message':{'refusal':'no'},'finish_reason':'stop'}]}])
def test_empty_refusal_error_is_not_success(response):
    with pytest.raises(ValueError): request(make(Client(response)))

def test_indexed_stream_accumulates_multiple_tool_calls(capsys):
    stream = Stream([delta('hi', [{'index':0,'id':'c1','function':{'name':'read','arguments':'{"pa'}}, {'index':1,'id':'c2','function':{'name':'read','arguments':'{'}}]), delta(calls=[{'index':1,'function':{'arguments':'}'}},{'index':0,'function':{'arguments':'th":"a"}'}}]), delta(finish='tool_calls')])
    response = request(make(Client(stream)), streaming=True)
    assert [(b.id,b.input) for b in response.content if b.type == 'tool_use'] == [('c1',{'path':'a'}),('c2',{})]
    assert capsys.readouterr().out == 'hi\n'
    assert stream.closed

@pytest.mark.parametrize('tail', [[], [RuntimeError('lost')], [delta(finish='content_filter')], [delta(calls=[{'index':0,'id':'c1','function':{'name':'read','arguments':'{'}}]),delta(finish='tool_calls')]])
def test_invalid_stream_is_partial_and_closed(tail):
    stream = Stream([delta('visible'),*tail])
    with pytest.raises(PartialStreamError): request(make(Client(stream)), streaming=True)
    assert stream.closed


def test_real_sdk_completion_and_sdk_like_history_are_supported():
    from openai.types.chat import ChatCompletion
    sdk = ChatCompletion.model_validate({'id':'chat1','object':'chat.completion','created':1,'model':'m','choices':[{'index':0,'message':{'role':'assistant','content':None,'tool_calls':[call()]},'finish_reason':'tool_calls'}]})
    client = Client(sdk, completion())
    adapter = make(client)
    first = request(adapter)
    messages = [NS(role='assistant',content=[NS(type='tool_use',id=first.content[0].id,name='read',input={'path':'a'})]), NS(role='user',content=[NS(type='tool_result',tool_use_id='c1',content=[NS(type='text',text='contents')])])]
    assert request(adapter,messages).content[0].text == 'finished'
    assert client.requests[1]['messages'][-1] == {'role':'tool','tool_call_id':'c1','content':'contents'}


@pytest.mark.parametrize('streaming', [False,True])
def test_length_is_mapped_to_max_tokens(streaming):
    native = Stream([delta(finish='length')]) if streaming else completion('',reason='length')
    assert request(make(Client(native)),streaming=streaming).stop_reason == 'max_tokens'


def test_chat_tool_only_stream_without_finish_never_returns_call():
    stream = Stream([delta(calls=[dict(call(),index=0)])])
    with pytest.raises(ValueError, match='结束'):
        request(make(Client(stream)),streaming=True)
    assert stream.closed


@pytest.mark.parametrize('streaming', [False, True])
def test_truncated_tool_arguments_allow_budget_recovery_without_execution(streaming):
    partial = call(args='{"path":')
    native = Stream([delta(calls=[dict(partial, index=0)]), delta(finish='length')]) if streaming else completion(None, [partial], 'length')
    result = request(make(Client(native)), streaming=streaming)
    assert result.stop_reason == 'max_tokens'
    assert result.content == []
