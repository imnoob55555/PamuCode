"""Agent-turn orchestration over explicit runtime state."""

from __future__ import annotations

import json
import time

from .. import theme
from ..features.background import (
    running_task_count,
    collect_background_results,
    start_background_task,
)
from ..adapters.common import null_progress, value as _block_value
from ..features.mcp import snapshot_mcp_tools
from ..features.sessions import checkpoint
from ..features.session_store import SessionError
from ..features.scheduler import consume_cron_queue
from ..features.teams.protocol import collect_lead_inbox
from ..logging_setup import get_logger
from ..runtime import RuntimeContext
from ..working_mode import tool_denial
from ..thinking import plan_request, strip_thinking_blocks
from ..tools.builtin import resolve_tool_cwd
from ..tools.visibility import offered_tools
from ..tools.executor import (
    TOOL_REPEAT_ERROR, TOOL_REPEAT_STOP, ToolRepeatLimit, execute_tool, should_run_background,
)
from .compaction import persist_large_output
from .context import append_user_text_blocks, build_context
from .context_manager import archive_result, compact, normalize_results, register_history_tools
from .context_budget import prepare_budget, record_usage, resolve_window
from .turn_budget import TurnBudget
from .recovery import (
    PartialStreamError,
    RecoveryState,
    append_unrecoverable_error,
    describe_model_error,
    is_prompt_too_long_error,
    with_retry,
)
from .interruption import CommandInterrupted, ResponseInterrupted, protect_interrupt_cleanup

CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly — "
    "no apology, no recap. Pick up mid-thought."
)
TEAM_GUARDED_TOOLS = {"bash", "write_file"}
LOGGER = get_logger("pamu.loop")


def _has_tool_use(content) -> bool:
    return any(_block_value(block, "type") == "tool_use" for block in content)


def _format_team_inbox(messages: list[dict]) -> str:
    lines = ["[Team inbox]"]
    for message in messages:
        content = message.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        lines.append(
            f"From {message.get('from')}"
            f"({message.get('type')})"
            f"{content}"
        )
    return "\n".join(lines)


def _collect_team_messages(runtime: RuntimeContext) -> list[dict]:
    return collect_lead_inbox(
        runtime.bus,
        runtime.protocols,
        hook=runtime.hooks.trigger,
        cwd_resolver=lambda cwd: resolve_tool_cwd(runtime.config.workdir, cwd),
        guarded_tools=TEAM_GUARDED_TOOLS,
    )


def _has_active_teammates(runtime: RuntimeContext) -> bool:
    with runtime.team.lock:
        return bool(runtime.team.active)


def _wait_for_team_activity(runtime: RuntimeContext) -> bool:
    deadline = time.time() + runtime.config.permission_timeout
    while True:
        messages = _collect_team_messages(runtime)
        if messages:
            append_user_text_blocks(
                runtime.session.history,
                [_format_team_inbox(messages)],
            )
            return True
        if not _has_active_teammates(runtime):
            return False
        if time.time() >= deadline:
            print("  " + theme.paint("[team] wait timed out; teammates remain active", "warning"))
            return False
        time.sleep(runtime.config.permission_poll_interval)


def _update_context(runtime: RuntimeContext, tools: list[dict] | None = None) -> None:
    if tools is None:
        tools, _handlers = snapshot_mcp_tools(
            runtime.mcp, *runtime.tools.snapshot()
        )
    runtime.session.context = build_context(runtime, tools)


def run_agent_loop(runtime: RuntimeContext) -> str | None:
    """Run one agent turn, mutating only the supplied runtime's owned state."""
    session = runtime.session
    messages = session.history
    router = runtime.model
    assert router is not None
    provider, primary_model, fallback_model = router.snapshot()
    LOGGER.info(
        "turn start provider=%s model=%s fallback=%s rounds=%d",
        provider,
        primary_model,
        fallback_model,
        len(messages),
    )
    state = RecoveryState(
        current_model=primary_model,
        fallback_model=fallback_model,
    )
    max_tokens = runtime.config.default_max_tokens
    repeat_limit = ToolRepeatLimit()
    budget = None if runtime.config.headless else TurnBudget(
        runtime.config.max_turn_model_calls, runtime.config.max_turn_tool_calls, runtime.config.max_turn_tokens,
    )

    def request_history(thinking):
        return messages if thinking else strip_thinking_blocks(messages)

    while True:
        if budget is not None and budget.exceeded():
            if runtime.hooks.trigger("TurnLimit", budget.summary()) is True:
                budget.reset()
            else:
                print(f"{budget.summary()}，已达到单轮上限并停止。可继续输入指令，或调整 PAMU_MAX_TURN_* 设置。")
                _update_context(runtime)
                return "turn_limit"
        checkpoint(runtime, status="running")
        fired_jobs = consume_cron_queue(runtime.scheduler)
        pending_texts = [
            f"[Scheduled: {job.id}] {job.prompt}" for job in fired_jobs
        ]
        pending_texts.extend(collect_background_results(runtime.background))
        team_messages = _collect_team_messages(runtime)
        if team_messages:
            pending_texts.append(_format_team_inbox(team_messages))
        append_user_text_blocks(messages, pending_texts)

        register_history_tools(runtime)

        if session.rounds_since_todo >= 3:
            messages.append(
                {
                    "role": "user",
                    "content": "<reminder> Update your todos.</reminder>",
                }
            )
            session.rounds_since_todo = 0

        tools, handlers = snapshot_mcp_tools(
            runtime.mcp, *runtime.tools.snapshot()
        )
        tools = offered_tools(runtime, [tool for tool in tools if not tool_denial(session, tool["name"])])
        _update_context(runtime, tools)
        system = runtime.prompt_builder.build(session.context)
        thinking, request_max_tokens = plan_request(
            router.thinking_snapshot(),
            max_tokens,
            runtime.config.escalated_max_tokens,
        )
        request_messages = request_history(thinking)
        checkpoint(runtime)
        report = prepare_budget(runtime, system, request_messages, tools, request_max_tokens, model=state.current_model)
        if normalize_results(runtime, messages, min(4000, max(128, report.budget.input_limit * 15 // 100))):
            request_messages = request_history(thinking)
            report = prepare_budget(runtime, system, request_messages, tools, request_max_tokens, model=state.current_model)
        if report.input_tokens > report.budget.trigger:
            try:
                compacted = compact(runtime, system=system, tools=tools, model=state.current_model)
            except SessionError:
                raise
            except ValueError as exc:
                print(f'上下文压缩：{exc}')
            else:
                if compacted:
                    request_messages = request_history(thinking)
                    report = prepare_budget(runtime, system, request_messages, tools, request_max_tokens, model=state.current_model)
        if report.input_tokens > report.budget.input_limit:
            print('上下文超出输入预算，请使用 /context 查看占用并设置实际窗口。')
            return
        if thinking and thinking.get('budget_tokens', 0) + 1024 > report.budget.output_tokens:
            print('当前窗口不足以容纳所选思考预算，请调整窗口或思考档位。')
            return
        request_max_tokens = report.budget.output_tokens

        def request_call(
            system=system,
            request_messages=request_messages,
            request_max_tokens=request_max_tokens,
            thinking=thinking,
            tools=tools,
        ):
            nonlocal report
            checkpoint(runtime, status="running")
            # with_retry may change current_model; rebuild that attempt's budget.
            report = prepare_budget(runtime, system, request_messages, tools, request_max_tokens, model=state.current_model)
            if report.input_tokens > report.budget.input_limit:
                compact(runtime, system=system, tools=tools, model=state.current_model, emergency=True)
                request_messages = list(messages) if thinking else strip_thinking_blocks(messages)
                report = prepare_budget(runtime, system, request_messages, tools, request_max_tokens, model=state.current_model)
            if report.input_tokens > report.budget.input_limit:
                raise ValueError('当前备用模型窗口无法容纳请求')
            if thinking and thinking.get('budget_tokens', 0) + 1024 > report.budget.output_tokens:
                raise ValueError('当前备用模型窗口无法容纳思考预算')
            adapter = runtime.llm
            if hasattr(adapter, 'with_context_window'):
                adapter = adapter.with_context_window(resolve_window(runtime, state.current_model)[0])
            return adapter.create_streaming(
                system=system,
                messages=request_messages,
                model=state.current_model,
                max_tokens=report.budget.output_tokens,
                tools=tools,
                thinking=thinking,
            )

        try:
            response = with_retry(
                request_call,
                state,
                max_transient_retries=runtime.config.max_transient_retries,
                max_consecutive_529=runtime.config.max_consecutive_529,
                base_delay_ms=runtime.config.base_delay_ms,
                progress=null_progress if runtime.config.headless else getattr(runtime.llm, 'progress', null_progress),
            )
            usage = record_usage(runtime, response, model=state.current_model, estimated_input=report.input_tokens)
            if budget is not None:
                budget.record_model_call(usage, report.input_tokens)
        except ResponseInterrupted as exc:
            with protect_interrupt_cleanup():
                if exc.partial_text:
                    messages.append({'role': 'assistant', 'content': [
                        {'type': 'text', 'text': exc.partial_text + '\n[用户中断，回复未完成]'}]})
                    checkpoint(runtime, status='interrupted')
            raise
        except PartialStreamError as stream_exc:
            state.has_escalated = True
            max_tokens = runtime.config.escalated_max_tokens
            partial_text = stream_exc.partial_text
            if state.continuation_count < runtime.config.max_continuations:
                messages.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": partial_text}],
                    }
                )
                state.continuation_count += 1
                LOGGER.warning(
                    "stream interrupted; continuation %d/%d",
                    state.continuation_count,
                    runtime.config.max_continuations,
                )
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                continue
            cause_text = (
                f"{type(stream_exc.cause).__name__}: "
                f"{describe_model_error(stream_exc.cause)}"
            )
            marker = f"[Stream interrupted: {cause_text}]"
            separator = "" if partial_text.endswith("\n") else "\n"
            print(marker)
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": f"{partial_text}{separator}{marker}",
                        }
                    ],
                }
            )
            _update_context(runtime)
            return
        except SessionError:
            raise
        except Exception as exc:
            if (
                is_prompt_too_long_error(exc)
                and state.reactive_compact_count
                < runtime.config.max_reactive_compacts
            ):
                state.reactive_compact_count += 1
                LOGGER.warning("recovery: reactive compact")
                try:
                    compacted = compact(runtime, system=system, tools=tools, model=state.current_model, emergency=True)
                except SessionError:
                    raise
                except Exception as compact_exc:
                    append_unrecoverable_error(messages, compact_exc)
                    _update_context(runtime)
                    return
                if compacted:
                    continue
            LOGGER.error(
                "unrecoverable error: %s: %s",
                type(exc).__name__,
                str(exc)[:300],
            )
            append_unrecoverable_error(messages, exc)
            _update_context(runtime)
            return

        if response.stop_reason == "max_tokens":
            messages.append({"role": "assistant", "content": response.content})
            truncated_tool_uses = [
                block
                for block in response.content
                if _block_value(block, "type") == "tool_use"
            ]
            if truncated_tool_uses:
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": _block_value(block, "id"),
                                "content": (
                                    "Tool call was not executed because the "
                                    "response hit the output token limit."
                                ),
                                "is_error": True,
                            }
                            for block in truncated_tool_uses
                        ],
                    }
                )
            checkpoint(runtime)
            state.has_escalated = True
            max_tokens = runtime.config.escalated_max_tokens
            if state.continuation_count < runtime.config.max_continuations:
                state.continuation_count += 1
                if truncated_tool_uses:
                    messages[-1]["content"].append(
                        {"type": "text", "text": CONTINUATION_PROMPT}
                    )
                else:
                    messages.append(
                        {"role": "user", "content": CONTINUATION_PROMPT}
                    )
                LOGGER.info("output token limit, continuing response %d/%d",
                            state.continuation_count, runtime.config.max_continuations)
                continue
            print("  " + theme.paint("[max_tokens] recovery limit reached", "error"))
            _update_context(runtime)
            return

        messages.append({"role": "assistant", "content": response.content})
        checkpoint(runtime)
        if not _has_tool_use(response.content):
            force = runtime.hooks.trigger("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            if _wait_for_team_activity(runtime):
                continue
            checkpoint(runtime)
            _update_context(runtime)
            LOGGER.info("turn complete")
            return

        session.rounds_since_todo += 1
        results = []
        result_message = {"role": "user", "content": results}

        tool_blocks = {
            _block_value(block, "id"): block
            for block in response.content if _block_value(block, "type") == "tool_use"
        }

        def add_result(result):
            text = str(result.get('content', ''))
            runtime.hooks.trigger("ToolResult", tool_blocks.get(result["tool_use_id"]), result)
            result_limit = min(4000, max(128, report.budget.input_limit * 15 // 100))
            replacement = archive_result(runtime, text, result_limit)
            if replacement != text:
                result['content'] = replacement
            if not results:
                messages.append(result_message)
            results.append(result)
            checkpoint(runtime, tool_id=result["tool_use_id"], tool_status="completed")

        compacted_now = False
        repeated = 0
        for block in response.content:
            if _block_value(block, "type") != "tool_use":
                continue
            name = _block_value(block, "name")
            tool_input = _block_value(block, "input", {})
            tool_use_id = _block_value(block, "id")
            LOGGER.info("tool_call name=%s", name)
            if not repeat_limit.allow(name, tool_input):
                repeated += 1
                add_result({"type": "tool_result", "tool_use_id": tool_use_id,
                            "content": TOOL_REPEAT_ERROR, "is_error": True})
                continue
            denied = tool_denial(session, name)
            if denied:
                add_result({"type": "tool_result", "tool_use_id": tool_use_id,
                            "content": denied, "is_error": True})
                continue
            if runtime.config.headless:
                runtime.hooks.trigger("PreToolUse", block)
            if name == "compact":
                checkpoint(runtime, tool_id=tool_use_id, tool_status="running")
                # Pair every call before replacing history, including later calls
                # deliberately skipped by the compact control tool.
                completed_ids = {r["tool_use_id"] for r in results}
                for pending in response.content:
                    if _block_value(pending, "type") != "tool_use":
                        continue
                    pending_id = _block_value(pending, "id")
                    if pending_id not in completed_ids:
                        add_result({"type": "tool_result", "tool_use_id": pending_id,
                                    "content": "会话上下文已压缩" if pending_id == tool_use_id else "上下文压缩，工具未执行",
                                    "is_error": pending_id != tool_use_id})
                try:
                    compact(runtime, focus=tool_input.get('focus', ''), system=system, tools=tools, model=state.current_model)
                except SessionError:
                    raise
                except ValueError as exc:
                    print(f'上下文压缩：{exc}')
                checkpoint(runtime)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[Compacted. Continue with summarized context.]"
                        ),
                    }
                )
                compacted_now = True
                break

            if budget is not None:
                budget.record_tool_call()
            runtime.hooks.trigger("ToolStart", block)
            blocked = None if runtime.config.headless else runtime.hooks.trigger("PreToolUse", block)
            if blocked:
                add_result(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": str(blocked),
                    }
                )
                continue

            checkpoint(runtime, tool_id=tool_use_id, tool_status="running")
            if not runtime.config.headless and should_run_background(name, tool_input):
                limit = runtime.config.max_background_tasks
                if running_task_count(runtime.background) >= limit:
                    add_result({"type": "tool_result", "tool_use_id": tool_use_id, "is_error": True,
                                "content": f"Error: 已有 {limit} 个后台命令在运行，达到上限 PAMU_MAX_BACKGROUND_TASKS。"
                                           "请等待其中一个完成，或改为前台执行。"})
                    continue
                background_id = start_background_task(
                    runtime.background,
                    block,
                    handlers,
                    post_tool=lambda used_block, output: runtime.hooks.trigger(
                        "PostToolUse", used_block, output
                    ),
                    persist_output=lambda used_id, output: persist_large_output(
                        runtime.config, used_id, output
                    ),
                )
                add_result(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": (
                            f"[Background task {background_id} started] "
                            f"Command: {tool_input.get('command', '')}. "
                            "Result will be available when complete."
                        ),
                    }
                )
                continue

            try:
                output = execute_tool(block, handlers)
            except CommandInterrupted as exc:
                with protect_interrupt_cleanup():
                    add_result({'type': 'tool_result', 'tool_use_id': tool_use_id,
                                'content': str(exc), 'is_error': True})
                raise
            if name == "todo_write":
                session.rounds_since_todo = 0
            add_result(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": output,
                }
            )
            runtime.hooks.trigger("PostToolUse", block, output)

        stop_repeating = repeat_limit.should_stop(len(results), repeated)
        if compacted_now:
            continue
        user_content = results
        background_notifications = collect_background_results(runtime.background)
        user_content.extend(
            {"type": "text", "text": notification}
            for notification in background_notifications
        )
        if not results:
            messages.append({"role": "user", "content": user_content})
        checkpoint(runtime)
        if stop_repeating:
            LOGGER.info("turn stopped after repeated tool calls")
            print(TOOL_REPEAT_STOP)
            _update_context(runtime)
            return "repeated_tool_calls"
