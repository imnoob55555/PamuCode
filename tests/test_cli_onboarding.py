import threading
from types import SimpleNamespace
import pytest

from agent_app import cli
from agent_app.features import auth
from agent_app.runtime import ModelRouter


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    import agent_app.bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "load_runtime_environment", lambda: {})


class ShellUI:
    def __init__(self, queries):
        self.queries = iter(queries)

    def prompt(self):
        return next(self.queries)


def runtime():
    return SimpleNamespace(
        stop_event=threading.Event(),
        agent_lock=threading.Lock(),
        hooks=SimpleNamespace(trigger=lambda *args: None),
        model=ModelRouter(provider="deepseek", primary_model="selected", llm=object()),
    )


def test_first_launch_accepts_help_and_prompts_login_without_runtime(tmp_path, capsys):
    calls = []
    code = cli.main(
        runtime_factory=lambda: calls.append("build"),
        argv=[],
        ui=ShellUI(["hello", "/help", "/status", "/quit"]),
        configuration_check=lambda: False,
        credentials_path=tmp_path / "credentials.json",
        start_threads=lambda r: calls.append("start"),
        stop_threads=lambda r, t: calls.append("stop"),
    )
    assert code is None
    assert calls == []
    assert "/login" in capsys.readouterr().out


def test_login_completes_before_runtime_and_threads_start(tmp_path, monkeypatch):
    from agent_app import onboarding

    path = tmp_path / "credentials.json"
    events = []

    def login(ui, credentials, provider=None):
        events.append("login")
        auth.store_login("deepseek", "key", "chosen", credentials)
        return "deepseek"

    def build():
        assert auth.load_credentials(path)["providers"]["deepseek"]["model"] == "chosen"
        events.append("build")
        return runtime()

    monkeypatch.setattr(onboarding, "login", login)
    cli.main(
        runtime_factory=build,
        argv=[],
        ui=ShellUI(["/login", "hello", "/quit"]),
        configuration_check=lambda: False,
        credentials_path=path,
        start_threads=lambda r: events.append("start") or [],
        run_turn=lambda r, q: events.append(q),
        stop_threads=lambda r, t: events.append("stop"),
    )
    assert events == ["login", "build", "start", "hello", "stop"]


def test_cancel_login_stays_in_unconfigured_shell(tmp_path, monkeypatch):
    from agent_app import onboarding

    monkeypatch.setattr(onboarding, "login", lambda *args: None)
    events = []
    cli.main(
        runtime_factory=lambda: events.append("build"),
        argv=[],
        ui=ShellUI(["/login", "/quit"]),
        configuration_check=lambda: False,
        credentials_path=tmp_path / "credentials.json",
    )
    assert events == []


def test_slash_login_always_starts_with_provider_menu(tmp_path, monkeypatch):
    from agent_app import onboarding

    path = tmp_path / "credentials.json"
    ui = ShellUI([])
    calls = []

    def login(seen_ui, seen_path):
        calls.append((seen_ui, seen_path))

    monkeypatch.setattr(onboarding, "login", login)
    cli.handle_slash_command(None, "/login", credentials_path=path, ui=ui)
    assert calls == [(ui, path)]


def test_existing_local_record_needs_no_online_verification(tmp_path, monkeypatch):
    import agent_app.bootstrap as bootstrap

    path = tmp_path / "credentials.json"
    auth.store_login("deepseek", "unverified-key", "chosen", path)
    monkeypatch.setattr(bootstrap, "load_runtime_environment", lambda: {})
    assert bootstrap.has_local_configuration(path)
    path.write_text('{"providers":{"deepseek":{"api_key":" "}}}')
    assert not bootstrap.has_local_configuration(path)


def test_fresh_settings_template_still_prompts_for_login(tmp_path, monkeypatch):
    from pathlib import Path
    from dotenv import dotenv_values
    import agent_app.bootstrap as bootstrap

    example = Path(__file__).resolve().parents[1] / ".settings.example"
    monkeypatch.setattr(
        bootstrap, "load_runtime_environment", lambda: dotenv_values(example)
    )
    assert not bootstrap.has_local_configuration(tmp_path / "missing-credentials.json")


def test_model_menu_updates_current_provider_not_stale_active(tmp_path):
    path = tmp_path / "credentials.json"
    auth.store_login("deepseek", "key", "old-model", path)
    auth.store_login("anthropic", "key", "keep-model", path)
    ui = SimpleNamespace(choose=lambda title, choices: "deepseek-v4-pro")
    r = runtime()
    cli.handle_slash_command(r, "/model", credentials_path=path, ui=ui)
    assert r.model.snapshot()[1] == "deepseek-v4-pro"
    records = auth.load_credentials(path)["providers"]
    assert records["deepseek"]["model"] == "deepseek-v4-pro"
    assert records["anthropic"]["model"] == "keep-model"


def test_unconfigured_status_tolerates_malformed_records(tmp_path, capsys):
    path = tmp_path / "credentials.json"
    path.write_text('{"providers":{"deepseek":[]}}')
    cli.handle_slash_command(None, "/status", credentials_path=path)
    assert "/login" in capsys.readouterr().out


def test_hot_switch_honors_same_provider_settings_key(tmp_path, monkeypatch):
    import agent_app.bootstrap as bootstrap

    path = tmp_path / "credentials.json"
    auth.store_login("deepseek", "stored-key", "chosen", path)
    monkeypatch.setattr(
        bootstrap,
        "load_runtime_environment",
        lambda: {
            "DEEPSEEK_API_KEY": "explicit-key",
        },
    )
    options = []
    cli.handle_slash_command(
        runtime(),
        "/provider deepseek",
        credentials_path=path,
        client_factory=lambda **kw: options.append(kw) or object(),
    )
    assert options[0]["api_key"] == "explicit-key"


def test_hot_anthropic_switch_preserves_custom_url_and_token(tmp_path, monkeypatch):
    import agent_app.bootstrap as bootstrap

    path = tmp_path / "credentials.json"
    auth.store_login("anthropic", "saved-key", "chosen", path)
    monkeypatch.setattr(
        bootstrap,
        "load_runtime_environment",
        lambda: {
            "ANTHROPIC_BASE_URL": "https://gateway.invalid",
            "ANTHROPIC_AUTH_TOKEN": "gateway-token",
        },
    )
    options = []
    cli.handle_slash_command(
        runtime(),
        "/provider anthropic",
        credentials_path=path,
        client_factory=lambda **kw: options.append(kw) or object(),
    )
    assert options == [
        {
            "base_url": "https://gateway.invalid",
            "api_key": "saved-key",
            "auth_token": "gateway-token",
        }
    ]


def test_provider_menu_switches_selected_provider_and_saved_model(tmp_path):
    path = tmp_path / "credentials.json"
    auth.store_login("anthropic", "fake-anthropic", "chosen-claude", path)
    auth.store_login("deepseek", "fake-deepseek", "chosen-deepseek", path)
    menus = []

    def choose(title, choices):
        menus.append((title, choices))
        assert choices[0][0] == "deepseek"
        assert "当前" in choices[0][1]
        assert "未配置" in dict(choices)["moonshot"]
        return "anthropic"

    r = runtime()
    options = []
    cli.handle_slash_command(
        r,
        "/provider",
        credentials_path=path,
        ui=SimpleNamespace(choose=choose),
        client_factory=lambda **kw: options.append(kw) or object(),
    )
    assert len(menus) == 1
    assert r.model.snapshot()[:2] == ("anthropic", "chosen-claude")
    assert options[0]["api_key"] == "fake-anthropic"
    assert auth.load_credentials(path)["active"] == "anthropic"


@pytest.mark.parametrize("command", ["/model", "/provider"])
def test_cancel_switch_menu_notifies_without_persistent_output(tmp_path, command, capsys):
    path = tmp_path / "credentials.json"
    auth.store_login("deepseek", "fake-key", "selected", path)
    before = path.read_bytes()
    r = runtime()
    selections = []
    notices = []

    def choose(title, choices):
        selections.append(title)
        return None

    cli.handle_slash_command(
        r,
        command,
        credentials_path=path,
        ui=SimpleNamespace(choose=choose, notify=notices.append),
    )
    assert len(selections) == 1
    assert r.model.snapshot()[:2] == ("deepseek", "selected")
    assert path.read_bytes() == before
    assert len(notices) == 1 and "取消" in notices[0]
    assert capsys.readouterr().out == ""


def test_provider_menu_unconfigured_option_prompts_login(tmp_path, capsys):
    path = tmp_path / "credentials.json"
    auth.store_login("deepseek", "fake-key", "selected", path)
    before = path.read_bytes()
    r = runtime()
    cli.handle_slash_command(
        r,
        "/provider",
        credentials_path=path,
        ui=SimpleNamespace(choose=lambda *args: "moonshot"),
        client_factory=lambda **kw: pytest.fail("unconfigured client built"),
    )
    assert "/login" in capsys.readouterr().out
    assert r.model.snapshot()[:2] == ("deepseek", "selected")
    assert path.read_bytes() == before
