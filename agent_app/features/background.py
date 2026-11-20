"""Owned state and worker lifecycle for background tool execution."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from ..tools.executor import execute_tool
from ..core.interruption import command_cancellation, check_command_cancelled
from ..logging_setup import get_logger

LOGGER = get_logger("pamu.background")


@dataclass(slots=True)
class BackgroundControl:
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)


@dataclass(slots=True)
class BackgroundState:
    counter: int = 0
    tasks: dict[str, dict] = field(default_factory=dict)
    results: dict[str, str] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    controls: dict[str, BackgroundControl] = field(default_factory=dict)
    closed: bool = False


def start_background_task(
    state: BackgroundState,
    block,
    handlers: dict,
    *,
    post_tool: Callable,
    persist_output: Callable[[str, str], str],
) -> str:
    handler_snapshot = dict(handlers)
    control = BackgroundControl()

    with state.lock:
        if state.closed:
            raise RuntimeError('后台命令管理器已关闭')
        state.counter += 1
        background_id = f"bg_{state.counter:04d}"
        state.controls[background_id] = control
        state.tasks[background_id] = {
            "id": background_id,
            "tool_use_id": block.id,
            "tool_name": block.name,
            "command": block.input.get("command", ""),
            "status": "running",
            "error": None,
        }

    def worker():
        status = "completed"
        error = None
        output = ""
        try:
            with command_cancellation(control.cancel):
                check_command_cancelled()
                output = str(execute_tool(block, handler_snapshot))
            post_tool(block, output)
            output = persist_output(block.id, output)
        except KeyboardInterrupt as exc:
            status = "cancelled"
            output = str(exc) or '用户已中断，命令可能已部分执行，请检查实际状态。'
            try:
                output = persist_output(block.id, output)
            except Exception as error:
                output += f'\n中断输出保存失败：{type(error).__name__}'
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            print(f"  \033[31m[background error] {background_id}: {error}\033[0m")
        finally:
            with state.lock:
                task = state.tasks.get(background_id)
                if task:
                    task["status"] = status
                    task["error"] = error
                    state.results[background_id] = output
            control.done.set()

    try:
        threading.Thread(target=worker, daemon=True).start()
    except Exception as exc:
        with state.lock:
            state.tasks[background_id]['status'] = 'failed'
            state.tasks[background_id]['error'] = f'后台线程启动失败：{type(exc).__name__}'
        control.done.set()
        raise
    except BaseException:
        control.cancel.set()
        # Keep the handle if a worker was actually started before interruption.
        # Its eventual notification will still be collectable.
        raise
    return background_id


def close_background_tasks(state: BackgroundState) -> dict[str, str]:
    with state.lock:
        state.closed = True
    return stop_background_tasks(state)


def stop_background_tasks(state: BackgroundState, selector: str = 'all', *, timeout=2.0) -> dict[str, str]:
    """Cancel exact, session-local handles and report only acknowledged outcomes."""
    with state.lock:
        if selector != 'all' and selector not in state.tasks:
            raise ValueError(f'后台任务不存在：{selector}')
        selected = {key: task for key, task in state.tasks.items() if selector == 'all' or key == selector}
        controls = {key: state.controls.get(key) for key in selected}
        for key, control in controls.items():
            if selected[key]['status'] == 'running' and control is not None:
                control.cancel.set()
    deadline = time.monotonic() + timeout
    for control in controls.values():
        if control is not None:
            control.done.wait(max(0, deadline - time.monotonic()))
    with state.lock:
        return {key: task['status'] for key, task in selected.items()}


def describe_background_tasks(state: BackgroundState) -> str:
    with state.lock:
        if not state.tasks:
            return '没有后台命令。'
        labels = {'running': '运行中', 'completed': '已完成', 'failed': '失败', 'cancelled': '已中断'}
        from .session_codec import safe_preview
        return '\n'.join(f"{key}  {labels[task['status']]}  {safe_preview(task['command'])}"
                         for key, task in state.tasks.items())


def collect_background_results(state: BackgroundState) -> list[str]:
    with state.lock:
        ready_ids = [
            background_id
            for background_id, task in state.tasks.items()
            if task["status"] in {"completed", "failed", "cancelled"}
        ]

    notifications = []
    for background_id in ready_ids:
        with state.lock:
            task = state.tasks.pop(background_id, None)
            if task is None:
                continue
            state.controls.pop(background_id, None)
            output = state.results.pop(background_id, "")
        summary_source = task.get("error") or output
        summary = summary_source[:200] if len(summary_source) > 200 else summary_source
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{escape(str(background_id))}</task_id>\n"
            f"  <status>{escape(str(task['status']))}</status>\n"
            f"  <command>{escape(str(task['command']))}</command>\n"
            f"  <summary>{escape(str(summary))}</summary>\n"
            f"</task_notification>"
        )
        LOGGER.debug("background result delivered id=%s status=%s", background_id, task["status"])
    return notifications
