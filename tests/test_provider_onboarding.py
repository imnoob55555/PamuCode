"""Local setup must never need a live API request."""

import stat
import sys
from types import SimpleNamespace
from typing import cast

import pytest

from agent_app.adapters import providers
from agent_app.config import AppConfig
from agent_app.features import auth


def test_saved_provider_ignores_corrupt_and_blank_records():
    data = {
        "active": "anthropic",
        "providers": {
            "anthropic": {"api_key": "  "},
            "zhipu": [],
            "unknown": {"api_key": "key"},
            "deepseek": {"api_key": "saved"},
        },
    }
    assert auth.configured_provider(data) == "deepseek"
    data["providers"]["deepseek"]["api_key"] = 123
    assert auth.configured_provider(data) is None


def test_key_saved_before_activation_and_without_model(tmp_path):
    path = tmp_path / "credentials.json"
    auth.store_login("anthropic", "previous", "old-model", path)
    auth.save_key("deepseek", "new-key", path)
    data = auth.load_credentials(path)
    assert data["active"] == "anthropic"
    assert data["providers"]["deepseek"] == {"api_key": "new-key"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_failed_atomic_save_preserves_previous_credentials(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    auth.store_login("anthropic", "previous", "old-model", path)
    before = path.read_bytes()

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr("os.replace", fail)
    with pytest.raises(OSError):
        auth.save_key("deepseek", "new-key", path)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_model_menu_preserves_custom_saved_model_and_has_choices():
    for spec in providers.PROVIDERS.values():
        choices = providers.model_choices(spec, "private-model")
        assert choices[0] == "private-model"
        if spec.default_model:
            assert spec.default_model in choices
            assert len(choices) >= 3
        else:
            assert choices == ("private-model",)
        assert len(choices) == len(set(choices))


def test_saved_anthropic_model_boots_without_model_env(tmp_path, monkeypatch):
    import agent_app.bootstrap as bootstrap

    calls = []
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(Anthropic=lambda **options: calls.append(options) or object()),
    )
    monkeypatch.setitem(
        sys.modules, "dotenv", SimpleNamespace(dotenv_values=lambda p: {})
    )
    monkeypatch.setattr(bootstrap, "build_runtime", lambda config, client, **kw: config)
    path = tmp_path / "credentials.json"
    auth.store_login("anthropic", "saved-key", "chosen-model", path)
    monkeypatch.setattr(bootstrap, "CREDENTIALS_PATH", path)
    for key in (
        "MODEL_ID",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    config = cast(AppConfig, bootstrap.build_default_runtime())
    assert config.primary_model == "chosen-model"
    assert calls == [{"api_key": "saved-key"}]


def test_explicit_model_setting_takes_priority_over_saved_anthropic_model(
    tmp_path, monkeypatch
):
    import agent_app.bootstrap as bootstrap

    monkeypatch.setitem(
        sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda **kw: object())
    )
    monkeypatch.setattr(
        bootstrap, "load_runtime_environment", lambda: {"MODEL_ID": "legacy-model"}
    )
    monkeypatch.setattr(bootstrap, "build_runtime", lambda config, client, **kw: config)
    path = tmp_path / "credentials.json"
    auth.store_login("anthropic", "saved-key", "chosen-model", path)
    assert (
        cast(AppConfig, bootstrap.build_default_runtime(credentials_path=path)).primary_model
        == "legacy-model"
    )


def test_openrouter_uses_anthropic_endpoint_and_bearer_auth():
    options = []
    providers.build_client(
        providers.get_provider("openrouter"),
        "router-key",
        anthropic_factory=lambda **kw: options.append(kw),
    )
    assert options == [
        {
            "base_url": "https://openrouter.ai/api",
            "api_key": "",
            "auth_token": "router-key",
        }
    ]


def test_startup_passes_token_to_custom_anthropic_gateway(tmp_path, monkeypatch):
    import agent_app.bootstrap as bootstrap

    options = []
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(Anthropic=lambda **kw: options.append(kw) or object()),
    )
    monkeypatch.setattr(
        bootstrap,
        "load_runtime_environment",
        lambda: {
            "MODEL_ID": "gateway-model",
            "ANTHROPIC_AUTH_TOKEN": "token",
            "ANTHROPIC_BASE_URL": "https://gateway.invalid",
        },
    )
    monkeypatch.setattr(bootstrap, "build_runtime", lambda *args, **kw: object())
    bootstrap.build_default_runtime(credentials_path=tmp_path / "missing")
    assert options == [
        {"api_key": "", "auth_token": "token", "base_url": "https://gateway.invalid"}
    ]


class SetupUI:
    def __init__(self, path, cancel_model=False):
        self.path = path
        self.cancel_model = cancel_model
        self.steps = []

    def choose(self, title, choices):
        self.steps.append(title)
        if len(self.steps) == 1:
            return "deepseek"
        data = auth.load_credentials(self.path)
        assert data["providers"]["deepseek"]["api_key"] == "new-key"
        assert data["active"] == "anthropic"
        return None if self.cancel_model else "deepseek-v4-pro"

    def secret(self, title, *, on_save):
        on_save("new-key")
        return "new-key"


def test_onboarding_saves_key_then_model_then_activates(tmp_path):
    from agent_app.onboarding import login

    path = tmp_path / "creds.json"
    auth.store_login("anthropic", "old-key", "old-model", path)
    assert login(SetupUI(path), path) == "deepseek"
    data = auth.load_credentials(path)
    assert data["active"] == "deepseek"
    assert data["providers"]["deepseek"]["model"] == "deepseek-v4-pro"


def test_cancel_model_keeps_saved_key_and_previous_active(tmp_path):
    from agent_app.onboarding import login

    path = tmp_path / "creds.json"
    auth.store_login("anthropic", "old-key", "old-model", path)
    assert login(SetupUI(path, cancel_model=True), path) is None
    data = auth.load_credentials(path)
    assert data["active"] == "anthropic"
    assert data["providers"]["deepseek"] == {"api_key": "new-key"}


def test_cancel_login_provider_menu_notifies_without_stdout(tmp_path, capsys):
    from agent_app.onboarding import login

    notices = []

    class CancelUI:
        def choose(self, title, choices):
            return None

        def notify(self, message):
            notices.append(message)

    assert login(CancelUI(), tmp_path / "creds.json") is None
    assert notices and "取消" in notices[0]
    assert capsys.readouterr().out == ""


def test_cancel_secret_after_provider_selection_notifies_before_provider_menu(
    tmp_path, capsys
):
    from agent_app.onboarding import login

    events = []
    notices = []

    class CancelSecretUI:
        def choose(self, title, choices):
            events.append("choose")
            return "deepseek" if events.count("choose") == 1 else None

        def secret(self, title, *, on_save):
            events.append("secret")
            return None

        def notify(self, message):
            events.append("notify")
            notices.append(message)

    assert login(CancelSecretUI(), tmp_path / "creds.json") is None
    assert events == ["choose", "secret", "notify", "choose", "notify"]
    assert notices == ["已取消登录。", "已取消登录。"]
    assert capsys.readouterr().out == ""
