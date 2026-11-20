"""Synchronous, non-recursive subagent execution with explicit dependencies."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from .. import theme
from ..core.recovery import describe_model_error
from ..logging_setup import get_logger
from ..tools.executor import TOOL_REPEAT_ERROR, TOOL_REPEAT_STOP, ToolRepeatLimit

LOGGER = get_logger("pamu.subagents")

TASK_TOOL_SCHEMA = {
    "name": "task",
    "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Complete instructions sent verbatim to the subagent. This is the only accepted parameter.",
            }
        },
        "required": ["description"],
        "additionalProperties": False,
    },
}


def register_subagent_tool(registry, subagent_dependencies) -> None:
    """Register the non-recursive synchronous subagent tool."""
    handler = (
        subagent_dependencies["task"]
        if isinstance(subagent_dependencies, Mapping)
        else subagent_dependencies
    )
    registry.register(TASK_TOOL_SCHEMA, handler)


def extract_text(content) -> str:
    if not isinstance(content, list):
        return str(content)
    return "\n".join(
        getattr(block, "text", "")
        for block in content
        if getattr(block, "type", None) == "text"
    )


def has_tool_use(content) -> bool:
    return any(
        (block.get("type") if isinstance(block, dict) else getattr(block, "type", None))
        == "tool_use"
        for block in content
    )


def spawn_subagent(
    description: str,
    llm: Callable,
    config,
    system: str,
    tools: list[dict],
    handlers: dict,
    hooks,
    model: str | None = None,
    context=None,
) -> str:
    """Run a fresh, bounded subagent conversation using only supplied tools."""
    active_model = model or config.primary_model
    LOGGER.info("subagent started")
    if context is None:
        from ..core.delegated_context import DelegatedContext
        context = DelegatedContext(config, llm, active_model)
    retrieval_schemas, retrieval_handlers = context.retrieval_tools()
    private_names = set(retrieval_handlers)
    tools = [tool for tool in tools if tool["name"] not in private_names] + retrieval_schemas
    handlers = {**handlers, **retrieval_handlers}
    messages: list[dict] = [{"role": "user", "content": description}]
    repeat_limit = ToolRepeatLimit()

    for _ in range(30):
        try:
            response = context.request(
                llm,
                model=active_model,
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=config.default_max_tokens,
            )
        except Exception as exc:
            error = f"[Subagent error] {type(exc).__name__}: {describe_model_error(exc)}"
            print("  " + theme.paint(error, "error"))
            return error
        messages.append({"role": "assistant", "content": response.content})
        if not has_tool_use(response.content):
            break
        results: list[dict] = []
        repeated = 0
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            if not repeat_limit.allow(block.name, block.input):
                repeated += 1
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": TOOL_REPEAT_ERROR, "is_error": True})
                continue
            blocked = hooks.trigger("PreToolUse", block)
            if blocked:
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": str(blocked),
                })
                continue
            handler = handlers.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            hooks.trigger("PostToolUse", block, output)
            result = {"type": "tool_result", "tool_use_id": block.id, "content": output}
            if block.name in private_names and str(output).startswith("Error:"):
                result["is_error"] = True
            results.append(result)
        messages.append({"role": "user", "content": results})
        if repeat_limit.should_stop(len(results), repeated):
            return TOOL_REPEAT_STOP

    result = extract_text(messages[-1]["content"])
    if not result:
        for message in reversed(messages):
            result = extract_text(message["content"])
            if result:
                break
        if not result:
            result = "Subagent stopped after 30 turns without final answer."
    LOGGER.info("subagent finished")
    return result
