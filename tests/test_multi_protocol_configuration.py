"""Offline acceptance for provider selection, local setup and adapter wiring."""

from types import SimpleNamespace

import pytest

from agent_app.adapters import providers
from agent_app.features import auth


@pytest.mark.parametrize('name,protocol,keyed', [
    ('openai', 'openai_responses', True),
    ('openai-compatible', 'openai_chat', False),
    ('ollama', 'ollama', False),
])
def test_protocol_registry(name, protocol, keyed):
    spec = providers.get_provider(name)
    assert spec.protocol == protocol
    assert spec.requires_key is keyed


def test_saved_ollama_is_configured_without_key(tmp_path):
    path = tmp_path / 'credentials.json'
    auth.store_login('ollama', '', 'installed-local-model', path)
    assert auth.configured_provider(auth.load_credentials(path)) == 'ollama'


def test_config_resolves_explicit_values_and_does_not_borrow_keys():
    from agent_app.provider_config import resolve_provider_config

    credentials = {'active': 'ollama', 'providers': {
        'ollama': {'model': 'saved', 'base_url': 'http://localhost:11434'},
    }}
    cfg = resolve_provider_config({
        'MODEL_ID': 'override', 'OLLAMA_BASE_URL': 'http://localhost:1234',
        'ANTHROPIC_API_KEY': 'unrelated-fake-key',
    }, credentials)
    assert cfg.model == 'override'
    assert cfg.base_url == 'http://localhost:1234'
    assert cfg.api_key is None
    assert cfg.model_source == '环境配置'


def test_compatible_config_preserves_protocol_address_and_optional_key(tmp_path):
    from agent_app.provider_config import resolve_provider_config

    path = tmp_path / 'credentials.json'
    auth.store_login('openai-compatible', '', 'local', path,
                     base_url='http://localhost:8080/v1', protocol='openai_responses')
    cfg = resolve_provider_config({}, auth.load_credentials(path))
    assert (cfg.protocol, cfg.base_url, cfg.model) == (
        'openai_responses', 'http://localhost:8080/v1', 'local')
    assert not cfg.api_key


@pytest.mark.parametrize('env', [
    {'PAMU_PROVIDER': 'unknown', 'MODEL_ID': 'm'},
    {'PAMU_PROVIDER': 'openai-compatible', 'MODEL_ID': 'm'},
    {'PAMU_PROVIDER': 'openai-compatible', 'MODEL_ID': 'm',
     'OPENAI_COMPATIBLE_BASE_URL': 'http://localhost:8000/v1',
     'OPENAI_COMPATIBLE_PROTOCOL': 'invalid'},
    {'PAMU_PROVIDER': 'ollama'},
    {'PAMU_PROVIDER': 'ollama', 'MODEL_ID': 'm', 'OLLAMA_BASE_URL': 'file:///tmp/model'},
])
def test_invalid_configuration_is_explicit(env):
    from agent_app.provider_config import resolve_provider_config

    with pytest.raises(ValueError):
        resolve_provider_config(env, {'providers': {}})


def test_startup_and_readiness_accept_keyless_ollama(tmp_path, monkeypatch):
    from agent_app import bootstrap

    path = tmp_path / 'credentials.json'
    auth.store_login('ollama', '', 'local-model', path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap, 'load_runtime_environment', lambda: {})
    assert bootstrap.has_local_configuration(path)
    runtime = bootstrap.build_default_runtime(credentials_path=path)
    try:
        assert runtime.model.snapshot()[:2] == ('ollama', 'local-model')
        assert runtime.llm.__class__.__name__ == 'OllamaAdapter'
        assert runtime.model.thinking_snapshot() == 'off'
    finally:
        runtime.llm.client.close()


def test_non_off_thinking_is_rejected_without_persistence(tmp_path, capsys):
    from agent_app.cli import handle_slash_command
    from agent_app.runtime import ModelRouter

    path = tmp_path / 'credentials.json'
    auth.store_login('ollama', '', 'local', path)
    before = path.read_bytes()
    runtime = SimpleNamespace(model=ModelRouter(provider='ollama', primary_model='local'))
    handle_slash_command(runtime, '/thinking high', credentials_path=path)
    assert runtime.model.thinking_snapshot() == 'off'
    assert path.read_bytes() == before
    assert '暂不支持' in capsys.readouterr().out


def test_offline_ollama_setup_skips_secret_and_saves_custom_address(tmp_path):
    from agent_app.onboarding import login

    path = tmp_path / 'credentials.json'
    answers = iter(['http://localhost:12345', 'my-local-model'])
    ui = SimpleNamespace(
        text=lambda *args, **kwargs: next(answers),
        choose=lambda *args, **kwargs: '',
        secret=lambda *args, **kwargs: pytest.fail('Ollama must not request a key'),
    )
    assert login(ui, path, 'ollama') == 'ollama'
    record = auth.get_login(auth.load_credentials(path), 'ollama')
    assert record['model'] == 'my-local-model'
    assert record['base_url'] == 'http://localhost:12345'


def test_openai_client_factory_disables_builtin_retries():
    calls = []
    client = providers.build_client(
        providers.get_provider('openai'), 'fake-key',
        openai_factory=lambda **kwargs: calls.append(kwargs) or object(),
    )
    assert client is not None
    assert calls[0]['max_retries'] == 0
    assert calls[0]['api_key'] == 'fake-key'


@pytest.mark.parametrize('wire_protocol', ['openai_chat', 'openai_responses'])
def test_real_sdk_keyless_gateway_does_not_use_official_key(monkeypatch, wire_protocol):
    from dataclasses import replace
    from importlib import import_module

    monkeypatch.setenv('OPENAI_API_KEY', 'unrelated-fake-official-key')
    spec = replace(providers.get_provider('openai-compatible'), protocol=wire_protocol)
    client = providers.build_client(spec, None, base_url='http://localhost:8080/v1')
    sent = []
    def send(request, **kwargs):
        sent.append(request)
        http = import_module(type(request).__module__.split('.')[0])
        payload = ({'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
                    'content': [{'type': 'output_text', 'text': 'done'}]}]}
                   if wire_protocol == 'openai_responses' else
                   {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'done'}, 'finish_reason': 'stop'}]})
        return http.Response(200, json=payload, request=request)
    monkeypatch.setattr(client._client, 'send', send)
    try:
        result = providers.build_adapter(spec, client, keyless=True).create(system=None, messages=[{'role': 'user', 'content': 'hello'}], model='local', max_tokens=256, tools=None)
        assert result.content[0].text == 'done'
        assert 'authorization' not in sent[0].headers
        assert str(sent[0].url).startswith('http://localhost:8080/v1/')
    finally:
        client.close()


def test_cli_verifies_selected_compatible_protocol_url_and_model(tmp_path):
    from agent_app.cli import run_command
    values = iter(['openai_responses', 'http://localhost:9876/v1', 'custom-model'])
    checked = []
    code = run_command(['login', 'openai-compatible'], credentials_path=tmp_path / 'creds.json',
                       input_fn=lambda prompt: next(values), secret_fn=lambda prompt: '',
                       verify=lambda spec, key, **kw: checked.append((spec, key, kw)) or True)
    assert code == 0
    spec, key, options = checked[0]
    assert (spec.protocol, spec.base_url, options['model']) == ('openai_responses', 'http://localhost:9876/v1', 'custom-model')
    assert not key


def test_compatible_offline_login_and_cancel_are_atomic(tmp_path):
    from agent_app.onboarding import login
    path = tmp_path / 'creds.json'
    choices = iter(['openai_responses', 'none', ''])
    texts = iter(['http://localhost:9876/v1', 'custom-model'])
    ui = SimpleNamespace(choose=lambda *a, **kw: next(choices), text=lambda *a: next(texts),
                         secret=lambda *a, **kw: pytest.fail('keyless setup requested secret'))
    assert login(ui, path, 'openai-compatible') == 'openai-compatible'
    saved = auth.get_login(auth.load_credentials(path), 'openai-compatible')
    assert (saved['protocol'], saved['base_url'], saved['model'], saved['api_key']) == ('openai_responses', 'http://localhost:9876/v1', 'custom-model', '')
    before = path.read_bytes()
    ui.choose = lambda *a, **kw: None
    assert login(ui, path, 'openai-compatible') is None
    assert path.read_bytes() == before


def test_keyless_use_live_switch_status_and_restart(tmp_path, monkeypatch, capsys):
    from agent_app import bootstrap, cli
    from agent_app.runtime import ModelRouter
    path = tmp_path / 'creds.json'
    monkeypatch.setattr(bootstrap, 'load_runtime_environment', lambda: {})
    auth.store_login('ollama', '', 'local', path, base_url='http://localhost:12345')
    assert cli.run_command(['use', 'ollama'], credentials_path=path) == 0
    runtime = SimpleNamespace(model=ModelRouter(provider='anthropic', primary_model='old'), llm=object())
    cli.handle_slash_command(runtime, '/provider ollama', credentials_path=path)
    try:
        assert runtime.model.snapshot()[:2] == ('ollama', 'local')
        assert runtime.llm.__class__.__name__ == 'OllamaAdapter'
        cli.handle_slash_command(runtime, '/model changed', credentials_path=path)
        cli.handle_slash_command(runtime, '/status', credentials_path=path)
        assert 'http://localhost:12345' in capsys.readouterr().out
        from agent_app.provider_config import resolve_provider_config
        assert resolve_provider_config({}, auth.load_credentials(path)).model == 'changed'
        monkeypatch.setattr(bootstrap, 'load_runtime_environment', lambda: {'MODEL_ID': 'override'})
        cli.run_command(['status'], credentials_path=path)
        assert 'override（环境配置）' in capsys.readouterr().out
    finally:
        runtime.llm.client.close()


def test_incomplete_explicit_provider_never_falls_back_to_saved_account():
    from agent_app.provider_config import resolve_provider_config
    credentials = {'active': 'ollama', 'providers': {'anthropic': {'api_key': 'fake-key', 'model': 'old'}}}
    with pytest.raises(ValueError):
        resolve_provider_config({}, credentials)


@pytest.mark.parametrize('url', ['https://user:secret@example.com', 'http://localhost:1234/?key=secret', 'http://localhost:99999', 'http://localhost/a b'])
def test_url_validation_never_echoes_embedded_secrets(url):
    from agent_app.provider_config import normalize_base_url
    with pytest.raises(ValueError) as caught:
        normalize_base_url(url)
    assert 'secret' not in str(caught.value)


def test_real_anthropic_client_cannot_borrow_another_providers_bearer_token(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'unrelated-fake-token')
    client = providers.build_client(providers.get_provider('deepseek'), 'fake-deepseek-key')
    try:
        assert not client.auth_token
        assert client.max_retries == 0
    finally:
        client.close()


def test_live_switch_accepts_environment_only_local_model(tmp_path, monkeypatch):
    from agent_app import bootstrap, cli
    from agent_app.runtime import ModelRouter
    monkeypatch.setattr(bootstrap, 'load_runtime_environment', lambda: {'PAMU_PROVIDER': 'ollama', 'MODEL_ID': 'env-model'})
    runtime = SimpleNamespace(model=ModelRouter(provider='anthropic', primary_model='old'), llm=None)
    cli.handle_slash_command(runtime, '/provider ollama', credentials_path=tmp_path / 'creds')
    assert runtime.model.snapshot()[:2] == ('ollama', 'env-model')
    runtime.llm.client.close()


def test_cli_login_verifies_custom_openai_base_url(tmp_path, monkeypatch):
    from agent_app import bootstrap, cli
    monkeypatch.setattr(bootstrap, 'load_runtime_environment', lambda: {'OPENAI_BASE_URL': 'http://localhost:9999/v1'})
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    calls = []
    assert cli.run_command(['login', 'openai'], credentials_path=tmp_path / 'creds',
        input_fn=lambda p: 'chosen-model', secret_fn=lambda p: pytest.fail('unexpected secret'),
        verify=lambda spec, key, **kw: calls.append(spec.base_url) or True) == 0
    assert calls == ['http://localhost:9999/v1']


@pytest.mark.parametrize('provider', ['openai', 'openai-compatible'])
def test_relogin_verifies_saved_endpoint_protocol_and_model(tmp_path, monkeypatch, provider):
    from agent_app import bootstrap, cli
    path = tmp_path / 'creds'
    auth.store_login(provider, 'fake-key', 'saved-model', path,
                     base_url='http://localhost:9876/v1', protocol='openai_responses')
    monkeypatch.setattr(bootstrap, 'load_runtime_environment', lambda: {})
    for variable in ('MODEL_ID', 'OPENAI_BASE_URL', 'OPENAI_COMPATIBLE_BASE_URL', 'OPENAI_COMPATIBLE_PROTOCOL'):
        monkeypatch.delenv(variable, raising=False)
    checked = []
    assert cli.run_command(['login', provider], credentials_path=path,
        input_fn=lambda prompt: '', secret_fn=lambda prompt: 'fake-key',
        verify=lambda spec, key, **kwargs: checked.append((spec, kwargs)) or True) == 0
    spec, kwargs = checked[0]
    assert (spec.base_url, spec.protocol, kwargs['model']) == ('http://localhost:9876/v1', 'openai_responses', 'saved-model')
