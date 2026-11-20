import random
import time
from contextlib import nullcontext
from dataclasses import dataclass

from .. import theme
from ..logging_setup import get_logger

LOGGER = get_logger("pamu.recovery")


@dataclass(slots=True)
class RecoveryState:
    current_model: str
    fallback_model: str | None = None
    consecutive_529: int = 0
    continuation_count: int = 0
    reactive_compact_count: int = 0
    has_escalated: bool = False


class PartialStreamError(Exception):
    def __init__(self, partial_text: str, cause: Exception):
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.partial_text = partial_text
        self.cause = cause


def get_status_code(exc):
    status_code = getattr(exc, "status_code", None)
    if status_code:
        return status_code

    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def is_rate_limit_error(exc):
    message = str(exc).lower()

    return (
        get_status_code(exc) == 429
        or "ratelimit" in message
        or "rate limit" in message
        or "429" in message
    )


def is_overloaded_error(exc):
    name = type(exc).__name__.lower()
    message = str(exc).lower()

    return (
        get_status_code(exc) == 529
        or "overloaded" in name
        or "overloaded" in message
        or "529" in message
    )


def is_prompt_too_long_error(exc):
    message = str(exc).lower()
    return (
        "prompt_is_too_long" in message
        or "context_length_exceeded" in message
        or "max_context_window" in message
        or ("prompt" in message and "too long" in message)
    )


def extract_retry_after(exc):
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)

    if not headers:
        headers = getattr(exc, "headers", None)

    if not headers:
        return None

    value = headers.get("retry-after")

    try:
        delay = float(value)
    except (TypeError, ValueError):
        return None

    return delay if delay > 0 else None


def retry_delay(
    attempt: int,
    retry_after: float | None = None,
    *,
    base_delay_ms: int = 500,
) -> float:
    if retry_after:
        return retry_after

    base = min(base_delay_ms * (2 ** attempt), 32000) / 1000
    jitter = random.uniform(0, base * 0.25)  # noqa: S311 - retry jitter only
    return base + jitter


def with_retry(
    fn,
    state: RecoveryState,
    *,
    max_transient_retries: int = 10,
    max_consecutive_529: int = 3,
    base_delay_ms: int = 500,
    progress=nullcontext,
):
    for attempt in range(max_transient_retries):
        try:
            response = fn()
            state.consecutive_529 = 0
            return response
        except PartialStreamError:
            raise
        except Exception as e:
            is_429 = is_rate_limit_error(e)
            is_529 = is_overloaded_error(e)

            if not is_429 and not is_529:
                raise

            if is_429:
                state.consecutive_529 = 0

            if is_529:
                state.consecutive_529 += 1
                if state.consecutive_529 >= max_consecutive_529:
                    if (
                        state.fallback_model
                        and state.current_model != state.fallback_model
                    ):
                        state.current_model = state.fallback_model
                        state.consecutive_529 = 0
                        LOGGER.warning("switching to fallback model %s after %d overloads",
                                       state.fallback_model, max_consecutive_529)
                    else:
                        state.consecutive_529 = 0
                        LOGGER.warning("no fallback model after %d overloads", max_consecutive_529)

            if attempt == max_transient_retries - 1:
                raise

            delay = retry_delay(
                attempt,
                extract_retry_after(e),
                base_delay_ms=base_delay_ms,
            )
            LOGGER.warning("%s, retry %d/%d after %.1fs", "rate limited" if is_429 else "overloaded",
                           attempt + 1, max_transient_retries, delay)
            with progress("Working"):
                time.sleep(delay)

    raise RuntimeError("unreachable")


def describe_model_error(exc):
    """Keep status-based recovery intact and give users an actionable message."""
    status = get_status_code(exc)
    if is_prompt_too_long_error(exc):
        return "请求超过服务的上下文窗口，请使用 /context 检查窗口设置或缩减当前请求。"
    if status in (401, 403):
        return "服务认证失败，请检查当前供应商的 API Key 或访问权限。"
    if status == 404:
        return "模型或接口不存在，请检查模型 ID、服务地址及协议；Ollama 请确认模型已安装。"
    if status in (400, 422):
        return "服务拒绝了请求，请检查接口协议、模型的工具调用能力及请求参数。"
    if status == 429:
        return "服务限流或额度不足，请稍后重试并检查账户额度。"
    if status and status >= 500:
        return "模型服务暂时不可用，请检查服务状态后重试。"
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "模型请求超时，请检查服务状态、网络和本地模型运行情况。"
    if "connect" in name:
        return "无法连接模型服务，请检查服务地址、网络及服务是否已启动。"
    return str(exc)[:300]


def append_unrecoverable_error(messages, exc):
    name = type(exc).__name__
    detail = describe_model_error(exc)
    text = f"[Error] {name}: {detail}"

    messages.append({
        "role": "assistant",
        "content": [{
            "type": "text",
            "text": text,
        }],
    })

    print("  " + theme.paint(f"[unrecoverable] {name}: {detail[:100]}", "error"))
