"""Read-only dynamic context construction and message helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

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
        "project_rules": _project_rules(runtime.config.workdir, confined=getattr(runtime.config, 'sandbox_mode', 'off') == 'confined'),
    }


def _project_rules(workdir, *, confined=False):
    path = workdir / 'AGENTS.md'
    if confined:
        from ..security import ExecutionPolicy
        from ..security.files import open_regular, read_bounded
        policy = ExecutionPolicy(workdir, workdir, mode='confined')
        try:
            with open_regular(policy, path) as stream:
                return read_bounded(stream)
        except FileNotFoundError:
            return ''
    if not path.exists() or path.is_symlink() or not path.is_file():
        return ''
    # A large required instruction file is reported by the budget gate, not truncated.
    return path.read_text(encoding='utf-8')


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
