import copy
from types import SimpleNamespace

import pytest

from agent_app.config import AppConfig
from agent_app.runtime import ModelRouter, SessionState
from agent_app.features.sessions import SessionPersistence, checkpoint
from agent_app.features.session_store import SessionError


def runtime(tmp_path):
    return SimpleNamespace(
        session=SessionState(), model=ModelRouter(primary_model='test'),
        config=AppConfig.from_env(tmp_path, environ={'MODEL_ID': 'test'}),
        sessions=SessionPersistence(tmp_path),
    )


def call(tool_id):
    return {'type': 'tool_use', 'id': tool_id, 'name': 'bash', 'input': {}}


def result(tool_id, text):
    return {'type': 'tool_result', 'tool_use_id': tool_id, 'content': text}


def test_lazy_roundtrip_identity_allowlist_and_unchanged_timestamp(tmp_path):
    rt = runtime(tmp_path)
    checkpoint(rt)
    assert rt.sessions.session_id is None
    assert not rt.config.sessions_dir.exists()
    rt.session.history.append({'role': 'user', 'content': 'hello'})
    rt.session.todos.append({'content': 'work', 'status': 'pending'})
    rt.session.rounds_since_todo = 2
    checkpoint(rt, status='idle')
    saved = rt.sessions.store.load(rt.sessions.session_id)
    checkpoint(rt, status='idle')
    assert rt.sessions.store.load(saved['id']) == saved
    assert set(saved['selection']) == {'provider', 'protocol', 'base_url', 'primary_model', 'fallback_model', 'thinking_level'}
    rt.sessions.close()
    restored = runtime(tmp_path)
    original, history, todos = restored.session, restored.session.history, restored.session.todos
    restored.session.context['stale'] = True
    restored.sessions.restore(restored, saved, restored.sessions.store.acquire(saved['id']))
    assert restored.session is original
    assert restored.session.history is history
    assert restored.session.todos is todos
    assert restored.session.history == saved['history']
    assert restored.session.todos == saved['todos']
    assert restored.session.rounds_since_todo == 2
    assert restored.session.context == {}
    restored.sessions.close()


def test_repair_scoped_batches_and_idempotence(tmp_path):
    rt = runtime(tmp_path)
    rt.session.history[:] = [
        {'role': 'user', 'content': 'go'},
        {'role': 'assistant', 'content': [call('reused')]},
        {'role': 'user', 'content': [result('reused', 'completed earlier')]},
        {'role': 'assistant', 'content': [call('reused'), call('pending'), call('done')]},
        {'role': 'user', 'content': [result('done', 'preserved')]},
    ]
    checkpoint(rt, status='running', tool_id='reused', tool_status='running')
    saved = rt.sessions.store.load(rt.sessions.session_id)
    rt.sessions.close()
    restored = runtime(tmp_path)
    restored.sessions.restore(restored, saved, restored.sessions.store.acquire(saved['id']))
    blocks = restored.session.history[-1]['content']
    assert blocks[0] == result('done', 'preserved')
    assert {b['tool_use_id']: b['content'] for b in blocks} == {
        'done': 'preserved', 'pending': '上次会话中断，此工具未执行',
        'reused': '执行结果未知，可能已产生副作用，请先检查实际状态',
    }
    assert all(b['is_error'] for b in blocks[1:])
    checkpoint(restored, status='idle')
    repaired = restored.sessions.store.load(saved['id'])
    restored.sessions.close()
    again = runtime(tmp_path)
    again.sessions.restore(again, repaired, again.sessions.store.acquire(saved['id']))
    assert again.session.history == restored.session.history
    again.sessions.close()


def test_archive_failure_and_closed_writes(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    rt.session.history[:] = [{'role': 'user', 'content': 'original'}]
    checkpoint(rt)
    before = copy.deepcopy(rt.session.history)
    rt.session.history[:] = [{'role': 'user', 'content': 'summary'}]
    rt.sessions.archive_history(before, rt.session.history)
    rt.sessions.archive_history(before, rt.session.history)
    checkpoint(rt)
    saved = rt.sessions.store.load(rt.sessions.session_id)
    assert saved['archive'] == [before]
    def fail(snapshot):
        raise OSError('private detail')
    monkeypatch.setattr(rt.sessions.store, 'save', fail)
    rt.session.history.append({'role': 'assistant', 'content': 'answer'})
    with pytest.raises(SessionError, match='无法保存会话'):
        checkpoint(rt)
    assert rt.sessions.failed
    rt.sessions.close()
    assert rt.sessions.store.load(saved['id']) == saved
    with pytest.raises(SessionError):
        checkpoint(rt)


def test_legacy_fake_checkpoint():
    checkpoint(SimpleNamespace())

from tests.test_agent_app_loop import runtime_factory, text_response, tool_schema
from agent_app.cli import run_agent_turn


def multi_response(*ids):
    return SimpleNamespace(stop_reason='tool_use', content=[SimpleNamespace(**call(i)) for i in ids])


def test_loop_checkpoints_completed_first_tool_and_running_later_tool(runtime_factory):
    rt = runtime_factory([multi_response('a', 'b')])
    rt.sessions = SessionPersistence(rt.config.workdir)
    observed = []
    def handler(**kwargs):
        saved = rt.sessions.store.load(rt.sessions.session_id)
        observed.append(saved)
        if len(observed) == 2:
            raise KeyboardInterrupt()
        return 'first result'
    rt.tools.register(tool_schema('bash'), handler)
    with pytest.raises(KeyboardInterrupt):
        run_agent_turn(rt, 'go')
    saved = rt.sessions.store.load(rt.sessions.session_id)
    assert saved['status'] == 'interrupted'
    assert saved['tool_states'] == {'a': 'completed', 'b': 'running'}
    assert saved['history'][-1]['content'] == [result('a', 'first result')]
    assert observed[0]['tool_states'] == {'a': 'running', 'b': 'pending'}
    assert observed[1]['history'][-1]['content'] == [result('a', 'first result')]
    rt.sessions.close()


def test_failed_checkpoint_prevents_next_tool(runtime_factory, monkeypatch):
    rt = runtime_factory([multi_response('a', 'b')])
    rt.sessions = SessionPersistence(rt.config.workdir)
    executed = []
    def handler(**kwargs):
        executed.append('a')
        def fail(snapshot):
            raise OSError('private')
        monkeypatch.setattr(rt.sessions.store, 'save', fail)
        return 'done'
    rt.tools.register(tool_schema('bash'), handler)
    with pytest.raises(SessionError):
        run_agent_turn(rt, 'go')
    assert executed == ['a']
    assert rt.sessions.failed
    rt.sessions.close()


def test_complete_loop_persists_final_answer(runtime_factory):
    rt = runtime_factory([text_response('final')])
    rt.sessions = SessionPersistence(rt.config.workdir)
    run_agent_turn(rt, 'hello')
    saved = rt.sessions.store.load(rt.sessions.session_id)
    assert saved['status'] == 'idle'
    assert saved['history'][-1] == {'role': 'assistant', 'content': [{'type': 'text', 'text': 'final'}]}
    rt.sessions.close()

from dataclasses import replace
from agent_app.core.recovery import PartialStreamError
from tests.test_agent_app_loop import FakeAPIError
from agent_app.features.todos import register_todo_tools


@pytest.mark.parametrize('mode', ['auto', 'reactive', 'manual'])
def test_compaction_archives_original_and_pairs_manual_calls(runtime_factory, mode):
    responses = [text_response()]
    if mode == 'reactive':
        responses.insert(0, FakeAPIError(400, 'prompt is too long'))
    elif mode == 'manual':
        responses.insert(0, SimpleNamespace(stop_reason='tool_use', content=[
            SimpleNamespace(type='tool_use', id='compact', name='compact', input={}),
            SimpleNamespace(**call('skip')),
        ]))
    rt = runtime_factory(responses)
    if mode == 'auto':
        rt.config = replace(rt.config, context_limit=100)
    rt.sessions = SessionPersistence(rt.config.workdir)
    executed = []
    rt.tools.register(tool_schema('bash'), lambda **kw: executed.append(True))
    run_agent_turn(rt, 'x' * 200)
    saved = rt.sessions.store.load(rt.sessions.session_id)
    assert saved['archive'][0][0] == {'role': 'user', 'content': 'x' * 200}
    if mode == 'manual':
        assert not executed
        archived = saved['archive'][0]
        assert {b['tool_use_id'] for b in archived[-1]['content']} == {'compact', 'skip'}
    rt.sessions.close()


@pytest.mark.parametrize('mode', ['partial', 'max_tokens'])
def test_interrupted_response_checkpoint_before_continuation(runtime_factory, mode):
    if mode == 'partial':
        initial = PartialStreamError('partial', RuntimeError('network'))
    else:
        initial = multi_response('truncated')
        initial.stop_reason = 'max_tokens'
    rt = runtime_factory([initial, KeyboardInterrupt()])
    rt.sessions = SessionPersistence(rt.config.workdir)
    with pytest.raises(KeyboardInterrupt):
        run_agent_turn(rt, 'go')
    saved = rt.sessions.store.load(rt.sessions.session_id)
    if mode == 'partial':
        assert saved['history'][1]['content'][0]['text'] == 'partial'
    else:
        assert saved['history'][2]['content'][0]['tool_use_id'] == 'truncated'
        assert saved['history'][2]['content'][0]['is_error']
    assert saved['status'] == 'interrupted'
    rt.sessions.close()


def test_restore_keeps_todo_handler_bound_and_sanitizes_extra_fields(tmp_path):
    rt = runtime(tmp_path)
    rt.session.history.append({'role': 'user', 'content': 'todos'})
    rt.session.todos = [{'content': 'old', 'status': 'pending', 'extra': 'ignored'}]
    checkpoint(rt)
    saved = rt.sessions.store.load(rt.sessions.session_id)
    assert saved['todos'] == [{'content': 'old', 'status': 'pending'}]
    rt.sessions.close()
    restored = runtime(tmp_path)
    from agent_app.tools.registry import ToolRegistry
    restored.tools = ToolRegistry()
    register_todo_tools(restored.tools, restored.session)
    original = restored.session
    restored.sessions.restore(restored, saved, restored.sessions.store.acquire(saved['id']))
    _, handlers = restored.tools.snapshot()
    handlers['todo_write']([{'content': 'continue', 'status': 'in_progress'}])
    assert restored.session is original
    assert restored.session.todos == [{'content': 'continue', 'status': 'in_progress'}]
    restored.sessions.close()


def test_closed_during_permission_hook_prevents_tool(runtime_factory):
    rt = runtime_factory([multi_response('a')])
    rt.sessions = SessionPersistence(rt.config.workdir)
    executed = []
    rt.tools.register(tool_schema('bash'), lambda **kw: executed.append(True))
    rt.hooks.register('PreToolUse', lambda *args: rt.sessions.close())
    with pytest.raises(SessionError):
        run_agent_turn(rt, 'go')
    assert executed == []


def test_original_survives_interruption_during_auto_summary(runtime_factory):
    rt = runtime_factory([])
    rt.config = replace(rt.config, context_limit=100, keep_recent=1)
    rt.sessions = SessionPersistence(rt.config.workdir)
    rt.session.history[:] = [
        {'role': 'user', 'content': 'go'},
        {'role': 'assistant', 'content': [call('old')]},
        {'role': 'user', 'content': [result('old', 'x' * 200)]},
        {'role': 'assistant', 'content': [call('new')]},
        {'role': 'user', 'content': [result('new', 'y' * 200)]},
    ]
    original = copy.deepcopy(rt.session.history)
    def interrupted(**kwargs):
        raise KeyboardInterrupt()
    rt.llm.create = interrupted
    with pytest.raises(KeyboardInterrupt):
        run_agent_turn(rt)
    saved = rt.sessions.store.load(rt.sessions.session_id)
    assert saved['archive'][0] == original
    rt.sessions.close()


def test_worker_stops_on_persistence_failure(runtime_factory, monkeypatch, capsys):
    import agent_app.cli as cli
    rt = runtime_factory([])
    monkeypatch.setattr(cli, 'has_cron_queue', lambda scheduler: True)
    calls = []
    def failed(runtime):
        calls.append(True)
        raise SessionError('无法保存会话')
    monkeypatch.setattr(cli, 'run_agent_turn', failed)
    cli._queue_processor_loop(rt)
    assert calls == [True]
    assert rt.stop_event.is_set()
    assert '无法保存会话' in capsys.readouterr().out


def test_swallowed_summary_persistence_error_still_prevents_model_request(runtime_factory, monkeypatch):
    import agent_app.core.loop as loop
    rt = runtime_factory([text_response()])
    rt.sessions = SessionPersistence(rt.config.workdir)
    def swallowed(memory, messages, summarize):
        rt.sessions.close()
        try:
            summarize('memory')
        except Exception:
            pass
        return messages
    monkeypatch.setattr(loop, 'build_request_messages_with_memories', swallowed)
    with pytest.raises(SessionError):
        run_agent_turn(rt, 'go')
    assert rt.llm.requests == []


def test_completed_result_is_saved_before_post_tool_hook_interrupts(runtime_factory):
    rt = runtime_factory([multi_response('a', 'b')])
    rt.sessions = SessionPersistence(rt.config.workdir)
    rt.tools.register(tool_schema('bash'), lambda **kw: 'completed output')
    def interrupt(*args):
        raise KeyboardInterrupt()
    rt.hooks.register('PostToolUse', interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_agent_turn(rt, 'go')
    saved = rt.sessions.store.load(rt.sessions.session_id)
    assert saved['history'][-1]['content'] == [result('a', 'completed output')]
    assert saved['tool_states'] == {'a': 'completed', 'b': 'pending'}
    rt.sessions.close()


def test_restore_completed_subagent_and_background_do_not_warn(tmp_path):
    rt = runtime(tmp_path)
    task = dict(call('sub'), name='task')
    rt.session.history[:] = [
        {'role': 'user', 'content': 'go'},
        {'role': 'assistant', 'content': [task, call('bg')]},
        {'role': 'user', 'content': [result('sub', 'finished'), result('bg', '[Background task bg_0001 started] command')]},
        {'role': 'user', 'content': '<task_notification>\n  <task_id>bg_0001</task_id>\n  <status>completed</status>\n</task_notification>'},
    ]
    checkpoint(rt)
    saved = rt.sessions.store.load(rt.sessions.session_id)
    rt.sessions.close()
    restored = runtime(tmp_path)
    restored.sessions.restore(restored, saved, restored.sessions.store.acquire(saved['id']))
    assert restored.session.history == saved['history']
    restored.sessions.close()


def test_restore_places_missing_results_before_existing_text(tmp_path):
    rt = runtime(tmp_path)
    rt.session.history[:] = [
        {'role': 'user', 'content': 'go'},
        {'role': 'assistant', 'content': [call('a'), call('b')]},
        {'role': 'user', 'content': [result('a', 'done'), {'type': 'text', 'text': 'additional context'}]},
    ]
    checkpoint(rt)
    saved = rt.sessions.store.load(rt.sessions.session_id)
    rt.sessions.close()
    restored = runtime(tmp_path)
    restored.sessions.restore(restored, saved, restored.sessions.store.acquire(saved['id']))
    blocks = restored.session.history[-1]['content']
    assert [block['type'] for block in blocks] == ['tool_result', 'tool_result', 'text']
    assert blocks[0] == result('a', 'done')
    assert blocks[-1]['text'] == 'additional context'
    restored.sessions.close()


@pytest.mark.parametrize('kind', ['background', 'running_subagent'])
def test_restore_warns_for_unfinished_background_or_subagent(tmp_path, kind):
    rt = runtime(tmp_path)
    block = dict(call('work'), name='task' if kind == 'running_subagent' else 'bash')
    rt.session.history[:] = [
        {'role': 'user', 'content': 'go'},
        {'role': 'assistant', 'content': [block]},
    ]
    if kind == 'background':
        rt.session.history.append({'role': 'user', 'content': [result('work', '[Background task bg_0001 started] command')]})
    checkpoint(rt, status='running', tool_id='work', tool_status='running')
    saved = rt.sessions.store.load(rt.sessions.session_id)
    rt.sessions.close()
    restored = runtime(tmp_path)
    restored.sessions.restore(restored, saved, restored.sessions.store.acquire(saved['id']))
    assert '后台任务或子代理无法自动恢复' in restored.session.history[-1]['content']
    restored.sessions.close()


@pytest.mark.parametrize('prior_notice', [False, True])
def test_restore_warns_for_later_background_after_completion_or_notice(tmp_path, prior_notice):
    notice = '[会话恢复] 之前的后台任务或子代理无法自动恢复，请检查实际状态。'
    rt = runtime(tmp_path)
    rt.session.history[:] = [
        {'role': 'user', 'content': 'go'},
        {'role': 'assistant', 'content': [call('first')]},
        {'role': 'user', 'content': [result('first', '[Background task bg_0001 started] command')]},
        {'role': 'user', 'content': notice if prior_notice else '<task_notification><task_id>bg_0001</task_id><status>completed</status></task_notification>'},
        {'role': 'assistant', 'content': [call('second')]},
        {'role': 'user', 'content': [result('second', '[Background task bg_0001 started] another command')]},
    ]
    checkpoint(rt)
    saved = rt.sessions.store.load(rt.sessions.session_id)
    rt.sessions.close()
    restored = runtime(tmp_path)
    restored.sessions.restore(restored, saved, restored.sessions.store.acquire(saved['id']))
    assert restored.session.history[-1]['content'] == notice
    assert sum(m['content'] == notice for m in restored.session.history) == (2 if prior_notice else 1)
    checkpoint(restored)
    saved = restored.sessions.store.load(saved['id'])
    restored.sessions.close()
    again = runtime(tmp_path)
    again.sessions.restore(again, saved, again.sessions.store.acquire(saved['id']))
    assert again.session.history == restored.session.history
    again.sessions.close()
