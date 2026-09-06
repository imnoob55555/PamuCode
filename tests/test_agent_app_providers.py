"""Acceptance tests for the provider registry and runtime switching."""

from __future__ import annotations

import types

import pytest

from agent_app.adapters import providers
from agent_app.config import AppConfig
from agent_app.runtime import ModelRouter


class FakeSDKClient:
    def __init__(self, **_options):
        self.messages = types.SimpleNamespace(
            create=lambda **_kwargs: None,
            stream=None,
        )


def _spec(name="deepseek"):
    return providers.get_provider(name)


def test_registry_contains_supported_providers():
    for name in ("anthropic", "deepseek", "moonshot", "zhipu", "openrouter"):
        spec = providers.get_provider(name)
        assert spec.name == name
        assert spec.base_url.startswith("https://")
        assert spec.default_model


def test_get_provider_unknown_name_raises():
    with pytest.raises(providers.UnknownProviderError):
        providers.get_provider("nope")


def test_build_client_passes_base_url_and_key():
    calls = []

    def factory(**options):
        calls.append(options)
        return FakeSDKClient(**options)

    providers.build_client(
        _spec("deepseek"), "sk-ds", anthropic_factory=factory
    )

    assert calls == [
        {"base_url": "https://api.deepseek.com/anthropic", "api_key": "sk-ds"}
    ]


def test_build_client_requires_key_for_keyed_providers():
    with pytest.raises(providers.MissingApiKeyError):
        providers.build_client(_spec("deepseek"), None, anthropic_factory=dict)


def test_verify_login_accepts_and_rejects():
    def ok_factory(**_options):
        return FakeSDKClient()

    def failing_factory(**_options):
        client = types.SimpleNamespace()
        client.messages = types.SimpleNamespace(
            create=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("401"))
        )
        return client

    assert providers.verify_login(
        _spec("deepseek"), "sk-good", anthropic_factory=ok_factory
    )
    assert not providers.verify_login(
        _spec("deepseek"), "sk-bad", anthropic_factory=failing_factory
    )


def test_apply_provider_swaps_adapter_model_and_runtime_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ID", "test-model")
    config = AppConfig.from_env(tmp_path)
    built = []

    def factory(**options):
        built.append(options)
        return FakeSDKClient(**options)

    runtime = types.SimpleNamespace(
        config=config,
        model=ModelRouter(
            provider="anthropic", primary_model="test-model", llm=object()
        ),
        llm=object(),
    )

    adapter = providers.apply_provider(
        runtime,
        _spec("moonshot"),
        "sk-ms",
        "kimi-custom",
        anthropic_factory=factory,
    )

    assert built == [
        {
            "base_url": "https://api.moonshot.cn/anthropic",
            "api_key": "sk-ms",
        }
    ]
    assert runtime.llm is adapter
    assert runtime.model.llm is adapter
    assert runtime.model.snapshot() == ("moonshot", "kimi-custom", None)


def test_bootstrap_uses_logged_in_provider_on_default_runtime(
    tmp_path, monkeypatch
):
    import agent_app.bootstrap as bootstrap
    from agent_app.features.auth import store_login

    client_calls = []

    class FakeAnthropic:
        def __init__(self, **kwargs):
            client_calls.append(kwargs)
            self.messages = FakeSDKClient(**kwargs).messages

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = FakeAnthropic
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.dotenv_values = lambda _path: {}
    import sys

    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    monkeypatch.setattr(bootstrap, "GLOBAL_SETTINGS_PATH", tmp_path / "missing")
    credentials = tmp_path / "credentials.json"
    store_login("deepseek", "sk-ds", "deepseek-reasoner", credentials)
    monkeypatch.setattr(bootstrap, "CREDENTIALS_PATH", credentials)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MODEL_ID", "should-be-overridden")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "legacy-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://legacy.invalid")

    runtime = bootstrap.build_default_runtime()

    assert client_calls == [
        {
            "base_url": "https://api.deepseek.com/anthropic",
            "api_key": "sk-ds",
        }
    ]
    assert runtime.model.snapshot()[:2] == ("deepseek", "deepseek-reasoner")
    assert runtime.config.primary_model == "deepseek-reasoner"


def test_bootstrap_provider_env_key_overrides_stored_key(tmp_path, monkeypatch):
    import agent_app.bootstrap as bootstrap
    from agent_app.features.auth import store_login

    client_calls = []

    class FakeAnthropic:
        def __init__(self, **kwargs):
            client_calls.append(kwargs)
            self.messages = FakeSDKClient(**kwargs).messages

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = FakeAnthropic
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.dotenv_values = lambda _path: {}
    import sys

    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    monkeypatch.setattr(bootstrap, "GLOBAL_SETTINGS_PATH", tmp_path / "missing")
    credentials = tmp_path / "credentials.json"
    store_login("zhipu", "stored-key", "glm-4.6", credentials)
    monkeypatch.setattr(bootstrap, "CREDENTIALS_PATH", credentials)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MODEL_ID", "ignored")
    monkeypatch.setenv("ZHIPU_API_KEY", "env-key")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    runtime = bootstrap.build_default_runtime()

    assert client_calls[-1] == {
        "base_url": "https://open.bigmodel.cn/api/anthropic",
        "api_key": "env-key",
    }
    assert runtime.config.primary_model == "glm-4.6"


def test_bootstrap_falls_back_to_anthropic_env_mode_without_login(
    tmp_path, monkeypatch
):
    import agent_app.bootstrap as bootstrap

    client_calls = []

    class FakeAnthropic:
        def __init__(self, **kwargs):
            client_calls.append(kwargs)
            self.messages = FakeSDKClient(**kwargs).messages

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = FakeAnthropic
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.dotenv_values = lambda _path: {}
    import sys

    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    monkeypatch.setattr(bootstrap, "GLOBAL_SETTINGS_PATH", tmp_path / "missing")
    monkeypatch.setattr(
        bootstrap, "CREDENTIALS_PATH", tmp_path / "missing-creds.json"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MODEL_ID", "claude-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    runtime = bootstrap.build_default_runtime()

    assert client_calls == [{"api_key": "env-key"}]
    assert runtime.config.primary_model == "claude-test"
    assert runtime.model.snapshot()[:2] == ("anthropic", "claude-test")
