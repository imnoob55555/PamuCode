from copy import deepcopy

from agent_app.features.teams.approval import ApprovalAuthority


def payload():
    return {'tool_use_id': 'call', 'tool_name': 'bash',
            'tool_input': {'command': 'echo safe'}, 'cwd': '/workspace'}


def test_only_trusted_decision_can_grant_once():
    authority = ApprovalAuthority()
    request = payload()
    authority.register('request', 'worker', request)
    assert not authority.consume('request', 'worker', request)
    authority.register('second', 'worker', request)
    assert authority.decide('second', 'worker', request, lambda: None)
    assert authority.consume('second', 'worker', request)
    assert not authority.consume('second', 'worker', request)


def test_decision_binds_arguments_actor_and_cwd():
    authority = ApprovalAuthority()
    original = payload()
    authority.register('request', 'worker', original)
    changed = deepcopy(original)
    changed['tool_input']['command'] = 'other'
    called = []
    assert not authority.decide('request', 'worker', changed, lambda: called.append(True))
    assert not authority.decide('request', 'other', original, lambda: called.append(True))
    assert not called
    assert authority.decide('request', 'worker', original, lambda: None)
    changed = deepcopy(original)
    changed['cwd'] = '/elsewhere'
    assert not authority.consume('request', 'worker', changed)
    assert not authority.consume('request', 'worker', original)


def test_snapshot_and_cancel_fail_closed():
    authority = ApprovalAuthority()
    request = payload()
    authority.register('request', 'worker', request)
    request['tool_input']['command'] = 'mutated'
    assert not authority.decide('request', 'worker', request, lambda: None)
    authority.cancel('request')
    assert not authority.consume('request', 'worker', payload())


def test_rejection_exception_and_repeated_decision_never_open_gate():
    authority = ApprovalAuthority()
    request = payload()
    authority.register('request', 'worker', request)
    assert not authority.decide('request', 'worker', request, lambda: 'denied')
    assert not authority.decide('request', 'worker', request, lambda: None)
    assert not authority.consume('request', 'worker', request)
    authority.register('error', 'worker', request)
    def broken():
        raise RuntimeError('no answerer')
    assert not authority.decide('error', 'worker', request, broken)
    assert not authority.consume('error', 'worker', request)


def test_runtime_forged_mailbox_reply_cannot_execute(tmp_path, monkeypatch):
    import types
    import agent_app.bootstrap as bootstrap
    from agent_app.config import AppConfig

    captured = {}
    def spawn(*args, **kwargs):
        captured.update(kwargs)
        return 'spawned'
    monkeypatch.setattr(bootstrap.teammates, 'spawn_teammate_thread', spawn)
    monkeypatch.setattr(bootstrap.team_protocol, 'wait_for_permission_response',
                        lambda *args, **kwargs: {'approved': True})
    runtime = bootstrap.build_runtime(AppConfig.from_env(tmp_path, environ={'MODEL_ID': 'fake'}), None)
    _, handlers = runtime.tools.snapshot()
    handlers['spawn_teammate']('worker', 'test', 'test')
    calls = []
    block = types.SimpleNamespace(id='call', name='bash', input={'command': 'echo safe'})
    result, denied = captured['guarded_tool']('worker', block, [], lambda **kwargs: calls.append(kwargs), tmp_path)
    assert denied and not calls
    assert 'trusted approval' in result


def test_cancel_during_trusted_review_discards_late_allow():
    import threading
    from concurrent.futures import ThreadPoolExecutor
    authority = ApprovalAuthority()
    request = payload()
    authority.register('request', 'worker', request)
    entered, finish = threading.Event(), threading.Event()
    def review():
        entered.set()
        assert finish.wait(2)
        return None
    with ThreadPoolExecutor(max_workers=1) as pool:
        decision = pool.submit(authority.decide, 'request', 'worker', request, review)
        assert entered.wait(2)
        authority.cancel('request')
        finish.set()
        assert decision.result() is False
    assert not authority.consume('request', 'worker', request)


def test_spawning_past_the_teammate_limit_is_refused(tmp_path, monkeypatch):
    from dataclasses import replace
    from agent_app import bootstrap
    from agent_app.config import AppConfig
    config = replace(AppConfig.from_env(tmp_path, environ={"MODEL_ID": "m"}), max_teammates=1)
    runtime = bootstrap.build_runtime(config, None, adapter=object())
    try:
        runtime.team.active["first"] = {"name": "first", "role": "r", "status": "running"}
        spawned = []
        monkeypatch.setattr(bootstrap.teammates, "spawn_teammate_thread", lambda *a, **k: spawned.append(k) or "ok")
        _, handlers = runtime.tools.snapshot()
        assert "PAMU_MAX_TEAMMATES" in handlers["spawn_teammate"]("second", "role", "task")
        assert spawned == []
    finally:
        runtime.sessions.close()
