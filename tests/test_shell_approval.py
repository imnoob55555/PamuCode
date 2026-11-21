import threading
from types import SimpleNamespace

import pytest

from agent_app import cli_progress
from agent_app.config import AppConfig
from agent_app.tools.hooks import make_permission_hook


def bash(command, **extra):
    return SimpleNamespace(name="bash", input={"command": command}, **extra)


class Answers:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)


def test_read_only_commands_skip_confirmation(tmp_path):
    answers = Answers()
    hook = make_permission_hook(tmp_path, answers)
    assert hook(bash("git status && ls")) is None
    assert answers.prompts == []


def test_unknown_commands_ask_and_respect_the_answer(tmp_path, capsys):
    answers = Answers("n", "y")
    hook = make_permission_hook(tmp_path, answers)
    assert hook(bash("git push --force")) == "Permission denied by user"
    assert hook(bash("git push --force")) is None
    assert answers.prompts == ["  Allow this change? [y/N] "] * 2
    out = capsys.readouterr().out
    assert "Command needs approval" in out and "Command: git push --force" in out
    assert "Reason: git push" in out and "a = 本会话不再询问 git push" in out


def test_always_answer_remembers_the_command_prefix_for_this_hook_only(tmp_path):
    answers = Answers("a")
    hook = make_permission_hook(tmp_path, answers)
    assert hook(bash("uv run pytest -q")) is None
    assert hook(bash("uv run pytest tests/test_x.py")) is None
    assert answers.prompts == ["  Allow this change? [y/N] "]
    other = Answers("n")
    assert make_permission_hook(tmp_path, other)(bash("uv run pytest")) == "Permission denied by user"


def test_writes_through_redirection_are_never_remembered(tmp_path):
    answers = Answers("a", "n")
    hook = make_permission_hook(tmp_path, answers)
    assert hook(bash("echo x > out.txt")) is None
    assert hook(bash("echo x > out.txt")) == "Permission denied by user"


def test_denied_commands_never_reach_confirmation_even_in_auto_mode(tmp_path, capsys):
    answers = Answers()
    for approval in ("ask", "auto"):
        hook = make_permission_hook(tmp_path, answers, shell_approval=approval)
        assert hook(bash("s\\udo rm -rf /tmp/x")) == "Permission denied by deny list"
    assert answers.prompts == []
    assert "Blocked: sudo is never allowed" in capsys.readouterr().out


def test_auto_mode_runs_unknown_commands_without_asking(tmp_path):
    answers = Answers()
    assert make_permission_hook(tmp_path, answers, shell_approval="auto")(bash("touch a")) is None
    assert answers.prompts == []


def test_teammate_requests_name_the_teammate(tmp_path):
    answers = Answers("y")
    hook = make_permission_hook(tmp_path, answers)
    assert hook(bash("touch a", agent="worker")) is None
    assert answers.prompts == ["  Allow teammate 'worker' to apply this change? [y/N] "]


def test_background_threads_are_refused_without_reading_stdin(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("background thread read stdin"))
    results = []
    worker = threading.Thread(target=lambda: results.append(cli_progress.terminal_confirmation("ok? ")))
    worker.start()
    worker.join(2)
    assert results == [""]
    assert cli_progress.BACKGROUND_APPROVAL_DENIED in capsys.readouterr().out


def test_unreadable_stdin_counts_as_refusal(monkeypatch):
    def unreadable(_prompt):
        raise OSError("stdin closed")

    monkeypatch.setattr("builtins.input", unreadable)
    assert cli_progress.terminal_confirmation("ok? ") == ""


def test_shell_approval_setting_is_validated(tmp_path):
    assert AppConfig.from_env(tmp_path, environ={"MODEL_ID": "m"}).shell_approval == "ask"
    assert AppConfig.from_env(tmp_path, environ={"MODEL_ID": "m", "PAMU_SHELL_APPROVAL": "auto"}).shell_approval == "auto"
    with pytest.raises(ValueError, match="PAMU_SHELL_APPROVAL"):
        AppConfig.from_env(tmp_path, environ={"MODEL_ID": "m", "PAMU_SHELL_APPROVAL": "yes"})


def test_limit_settings_must_be_positive_integers(tmp_path):
    config = AppConfig.from_env(tmp_path, environ={"MODEL_ID": "m", "PAMU_MAX_TURN_MODEL_CALLS": "7"})
    assert (config.max_turn_model_calls, config.max_turn_tool_calls, config.max_teammates) == (7, 200, 4)
    for value in ("0", "-1", "many"):
        with pytest.raises(ValueError, match="PAMU_MAX_TEAMMATES"):
            AppConfig.from_env(tmp_path, environ={"MODEL_ID": "m", "PAMU_MAX_TEAMMATES": value})
