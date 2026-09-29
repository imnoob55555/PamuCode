from types import SimpleNamespace

from agent_app.thinking import (
    DEFAULT_THINKING_LEVEL,
    THINKING_BUDGETS,
    THINKING_COLORS,
    THINKING_LEVELS,
    normalize_level,
    plan_request,
    strip_thinking_blocks,
    style_class,
)


def _type(block):
    return block["type"] if isinstance(block, dict) else block.type


def test_levels_colors_and_budgets_are_aligned():
    assert THINKING_LEVELS == ("off", "minimal", "low", "medium", "high")
    assert DEFAULT_THINKING_LEVEL == "off"
    assert set(THINKING_COLORS) == set(THINKING_LEVELS)
    assert THINKING_BUDGETS == {
        "minimal": 1024,
        "low": 2048,
        "medium": 8192,
        "high": 16384,
    }


def test_normalize_level_accepts_case_and_space():
    assert normalize_level(" High ") == "high"
    assert normalize_level("off") == "off"
    assert normalize_level("turbo") is None
    assert normalize_level(None) is None


def test_style_class_falls_back_to_off():
    assert style_class("high") == "class:thinking-high"
    assert style_class("turbo") == "class:thinking-off"


def test_plan_request_omits_thinking_for_off():
    assert plan_request("off", 8000, 64000) == (None, 8000)


def test_plan_request_grows_max_tokens_by_the_budget():
    payload, limit = plan_request("medium", 8000, 64000)

    assert payload == {"type": "enabled", "budget_tokens": 8192}
    assert limit == 16192


def test_plan_request_caps_at_ceiling_and_keeps_answer_room():
    payload, limit = plan_request("high", 64000, 64000)
    assert limit == 64000
    assert payload == {"type": "enabled", "budget_tokens": 16384}

    payload, limit = plan_request("high", 1000, 1000)
    assert (payload, limit) == (None, 1000)


def test_strip_thinking_blocks_keeps_other_blocks_and_messages():
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "..."},
                {"type": "redacted_thinking", "data": "..."},
                {"type": "text", "text": "answer"},
                SimpleNamespace(type="tool_use", id="t1", name="bash", input={}),
            ],
        },
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "only"}]},
    ]

    cleaned = strip_thinking_blocks(messages)

    assert cleaned[0] == messages[0]
    assert [_type(block) for block in cleaned[1]["content"]] == ["text", "tool_use"]
    assert len(cleaned) == 2


def test_strip_thinking_blocks_leaves_string_content_untouched():
    messages = [{"role": "user", "content": "hi"}]

    assert strip_thinking_blocks(messages) == messages
