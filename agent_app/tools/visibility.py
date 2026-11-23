"""Leave follow-up tools out of model requests while they have nothing to act on.

Only the offered schemas shrink: handlers stay registered, so a hidden tool that
is still called runs normally. A group stays offered once the conversation has
used any of its tools, and any state that cannot be inspected keeps it offered.
"""

from __future__ import annotations

from ..adapters.common import value

FOLLOW_UP_TOOLS = {
    "team": ("send_message", "check_inbox", "request_shutdown", "request_plan", "review_plan"),
    "tasks": ("list_tasks", "get_task", "claim_task", "complete_task"),
    "cron": ("list_crons", "cancel_cron"),
    "worktrees": ("remove_worktree", "keep_worktree"),
}


def _team(runtime):
    with runtime.team.lock:
        if runtime.team.active:
            return True
    with runtime.protocols.lock:
        if runtime.protocols.pending:
            return True
    path = runtime.bus.mailbox_path("lead")
    return path.exists() and path.stat().st_size > 0


def _tasks(runtime):
    root = runtime.tasks.root
    return root.exists() and any(root.glob("task_*.json"))


def _cron(runtime):
    with runtime.scheduler.lock:
        return bool(runtime.scheduler.jobs)


def _worktrees(runtime):
    root = runtime.worktrees.root
    return root.exists() and any(path.is_dir() for path in root.iterdir())


CHECKS = {"team": _team, "tasks": _tasks, "cron": _cron, "worktrees": _worktrees}


def _used_tools(history):
    return {
        value(block, "name")
        for message in history if isinstance(value(message, "content"), list)
        for block in value(message, "content") if value(block, "type") == "tool_use"
    }


def _active(runtime, group):
    try:
        return CHECKS[group](runtime)
    except Exception:
        return True


def offered_tools(runtime, tools: list[dict]) -> list[dict]:
    used = _used_tools(getattr(runtime.session, "history", []))
    hidden = {
        name
        for group, names in FOLLOW_UP_TOOLS.items()
        if not used.intersection(names) and not _active(runtime, group)
        for name in names
    }
    return [tool for tool in tools if tool["name"] not in hidden] if hidden else tools
