"""Read-only dynamic context construction and message helpers."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from .context_budget import ContextBudget, resolve_window
from .context_materials import optional_materials
from ..features.todos import format_current_todos
from ..working_mode import working_mode


def build_context(runtime, tools: list[dict]) -> dict:
    window = (resolve_window(runtime)[0] if getattr(runtime, "model", None) is not None
              else getattr(runtime.config, "context_window", None) or 16384)
    budget = ContextBudget(window, runtime.config.default_max_tokens)
    materials = optional_materials(runtime, budget.input_limit * 15 // 100)
    tool_names = sorted(tool["name"] for tool in tools)
    serialized_tools = json.dumps(
        tools,
        ensure_ascii=False,
        sort_keys=True,
    )
    tool_fingerprint = hashlib.sha256(
        serialized_tools.encode("utf-8")
    ).hexdigest()

    repository = getattr(runtime, 'repository', None)
    with runtime.mcp.lock:
        connected_mcp = sorted(runtime.mcp.clients)
    with runtime.team.lock:
        active_names = sorted(runtime.team.active)

    return {
        "working_mode": working_mode(runtime.session),
        "enabled_tools": tool_names,
        "workspace": str(runtime.config.workdir),
        **materials,
        "todos": format_current_todos(runtime.session),
        "active_teammates": active_names,
        "connect_mcp": connected_mcp,
        "tool_fingerprint": tool_fingerprint,
        "current_time": runtime.session.context_state.get('turn_time') or datetime.now().isoformat(timespec="seconds"),
        "pinned_constraints": '\n'.join(p['text'] for p in runtime.session.context_state.get('pins', [])),
        "memory_writable": (bool(getattr(runtime.config, "auto_memory", True)) and not getattr(runtime.config, "headless", False)
                            and working_mode(runtime.session) != "plan"),
        "project_rules": _project_rules(runtime.config.workdir, confined=getattr(runtime.config, 'sandbox_mode', 'off') == 'confined',
                                       root=getattr(repository, 'root', None)),
        "git_snapshot": getattr(repository, 'snapshot', ''),
    }


INSTRUCTION_FILES = ('AGENTS.md', 'CLAUDE.md')
INSTRUCTION_WARNING_BYTES = 32 * 1024


def _instruction_dirs(workdir, root):
    """Directories from the git root down to the workspace; only the workspace without a root."""
    workdir = Path(workdir).resolve()
    if root is None or not workdir.is_relative_to(root):
        return [workdir]
    dirs = [Path(root)]
    for part in workdir.relative_to(root).parts:
        dirs.append(dirs[-1] / part)
    return dirs


def _read_instruction(directory, *, confined, workdir):
    for name in INSTRUCTION_FILES:
        path = directory / name
        if confined:
            from ..security import ExecutionPolicy
            from ..security.files import open_regular, read_bounded
            policy = ExecutionPolicy(workdir, workdir, mode='confined')
            try:
                with open_regular(policy, path) as stream:
                    text = read_bounded(stream)
            except FileNotFoundError:
                continue
        elif path.is_file() and not path.is_symlink():
            text = path.read_text(encoding='utf-8')
        else:
            continue
        if text.strip():
            return name, text
    return None


def _project_rules(workdir, *, confined=False, root=None):
    """Concatenate one AGENTS.md (or CLAUDE.md) per directory, outermost first.

    Confined mode never leaves the workspace. A large required instruction file
    is reported at startup and by the budget gate, not truncated.
    """
    workdir = Path(workdir)
    dirs = [workdir] if confined else _instruction_dirs(workdir, root)
    base = dirs[0]
    parts = []
    for directory in dirs:
        found = _read_instruction(directory, confined=confined, workdir=workdir)
        if found:
            name, text = found
            label = (directory / name).relative_to(base).as_posix()
            parts.append(f'From {label}:\n{text}')
    return '\n\n'.join(parts)


def warn_large_project_rules(config, repository, print_fn=None):
    try:
        text = _project_rules(config.workdir, confined=getattr(config, 'sandbox_mode', 'off') == 'confined',
                              root=getattr(repository, 'root', None))
    except OSError:
        return
    size = len(text.encode('utf-8'))
    if size > INSTRUCTION_WARNING_BYTES:
        (print_fn or (lambda message: print(message, file=sys.stderr)))(
            f'提示：项目指令（AGENTS.md/CLAUDE.md）合计 {size // 1024} KB，每次请求都会完整带上，占用较多上下文。')


def append_user_text_blocks(messages: list, texts: list[str]) -> None:
    if not texts:
        return

    blocks = [{"type": "text", "text": text} for text in texts]

    if messages and messages[-1].get("role") == "user":
        content = messages[-1].get("content")
        if isinstance(content, list):
            content.extend(blocks)
        else:
            messages[-1]["content"] = [
                {"type": "text", "text": str(content)},
                *blocks,
            ]
    else:
        messages.append({"role": "user", "content": blocks})
