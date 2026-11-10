import threading
from types import SimpleNamespace
import pytest
from agent_app.cli import handle_slash_command, main
from agent_app.features.sessions import checkpoint
from agent_app.features.session_store import SessionStore
from tests.test_session_runtime import runtime


def ready(path):
    rt = runtime(path)
    rt.agent_lock = threading.Lock()
    rt.hooks = SimpleNamespace(trigger=lambda *a: None)
    return rt


def test_resume_action_and_help(tmp_path):
    from agent_app.repl_sessions import ResumeAction
    from agent_app.slash_commands import help_text
    assert handle_slash_command(ready(tmp_path), '/resume --last') == ResumeAction(('--last',))
    assert '/resume' in help_text()


def test_repl_switch_isolates_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    b = ready(tmp_path)
    b.session.history.append({'role': 'user', 'content': 'B question'})
    checkpoint(b)
    bid = b.sessions.session_id
    b.sessions.close()
    a = ready(tmp_path)
    a.session.history.append({'role': 'user', 'content': 'A question'})
    checkpoint(a)
    aid = a.sessions.session_id
    target = ready(tmp_path)
    runtimes = iter((a, target))
    inputs = iter(('/resume --last', 'continue B', '/quit'))
    monkeypatch.setattr('builtins.input', lambda *_: next(inputs))
    seen = []
    def turn(rt, query):
        seen.append((rt, list(rt.session.history), query))
    assert main(argv=[], runtime_factory=lambda: next(runtimes), run_turn=turn,
                start_threads=lambda r: [], stop_threads=lambda *a: None,
                credentials_path=tmp_path/'fake.json') in (None, 0)
    assert seen[0][0] is target
    assert seen[0][1] == [{'role': 'user', 'content': 'B question'}]
    assert SessionStore(tmp_path).load(aid)['history'][0]['content'] == 'A question'
    assert target.sessions.session_id == bid


def test_current_session_noop_and_noninteractive_list(tmp_path, monkeypatch, capsys):
    from agent_app.session_cli import select_session
    monkeypatch.chdir(tmp_path)
    rt = ready(tmp_path)
    rt.session.history.append({'role': 'user', 'content': 'A'})
    checkpoint(rt)
    assert select_session([rt.sessions.session_id[:8]], None, exclude_id=rt.sessions.session_id, slash=True) is None
    assert '当前已在此会话中' in capsys.readouterr().out
    assert select_session([], None, exclude_id=rt.sessions.session_id, slash=True) is None
    rt.sessions.close()


def pair(tmp_path, monkeypatch):
    from agent_app.features.scheduler import SchedulerState
    monkeypatch.chdir(tmp_path)
    b = ready(tmp_path)
    b.session.history.append({'role': 'user', 'content': 'B'})
    checkpoint(b)
    bid = b.sessions.session_id
    b.sessions.close()
    a = ready(tmp_path)
    a.session.history.append({'role': 'user', 'content': 'A'})
    checkpoint(a)
    a.scheduler = SchedulerState()
    a.stop_event = threading.Event()
    target = ready(tmp_path)
    target.scheduler = SchedulerState()
    target.stop_event = threading.Event()
    return a, target, bid


@pytest.mark.parametrize('kind', ['background', 'transient', 'queue', 'team', 'inbox', 'busy'])
def test_switch_guards_keep_current(tmp_path, monkeypatch, kind):
    from agent_app.repl_sessions import resume_runtime, ResumeAction
    from agent_app.features.background import BackgroundState
    from agent_app.features.scheduler import CronJob
    from agent_app.features.teams.bus import MessageBus
    a, target, bid = pair(tmp_path, monkeypatch)
    if kind == 'background':
        a.background = BackgroundState(tasks={'bg_1': {'status': 'running'}})
    if kind == 'transient':
        a.scheduler.jobs['cron'] = CronJob('cron', '* * * * *', 'job', True, False)
    if kind == 'queue':
        a.scheduler.queue.append(object())
    if kind == 'team':
        a.team = SimpleNamespace(lock=threading.Lock(), active={'friend': {}})
    if kind == 'inbox':
        a.bus = MessageBus(tmp_path/'inbox')
        a.bus.send('friend', 'lead', 'result')
    if kind == 'busy':
        a.agent_lock.acquire()
    def fail(*a): raise AssertionError('threads must remain untouched')
    result, handles = resume_runtime(a, [], ResumeAction((bid,)), create_runtime=lambda _: target,
                                    start_threads=fail, stop_threads=fail)
    assert result is a
    assert not a.scheduler.paused
    SessionStore(tmp_path).acquire(bid).close()
    if kind == 'busy': a.agent_lock.release()
    a.sessions.close()


def test_start_failure_rolls_back_and_preserves_dedup(tmp_path, monkeypatch):
    from agent_app.repl_sessions import resume_runtime, ResumeAction
    a, target, bid = pair(tmp_path, monkeypatch)
    a.scheduler.last_fired['cron'] = '2026-11-10 10:10'
    starts = []
    def start(rt):
        starts.append(rt)
        if rt is target:
            assert target.scheduler.last_fired == a.scheduler.last_fired
            raise RuntimeError('startup')
        return []
    def stop(rt, handles): rt.stop_event.set()
    result, _ = resume_runtime(a, [], ResumeAction((bid,)), create_runtime=lambda _: target,
                              start_threads=start, stop_threads=stop)
    assert result is a and starts == [target, a]
    assert not a.stop_event.is_set() and not a.scheduler.paused
    SessionStore(tmp_path).acquire(bid).close()
    a.sessions.close()


@pytest.mark.parametrize('kind', ['build', 'occupied', 'corrupt'])
def test_prepare_failure_keeps_old_runtime(tmp_path, monkeypatch, kind):
    from agent_app.repl_sessions import resume_runtime, ResumeAction
    a, target, bid = pair(tmp_path, monkeypatch)
    lease = SessionStore(tmp_path).acquire(bid) if kind == 'occupied' else None
    if kind == 'corrupt':
        (tmp_path/'.pamu'/'sessions'/f'{bid}.json').write_text('{')
    def create(_):
        if kind == 'build': raise RuntimeError('build failed')
        return target
    def fail(*a): raise AssertionError('must not stop current threads')
    result, _ = resume_runtime(a, [], ResumeAction((bid,)), create_runtime=create,
                              start_threads=fail, stop_threads=fail)
    assert result is a and not a.sessions.failed
    if lease: lease.close()
    SessionStore(tmp_path).acquire(bid).close()
    a.sessions.close()


def test_storage_failure_is_fatal_and_releases_target(tmp_path, monkeypatch):
    from agent_app.repl_sessions import resume_runtime, ResumeAction
    from agent_app.features.session_store import SessionError
    a, target, bid = pair(tmp_path, monkeypatch)
    def fail(*a): raise OSError('disk full')
    monkeypatch.setattr(a.sessions.store, 'save', fail)
    a.session.history.append({'role': 'assistant', 'content': 'changed'})
    with pytest.raises(SessionError):
        resume_runtime(a, [], ResumeAction((bid,)), create_runtime=lambda _: target,
                       start_threads=lambda _: [], stop_threads=lambda *a: None)
    SessionStore(tmp_path).acquire(bid).close()
    a.sessions.close()


def test_completed_background_results_saved_before_handoff(tmp_path, monkeypatch):
    from agent_app.repl_sessions import resume_runtime, ResumeAction
    from agent_app.features.background import BackgroundState
    a, target, bid = pair(tmp_path, monkeypatch)
    aid = a.sessions.session_id
    a.background = BackgroundState(tasks={'bg_1': {'status': 'completed', 'command': 'done', 'error': None}}, results={'bg_1': 'finished'})
    result, _ = resume_runtime(a, [], ResumeAction((bid,)), create_runtime=lambda _: target,
                              start_threads=lambda _: [], stop_threads=lambda *a: None)
    assert result is target
    saved = SessionStore(tmp_path).load(aid)
    assert 'task_notification' in str(saved['history']) and 'finished' in str(saved['history'])
    assert 'finished' not in str(target.session.history)
    target.sessions.close()


def test_picker_cancel_and_noninteractive_do_not_consume_input(tmp_path, monkeypatch, capsys):
    from agent_app.session_cli import select_session
    a, target, bid = pair(tmp_path, monkeypatch)
    def fail(*a): raise AssertionError('must not read input')
    monkeypatch.setattr('builtins.input', fail)
    with pytest.raises(Exception, match='/resume --last'):
        select_session([], None, slash=True, exclude_id=a.sessions.session_id)
    assert bid in capsys.readouterr().out
    assert select_session([], SimpleNamespace(choose=lambda *a: None), slash=True,
                          exclude_id=a.sessions.session_id) is None
    a.sessions.close()
    target.sessions.close()


def test_unstopped_old_threads_are_fatal_without_starting_target(tmp_path, monkeypatch):
    from agent_app.repl_sessions import resume_runtime, ResumeAction, HandoffFatalError
    a, target, bid = pair(tmp_path, monkeypatch)
    def fail(*a): raise AssertionError('must not start target')
    with pytest.raises(HandoffFatalError):
        resume_runtime(a, [SimpleNamespace(is_alive=lambda: True)], ResumeAction((bid,)),
                       create_runtime=lambda _: target, start_threads=fail, stop_threads=lambda *a: None)
    SessionStore(tmp_path).acquire(bid).close()
    a.sessions.close()


def test_target_start_interrupt_cleans_partial_and_restarts_old(tmp_path, monkeypatch):
    from agent_app.repl_sessions import resume_runtime, ResumeAction
    a, target, bid = pair(tmp_path, monkeypatch)
    partial = SimpleNamespace(alive=True)
    partial.is_alive = lambda: partial.alive
    starts = []
    def start(rt):
        starts.append(rt)
        if rt is target:
            error = KeyboardInterrupt()
            error.threads = [partial]
            raise error
        return []
    def stop(rt, handles):
        rt.stop_event.set()
        for handle in handles:
            handle.alive = False
    result, _ = resume_runtime(a, [], ResumeAction((bid,)), create_runtime=lambda _: target,
                              start_threads=start, stop_threads=stop)
    assert result is a and starts == [target, a]
    assert not partial.alive and not a.stop_event.is_set() and not a.scheduler.paused
    SessionStore(tmp_path).acquire(bid).close()
    a.sessions.close()


def test_old_stop_interrupt_is_fatal_without_returning_stopped_slot(tmp_path, monkeypatch):
    from agent_app.repl_sessions import resume_runtime, ResumeAction, HandoffFatalError
    a, target, bid = pair(tmp_path, monkeypatch)
    def stop(rt, handles):
        rt.stop_event.set()
        raise KeyboardInterrupt()
    with pytest.raises(HandoffFatalError):
        resume_runtime(a, [], ResumeAction((bid,)), create_runtime=lambda _: target,
                       start_threads=lambda _: [], stop_threads=stop)
    SessionStore(tmp_path).acquire(bid).close()
    a.sessions.close()


def test_postcommit_interrupt_keeps_new_slot(tmp_path, monkeypatch):
    import agent_app.repl_sessions as module
    a, target, bid = pair(tmp_path, monkeypatch)
    def interrupt(*a): raise KeyboardInterrupt()
    monkeypatch.setattr(module, 'show_restored_session', interrupt)
    result, _ = module.resume_runtime(a, [], module.ResumeAction((bid,)), create_runtime=lambda _: target,
                                     start_threads=lambda _: [], stop_threads=lambda *a: None)
    assert result is target
    assert target.sessions.session_id == bid
    target.sessions.close()


def test_unknown_partial_start_interrupt_is_fatal(tmp_path, monkeypatch):
    from agent_app.repl_sessions import resume_runtime, ResumeAction, HandoffFatalError
    a, target, bid = pair(tmp_path, monkeypatch)
    def start(rt): raise KeyboardInterrupt()
    with pytest.raises(HandoffFatalError):
        resume_runtime(a, [], ResumeAction((bid,)), create_runtime=lambda _: target,
                       start_threads=start, stop_threads=lambda *a: None)
    SessionStore(tmp_path).acquire(bid).close()
    a.sessions.close()
