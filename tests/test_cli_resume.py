import copy
import json
import threading
import uuid
from types import SimpleNamespace
import pytest
from agent_app.features.sessions import checkpoint
from agent_app.features.session_store import SessionError, SessionStore
from tests.test_session_runtime import runtime

from agent_app.cli import main


def test_resume_without_sessions_exits_without_reading_stdin(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('builtins.input', lambda *_: (_ for _ in ()).throw(AssertionError('read stdin')))
    assert main(argv=['resume'], runtime_factory=lambda: None) == 0
    assert '会话' in capsys.readouterr().out


def test_resume_rejects_conflicting_arguments(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(argv=['resume', '--last', '12345678'], runtime_factory=lambda: None) == 2


def test_last_restores_before_workers_and_preserves_idle_snapshot(tmp_path, monkeypatch):
    from threading import Lock
    from tests.test_session_runtime import runtime
    from agent_app.features.sessions import checkpoint
    monkeypatch.chdir(tmp_path)
    original = runtime(tmp_path)
    original.session.history.append({'role': 'user', 'content': 'original question'})
    checkpoint(original, status='idle')
    saved = original.sessions.store.load(original.sessions.session_id)
    original.sessions.close()
    restored = runtime(tmp_path)
    restored.agent_lock = Lock()
    observed = []
    monkeypatch.setattr('builtins.input', lambda _: '/quit')
    assert main(argv=['resume', '--last'], runtime_factory=lambda: restored,
                start_threads=lambda r: observed.append(list(r.session.history)) or [],
                stop_threads=lambda *_: None) in (0, None)
    assert observed[0][0]['content'] == 'original question'
    assert restored.sessions.store.load(saved['id']) == saved



@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rt = runtime(tmp_path)
    rt.session.history[:] = [
        {'role': 'user', 'content': 'question\x1b[31m\nnext'},
        {'role': 'assistant', 'content': [
            {'type': 'text', 'text': 'visible answer'},
            {'type': 'thinking', 'thinking': 'HIDDEN_REASON', 'signature': 'sig'},
        ]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'old', 'content': 'HIDDEN_TOOL'}]},
    ]
    checkpoint(rt)
    snap = rt.sessions.store.load(rt.sessions.session_id)
    rt.sessions.close()
    return snap


def launch(tmp_path, monkeypatch, args, *, rt=None, commands=('/quit',), **kwargs):
    rt = rt or runtime(tmp_path)
    rt.agent_lock = threading.Lock()
    rt.hooks = SimpleNamespace(trigger=lambda *a: None)
    inputs = iter(commands)
    monkeypatch.setattr('builtins.input', lambda *_: next(inputs))
    code = main(argv=args, runtime_factory=lambda: rt,
                start_threads=lambda r: [], stop_threads=lambda *a: None,
                credentials_path=tmp_path / 'credentials.json', **kwargs)
    return code, rt


@pytest.mark.parametrize('kind', ['id', 'prefix', 'last'])
def test_entrypoints_preview_only_visible_text(saved, tmp_path, monkeypatch, capsys, kind):
    argument = {'id': saved['id'], 'prefix': saved['id'][:8], 'last': '--last'}[kind]
    code, rt = launch(tmp_path, monkeypatch, ['resume', argument])
    assert code in (None, 0)
    output = capsys.readouterr().out
    assert 'visible answer' in output
    assert 'HIDDEN_REASON' not in output and 'HIDDEN_TOOL' not in output
    assert '\x1b' not in output
    assert rt.session.history


def test_picker_and_cancel(saved, tmp_path, monkeypatch):
    from agent_app.session_cli import select_session
    seen = []
    ui = SimpleNamespace(choose=lambda title, choices: seen.append(choices) or None)
    assert select_session([], ui) is None
    assert seen[0][0][0] == saved['id']
    assert saved['id'][:8] in seen[0][0][1]
    assert '\x1b' not in seen[0][0][1]
    ui.choose = lambda *a: saved['id']
    selected, lease = select_session([], ui)
    assert selected['id'] == saved['id']
    lease.close()


def test_picker_interrupt_is_clean_cancel(saved, monkeypatch):
    def cancel(*a): raise KeyboardInterrupt()
    assert main(argv=['resume'], ui=SimpleNamespace(choose=cancel), runtime_factory=lambda: None) == 0


def test_noninteractive_lists_without_constructing_or_reading(saved, monkeypatch, capsys):
    def fail(*a): raise AssertionError('unexpected read or construction')
    monkeypatch.setattr('builtins.input', fail)
    assert main(argv=['resume'], runtime_factory=fail) == 2
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert saved['id'] in output
    assert 'pamu resume --last' in output and 'pamu resume <会话编号>' in output


@pytest.mark.parametrize('argument', ['bad', '../escape.json', '12345678', '--unknown'])
def test_bad_ids_and_options(saved, argument):
    assert main(argv=['resume', argument], runtime_factory=lambda: None) == 2


def test_ambiguous_and_malformed_snapshots(saved, tmp_path):
    store = SessionStore(tmp_path)
    other = copy.deepcopy(saved)
    other['id'] = saved['id'][:9] + 'abcd' + saved['id'][13:]
    store.save(other)
    assert main(argv=['resume', saved['id'][:8]], runtime_factory=lambda: None) == 2
    (tmp_path / '.pamu' / 'sessions' / (saved['id'] + '.json')).write_text('{')
    assert main(argv=['resume', saved['id']], runtime_factory=lambda: None) == 2


def test_occupied_last_does_not_fall_back(saved, tmp_path):
    store = SessionStore(tmp_path)
    older = copy.deepcopy(saved)
    older.update(id=str(uuid.uuid4()), updated_at=saved['updated_at'] - 1)
    store.save(older)
    lease = store.acquire(saved['id'])
    try:
        assert main(argv=['resume', '--last'], runtime_factory=lambda: None) == 2
    finally:
        lease.close()


def test_plain_pamu_does_not_restore_or_save_empty(saved, tmp_path, monkeypatch):
    code, rt = launch(tmp_path, monkeypatch, [])
    assert code in (None, 0)
    assert rt.session.history == []
    assert rt.sessions.session_id is None
    assert len(SessionStore(tmp_path).list_sessions()[0]) == 1


def test_restore_displays_selection_override(saved, tmp_path, monkeypatch, capsys):
    rt = runtime(tmp_path)
    rt.model.switch_model('explicit-current-model')
    launch(tmp_path, monkeypatch, ['resume', '--last'], rt=rt)
    assert 'explicit-current-model' in capsys.readouterr().out


def test_slash_mutation_and_checkpoint_share_agent_lock(saved, tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    switch = rt.model.switch_model
    def checked(self, model):
        assert rt.agent_lock.locked(), 'model mutation must hold agent_lock'
        switch(model)
    monkeypatch.setattr(type(rt.model), 'switch_model', checked)
    code, rt = launch(tmp_path, monkeypatch, ['resume', '--last'], rt=rt, commands=('/model changed', '/quit'))
    assert code in (None, 0)
    assert SessionStore(tmp_path).load(saved['id'])['selection']['primary_model'] == 'changed'


def test_checkpoint_error_exits_two_and_releases_lease(saved, tmp_path, monkeypatch, capsys):
    rt = runtime(tmp_path)
    def fail(*a, **kw): raise SessionError('无法保存会话')
    monkeypatch.setattr(rt.sessions, 'checkpoint', fail)
    code, rt = launch(tmp_path, monkeypatch, ['resume', '--last'], rt=rt, commands=('/model changed',))
    assert code == 2
    assert '会话错误' in capsys.readouterr().err
    SessionStore(tmp_path).acquire(saved['id']).close()
    with pytest.raises(SessionError): rt.sessions._check()


def test_busy_shutdown_closes_without_unsafe_checkpoint(saved, tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    rt.agent_lock = threading.Lock()
    def start(r): r.agent_lock.acquire(); return []
    monkeypatch.setattr('builtins.input', lambda *_: '/quit')
    assert main(argv=['resume', '--last'], runtime_factory=lambda: rt, start_threads=start,
                stop_threads=lambda *a: None, credentials_path=tmp_path/'credentials.json') in (0, None)
    assert SessionStore(tmp_path).load(saved['id']) == saved
    with pytest.raises(SessionError): checkpoint(rt)
    SessionStore(tmp_path).acquire(saved['id']).close()
    rt.agent_lock.release()

@pytest.fixture
def configure(tmp_path, monkeypatch):
    import agent_app.bootstrap as bootstrap
    from agent_app.adapters import providers
    monkeypatch.chdir(tmp_path)
    calls = []
    def client(spec, key, **kw):
        calls.append((spec, key, kw))
        return object()
    monkeypatch.setattr(providers, 'build_client', client)
    monkeypatch.setattr(providers, 'build_adapter', lambda *a, **kw: object())
    monkeypatch.setattr(bootstrap, 'build_runtime', lambda config, client, **kw: SimpleNamespace(config=config, **kw))
    credentials = tmp_path / 'isolated-credentials.json'
    def build(env, selection, records=None):
        monkeypatch.setattr(bootstrap, 'load_runtime_environment', lambda: dict(env))
        if records is not None:
            credentials.write_text(json.dumps(records))
        before = credentials.read_bytes() if credentials.exists() else None
        result = bootstrap.build_default_runtime(credentials_path=credentials, session_selection=selection)
        assert (credentials.read_bytes() if credentials.exists() else None) == before
        return result, calls[-1]
    return build


def selection(**kw):
    return dict(dict(provider='anthropic', protocol='anthropic_messages',
                     base_url='https://api.anthropic.com', primary_model='saved-model',
                     fallback_model='saved-fallback', thinking_level='high'), **kw)


def test_saved_model_and_thinking_are_session_defaults_not_global_login(configure):
    rt, (_, key, _) = configure({}, selection(), {'active': 'openai', 'providers': {
        'anthropic': {'api_key': 'fake-current-anthropic', 'model': 'global-model', 'thinking': 'off'},
        'openai': {'api_key': 'fake-other-key', 'model': 'other-model'},
    }})
    assert rt.config.primary_model == 'saved-model'
    assert rt.config.fallback_model == 'saved-fallback'
    assert rt.thinking_level == 'high'
    assert key == 'fake-current-anthropic'


@pytest.mark.parametrize('env, expected', [
    ({'MODEL_ID': 'explicit'}, 'explicit'),
    ({'PAMU_PROVIDER': 'deepseek'}, 'deepseek-chat'),
    ({'PAMU_PROVIDER': 'deepseek', 'MODEL_ID': 'explicit-target'}, 'explicit-target'),
])
def test_explicit_model_and_provider_override_saved(configure, env, expected):
    rt, _ = configure(dict(ANTHROPIC_API_KEY='fake-a', DEEPSEEK_API_KEY='fake-d', **env), selection())
    assert rt.config.primary_model == expected
    if env.get('PAMU_PROVIDER') == 'deepseek':
        assert rt.config.fallback_model is None
        assert rt.thinking_level == 'off'


@pytest.mark.parametrize('kind', ['endpoint', 'protocol', 'explicit', 'empty'])
def test_fallback_only_restored_in_matching_scope_without_explicit_override(configure, kind):
    env = {'OPENAI_COMPATIBLE_BASE_URL': 'http://current.local/v1'}
    saved_selection = selection(provider='openai-compatible', protocol='openai_chat', base_url=env['OPENAI_COMPATIBLE_BASE_URL'])
    expected = None
    if kind == 'endpoint': saved_selection['base_url'] = 'http://old.local/v1'
    if kind == 'protocol': env['OPENAI_COMPATIBLE_PROTOCOL'] = 'openai_responses'
    if kind == 'explicit': env['FALLBACK_MODEL_ID'] = expected = 'explicit-fallback'
    if kind == 'empty': env['FALLBACK_MODEL_ID'] = expected = ''
    rt, (_, key, options) = configure(env, saved_selection)
    assert rt.config.fallback_model == expected
    assert rt.thinking_level == 'off'
    assert options['base_url'] == 'http://current.local/v1'
    assert key is None


def test_saved_url_never_supplies_missing_current_address(configure):
    from agent_app.provider_config import ProviderConfigurationError
    with pytest.raises(ProviderConfigurationError):
        configure({}, selection(provider='openai-compatible', protocol='openai_chat', base_url='http://saved.local/v1'))


def test_saved_selection_cannot_supply_credentials(configure):
    from agent_app.adapters.providers import MissingApiKeyError
    with pytest.raises(MissingApiKeyError):
        configure({}, selection())


def test_ollama_restore_is_keyless_even_with_other_keys(configure):
    rt, (spec, key, _) = configure({'OPENAI_API_KEY': 'fake-other'}, selection(provider='ollama', protocol='ollama', base_url='http://localhost:11434'))
    assert spec.protocol == 'ollama' and key is None
    assert rt.config.primary_model == 'saved-model'
    assert rt.thinking_level == 'off'


def test_configuration_failure_releases_selected_lease(saved, tmp_path, monkeypatch):
    from agent_app.config import MissingConfigurationError
    def fail(): raise MissingConfigurationError('MODEL_ID')
    assert main(argv=['resume', '--last'], runtime_factory=fail,
                credentials_path=tmp_path/'credentials.json') == 2
    SessionStore(tmp_path).acquire(saved['id']).close()
    assert SessionStore(tmp_path).load(saved['id']) == saved


def test_unrenderable_date_does_not_break_listing(saved, tmp_path, capsys):
    saved['updated_at'] = 1e100
    SessionStore(tmp_path).save(saved)
    assert main(argv=['resume'], runtime_factory=lambda: None) == 2
    assert saved['id'] in capsys.readouterr().out


def test_session_error_during_construction_releases_lease(saved, tmp_path):
    def fail(): raise SessionError('无法创建会话存储')
    assert main(argv=['resume', '--last'], runtime_factory=fail) == 2
    SessionStore(tmp_path).acquire(saved['id']).close()


def test_readonly_model_command_does_not_checkpoint(saved, tmp_path, monkeypatch):
    from agent_app.cli import handle_slash_command
    rt = runtime(tmp_path)
    rt.agent_lock = threading.Lock()
    def fail(*a, **kw): raise AssertionError('readonly model command saved')
    monkeypatch.setattr(rt.sessions, 'checkpoint', fail)
    handle_slash_command(rt, '/model', credentials_path=tmp_path/'credentials.json')


@pytest.mark.parametrize('command, field, expected', [
    ('/thinking high', 'thinking_level', 'high'),
    ('/provider deepseek', 'provider', 'deepseek'),
])
def test_thinking_and_provider_checkpoint_with_mutation_lock(saved, tmp_path, monkeypatch, command, field, expected):
    from agent_app.cli import handle_slash_command
    from agent_app.features import auth
    import agent_app.bootstrap as bootstrap
    rt = runtime(tmp_path)
    rt.agent_lock = threading.Lock()
    rt.sessions.restore(rt, saved, rt.sessions.store.acquire(saved['id']))
    credentials = tmp_path / 'credentials.json'
    auth.store_login('deepseek', 'fake-provider-only', 'target-model', credentials)
    monkeypatch.setattr(bootstrap, 'load_runtime_environment', lambda: {})
    original_checkpoint = rt.sessions.checkpoint
    def checked(*args, **kwargs):
        assert rt.agent_lock.locked()
        return original_checkpoint(*args, **kwargs)
    monkeypatch.setattr(rt.sessions, 'checkpoint', checked)
    handle_slash_command(rt, command, credentials_path=credentials,
                         client_factory=lambda **kw: SimpleNamespace(messages=SimpleNamespace()))
    assert rt.sessions.store.load(saved['id'])['selection'][field] == expected
    rt.sessions.close()


def test_session_model_source_is_identified(configure):
    rt, _ = configure({'ANTHROPIC_API_KEY': 'fake-key'}, selection())
    assert rt.model_source == '恢复会话'


@pytest.mark.parametrize('layers, expected', [
    (({'MODEL_ID': 'process'}, {'MODEL_ID': 'project'}, {'MODEL_ID': 'home'}), 'process'),
    (({}, {'MODEL_ID': 'project'}, {'MODEL_ID': 'home'}), 'project'),
    (({}, {}, {'MODEL_ID': 'home'}), 'home'),
])
def test_explicit_settings_layers_remain_above_snapshot(configure, tmp_path, layers, expected):
    from agent_app.bootstrap import _load_environment
    process, project, home = layers
    home_path = tmp_path / 'home.settings'
    values = {tmp_path / '.pamu' / '.settings': project, home_path: home}
    environment = _load_environment(tmp_path, home_path, process, lambda path: values[path])
    environment['ANTHROPIC_API_KEY'] = 'fake-layer-key'
    rt, _ = configure(environment, selection())
    assert rt.config.primary_model == expected


def test_resume_preview_is_bounded(saved, tmp_path, monkeypatch, capsys):
    saved['history'] = [{'role': 'user', 'content': f'message-{i} ' + 'x' * 1000} for i in range(10)]
    saved['context_state'].pop('_message_records', None)
    SessionStore(tmp_path).save(saved)
    launch(tmp_path, monkeypatch, ['resume', '--last'])
    output = capsys.readouterr().out
    assert 'message-0' not in output and 'message-9' in output
    assert len(output) < 1000


def test_status_shows_unsaved_and_restored_id(saved, tmp_path, monkeypatch, capsys):
    launch(tmp_path, monkeypatch, [], commands=('/status', '/quit'))
    assert '会话：尚未保存' in capsys.readouterr().out
    launch(tmp_path, monkeypatch, ['resume', '--last'], commands=('/status', '/quit'))
    assert f"会话：{saved['id']}" in capsys.readouterr().out


def test_corrupt_session_is_skipped_in_listing(saved, tmp_path, capsys):
    (tmp_path / '.pamu' / 'sessions' / (str(uuid.uuid4()) + '.json')).write_text('{')
    assert main(argv=['resume'], runtime_factory=lambda: None) == 2
    output = capsys.readouterr().out
    assert '已跳过 1 个无法读取的会话' in output and saved['id'] in output


def test_resume_reloads_after_acquiring_lease(saved, monkeypatch):
    from agent_app.session_cli import select_session
    events = []
    load, acquire = SessionStore.load, SessionStore.acquire
    def tracked_load(self, value):
        events.append('load')
        return load(self, value)
    def tracked_acquire(self, value):
        events.append('acquire')
        return acquire(self, value)
    monkeypatch.setattr(SessionStore, 'load', tracked_load)
    monkeypatch.setattr(SessionStore, 'acquire', tracked_acquire)
    _, lease = select_session([saved['id']], None)
    assert events == ['load', 'acquire', 'load']
    lease.close()


def test_turn_persistence_failure_stops_repl_and_releases_lease(saved, tmp_path, monkeypatch):
    def fail(*args): raise SessionError('无法保存会话')
    code, _ = launch(tmp_path, monkeypatch, ['resume', '--last'], commands=('continue',), run_turn=fail)
    assert code == 2
    SessionStore(tmp_path).acquire(saved['id']).close()


def test_injected_runtime_without_sessions_has_chinese_error(saved, tmp_path, capsys):
    assert main(argv=['resume', '--last'], runtime_factory=lambda: SimpleNamespace()) == 2
    assert '当前运行时不支持会话恢复' in capsys.readouterr().err
    SessionStore(tmp_path).acquire(saved['id']).close()


def test_restore_interruption_closes_coordinator_and_lease(saved, tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    original_restore = rt.sessions.restore
    def interrupted(*args):
        original_restore(*args)
        raise KeyboardInterrupt()
    monkeypatch.setattr(rt.sessions, 'restore', interrupted)
    with pytest.raises(KeyboardInterrupt):
        main(argv=['resume', '--last'], runtime_factory=lambda: rt)
    with pytest.raises(SessionError): rt.sessions._check()
    SessionStore(tmp_path).acquire(saved['id']).close()


def test_restore_always_displays_active_model_and_todo_overview(saved, tmp_path, monkeypatch, capsys):
    saved['todos'] = [{'content': 'next task', 'status': 'pending'},
                      {'content': 'done task', 'status': 'completed'}]
    SessionStore(tmp_path).save(saved)
    launch(tmp_path, monkeypatch, ['resume', '--last'])
    output = capsys.readouterr().out
    assert '当前模型：test' in output
    assert '待办：2 项' in output and '已完成 1' in output
