import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_app.cli_exit import ExitGesture
from agent_app.terminal import TerminalUI
from agent_app.cli_exit import QuitRequested


@pytest.mark.parametrize("delta, expected", [(0.2, True), (1.0, True), (1.01, False)])
def test_double_interrupt_window(delta, expected):
    ticks = iter([10.0, 10.0 + delta])
    gesture = ExitGesture(clock=lambda: next(ticks))
    assert gesture.press() is False
    assert gesture.press() is expected


def test_other_input_disarms_exit():
    gesture = ExitGesture(clock=lambda: 10.0)
    assert not gesture.press()
    gesture.reset()
    assert not gesture.press()


def test_first_interrupt_clears_draft_and_accepts_next_query():
    with create_pipe_input() as pipe:
        ui = TerminalUI(input=pipe, output=DummyOutput())
        ui.repl_mode = True
        pipe.send_text("draft\x03hello\r")
        assert ui.prompt() == "hello"
    assert ui.history.get_strings() == ["hello"]


def test_double_interrupt_exits_without_history():
    with create_pipe_input() as pipe:
        ui = TerminalUI(input=pipe, output=DummyOutput())
        ui.repl_mode = True
        pipe.send_text("\x03\x03")
        with pytest.raises(QuitRequested):
            ui.prompt()
    assert ui.history.get_strings() == []


def test_read_first_ctrl_c_returns_to_prompt_and_second_raises_quit():
    with create_pipe_input() as pipe:
        ui = TerminalUI(input=pipe, output=DummyOutput())
        ui.repl_mode = True
        pipe.send_text("\x03")
        assert ui.text("Model ID") is None
        pipe.send_text("\x03")
        with pytest.raises(QuitRequested):
            ui.prompt()
