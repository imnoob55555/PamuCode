"""User-selected work scope, independent of model decisions and sandbox policy."""
from __future__ import annotations

from copy import deepcopy
from functools import wraps

# Shell commands and external tools have effects we cannot infer safely here.
PLAN_TOOLS = frozenset({
    "read_file", "glob", "load_skill", "list_skills", "compact", "todo_write",
    "history_search", "history_read",
})


def working_mode(session) -> str:
    mode = getattr(session, "context_state", {}).get("working_mode", "execute")
    # Unknown persisted values must never widen permissions.
    return mode if isinstance(mode, str) and mode in {"plan", "execute"} else "plan"


def tool_denial(session, name: str) -> str | None:
    if working_mode(session) == "plan" and name not in PLAN_TOOLS:
        return f"Error: 规划模式禁止执行 {name}；请使用读取工具。用户可通过 /execute 允许实施。"
    return None


def guard_handler(session, name, handler):
    @wraps(handler)
    def invoke(*args, **kwargs):
        denied = tool_denial(session, name)
        return denied if denied else handler(*args, **kwargs)
    return invoke


def delegated_scope(session) -> str:
    """Carry original user scope into a delegate's otherwise fresh conversation."""
    state = session.context_state
    request = state.get("current_request", {}).get("text", "")
    pins = "\n".join(pin["text"] for pin in state.get("pins", []))
    return (f"Parent working mode: {working_mode(session)}\n"
            f"Original user request (preserve its limits):\n{request}\n"
            f"User pinned constraints:\n{pins}\n"
            "The delegated task cannot expand the user's authorization.")


def switch_mode(runtime, mode: str) -> str:
    from .features.sessions import commit_history

    if mode not in {"plan", "execute"}:
        raise ValueError("工作模式必须为 plan 或 execute")
    if not runtime.agent_lock.acquire(blocking=False):
        raise ValueError("代理正在执行任务，结束后再切换工作模式")
    try:
        # An already-running process cannot be made read-only retroactively.
        with runtime.background.lock:
            busy = any(t["status"] == "running" for t in runtime.background.tasks.values())
        with runtime.team.lock:
            busy |= bool(runtime.team.active)
        if busy:
            raise ValueError("后台任务或队友仍在运行，结束后再切换工作模式")
        state = deepcopy(runtime.session.context_state)
        state["working_mode"] = mode
        commit_history(runtime, list(runtime.session.history), state)
    finally:
        runtime.agent_lock.release()
    return "已切换为规划模式（只读探索）。" if mode == "plan" else "已切换为执行模式（仍受安全策略限制）。"
