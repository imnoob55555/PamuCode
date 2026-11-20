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


@pytest.mark.parametrize('command', ['resume', '-r', 'continue', '-c'])
def test_resume_without_sessions_exits_without_reading_stdin(tmp_path, monkeypatch, capsys, command):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('builtins.input', lambda *_: (_ for _ in ()).throw(AssertionError('read stdin')))
    assert main(argv=[command], runtime_factory=lambda: None) == 0
    assert '会话' in capsys.readouterr().out


def test_resume_rejects_conflicting_arguments(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(argv=['resume', '--last', '12345678'], runtime_factory=lambda: None) == 2


@pytest.mark.parametrize('command', ['continue', '-c'])
def test_continue_restores_before_workers_and_preserves_idle_snapshot(tmp_path, monkeypatch, command):
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
    assert main(argv=[command], runtime_factory=lambda: restored,
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


@pytest.mark.parametrize('kind', ['id', 'prefix', 'continue', '-c', '-r'])
def test_entrypoints_replay_only_visible_text(saved, tmp_path, monkeypatch, capsys, kind):
    args = (['resume', saved['id']] if kind == 'id' else
            ['resume', saved['id'][:8]] if kind == 'prefix' else
            ['-r', saved['id']] if kind == '-r' else [kind])
    code, rt = launch(tmp_path, monkeypatch, args)
    assert code in (None, 0)
    output = capsys.readouterr().out
    assert '用户：\nquestion\nnext' in output
    assert 'PamuCode：\nvisible answer' in output
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


@pytest.mark.parametrize('command', ['resume', '-r'])
def test_resume_alias_opens_picker_and_restores_selection(saved, tmp_path, monkeypatch, command):
    choices_seen = []
    ui = SimpleNamespace(
        choose=lambda title, choices: choices_seen.append(choices) or saved['id'],
        prompt=lambda: '/quit',
    )
    code, rt = launch(tmp_path, monkeypatch, [command], ui=ui)
    assert code in (None, 0)
    assert choices_seen[0][0][0] == saved['id']
    assert rt.sessions.session_id == saved['id']


@pytest.mark.parametrize('command', ['continue', '-c'])
def test_continue_selects_newest_without_opening_picker(saved, tmp_path, monkeypatch, command):
    older = copy.deepcopy(saved)
    older.update(id=str(uuid.uuid4()), updated_at=saved['updated_at'] - 1)
    SessionStore(tmp_path).save(older)
    def unexpected(*args):
        raise AssertionError('continue must not open picker')
    ui = SimpleNamespace(choose=unexpected, prompt=lambda: '/quit')
    code, rt = launch(tmp_path, monkeypatch, [command], ui=ui)
    assert code in (None, 0)
    assert rt.sessions.session_id == saved['id']


@pytest.mark.parametrize('command', ['continue', '-c'])
@pytest.mark.parametrize('argument', ['--last', '12345678'])
def test_continue_rejects_arguments_before_building_runtime(tmp_path, monkeypatch, capsys, command, argument):
    monkeypatch.chdir(tmp_path)
    def unexpected():
        raise AssertionError('unexpected runtime construction')
    assert main(argv=[command, argument], runtime_factory=unexpected) == 2
    assert 'pamu continue' in capsys.readouterr().err


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
    assert 'pamu continue' in output and 'pamu -c' in output and 'pamu resume <会话编号>' in output


@pytest.mark.parametrize('argument', ['bad', '../escape.json', '12345678', '--unknown', '--last'])
def test_bad_ids_and_options(saved, argument, capsys):
    assert main(argv=['resume', argument], runtime_factory=lambda: None) == 2
    if argument == '--last':
        assert capsys.readouterr().err == '会话错误：用法：pamu resume [会话编号]\n'


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
        assert main(argv=['continue'], runtime_factory=lambda: None) == 2
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
    launch(tmp_path, monkeypatch, ['continue'], rt=rt)
    assert 'explicit-current-model' in capsys.readouterr().out


def test_slash_mutation_and_checkpoint_share_agent_lock(saved, tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    switch = rt.model.switch_model
    def checked(self, model):
        assert rt.agent_lock.locked(), 'model mutation must hold agent_lock'
        switch(model)
    monkeypatch.setattr(type(rt.model), 'switch_model', checked)
    code, rt = launch(tmp_path, monkeypatch, ['continue'], rt=rt, commands=('/model changed', '/quit'))
    assert code in (None, 0)
    assert SessionStore(tmp_path).load(saved['id'])['selection']['primary_model'] == 'changed'


def test_checkpoint_error_exits_two_and_releases_lease(saved, tmp_path, monkeypatch, capsys):
    rt = runtime(tmp_path)
    def fail(*a, **kw): raise SessionError('无法保存会话')
    monkeypatch.setattr(rt.sessions, 'checkpoint', fail)
    code, rt = launch(tmp_path, monkeypatch, ['continue'], rt=rt, commands=('/model changed',))
    assert code == 2
    assert '会话错误' in capsys.readouterr().err
    SessionStore(tmp_path).acquire(saved['id']).close()
    with pytest.raises(SessionError): rt.sessions._check()


def test_busy_shutdown_closes_without_unsafe_checkpoint(saved, tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    rt.agent_lock = threading.Lock()
    def start(r): r.agent_lock.acquire(); return []
    monkeypatch.setattr('builtins.input', lambda *_: '/quit')
    assert main(argv=['continue'], runtime_factory=lambda: rt, start_threads=start,
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
    assert main(argv=['continue'], runtime_factory=fail,
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
    assert main(argv=['continue'], runtime_factory=fail) == 2
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


def test_resume_replays_long_multiline_messages_and_labels_interruptions(saved, tmp_path, monkeypatch, capsys):
    answer = '已创建 add.py。\n\n```python\ndef add(a, b):\n    return a + b\n```\n' + 'x' * 1000 + '\n验证通过。'
    saved['history'] = [
        {'role': 'user', 'content': '创建加法函数'},
        {'role': 'assistant', 'content': answer},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': '查看当前有多少 skills？'},
            {'type': 'text', 'text': '<task_notification>RAW_NOTIFICATION</task_notification>'},
        ]},
        {'role': 'user', '_pamu_source': 'system',
         'content': '[用户中断] 用户主动停止了上一轮。RAW_INSTRUCTIONS'},
        {'role': 'user', 'content': '[Compacted]\nRAW_SUMMARY'},
        {'role': 'user', 'content': '<reminder>RAW_REMINDER</reminder>'},
        {'role': 'user', 'content': '再帮我验证一次。'},
        {'role': 'assistant', 'content': '验证结果一致。'},
    ]
    saved['context_state'].pop('_message_records', None)
    SessionStore(tmp_path).save(saved)
    launch(tmp_path, monkeypatch, ['continue'])
    output = capsys.readouterr().out
    entries = ['用户：\n创建加法函数', f'PamuCode：\n{answer}', '用户：\n查看当前有多少 skills？',
               '系统：\n上一轮由用户中断。', '用户：\n再帮我验证一次。', 'PamuCode：\n验证结果一致。']
    positions = [output.index(entry) for entry in entries]
    assert positions == sorted(positions)
    assert 'RAW_' not in output
    assert '已回到此会话，可以继续输入。' in output


def test_resume_displays_shell_entries_separately_and_matches_each_batch(saved, tmp_path, monkeypatch, capsys):
    def shell(tool_id, command):
        return {'type': 'tool_use', 'id': tool_id, 'name': 'bash', 'input': {'command': command}}

    def result(tool_id, text):
        return {'type': 'tool_result', 'tool_use_id': tool_id, 'content': text}

    saved['history'] = [
        {'role': 'user', 'content': 'Run several checks'},
        {'role': 'assistant', 'content': [
            {'type': 'text', 'text': 'Checking the workspace.'},
            shell('reused', 'printf ok\n:'),
            shell('blocked', 'rm blocked.txt'),
            shell('background', 'sleep 1'),
            {'type': 'tool_use', 'id': 'read', 'name': 'read_file', 'input': {'path': 'HIDDEN_PATH'}},
        ]},
        {'role': 'user', 'content': [
            result('reused', 'HIDDEN_STDOUT'), result('blocked', 'Permission denied by user'),
            result('background', '[Background task bg_0001 started] Command: sleep 1.'),
            result('read', 'HIDDEN_FILE_CONTENT'),
        ]},
        {'role': 'assistant', 'content': 'Done checking.'},
        {'role': 'assistant', 'content': [shell('reused', 'touch never-created')]},
    ]
    saved['context_state'].pop('_message_records', None)
    saved['tool_states'] = {'reused': 'pending'}
    SessionStore(tmp_path).save(saved)

    launch(tmp_path, monkeypatch, ['-r', saved['id']])
    output = capsys.readouterr().out
    entries = ['PamuCode：\nChecking the workspace.', 'Shell（结果已记录）：\n$ printf ok\n  :',
               'Shell（未执行）：\n$ rm blocked.txt', 'Shell（已提交后台 bg_0001）：\n$ sleep 1',
               'PamuCode：\nDone checking.', 'Shell（未执行）：\n$ touch never-created']
    positions = [output.index(entry) for entry in entries]
    assert positions == sorted(positions)
    assert 'HIDDEN_' not in output
    assert not (tmp_path / 'never-created').exists()


@pytest.mark.parametrize('old_ids', [False, True])
def test_resume_replays_compacted_dialogue_in_order_once(tmp_path, monkeypatch, capsys, old_ids):
    from agent_app.adapters.common import TextBlock, ToolUseBlock
    from agent_app.core.context_manager import compact
    from tests.test_context_manager import runtime as compact_runtime

    monkeypatch.chdir(tmp_path)
    summary = dict(goal='summary', constraints=[], decisions=[], completed=[], remaining=[], uncertainties=[], references=[])
    original = compact_runtime(tmp_path, json.dumps(summary))
    old_request = '早期需求' + ' evidence' * 400
    original.session.history[:] = [
        {'role': 'user', 'content': old_request},
        {'role': 'assistant', 'content': [
            {'type': 'thinking', 'thinking': 'HIDDEN_ARCHIVED_REASON', 'signature': 'sig'},
            TextBlock('早期回复'),
            ToolUseBlock('archived-bash', 'bash', {'command': 'printf archived'}),
        ]},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'archived-bash', 'content': 'HIDDEN_ARCHIVED_STDOUT'},
        ]},
        {'role': 'user', 'content': '检查结果'},
        {'role': 'assistant', 'content': [TextBlock('检查完毕')]},
        {'role': 'user', 'content': '检查结果'},
    ]
    assert compact(original)
    assert original.session.history[0]['content'].startswith('[Compacted]')
    original.session.history.append({'role': 'assistant', 'content': '再次检查完毕'})
    checkpoint(original)
    store = original.sessions.store
    snapshot = store.load(original.sessions.session_id)
    retained = next(i for i, message in enumerate(snapshot['history'])
                    if message['content'] == [{'type': 'text', 'text': '检查完毕'}])
    record = snapshot['context_state']['_message_records'][retained]
    assert record['id'] in {entry['id'] for entry in snapshot['archive']}
    if old_ids:
        record['id'] = str(uuid.uuid4())
        store.save(snapshot)
    original.sessions.close()
    capsys.readouterr()

    code, _ = launch(tmp_path, monkeypatch, ['-c'])
    assert code in (0, None)
    output = capsys.readouterr().out
    expected = '\n\n'.join([f'用户：\n{old_request}', 'PamuCode：\n早期回复',
                            'Shell（结果已记录）：\n$ printf archived', '用户：\n检查结果',
                            'PamuCode：\n检查完毕', '用户：\n检查结果', 'PamuCode：\n再次检查完毕'])
    assert expected in output
    assert output.count('PamuCode：\n检查完毕') == 1
    assert output.count('用户：\n检查结果') == 2
    assert 'HIDDEN_ARCHIVED_' not in output


def test_legacy_replay_keeps_retained_messages_once(saved, tmp_path, monkeypatch, capsys):
    first = {'role': 'user', 'content': 'legacy question'}
    retained = {'role': 'assistant', 'content': 'retained answer'}
    latest = {'role': 'user', 'content': 'next question'}
    saved.update(schema_version=1, archive=[[first, retained]], history=[
        {'role': 'user', 'content': '[Reactive compact]\nRAW_SUMMARY'}, retained, latest,
    ])
    del saved['context_state']
    SessionStore(tmp_path).save(saved)
    launch(tmp_path, monkeypatch, ['-c'])
    output = capsys.readouterr().out
    assert '用户：\nlegacy question\n\nPamuCode：\nretained answer\n\n用户：\nnext question' in output
    assert output.count('retained answer') == 1
    assert 'RAW_SUMMARY' not in output


def test_missing_archived_dialogue_does_not_prevent_continuing(saved, tmp_path, monkeypatch, capsys):
    saved['archive'] = [dict(id='missing', ref='sha256:' + '0' * 64, source='user', turn_id='missing')]
    SessionStore(tmp_path).save(saved)
    seen = []
    code, _ = launch(tmp_path, monkeypatch, ['-c'], commands=('继续', '/quit'),
                     run_turn=lambda current, query: seen.append((current.sessions.session_id, query)))
    assert code in (0, None)
    assert seen == [(saved['id'], '继续')]
    output = capsys.readouterr().out
    assert '部分归档消息无法读取。' in output
    assert 'visible answer' in output


def test_interrupting_history_replay_leaves_the_restored_session_open(saved, tmp_path, monkeypatch, capsys):
    from agent_app import session_cli

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(session_cli, '_session_dialogue', interrupt)
    seen = []
    code, _ = launch(tmp_path, monkeypatch, ['-r', saved['id']], commands=('继续', '/quit'),
                     run_turn=lambda current, query: seen.append((current.sessions.session_id, query)))
    assert code in (0, None)
    assert seen == [(saved['id'], '继续')]
    assert '已跳过剩余历史。' in capsys.readouterr().out


def test_status_shows_unsaved_and_restored_id(saved, tmp_path, monkeypatch, capsys):
    launch(tmp_path, monkeypatch, [], commands=('/status', '/quit'))
    assert '会话：尚未保存' in capsys.readouterr().out
    launch(tmp_path, monkeypatch, ['continue'], commands=('/status', '/quit'))
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
    code, _ = launch(tmp_path, monkeypatch, ['continue'], commands=('continue',), run_turn=fail)
    assert code == 2
    SessionStore(tmp_path).acquire(saved['id']).close()


def test_injected_runtime_without_sessions_has_chinese_error(saved, tmp_path, capsys):
    assert main(argv=['continue'], runtime_factory=lambda: SimpleNamespace()) == 2
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
        main(argv=['continue'], runtime_factory=lambda: rt)
    with pytest.raises(SessionError): rt.sessions._check()
    SessionStore(tmp_path).acquire(saved['id']).close()


def test_restore_always_displays_active_model_and_todo_overview(saved, tmp_path, monkeypatch, capsys):
    saved['todos'] = [{'content': 'next task', 'status': 'pending'},
                      {'content': 'done task', 'status': 'completed'}]
    SessionStore(tmp_path).save(saved)
    launch(tmp_path, monkeypatch, ['continue'])
    output = capsys.readouterr().out
    assert '当前模型：test' in output
    assert '待办：2 项' in output and '已完成 1' in output
