"""Acceptance tests for provider CLI commands and REPL slash commands."""

from __future__ import annotations

import threading
import types

import pytest

import agent_app.cli as cli
from agent_app.cli import handle_slash_command, main, run_command
from agent_app.features.auth import load_credentials, store_login
from agent_app.runtime import ModelRouter


@pytest.fixture(autouse=True)
def isolated_provider_settings(monkeypatch):
    import agent_app.bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, "load_runtime_environment", dict)


class FakeClient:
    def __init__(self, **_options):
        self.messages = types.SimpleNamespace(create=lambda **_k: None, stream=None)


def fake_factory_collector(calls):
    def factory(**options):
        calls.append(options)
        return FakeClient(**options)

    return factory


def ok_verify(spec, api_key, *, anthropic_factory=None, model=None):
    return True


def bad_verify(spec, api_key, *, anthropic_factory=None, model=None):
    return False


# ---------------------------------------------------------------------------
# pamu login
# ---------------------------------------------------------------------------


def test_login_stores_credential_and_sets_active(tmp_path, capsys):
    path = tmp_path / "credentials.json"
    inputs = iter(["deepseek-reasoner"])

    code = run_command(
        ["login", "deepseek"],
        credentials_path=path,
        verify=ok_verify,
        client_factory=fake_factory_collector([]),
        input_fn=lambda _prompt: next(inputs),
        secret_fn=lambda _prompt: "sk-ds",
    )

    data = load_credentials(path)
    assert code == 0
    assert data["active"] == "deepseek"
    assert data["providers"]["deepseek"]["api_key"] == "sk-ds"
    assert data["providers"]["deepseek"]["model"] == "deepseek-reasoner"
    assert "已登录" in capsys.readouterr().out


def test_login_rejects_invalid_key(tmp_path):
    path = tmp_path / "credentials.json"

    code = run_command(
        ["login", "deepseek"],
        credentials_path=path,
        verify=bad_verify,
        client_factory=fake_factory_collector([]),
        input_fn=lambda _prompt: "",
        secret_fn=lambda _prompt: "sk-bad",
    )

    assert code == 1
    assert not path.exists()


def test_login_no_verify_flag_skips_validation(tmp_path):
    path = tmp_path / "credentials.json"
    calls = []

    code = run_command(
        ["login", "moonshot", "--no-verify"],
        credentials_path=path,
        verify=lambda *_a, **_k: calls.append("verify") or True,
        client_factory=fake_factory_collector([]),
        input_fn=lambda _prompt: "",
        secret_fn=lambda _prompt: "sk-ms",
    )

    assert code == 0
    assert calls == []
    assert load_credentials(path)["active"] == "moonshot"


def test_login_uses_env_key_without_prompting(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    secrets = []

    code = run_command(
        ["login", "deepseek"],
        credentials_path=path,
        verify=ok_verify,
        client_factory=fake_factory_collector([]),
        input_fn=lambda _prompt: "",
        secret_fn=lambda _prompt: secrets.append("prompted"),
    )

    assert code == 0
    assert secrets == []
    assert load_credentials(path)["providers"]["deepseek"]["api_key"] == "env-key"


def test_login_unknown_provider_fails(tmp_path):
    code = run_command(
        ["login", "nope"],
        credentials_path=tmp_path / "credentials.json",
        verify=ok_verify,
        client_factory=fake_factory_collector([]),
        input_fn=lambda _prompt: "",
        secret_fn=lambda _prompt: "x",
    )
    assert code == 2


def test_login_without_key_cancels(tmp_path):
    code = run_command(
        ["login", "zhipu"],
        credentials_path=tmp_path / "credentials.json",
        verify=ok_verify,
        client_factory=fake_factory_collector([]),
        input_fn=lambda _prompt: "",
        secret_fn=lambda _prompt: "   ",
    )
    assert code == 2
    assert not (tmp_path / "credentials.json").exists()


def test_login_survives_eof_on_model_prompt(tmp_path):
    """stdin 中断时应优雅取消，而不是堆栈崩溃。"""
    def eof_input(_prompt):
        raise EOFError

    code = run_command(
        ["login", "zhipu", "--no-verify"],
        credentials_path=tmp_path / "credentials.json",
        verify=ok_verify,
        client_factory=None,
        input_fn=eof_input,
        secret_fn=lambda _prompt: "sk-zp",
    )

    assert code == 0
    data = load_credentials(tmp_path / "credentials.json")
    # EOF 退化为空串，落到供应商默认模型
    assert data["providers"]["zhipu"]["model"] == "glm-4.6"


# ---------------------------------------------------------------------------
# pamu logout / status / use
# ---------------------------------------------------------------------------


def test_logout_single_and_all(tmp_path):
    path = tmp_path / "credentials.json"
    store_login("deepseek", "sk-ds", None, path)
    store_login("moonshot", "sk-ms", None, path)

    assert run_command(
        ["logout", "deepseek"], credentials_path=path, verify=ok_verify,
        client_factory=None, input_fn=input, secret_fn=lambda _p: "",
    ) == 0
    assert set(load_credentials(path)["providers"]) == {"moonshot"}

    assert run_command(
        ["logout"], credentials_path=path, verify=ok_verify,
        client_factory=None, input_fn=input, secret_fn=lambda _p: "",
    ) == 0
    assert load_credentials(path) == {"providers": {}, "active": None}


def test_status_reports_active_and_logins(tmp_path, capsys):
    path = tmp_path / "credentials.json"
    store_login("deepseek", "sk-ds", "deepseek-chat", path)

    code = run_command(
        ["status"],
        credentials_path=path,
        verify=ok_verify,
        client_factory=None,
        input_fn=input,
        secret_fn=lambda _p: "",
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "当前供应商：deepseek" in out
    assert "deepseek-chat" in out
    assert str(path) in out


def test_use_requires_login_first(tmp_path, capsys):
    path = tmp_path / "credentials.json"

    code = run_command(
        ["use", "deepseek"],
        credentials_path=path,
        verify=ok_verify,
        client_factory=None,
        input_fn=input,
        secret_fn=lambda _p: "",
    )

    assert code == 2
    assert "pamu login deepseek" in capsys.readouterr().err


def test_use_switches_persisted_default(tmp_path):
    path = tmp_path / "credentials.json"
    store_login("deepseek", "sk-ds", None, path)
    store_login("moonshot", "sk-ms", None, path)

    code = run_command(
        ["use", "moonshot"],
        credentials_path=path,
        verify=ok_verify,
        client_factory=None,
        input_fn=input,
        secret_fn=lambda _p: "",
    )

    assert code == 0
    assert load_credentials(path)["active"] == "moonshot"


# ---------------------------------------------------------------------------
# REPL slash commands
# ---------------------------------------------------------------------------


def _runtime():
    return types.SimpleNamespace(
        model=ModelRouter(
            provider="anthropic", primary_model="claude-test", llm=object()
        ),
        llm=object(),
    )


def test_run_agent_turn_keeps_slash_commands_out_of_history(monkeypatch):
    monkeypatch.setattr(cli, "run_agent_loop", lambda _runtime: None)
    runtime = types.SimpleNamespace(session=types.SimpleNamespace(history=[]))

    cli.run_agent_turn(runtime, "/model glm-4.6")
    assert runtime.session.history == []

    cli.run_agent_turn(runtime, "普通问题")
    assert runtime.session.history == [{"role": "user", "content": "普通问题"}]


def test_slash_status_shows_provider_and_model(capsys):
    handle_slash_command(_runtime(), "/status")
    out = capsys.readouterr().out
    assert "供应商：anthropic" in out
    assert "模型：claude-test" in out


def test_unknown_command_is_transient_when_ui_is_available(capsys):
    notices = []
    handle_slash_command(
        None, "/unknown", ui=types.SimpleNamespace(notify=notices.append)
    )
    assert capsys.readouterr().out == ""
    assert len(notices) == 1 and "未知命令" in notices[0]


def test_quit_requires_exact_command_and_has_no_argument(capsys):
    from agent_app.cli_exit import ExitAction

    assert handle_slash_command(None, "/quit") is ExitAction.QUIT
    assert handle_slash_command(None, "/quite") is None
    assert handle_slash_command(None, "/quit extra") is None
    assert "用法：/quit" in capsys.readouterr().out


def test_slash_model_switches_current_session_model(tmp_path, capsys):
    runtime = _runtime()

    handle_slash_command(
        runtime, "/model glm-4.6", credentials_path=tmp_path / "creds.json"
    )

    assert runtime.model.snapshot()[1] == "glm-4.6"
    assert "glm-4.6" in capsys.readouterr().out


def test_slash_model_persists_when_provider_logged_in(tmp_path):
    path = tmp_path / "credentials.json"
    store_login("zhipu", "sk-zp", "glm-4.6", path)
    runtime = _runtime()
    runtime.model.switch("zhipu", "glm-4.6", None, object())

    handle_slash_command(runtime, "/model glm-4.7", credentials_path=path)

    assert load_credentials(path)["providers"]["zhipu"]["model"] == "glm-4.7"


def test_slash_provider_switches_runtime_and_persists(tmp_path, capsys):
    path = tmp_path / "credentials.json"
    store_login("deepseek", "sk-ds", "deepseek-chat", path)
    runtime = _runtime()
    built = []

    handle_slash_command(
        runtime,
        "/provider deepseek",
        credentials_path=path,
        client_factory=fake_factory_collector(built),
    )

    assert built == [
        {
            "base_url": "https://api.deepseek.com/anthropic",
            "api_key": "sk-ds",
        }
    ]
    assert runtime.model.snapshot()[:2] == ("deepseek", "deepseek-chat")
    assert runtime.llm is runtime.model.llm
    assert load_credentials(path)["active"] == "deepseek"
    assert "DeepSeek" in capsys.readouterr().out


def test_slash_provider_requires_login(tmp_path, capsys):
    path = tmp_path / "credentials.json"

    handle_slash_command(
        _runtime(), "/provider moonshot", credentials_path=path
    )

    assert "pamu login moonshot" in capsys.readouterr().out
    assert load_credentials(path) == {"providers": {}, "active": None}


def test_slash_provider_unknown_name(tmp_path, capsys):
    handle_slash_command(
        _runtime(), "/provider nope", credentials_path=tmp_path / "creds.json"
    )
    assert "未知供应商" in capsys.readouterr().out


def test_slash_help_lists_commands(capsys):
    handle_slash_command(_runtime(), "/help")
    out = capsys.readouterr().out
    for expected in ("/status", "/model", "/provider", "/help"):
        assert expected in out


def test_slash_unknown_command_hints_help(capsys):
    handle_slash_command(_runtime(), "/wat")
    assert "/help" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main() dispatch wiring
# ---------------------------------------------------------------------------


def test_main_dispatches_commands_without_entering_repl(monkeypatch, tmp_path):
    def unexpected_factory():
        raise AssertionError("REPL must not start for account commands")

    monkeypatch.setattr(
        "builtins.input", lambda _prompt: (_ for _ in ()).throw(AssertionError())
    )
    code = main(
        runtime_factory=unexpected_factory,
        argv=["status"],
        credentials_path=tmp_path / "pamu-missing-creds.json",
        verify=ok_verify,
        client_factory=None,
    )
    assert code == 0


def test_main_routes_slash_commands_in_repl(monkeypatch, tmp_path):
    runtime = _runtime()
    runtime.stop_event = threading.Event()
    runtime.agent_lock = threading.Lock()
    runtime.hooks = types.SimpleNamespace(trigger=lambda *_a: None)
    queries = iter(["/model switched-model", "/quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(queries))

    main(
        runtime_factory=lambda: runtime,
        run_turn=lambda *_a: None,
        start_threads=lambda _r: [],
        stop_threads=lambda _r, _t: _r.stop_event.set(),
        argv=[],
        credentials_path=tmp_path / "creds.json",
    )

    assert runtime.model.snapshot()[1] == "switched-model"


def test_run_command_rejects_unknown_command(tmp_path):
    assert (
        run_command(
            ["frobnicate"],
            credentials_path=tmp_path / "none.json",
            verify=ok_verify,
            client_factory=None,
            input_fn=input,
            secret_fn=lambda _p: "",
        )
        == 2
    )


def test_slash_thinking_switches_level_and_persists(tmp_path, capsys):
    path = tmp_path / "credentials.json"
    store_login("anthropic", "sk-ant", "claude-test", path)
    runtime = _runtime()
    runtime.model.switch("anthropic", "claude-test", None, object())

    handle_slash_command(runtime, "/thinking high", credentials_path=path)

    assert runtime.model.thinking_snapshot() == "high"
    assert load_credentials(path)["providers"]["anthropic"]["thinking"] == "high"
    assert "high" in capsys.readouterr().out


def test_slash_thinking_rejects_unknown_level(tmp_path, capsys):
    runtime = _runtime()

    handle_slash_command(
        runtime, "/thinking turbo", credentials_path=tmp_path / "creds.json"
    )

    assert runtime.model.thinking_snapshot() == "off"
    assert "未知思考程度" in capsys.readouterr().out


def test_slash_thinking_without_argument_uses_menu(tmp_path):
    path = tmp_path / "credentials.json"
    store_login("anthropic", "sk-ant", "claude-test", path)
    runtime = _runtime()
    runtime.model.switch("anthropic", "claude-test", None, object())
    runtime.model.switch_thinking("low")
    seen = []

    def choose(title, choices, *, initial=None):
        seen.append((title, [key for key, _ in choices], initial))
        return "medium"

    handle_slash_command(
        runtime,
        "/thinking",
        credentials_path=path,
        ui=types.SimpleNamespace(choose=choose),
    )

    assert runtime.model.thinking_snapshot() == "medium"
    assert load_credentials(path)["providers"]["anthropic"]["thinking"] == "medium"
    assert seen == [("思考程度", ["off", "minimal", "low", "medium", "high"], "low")]


def test_slash_status_shows_thinking_level(capsys):
    runtime = _runtime()
    runtime.model.switch_thinking("high")

    handle_slash_command(runtime, "/status")

    assert "思考程度：high" in capsys.readouterr().out
