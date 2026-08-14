"""Interactive command-line lifecycle for the agent runtime."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from .bootstrap import build_default_runtime
from .config import MissingConfigurationError
from .core.loop import run_agent_loop
from .features.scheduler import (
    cron_scheduler_loop,
    has_cron_queue,
    load_durable_jobs,
)


def run_agent_turn(runtime, user_query: str | None = None) -> None:
    if user_query:
        runtime.session.history.append({"role": "user", "content": user_query})
    run_agent_loop(runtime)
    print()


def _queue_processor_loop(runtime) -> None:
    while not runtime.stop_event.wait(0.2):
        if not has_cron_queue(runtime.scheduler):
            continue
        if not runtime.agent_lock.acquire(blocking=False):
            continue
        try:
            if has_cron_queue(runtime.scheduler):
                print("\n  \033[35m[queue processor] delivering scheduled work\033[0m")
                run_agent_turn(runtime)
        finally:
            runtime.agent_lock.release()


def start_runtime_threads(runtime) -> list[threading.Thread]:
    load_durable_jobs(runtime.scheduler, runtime.config)
    scheduler = threading.Thread(
        target=cron_scheduler_loop,
        args=(runtime.scheduler, runtime.config, runtime.stop_event),
        daemon=True,
        name="cron-scheduler",
    )
    processor = threading.Thread(
        target=_queue_processor_loop,
        args=(runtime,),
        daemon=True,
        name="cron-queue-processor",
    )
    threads = [scheduler, processor]
    started = []
    try:
        for thread in threads:
            thread.start()
            started.append(thread)
    except Exception:
        runtime.stop_event.set()
        for thread in started:
            thread.join(timeout=1.0)
        raise
    return threads


def stop_runtime_threads(runtime, threads) -> None:
    runtime.stop_event.set()
    for thread in threads:
        thread.join(timeout=1.0)


def main(
    runtime_factory=build_default_runtime,
    run_turn=run_agent_turn,
    start_threads=start_runtime_threads,
    stop_threads=stop_runtime_threads,
) -> int | None:
    try:
        runtime = runtime_factory()
    except MissingConfigurationError as error:
        global_config = Path.home() / ".pamu" / ".settings"
        project_config = Path.cwd().resolve() / ".pamu" / ".settings"
        print(
            f"配置错误：缺少必需配置项 {error.key}。\n"
            "请在以下任一位置添加配置：\n"
            f"  全局：{global_config}\n"
            f"  项目：{project_config}\n"
            "示例：MODEL_ID=claude-sonnet-4-6",
            file=sys.stderr,
        )
        return 2
    threads = []
    try:
        threads = start_threads(runtime)
        print("开拓者终于等到你了！欢迎使用Pamu帕！你可以输入 'q'，'exit'或 '空格符' 退出帕！。")
        while True:
            try:
                query = input("\033[36m>> \033[0m")
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "exit", ""):
                break
            runtime.hooks.trigger("UserPromptSubmit", query)
            with runtime.agent_lock:
                run_turn(runtime, query)
    finally:
        stop_threads(runtime, threads)
