"""Instance-owned lifecycle hooks for the agent application."""

from __future__ import annotations

import difflib
import logging
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import theme
from ..logging_setup import get_logger
from ..rendering import approval_panel, blocked_notice, diff_block
from ..security.command_policy import ASK, DENY, classify
from .builtin import safe_path

LOGGER = get_logger("pamu.hooks")


@dataclass(slots=True)
class HookRegistry:
    callbacks: dict[str, list[Callable]] = field(
        default_factory=lambda: {
            "UserPromptSubmit": [],
            "PreToolUse": [],
            "PostToolUse": [],
            "ToolStart": [],
            "ToolResult": [],
            "Stop": [],
        }
    )

    def register(self, event: str, callback: Callable) -> None:
        self.callbacks[event].append(callback)

    def trigger(self, event: str, *args):
        for callback in self.callbacks[event]:
            result = callback(*args)
            if result is not None:
                return result
        return None


def make_permission_hook(
    root: Path,
    confirmation: Callable[[str], str],
    mcp_metadata: (
        Mapping[str, Mapping]
        | Callable[[], Mapping[str, Mapping]]
        | None
    ) = None,
    mcp_lock=None,
    *,
    mcp_state=None,
    shell_approval: str = "ask",
):
    session_allowed: set[str] = set()

    def resolve(value, default) -> Any:
        if value is None:
            return default
        return value() if callable(value) else value

    def permission_hook(block, *, _root=root):
        if block.name == "bash":
            command = block.input.get("command", "")
            decision = classify(command)
            if decision.action == DENY:
                print(blocked_notice(decision.reason) if theme.rich_output() else f"\n⛔ Blocked: {decision.reason}")
                return "Permission denied by deny list"
            remembered = decision.rememberable and set(decision.prefixes) <= session_allowed
            if decision.action == ASK and shell_approval != "auto" and not remembered:
                agent = getattr(block, "agent", None)
                rows = [("Tool", block.name), ("Command", str(command)), ("Reason", decision.reason)]
                if agent:
                    rows.append(("Teammate", agent))
                if decision.rememberable:
                    rows.append(("Always", f"a = 本会话不再询问 {', '.join(decision.prefixes)}"))
                if theme.rich_output():
                    print(approval_panel("Command needs approval", rows))
                else:
                    print("\n⚠  Command needs approval")
                    print("\n".join(f"   {label}: {value}" for label, value in rows))
                prompt = (
                    f"  Allow teammate '{agent}' to apply this change? [y/N] "
                    if agent
                    else "  Allow this change? [y/N] "
                )
                answer = confirmation(prompt).strip().lower()
                if answer in ("a", "always") and decision.rememberable:
                    session_allowed.update(decision.prefixes)
                elif answer not in ("y", "yes", "a", "always"):
                    return "Permission denied by user"

        if block.name == "schedule_cron" and (block.input.get("recurring") or block.input.get("durable")):
            rows = [("Cron", str(block.input.get("cron", ""))), ("Prompt", str(block.input.get("prompt", "")))]
            if block.input.get("recurring"):
                rows.append(("Recurring", "repeats until cancelled or 7 days pass"))
            if block.input.get("durable"):
                rows.append(("Durable", "saved to disk and reloaded on every start"))
            if theme.rich_output():
                print(approval_panel("Scheduled task needs approval", rows))
            else:
                print("\n⚠  Scheduled task needs approval")
                print("\n".join(f"   {label}: {value}" for label, value in rows))
            if confirmation("  Allow this scheduled task? [y/N] ").strip().lower() not in ("y", "yes"):
                return "Permission denied by user"

        if block.name.startswith("mcp__"):
            state = resolve(mcp_state, None)
            if state is not None:
                metadata = state.metadata
                lock = state.lock
            else:
                metadata = resolve(mcp_metadata, {})
                lock = resolve(mcp_lock, nullcontext())
            with lock:
                tool_metadata = dict(metadata.get(block.name, {}))
            if not tool_metadata:
                return "Permission denied: unknown MCP tool metadata"
            if tool_metadata.get("destructive"):
                if theme.rich_output():
                    print(approval_panel("Potentially destructive MCP tool", [
                        ("Server", str(tool_metadata["server"])),
                        ("Tool", str(tool_metadata["original_name"])),
                        ("Input", str(block.input)),
                    ]))
                else:
                    print("\n⚠  Potentially destructive MCP tool")
                    print(
                        f"  Server: {tool_metadata['server']}\n"
                        f"  Tool: {tool_metadata['original_name']}\n"
                        f"  Input: {block.input}"
                    )
                if confirmation("  Allow this MCP action? [y/N] ").strip().lower() not in ("y", "yes"):
                    return "Permission denied by user"
        return None

    return permission_hook


def make_log_hook(root: Path, logger: logging.Logger | None = None):
    hook_logger = logger or LOGGER

    def log_hook(block, *, _root=root):
        hook_logger.info("tool_use name=%s", block.name)
        return None

    return log_hook


def make_large_output_hook(root: Path):
    def large_output_hook(block, output, *, _root=root):
        size = len(str(output))
        if size > 100000:
            LOGGER.warning("large tool output name=%s chars=%d", block.name, size)
        return None

    return large_output_hook


def make_context_inject_hook(root: Path):
    def context_inject_hook(query: str, *, _root=root):
        LOGGER.debug("prompt submitted workspace=%s", _root)
        return None

    return context_inject_hook


def make_summary_hook(root: Path):
    def summary_hook(messages: list, *, _root=root):
        tool_count = sum(
            1
            for message in messages
            for block in (
                message.get("content")
                if isinstance(message.get("content"), list)
                else []
            )
            if isinstance(block, dict) and block.get("type") == "tool_result"
        )
        LOGGER.debug("turn stopped with %d tool results in context", tool_count)
        return None

    return summary_hook


def make_diff_preview_hook(root: Path, confirmation: Callable[[str], str], *, policy=None):
    def diff_preview_hook(block, *, _root=root):
        if block.name not in ("write_file", "edit_file"):
            return None
        path = block.input.get("path", "")
        try:
            file_path = safe_path(_root, path, getattr(block, "cwd", None), policy=policy, write=True)
            if policy is not None and policy.mode == 'confined':
                from ..security.files import open_regular, read_bounded
                try:
                    with open_regular(policy, file_path) as stream:
                        old_text = read_bounded(stream)
                except FileNotFoundError:
                    old_text = ''
            else:
                old_text = file_path.read_text() if file_path.exists() else ""
        except Exception as exc:
            return f"[HOOK] Error: {exc}"
        if block.name == "write_file":
            new_text = block.input.get("content", "")
        else:
            old_value = block.input.get("old_text", "")
            new_value = block.input.get("new_text", "")
            if old_value not in old_text:
                return None
            new_text = old_text.replace(old_value, new_value, 1)
        diff = difflib.unified_diff(
            old_text.splitlines(), new_text.splitlines(),
            fromfile=f"{path}before", tofile=f"{path}after", lineterm="",
        )
        if theme.rich_output():
            print(diff_block(list(diff)))
        else:
            print("\n".join(diff) or "(no diff)")
        if confirmation("  Apply change? [y/N] ").strip().lower() not in ("y", "yes"):
            return "File change rejected by user"
        return None

    return diff_preview_hook
