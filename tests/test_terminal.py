import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_app import terminal
from agent_app.slash_commands import match_commands


def test_slash_matches_names_descriptions_and_hides_for_normal_text():
    assert {item.name for item in match_commands("/")} == {
        "/login",
        "/status",
        "/model",
        "/provider",
        "/help",
        "/quit",
    }
    assert [item.name for item in match_commands(" /LOG")] == ["/login"]
    assert "/model" in [item.name for item in match_commands("/模型")]
    assert [item.name for item in match_commands("/model some-id")] == ["/model"]
    assert match_commands("hello /model") == []
    assert match_commands("/unknown") == []


@pytest.mark.parametrize(
    "keys,expected",
    [
        ("\x1b[B\r", "second"),
        ("\x1b[B\x1b[A\r", "first"),
        ("\x03", None),
        ("\x1b", None),
    ],
)
def test_choice_uses_arrows_enter_and_cancel(keys, expected):
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        assert (
            ui.choose("provider", [("first", "First"), ("second", "Second")])
            == expected
        )


def test_secret_enter_does_not_save_ctrl_s_rejects_empty_then_saves():
    saved = []
    with create_pipe_input() as pipe:
        pipe.send_text("\x13\rsecret-value\r\x13")
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        assert ui.secret("API Key", on_save=saved.append) == "secret-value"
    assert saved == ["secret-value"]


def test_secret_retries_write_failure_without_leaking_error(capsys):
    saved = []

    def save(value):
        saved.append(value)
        if len(saved) == 1:
            raise OSError(f"failure with {value}")

    with create_pipe_input() as pipe:
        pipe.send_text("secret-value\x13\x13")
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        assert ui.secret("API Key", on_save=save) == "secret-value"
    assert len(saved) == 2
    assert "secret-value" not in capsys.readouterr().out


def test_ctrl_c_aborts_key_entry_instead_of_going_back():
    with create_pipe_input() as pipe:
        pipe.send_text("partial-key\x03")
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        with pytest.raises(KeyboardInterrupt):
            ui.secret("API Key", on_save=lambda key: pytest.fail("canceled key saved"))


@pytest.mark.parametrize(
    "keys,expected",
    [
        ("/log\t\r", "/login"),
        ("/\r", "/login"),
        ("/\x1b[B\r", "/status"),
        ("/\x1b[A\r", "/quit"),
        ("/\x1b[B\x1b[B\x1b[B\r", "/provider"),
        ("/\x1b[B\t\r", "/status"),
        ("/\x1b[A\t\r", "/quit"),
        ("/mo\r", "/model"),
        ("/mo custom-model\r", "/model"),
        ("/prov deepseek\r", "/provider"),
        ("/unknown\r", "/unknown"),
        ("hello\r", "hello"),
        ("/log\x7f\x7f\x7fhelp\r", "/help"),
    ],
)
def test_prompt_enter_executes_highlighted_menu_command(
    keys, expected
):
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        assert ui.prompt() == expected


def test_prompt_keeps_slash_commands_out_of_input_history():
    with create_pipe_input() as pipe:
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        pipe.send_text("/model custom\r")
        assert ui.prompt() == "/model"
        pipe.send_text("普通问题\r")
        assert ui.prompt() == "普通问题"
    assert ui.history.get_strings() == ["普通问题"]


@pytest.mark.parametrize("command", ["/login", "/model", "/provider"])
def test_menu_command_suggestion_has_no_parameter(command):
    assert match_commands(command)[0].usage == command


def test_slash_hints_redraw_below_input_as_keyword_changes(monkeypatch):
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import to_formatted_text
    from prompt_toolkit.layout.controls import FormattedTextControl

    original_run = Application.run
    snapshots = []
    steps = iter([("", "/"), ("/", "log"), ("/log", "\x7f\x7f\x7f"), ("/", "help\r")])
    expected, keys = next(steps)
    with create_pipe_input() as pipe:

        def run(app):
            def after_render(_):
                nonlocal expected, keys
                if app.is_done or app.current_buffer.text != expected:
                    return
                windows = app.layout.visible_windows
                assert windows[0].content.buffer is app.current_buffer
                rows = [
                    "".join(t[1] for t in to_formatted_text(window.content.text))
                    for window in windows[1:]
                    if isinstance(window.content, FormattedTextControl)
                ]
                snapshots.append((expected, "\n".join(rows)))
                pipe.send_text(keys)
                expected, keys = next(steps, (None, ""))

            app.after_render += after_render
            return original_run(app)

        monkeypatch.setattr(Application, "run", run)
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        assert ui.prompt() == "/help"
    all_hints = snapshots[1][1]
    filtered = snapshots[2][1]
    assert "/status" in all_hints and "显示命令帮助" in all_hints
    assert "/login" in filtered and "配置 API Key 并选择模型" in filtered
    assert "/status" not in filtered and "/help" not in filtered
    assert "/status" in snapshots[3][1]  # Backspace restores the full list.
