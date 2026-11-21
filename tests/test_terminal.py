import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_app import terminal
from agent_app.slash_commands import match_commands


def _buffer_index(app):
    from prompt_toolkit.layout.controls import BufferControl

    return next(
        index for index, window in enumerate(app.layout.visible_windows)
        if isinstance(window.content, BufferControl)
    )


def _plain(window):
    from prompt_toolkit.formatted_text import to_formatted_text
    from prompt_toolkit.layout.controls import FormattedTextControl

    if not isinstance(window.content, FormattedTextControl):
        return None
    return "".join(part[1] for part in to_formatted_text(window.content.text))


def test_slash_matches_names_descriptions_and_hides_for_normal_text():
    assert {item.name for item in match_commands("/")} == {
        "/login",
        "/status",
        "/model",
        "/provider",
        "/thinking",
        "/resume",
        "/delete",
        "/background",
        "/cron",
        "/stop",
        "/context",
        "/compact",
        "/help",
        "/quit",
        "/plan",
        "/execute",
        "/skill",
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


def test_prompt_saves_slash_commands_in_input_history():
    with create_pipe_input() as pipe:
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        pipe.send_text("/model custom\r")
        assert ui.prompt() == "/model"
        pipe.send_text("普通问题\r")
        assert ui.prompt() == "普通问题"
    assert ui.history.get_strings() == ["/model", "普通问题"]


def test_arrows_recall_slash_commands_with_arguments_among_user_messages():
    with create_pipe_input() as pipe:
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        for message in ('/status', '/context window 64000', '普通问题'):
            pipe.send_text(message + '\r')
            assert ui.prompt() == message
        pipe.send_text('\x1b[A\x1b[A\r')
        assert ui.prompt() == '/context window 64000'
        # A recalled slash command must remain in history-navigation mode.
        # Otherwise the second Up selects a slash-menu item instead.
        pipe.send_text('草稿\x1b[A\x1b[A\x1b[A\x1b[A\x1b[B\r')
        assert ui.prompt() == '/context window 64000'


def test_up_recalls_previous_message_even_with_unrelated_draft():
    with create_pipe_input() as pipe:
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        pipe.send_text('上一条用户消息\r')
        assert ui.prompt() == '上一条用户消息'
        pipe.send_text('新草稿\x1b[A\r')
        assert ui.prompt() == '上一条用户消息'


def test_up_down_browse_user_messages_and_restore_draft():
    with create_pipe_input() as pipe:
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        ui.history.append_string('第一条')
        ui.history.append_string('第二条')
        pipe.send_text('草稿\x1b[A\x1b[A\x1b[B\x1b[B\r')
        assert ui.prompt() == '草稿'


def test_up_recalls_user_message_from_multiline_pasted_draft():
    with create_pipe_input() as pipe:
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        ui.history.append_string('上一条用户消息')
        pipe.send_text('\x1b[200~第一行草稿\n第二行草稿\x1b[201~\x1b[A\r')
        assert ui.prompt() == '上一条用户消息'


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
                position = _buffer_index(app)
                assert windows[position].content.buffer is app.current_buffer
                rows = [
                    "".join(t[1] for t in to_formatted_text(window.content.text))
                    for window in windows[position + 1:]
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
    assert "/login" in filtered and "配置模型服务与认证" in filtered
    assert "/status" not in filtered and "/help" not in filtered
    assert "/status" in snapshots[3][1]  # Backspace restores the full list.


def test_notice_above_input_expires_without_keystrokes(monkeypatch, capsys):
    import asyncio
    from time import monotonic

    from prompt_toolkit.application import Application

    original_run = Application.run
    rendered_at = []
    elapsed = []
    with create_pipe_input() as pipe:
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        ui.notify("已取消模型选择。")

        def run(app):
            watchdog = None

            def after_render(_):
                nonlocal watchdog
                if app.is_done:
                    return
                windows = app.layout.visible_windows
                texts = [_plain(window) for window in windows]
                if not rendered_at:
                    assert texts.index("已取消模型选择。") < _buffer_index(app)
                    rendered_at.append(monotonic())
                    watchdog = asyncio.get_running_loop().call_later(
                        5, lambda: app.exit(exception=AssertionError("notice did not expire"))
                    )
                elif not ui.notice and not elapsed:
                    elapsed.append(monotonic() - rendered_at[0])
                    assert "已取消模型选择。" not in texts
                    pipe.send_text("hello\r")

            app.after_render += after_render
            try:
                return original_run(app)
            finally:
                if watchdog is not None:
                    watchdog.cancel()

        monkeypatch.setattr(Application, "run", run)
        assert ui.prompt() == "hello"

    assert len(elapsed) == 1 and 2.8 <= elapsed[0] < 4.5
    assert ui.history.get_strings() == ["hello"]
    assert ui._notice_timer is None and ui._active_app is None
    assert capsys.readouterr().out == ""


def test_new_notice_restarts_timer_and_typing_keeps_notice(monkeypatch):
    import asyncio

    from prompt_toolkit.application import Application

    original_run = Application.run
    monkeypatch.setattr(terminal, "NOTICE_SECONDS", 0.12)
    with create_pipe_input() as pipe:
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        timers = []
        observed = []

        def run(app):
            handles = []

            def after_render(_):
                if app.is_done or timers:
                    return
                # Start both deadlines after UI construction/rendering, so
                # startup latency cannot expire the first notice prematurely.
                ui.notify("first")
                timers.append(ui._notice_timer)
                loop = asyncio.get_running_loop()
                handles.append(loop.call_later(0.06, lambda: ui.notify("second")))

                def type_before_expiry():
                    observed.append(ui.notice)
                    pipe.send_text("draft")

                def check_after_typing():
                    observed.append((ui.notice, app.current_buffer.text))

                def submit_after_expiry():
                    observed.append(ui.notice)
                    pipe.send_text("\r")

                handles.append(loop.call_later(0.14, type_before_expiry))
                handles.append(loop.call_later(0.16, check_after_typing))
                handles.append(loop.call_later(0.24, submit_after_expiry))

            app.after_render += after_render
            try:
                return original_run(app)
            finally:
                for handle in handles:
                    handle.cancel()

        monkeypatch.setattr(Application, "run", run)
        assert ui.prompt() == "draft"
    assert timers[0].cancelled()
    assert observed == ["second", ("second", "draft"), ""]
    assert ui.notice == ""


def test_notice_timer_is_cancelled_when_prompt_exits(monkeypatch):
    from prompt_toolkit.application import Application

    original_run = Application.run
    timers = []
    with create_pipe_input() as pipe:
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        ui.notify("temporary")

        def run(app):
            def after_render(_):
                if not app.is_done and not timers:
                    timers.append(ui._notice_timer)
                    pipe.send_text("\x04")

            app.after_render += after_render
            return original_run(app)

        monkeypatch.setattr(Application, "run", run)
        with pytest.raises(EOFError):
            ui.prompt()
    assert timers[0].cancelled()
    assert ui._notice_timer is None and ui._active_app is None


def test_prompt_mention_enter_completes_without_submitting(tmp_path):
    (tmp_path / "terminal.py").write_text("")
    with create_pipe_input() as pipe:
        pipe.send_text("@terminal\r\r")
        ui = terminal.TerminalUI(workdir=tmp_path, input=pipe, output=DummyOutput())
        assert ui.prompt() == "@terminal.py "


def test_prompt_mention_arrow_down_selects_second_match(tmp_path):
    (tmp_path / "terminal.py").write_text("")
    (tmp_path / "terminal_extra.py").write_text("")
    with create_pipe_input() as pipe:
        pipe.send_text("@terminal\x1b[B\r\r")
        ui = terminal.TerminalUI(workdir=tmp_path, input=pipe, output=DummyOutput())
        assert ui.prompt() == "@terminal_extra.py "


def test_prompt_mention_without_workdir_stays_plain_text():
    with create_pipe_input() as pipe:
        pipe.send_text("@terminal\r")
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        assert ui.prompt() == "@terminal"


def test_prompt_mention_ignores_email_addresses(tmp_path):
    (tmp_path / "example.com").write_text("")
    with create_pipe_input() as pipe:
        pipe.send_text("user@example.com\r\r")
        ui = terminal.TerminalUI(workdir=tmp_path, input=pipe, output=DummyOutput())
        assert ui.prompt() == "user@example.com"


def test_prompt_mention_escape_hides_menu(monkeypatch, tmp_path):
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import to_formatted_text
    from prompt_toolkit.layout.controls import FormattedTextControl

    (tmp_path / "terminal.py").write_text("")
    original_run = Application.run
    snapshots = []

    def menu_text(app):
        rows = []
        for window in app.layout.visible_windows[_buffer_index(app) + 1:]:
            if isinstance(window.content, FormattedTextControl) and window.style != "class:muted":
                rows.append(
                    "".join(
                        part[1] for part in to_formatted_text(window.content.text)
                    )
                )
        return "\n".join(rows)

    with create_pipe_input() as pipe:
        pipe.send_text("@terminal")

        def run(app):
            def after_render(_):
                if app.is_done or app.current_buffer.text != "@terminal":
                    return
                text = menu_text(app)
                if not snapshots:
                    snapshots.append(text)
                    pipe.send_text("\x1b")
                elif text == "" and snapshots[-1] != "":
                    snapshots.append(text)
                    pipe.send_text("\r")

            app.after_render += after_render
            return original_run(app)

        monkeypatch.setattr(Application, "run", run)
        ui = terminal.TerminalUI(workdir=tmp_path, input=pipe, output=DummyOutput())
        assert ui.prompt() == "@terminal"

    assert "terminal.py" in snapshots[0]
    assert snapshots[-1] == ""


def test_prompt_mention_quotes_paths_with_spaces(tmp_path):
    (tmp_path / "my file.md").write_text("")
    with create_pipe_input() as pipe:
        pipe.send_text("@my\r\r")
        ui = terminal.TerminalUI(workdir=tmp_path, input=pipe, output=DummyOutput())
        assert ui.prompt() == '@"my file.md" '


def test_prompt_mention_at_sign_lists_current_directory(tmp_path):
    (tmp_path / "alpha.py").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "beta.py").write_text("")
    with create_pipe_input() as pipe:
        pipe.send_text("@\r\r")
        ui = terminal.TerminalUI(workdir=tmp_path, input=pipe, output=DummyOutput())
        assert ui.prompt() == "@alpha.py "


def test_prompt_mention_directory_completion_drills_down(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "beta.py").write_text("")
    with create_pipe_input() as pipe:
        pipe.send_text("@sub\r\r\r")
        ui = terminal.TerminalUI(workdir=tmp_path, input=pipe, output=DummyOutput())
        assert ui.prompt() == "@sub/beta.py "


@pytest.mark.parametrize("level", ["off", "minimal", "low", "medium", "high"])
def test_prompt_border_color_follows_thinking_level(monkeypatch, level):
    from prompt_toolkit.application import Application

    original_run = Application.run
    borders = []
    with create_pipe_input() as pipe:

        def run(app):
            def after_render(_):
                if app.is_done or borders:
                    return
                borders.extend(
                    (window.char, window.style)
                    for window in app.layout.visible_windows
                    if window.char == "─"
                )
                pipe.send_text("hello\r")

            app.after_render += after_render
            return original_run(app)

        monkeypatch.setattr(Application, "run", run)
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        ui.thinking_level = level
        assert ui.prompt() == "hello"

    style = f"class:thinking-{level}"
    assert borders == [("─", style), ("─", style)]


def test_choice_honours_initial_selection():
    with create_pipe_input() as pipe:
        pipe.send_text("\r")
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        assert (
            ui.choose(
                "provider",
                [("first", "First"), ("second", "Second")],
                initial="second",
            )
            == "second"
        )


def test_prompt_puts_model_status_below_the_bottom_rule(monkeypatch):
    from prompt_toolkit.application import Application
    from prompt_toolkit.layout.containers import WindowAlign
    from prompt_toolkit.layout.controls import DummyControl

    original_run = Application.run
    captured = []
    with create_pipe_input() as pipe:
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        ui.model_name = "claude-sonnet-4-6"
        ui.thinking_level = "low"

        def run(app):
            def after_render(_):
                if app.is_done or captured:
                    return
                windows = app.layout.visible_windows
                statuses = [w for w in windows if w.style == "class:muted"
                            and _plain(w) == ui.status_text()]
                assert len(statuses) == 1
                status = statuses[0]
                assert status.align == WindowAlign.RIGHT
                assert _plain(status) == "claude-sonnet-4-6 · low · ⏵ execute"
                assert windows.index(status) > [w.char for w in windows].index("╯")
                rows = [w for w in windows if not isinstance(w.content, DummyControl)]
                assert rows[-1] is status
                captured.append(status)
                pipe.send_text("hello\r")

            app.after_render += after_render
            return original_run(app)

        monkeypatch.setattr(Application, "run", run)
        assert ui.prompt() == "hello"
    assert captured


def _plain_rows(rendered: str) -> list[str]:
    """Rebuild logical rows from PlainTextOutput's carriage-return fragments."""
    rows = []
    for chunk in rendered.split("\r\n"):
        parts = chunk.split("\r")
        rows.append(parts[0] + "".join(part.lstrip(" ") for part in parts[1:]))
    return rows


@pytest.mark.parametrize("model", ["", "test-model"])
def test_workspace_status_below_input_with_or_without_model(monkeypatch, tmp_path, model):
    from prompt_toolkit.application import Application

    original_run = Application.run
    workdir = tmp_path / "项目 with spaces" / ("long-directory-" * 8)
    captured = []
    with create_pipe_input() as pipe:
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput(), workdir=workdir)
        ui.model_name = model

        def run(app):
            def after_render(_):
                if app.is_done or captured:
                    return
                windows = app.layout.visible_windows
                directory = next(w for w in windows if _plain(w) == str(workdir))
                index = windows.index(directory)
                assert [w.char for w in windows[:index]].count("╯") == 1
                assert not directory.wrap_lines()
                if model:
                    status = next(w for w in windows if _plain(w) == ui.status_text())
                    positions = app.renderer.last_rendered_screen.visible_windows_to_write_positions
                    assert positions[directory].ypos == positions[status].ypos
                captured.append(directory)
                pipe.send_text("hello\r")

            app.after_render += after_render
            return original_run(app)

        monkeypatch.setattr(Application, "run", run)
        assert ui.prompt() == "hello"
    assert captured


def test_prompt_flushes_model_status_to_the_right_edge():
    import io

    from prompt_toolkit.output.plain_text import PlainTextOutput

    rendered = io.StringIO()
    output = PlainTextOutput(rendered)
    with create_pipe_input() as pipe:
        ui = terminal.TerminalUI(input=pipe, output=output)
        ui.model_name = "claude-sonnet-4-6"
        ui.thinking_level = "high"
        ui.context_used = 37
        pipe.send_text("hello\r")
        assert ui.prompt() == "hello"
    rows = _plain_rows(rendered.getvalue())
    status = next(row for row in rows if "claude-sonnet-4-6" in row)
    assert status.strip() == "Context 37% · claude-sonnet-4-6 · high · ⏵ execute"
    assert len(status.rstrip()) == output.get_size().columns - 2
    assert any(
        rows[index + 1] == status
        for index, row in enumerate(rows[:-1])
        if row.startswith("╰") and row.endswith("╯")
    )
