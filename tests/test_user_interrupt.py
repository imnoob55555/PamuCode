"""User cancellation crosses the model, tool, and persisted-session boundaries."""
import json
from types import SimpleNamespace as NS

import httpx
import pytest

from agent_app.cli import run_agent_turn, handle_slash_command
from agent_app.features.sessions import SessionPersistence
from tests.test_agent_app_loop import runtime_factory


@pytest.mark.parametrize('protocol', ['anthropic_messages', 'openai_chat', 'openai_responses', 'ollama'])
def test_interrupted_stream_is_closed_and_partial_text_saved_without_retry(runtime_factory, protocol):
    class InterruptedStream(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            if protocol == 'anthropic_messages':
                yield 'visible-partial'
            elif protocol == 'openai_chat':
                yield {'choices': [{'index': 0, 'delta': {'content': 'visible-partial'}, 'finish_reason': None}]}
            elif protocol == 'openai_responses':
                yield {'type': 'response.output_text.delta', 'delta': 'visible-partial'}
            else:
                yield (json.dumps({'message': {'role': 'assistant', 'content': 'visible-partial'}, 'done': False}) + '\n').encode()
            raise KeyboardInterrupt()
        @property
        def text_stream(self):
            return iter(self)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
        def close(self):
            self.closed = True

    stream = InterruptedStream()
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        return stream
    if protocol == 'anthropic_messages':
        from agent_app.adapters.anthropic import AnthropicAdapter
        adapter = AnthropicAdapter(NS(messages=NS(stream=create)))
    elif protocol == 'openai_chat':
        from agent_app.adapters.openai_chat import OpenAIChatAdapter
        adapter = OpenAIChatAdapter(NS(chat=NS(completions=NS(create=create))))
    elif protocol == 'openai_responses':
        from agent_app.adapters.openai_responses import OpenAIResponsesAdapter
        adapter = OpenAIResponsesAdapter(NS(responses=NS(create=create)))
    else:
        from agent_app.adapters.ollama import OllamaAdapter
        def transport(request):
            requests.append(request)
            return httpx.Response(200, stream=stream)
        adapter = OllamaAdapter(httpx.Client(base_url='http://localhost:11434', transport=httpx.MockTransport(transport)))

    rt = runtime_factory([])
    rt.llm = adapter
    rt.model.llm = adapter
    rt.model.protocol = protocol
    rt.sessions = SessionPersistence(rt.config.workdir)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_agent_turn(rt, 'first')
        assert stream.closed
        assert len(requests) == 1
        saved = rt.sessions.store.load(rt.sessions.session_id)
        assert saved['status'] == 'interrupted'
        assert saved['history'][1]['content'][0]['text'] == 'visible-partial\n[用户中断，回复未完成]'
        assert '[用户中断]' in saved['history'][-1]['content']
        records = saved['context_state']['_message_records']
        assert records[-1]['source'] == 'system'
        assert records[-1]['turn_id'] == records[0]['id']
    finally:
        rt.sessions.close()
        if protocol == 'ollama':
            adapter.client.close()


@pytest.mark.parametrize('busy', [False, True])
def test_stop_command_works_without_starting_or_overwriting_a_turn(runtime_factory, busy):
    from agent_app.features.background import start_background_task
    from agent_app.core.interruption import check_command_cancelled
    import threading
    rt = runtime_factory([])
    rt.sessions = SessionPersistence(rt.config.workdir)
    rt.session.history.append({'role': 'user', 'content': 'existing task'})
    entered = threading.Event()
    def handler(**kwargs):
        entered.set()
        while True:
            check_command_cancelled(started=True)
            release.wait(.005)
    release = threading.Event()
    task = start_background_task(rt.background, NS(id='call', name='bash', input={'command': 'long command'}),
        {'bash': handler}, post_tool=lambda *args: None, persist_output=lambda _id, output: output)
    assert entered.wait(1)
    output = []
    try:
        handle_slash_command(rt, '/background', print_fn=output.append)
        assert task in output[-1] and '运行中' in output[-1]
        if busy:
            with rt.agent_lock:
                handle_slash_command(rt, '/stop ' + task, print_fn=output.append)
        else:
            handle_slash_command(rt, '/stop ' + task, print_fn=output.append)
        assert '已中断' in output[-1]
        assert rt.llm.requests == []
        if busy:
            assert len(rt.session.history) == 1
            assert rt.background.tasks[task]['status'] == 'cancelled'
        else:
            saved = rt.sessions.store.load(rt.sessions.session_id)
            assert '<status>cancelled</status>' in saved['history'][-1]['content']
            assert not rt.background.tasks
    finally:
        rt.sessions.close()
