import io
import threading
from types import SimpleNamespace as NS

from agent_app import cli_progress, tool_display
from agent_app.tool_display import ToolDisplay


class TTY(io.StringIO):
    def isatty(self):
        return True


def attach_tty(monkeypatch, columns=60):
    """Point stdout at a fake terminal; pytest re-captures stdout per test phase."""
    stream = TTY()
    monkeypatch.setattr("sys.stdout", stream)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", str(columns))
    monkeypatch.setattr(cli_progress, "_shell_block_open", True)
    return stream


def block(tool_id, name, **tool_input):
    return NS(type="tool_use", id=tool_id, name=name, input=tool_input)


def run(display, used, content, **extra):
    display.start(used)
    display.result(used, {"type": "tool_result", "tool_use_id": used.id, "content": content, **extra})


def test_quick_tools_print_header_and_summary_after_completion(monkeypatch):
    tty = attach_tty(monkeypatch)
    display = ToolDisplay()
    run(display, block("1", "read_file", path="src/a.py"), "a\nb\n... (8 more lines);continue with offset=2")
    run(display, block("2", "glob", pattern="**/*.py"), "a.py\nb.py")
    run(display, block("3", "glob", pattern="*.md"), "(no matches)")
    run(display, block("4", "read_file", path="gone.py"), "Error: missing\ndetails")
    run(display, block("5", "create_task", subject="发布说明"), "Created task_1: 发布说明")
    assert tty.getvalue() == (
        "● Read(src/a.py)\n  ⎿  Read 2 lines (8 more)\n"
        "● Search(**/*.py)\n  ⎿  Found 2 files\n"
        "● Search(*.md)\n  ⎿  No matches\n"
        "● Read(gone.py)\n  ⎿  Error: missing\n"
        "● Create task(发布说明)\n  ⎿  Created task_1: 发布说明\n"
    )


def test_file_changes_show_header_before_the_diff_and_result_below(monkeypatch):
    tty = attach_tty(monkeypatch)
    display = ToolDisplay()
    edit = block("1", "edit_file", path="note.txt", old_text="a", new_text="b\nc")
    display.start(edit)
    print("  -a\n  +b\n  +c")
    display.result(edit, {"tool_use_id": "1", "content": "Edited note.txt"})
    write = block("2", "write_file", path="out.txt", content="x\ny\n")
    run(display, write, "File change rejected by user")
    assert tty.getvalue() == (
        "● Update(note.txt)\n  -a\n  +b\n  +c\n  ⎿  Updated note.txt · +2 −1\n"
        "● Write(out.txt)\n  ⎿  Rejected by user\n"
    )


def test_shell_lines_only_for_commands_that_did_not_run_in_foreground(monkeypatch):
    tty = attach_tty(monkeypatch)
    display = ToolDisplay()
    ran = block("1", "bash", command="echo hi")
    display.start(ran)
    monkeypatch.setattr(cli_progress, "_shell_runs", {threading.get_ident(): 99})
    display.result(ran, {"tool_use_id": "1", "content": "hi"})
    run(display, block("2", "bash", command="rm x"), "Permission denied by user")
    run(display, block("3", "bash", command="pytest"), "[Background task bg_0002 started] Command: pytest.")
    display.result(block("4", "bash", command="ls"), {"tool_use_id": "4", "content": "repeat", "is_error": True})
    assert tty.getvalue() == (
        "● Bash(rm x)\n  ⎿  Denied by user\n"
        "● Bash(pytest)\n  ⎿  Running in background · bg_0002\n"
        "● Bash(ls)\n  ⎿  repeat\n"
    )


def test_todos_render_as_a_checklist(monkeypatch):
    tty = attach_tty(monkeypatch)
    todos = [{"content": "读取", "status": "completed"}, {"content": "修改", "status": "in_progress"},
             {"content": "测试", "status": "pending"}]
    run(ToolDisplay(), block("1", "todo_write", todos=todos), "Updated 3 tasks")
    assert tty.getvalue() == "● Update Todos\n  ⎿  ☒ 读取\n     ◼ 修改\n     ☐ 测试\n"


def test_long_arguments_are_truncated_to_one_row(monkeypatch):
    tty = attach_tty(monkeypatch, columns=30)
    run(ToolDisplay(), block("1", "task", description="a" * 100), "done\nmore")
    first, second = tty.getvalue().splitlines()
    assert first.startswith("● Task(aaa") and first.endswith("…)") and len(first) <= 29
    assert second == "  ⎿  done"


def test_mcp_titles_and_plain_output_stays_silent(monkeypatch, capsys):
    assert tool_display.title("mcp__docs__search") == "docs · search (MCP)"
    assert tool_display.title("history_search") == "Search history"
    display = ToolDisplay()
    run(display, block("1", "read_file", path="a.py"), "x")
    assert capsys.readouterr().out == ""


def test_replay_lines_cover_saved_outcomes(monkeypatch):
    from agent_app.tool_display import replay_lines

    attach_tty(monkeypatch)
    edit = {"name": "edit_file", "input": {"path": "a.py", "old_text": "x", "new_text": "y"}}
    assert replay_lines(edit, {"content": "Edited a.py"}, "结果已记录") == [
        "● Update(a.py)", "  ⎿  Updated a.py · +1 −1"]
    assert replay_lines(edit, None, "结果未知") == ["● Update(a.py)", "  ⎿  结果未知"]
    assert replay_lines(edit, {"content": "Error: text not found in a.py", "is_error": True},
                        "结果已记录") == ["● Update(a.py)", "  ⎿  Error: text not found in a.py"]
    shell = {"name": "bash", "input": {"command": "sleep 9"}}
    assert replay_lines(shell, {"content": "命令已中断", "is_error": True}, "已中断") == ["● Interrupted sleep 9"]
    assert replay_lines(shell, {"content": "Error: Timeout (120s)"}, "结果已记录") == [
        "● Bash(sleep 9)", "  ⎿  Error: Timeout (120s)"]
    assert replay_lines({"name": "glob", "input": "bad"}, {"content": "(no matches)"}, "结果已记录") == [
        "● Search", "  ⎿  No matches"]
