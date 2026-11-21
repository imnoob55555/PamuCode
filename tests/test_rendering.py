import io

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_app import cli_progress, rendering, terminal, theme
from agent_app.slash_commands import SLASH_COMMANDS


class TTY(io.StringIO):
    def isatty(self):
        return True


def attach_tty(monkeypatch):
    """Point stdout at a fake terminal; pytest re-captures stdout per test phase."""
    stream = TTY()
    monkeypatch.setattr("sys.stdout", stream)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "40")
    monkeypatch.setenv("LINES", "24")
    monkeypatch.setattr(cli_progress, "_model_writer", cli_progress._ModelTextWriter())
    return stream


def plain(text):
    return text.plain


def test_markdown_lines_style_blocks_and_track_code_fences():
    lines = rendering.MarkdownLines()
    assert plain(lines.style("## 计划")) == "计划"
    assert plain(lines.style("- 查看 `cli.py` 和 **重点**")) == "• 查看 cli.py 和 重点"
    assert plain(lines.style("  - 嵌套")) == "  ◦ 嵌套"
    assert plain(lines.style("- [x] 完成")) == "☑ 完成"
    assert plain(lines.style("> 引用")) == "▎ 引用"
    assert plain(lines.style("[文档](https://example.com)")) == "文档 (https://example.com)"
    assert plain(lines.style("snake_case_name stays")) == "snake_case_name stays"
    assert plain(lines.style("```python")) == "```python"
    assert plain(lines.style("# not a heading in code")) == "# not a heading in code"
    assert plain(lines.style("```")) == "```"
    assert plain(lines.style("# Heading")) == "Heading"


def test_wrapped_rows_count_wide_characters_and_pending_wrap():
    assert rendering.wrapped_rows(2, "abc", 10) == 1
    assert rendering.wrapped_rows(2, "a" * 8, 10) == 1
    assert rendering.wrapped_rows(2, "a" * 9, 10) == 2
    assert rendering.wrapped_rows(0, "中" * 5 + "a", 10) == 2
    assert rendering.wrapped_rows(1, "中" * 5, 10) == 2


def test_streamed_model_text_is_bulleted_and_restyled_per_line(monkeypatch):
    tty = attach_tty(monkeypatch)
    cli_progress.write_model_text("# Ti")
    cli_progress.write_model_text("tle\nhello **b")
    assert cli_progress._model_line_open
    cli_progress.write_model_text("old**\n\n")
    cli_progress.write_model_text("tail")
    cli_progress.finish_model_text()
    assert not cli_progress._model_line_open
    assert tty.getvalue() == (
        "\n● # Title\r\033[J● Title\n"
        "  hello **bold**\r\033[J  hello bold\n"
        "\n"
        "  tail\r\033[J  tail\n"
    )
    assert not cli_progress._model_writer.active


def test_streamed_model_text_strips_terminal_sequences(monkeypatch):
    tty = attach_tty(monkeypatch)
    cli_progress.write_model_text("\x1b[31mred\x1b[0m\n")
    cli_progress.finish_model_text()
    assert "\x1b[31m" not in tty.getvalue()
    assert tty.getvalue().endswith("● red\n")


def test_user_message_echo_is_marked_only_on_terminals(monkeypatch):
    tty = attach_tty(monkeypatch)
    assert theme.user_message("第一行\n第二行") == "> 第一行\n  第二行"
    monkeypatch.setattr("sys.stdout", io.StringIO())
    assert theme.user_message("第一行\n第二行") == "第一行\n第二行"


def test_paint_honours_color_support(monkeypatch):
    stream = TTY()
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert theme.paint("ok", "success", stream).startswith("\x1b[")
    monkeypatch.setenv("NO_COLOR", "1")
    assert theme.paint("ok", "success", stream) == "ok"
    monkeypatch.delenv("NO_COLOR")
    assert theme.paint("ok", "success", io.StringIO()) == "ok"
    monkeypatch.setenv("TERM", "dumb")
    assert not theme.rich_output(stream)


def test_help_block_keeps_bracketed_arguments(monkeypatch):
    tty = attach_tty(monkeypatch)
    monkeypatch.setenv("COLUMNS", "120")
    text = rendering.help_block(SLASH_COMMANDS)
    assert text.startswith("可用命令\n")
    assert "[window <tokens> | pin <文本> | pins | unpin <编号>]" in text
    assert "[focus]" in text and "/quit" in text
    monkeypatch.setenv("COLUMNS", "40")
    narrow = rendering.help_block(SLASH_COMMANDS)
    assert "[focus]" not in narrow and "…" not in narrow
    assert all(command.name in narrow for command in SLASH_COMMANDS)


def test_approval_panel_and_diff_block(monkeypatch):
    tty = attach_tty(monkeypatch)
    panel = rendering.approval_panel("Potentially destructive command", [("Command", "rm [a].txt")])
    assert "⚠ Potentially destructive command" in panel
    assert "rm [a].txt" in panel and "╭" in panel and "╯" in panel
    diff = rendering.diff_block(["--- a", "+++ b", "@@ -1 +1 @@", "-before", "+after"])
    assert diff == "  @@ -1 +1 @@\n  -before\n  +after"
    assert rendering.diff_block(["--- a", "+++ b"]) == "  (no diff)"


def test_transcript_entries_render_roles(monkeypatch):
    tty = attach_tty(monkeypatch)
    assert rendering.transcript_entry("用户", "问题") == "> 问题"
    assert rendering.transcript_entry("PamuCode", "回答\n- 一项") == "● 回答\n  • 一项"
    assert rendering.transcript_entry("Shell（已完成）", "$ ls") == "● Shell（已完成）\n  $ ls"


def test_choice_number_keys_pick_directly():
    with create_pipe_input() as pipe:
        pipe.send_text("2")
        ui = terminal.TerminalUI(input=pipe, output=DummyOutput())
        assert ui.choose("provider", [("first", "First"), ("second", "Second")]) == "second"


def test_status_shows_plan_mode_and_context_level():
    ui = terminal.TerminalUI()
    ui.model_name, ui.thinking_level, ui.working_mode, ui.context_used = "m", "high", "plan", 85
    assert ui.status_text() == "Context 85% · m · high · ⏸ plan · 只读"
    assert ("class:status.context.high", "85%") in ui.status_fragments()
    assert ("class:status.plan", "⏸ plan · 只读") in ui.status_fragments()


def test_location_shows_home_relative_path_and_git_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal.Path, "home", lambda: tmp_path)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/feat/ui\n")
    (repo / "src").mkdir()
    assert terminal.git_branch(repo / "src") == "feat/ui"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (repo / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (repo / ".git" / "worktrees" / "wt" / "HEAD").write_text("0123456789abcdef\n")
    (worktree / ".git").write_text(f"gitdir: {repo / '.git' / 'worktrees' / 'wt'}\n")
    assert terminal.git_branch(worktree) == "0123456"
    assert terminal.git_branch(tmp_path) == ""
    ui = terminal.TerminalUI(workdir=repo)
    ui.branch = "feat/ui"
    assert ui.location_text() == "~/repo git:(feat/ui)"
