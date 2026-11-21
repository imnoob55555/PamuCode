"""Claude Code style tool call lines: ``● Read(path)`` followed by ``⎿ summary``.

Only the main agent loop fires the ``ToolStart``/``ToolResult`` hooks, so
subagent and teammate tools never interleave with the foreground transcript.
Shell commands keep their own ``Running``/``Ran`` status; they only get a line
here when they did not run in the foreground (denied, blocked, background).
"""

from __future__ import annotations

import json
import re

from prompt_toolkit.utils import get_cwidth

from . import theme
from .cli_progress import shell_line, shell_run_count, write_tool_lines
from .features.session_codec import safe_preview, safe_terminal_text
from .rendering import terminal_width

RESULT_MARK = "⎿"
EARLY_HEADERS = frozenset({"write_file", "edit_file", "task"})
TITLES = {
    "bash": "Bash",
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Update",
    "glob": "Search",
    "load_skill": "Skill",
    "task": "Task",
    "todo_write": "Update Todos",
    "history_search": "Search history",
    "history_read": "Read history",
    "compact": "Compact",
}
DENIALS = {
    "Permission denied by user": "Denied by user",
    "Permission denied by deny list": "Blocked by deny list",
    "File change rejected by user": "Rejected by user",
}
BACKGROUND = re.compile(r"\[Background task (bg_\d+) started\]")
TODO_MARKS = {"completed": ("☒", "muted"), "in_progress": ("◼", "accent.bold"), "pending": ("☐", "")}


def _value(block, key, default=None):
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _first_line(text: str) -> str:
    return next((line.strip() for line in str(text).splitlines() if line.strip()), "")


def _fit(text: str, width: int) -> str:
    if get_cwidth(text) <= width:
        return text
    used = 0
    for index, char in enumerate(text):
        used += get_cwidth(char)
        if used > width - 1:
            return text[:index] + "…"
    return text


def title(name: str) -> str:
    if name in TITLES:
        return TITLES[name]
    if name.startswith("mcp__"):
        _, server, tool = (name.split("__", 2) + ["", ""])[:3]
        return f"{server} · {tool} (MCP)"
    return name.replace("_", " ").capitalize()


def arguments(name: str, tool_input) -> str:
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if name == "todo_write":
        return ""
    if name == "read_file":
        path = str(tool_input.get("path", ""))
        offset, limit = tool_input.get("offset"), tool_input.get("limit")
        if isinstance(offset, int) and isinstance(limit, int):
            return f"{path} · lines {offset + 1}-{offset + limit}"
        if isinstance(offset, int) and offset:
            return f"{path} · from line {offset + 1}"
        return path
    key = {"bash": "command", "write_file": "path", "edit_file": "path", "glob": "pattern",
           "load_skill": "name", "task": "description", "history_search": "query",
           "history_read": "ref", "compact": "focus"}.get(name)
    if key is not None:
        return safe_preview(str(tool_input.get(key, "")), 200)
    if name.startswith("mcp__"):
        return safe_preview(json.dumps(tool_input, ensure_ascii=False), 200) if tool_input else ""
    return next((safe_preview(value, 200) for value in tool_input.values() if isinstance(value, str)), "")


def header(name: str, tool_input, dot: str) -> str:
    label = title(name)
    args = arguments(name, tool_input)
    width = terminal_width() - 1
    plain = f"● {label}" + (f"({args})" if args else "")
    if get_cwidth(plain) > width and args:
        args = _fit(args, max(4, width - get_cwidth(f"● {label}()")))
    line = f"{theme.bullet(dot)} {theme.paint(label, 'bold')}"
    return line + (f"({args})" if args else "")


def _result_line(text: str, style: str = "muted", first: bool = True) -> str:
    prefix = f"  {theme.paint(RESULT_MARK, 'dim')}  " if first else "     "
    width = terminal_width() - get_cwidth("  ⎿  ") - 1
    return prefix + theme.paint(_fit(text, width), style)


def _count_lines(text: str) -> int:
    return len(text.splitlines()) if text else 0


def _todo_lines(tool_input) -> list[str]:
    from .features.todos import _normalize_todos

    todos, error = _normalize_todos((tool_input or {}).get("todos", []))
    if error or not todos:
        return []
    lines = []
    for index, todo in enumerate(todos):
        mark, style = TODO_MARKS[todo["status"]]
        text = f"{mark} {safe_preview(str(todo['content']), 200)}"
        if todo["status"] == "completed":
            style = f"strike {theme.MUTED}"
        lines.append(_result_line(text, style or "default", first=index == 0))
    return lines


def summary(name: str, tool_input, content: str) -> str:
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if name == "read_file":
        lines = content.splitlines()
        more = re.match(r"\.\.\. \((\d+) more lines\)", lines[-1]) if lines else None
        if more:
            return f"Read {len(lines) - 1} lines ({more[1]} more)"
        return f"Read {len(lines)} lines"
    if name == "write_file":
        return f"Wrote {_count_lines(str(tool_input.get('content', '')))} lines to {tool_input.get('path', '')}"
    if name == "edit_file":
        removed = _count_lines(str(tool_input.get("old_text", "")))
        added = _count_lines(str(tool_input.get("new_text", "")))
        return f"Updated {tool_input.get('path', '')} · +{added} −{removed}"
    if name == "glob":
        if content.strip() == "(no matches)":
            return "No matches"
        count = _count_lines(content)
        return f"Found {count} file{'s' if count != 1 else ''}"
    if name == "load_skill":
        return f"Loaded skill {tool_input.get('name', '')}"
    if name == "history_search":
        return _first_line(content) or "No results"
    if name == "history_read":
        return f"Read {_count_lines(content)} lines"
    return _first_line(content) or "Done"


def is_error(content: str, result: dict) -> bool:
    return bool(result.get("is_error")) or content.startswith(("Error", "Permission denied", "[HOOK]")) \
        or content in DENIALS


def outcome_lines(name: str, tool_input, result: dict, *, header_shown: bool = False) -> list[str]:
    """The ``⎿`` result lines for a completed call, preceded by its header unless shown."""
    content = str(result.get("content", ""))
    background = BACKGROUND.match(content)
    failed = is_error(content, result)
    if background:
        lines = [_result_line(f"Running in background · {background[1]}")]
    elif failed:
        denial = DENIALS.get(content)
        lines = [_result_line(denial or _first_line(content), "warning" if denial else "error")]
    elif name == "todo_write":
        lines = _todo_lines(tool_input) or [_result_line(summary(name, tool_input, content))]
    else:
        lines = [_result_line(summary(name, tool_input, content))]
    if not header_shown:
        dot = "warning" if failed and content in DENIALS else "error" if failed else "success"
        lines.insert(0, header(name, tool_input, dot))
    return lines


def replay_lines(call: dict, result: dict | None, status: str) -> list[str]:
    """Render a saved tool call when a session is resumed.

    ``status`` is the resume classification of the call: recorded, not run,
    interrupted, unknown, or submitted to the background.
    """
    name = str(call.get("name", ""))
    tool_input = call.get("input") if isinstance(call.get("input"), dict) else {}
    content = str(result.get("content", "")) if result else ""
    if status.startswith("已提交后台"):
        return [header(name, tool_input, "success"), _result_line(f"Sent to background · {status.split()[-1]}")]
    if result is None or status in ("未执行", "结果未知"):
        reason = status if result is None else DENIALS.get(content) or _first_line(content) or status
        return [header(name, tool_input, "warning"), _result_line(reason, "warning")]
    if name == "bash" and (status == "已中断" or not is_error(content, result)):
        command = safe_terminal_text(str(tool_input.get("command", ""))).replace("\n", "\n  ")
        return [shell_line("interrupted" if status == "已中断" else "finished", command)]
    return outcome_lines(name, tool_input, result)


class ToolDisplay:
    """Print one tool call per block, tracking headers already shown early."""

    def __init__(self):
        self.started: dict[str, int] = {}
        self.headers: set[str] = set()

    def start(self, block) -> None:
        if not theme.rich_output():
            return
        name, tool_id = _value(block, "name"), _value(block, "id")
        self.started[tool_id] = shell_run_count()
        if name in EARLY_HEADERS or str(name).startswith("mcp__"):
            write_tool_lines([header(name, _value(block, "input", {}), "dim")])
            self.headers.add(tool_id)

    def result(self, block, result: dict) -> None:
        if block is None or not theme.rich_output():
            return
        name, tool_id = _value(block, "name"), _value(block, "id")
        early = tool_id in self.headers
        self.headers.discard(tool_id)
        runs = self.started.pop(tool_id, None)
        background = BACKGROUND.match(str(result.get("content", "")))
        if name == "bash" and not background and runs is not None and shell_run_count() != runs:
            return
        write_tool_lines(outcome_lines(name, _value(block, "input", {}), result, header_shown=early))
