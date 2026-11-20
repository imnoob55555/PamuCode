"""Tool execution and background-dispatch policy without application state."""

from __future__ import annotations

import hashlib
import json
import shlex

MAX_IDENTICAL_TOOL_CALLS = 3
TOOL_REPEAT_ERROR = (
    f'同一工具及参数在本轮已达到 {MAX_IDENTICAL_TOOL_CALLS} 次调用上限，此次未执行。'
    '请依据现有结果继续，换一种方法，或向用户说明当前进展。'
)
TOOL_REPEAT_STOP = '模型连续请求已达到重复调用上限的工具，本轮已停止。可继续输入指令。'

_SHELL_SEPARATORS = ';&|()\n'
_PACKAGE_MANAGERS = {'npm', 'pnpm', 'yarn', 'bun'}
_PACKAGE_ACTIONS = {'install', 'build', 'test', 'deploy'}


class ToolRepeatLimit:
    """Keep an invocation budget for one agent's turn."""

    def __init__(self):
        self._counts: dict[bytes, int] = {}
        self._previous_batch_blocked = False

    def allow(self, name: str, arguments: dict) -> bool:
        canonical = json.dumps((name, arguments), sort_keys=True, separators=(',', ':'))
        key = hashlib.sha256(canonical.encode()).digest()
        count = self._counts.get(key, 0)
        if count >= MAX_IDENTICAL_TOOL_CALLS:
            return False
        self._counts[key] = count + 1
        return True

    def should_stop(self, results: int, blocked: int) -> bool:
        # Give the model one chance to recover from a fully blocked batch.
        all_blocked = results > 0 and blocked == results
        stop = self._previous_batch_blocked and all_blocked
        self._previous_batch_blocked = all_blocked
        return stop


def _slow_invocation(argv: list[str]) -> bool:
    # Match executable/subcommand positions, never workspace names or arguments.
    start = 0
    while start + 2 < len(argv):
        program = argv[start].rsplit('/', 1)[-1].lower()
        if program not in ('uv', 'poetry', 'pipenv') or argv[start + 1] != 'run':
            break
        start += 2
    program = argv[start].rsplit('/', 1)[-1].lower()
    args = argv[start + 1:]
    if program in ('pytest', 'make', 'cmake'):
        return True
    if program.startswith(('python', 'pypy')) and len(args) >= 2 and args[0] == '-m':
        return args[1] in ('pytest', 'unittest') or args[1:3] == ['pip', 'install']
    if program in _PACKAGE_MANAGERS:
        return bool(args and (args[0] in _PACKAGE_ACTIONS or args[0] == 'run' and len(args) > 1 and args[1] in _PACKAGE_ACTIONS))
    if program in ('cargo', 'go', 'pip', 'pip3', 'poetry'):
        return bool(args and args[0] in _PACKAGE_ACTIONS)
    if program == 'uv':
        return args[:1] in (['sync'], ['build']) or args[:2] == ['pip', 'install']
    return program == 'docker' and args[:1] == ['build']


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    if tool_name != "bash":
        return False
    command = tool_input.get("command")
    if not isinstance(command, str):
        return False
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_SHELL_SEPARATORS)
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return False  # Let the shell report malformed syntax in the foreground.
    argv = []
    for token in tokens:
        if token and all(char in _SHELL_SEPARATORS for char in token):
            if argv and _slow_invocation(argv):
                return True
            argv.clear()
        else:
            argv.append(token)
    return bool(argv and _slow_invocation(argv))


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    if tool_name != "bash":
        return False
    requested = tool_input.get("run_in_background")
    return requested if isinstance(requested, bool) else is_slow_operation(tool_name, tool_input)


def execute_tool(block, handlers: dict) -> str:
    handler = handlers.get(block.name)
    if handler:
        return handler(**block.input)
    return f"Unknown tool: {block.name}"
