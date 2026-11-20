"""Response-local usage and conservative, protocol-aware request budgets."""
from dataclasses import dataclass
import json
import math

from ..adapters.anthropic import is_deepseek_messages_endpoint


def _value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def validate_window(value):
    if isinstance(value, bool):
        raise ValueError('上下文窗口必须是至少 4096 的整数')
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError('上下文窗口必须是至少 4096 的整数') from None
    if str(parsed) != str(value).strip() or parsed < 4096:
        raise ValueError('上下文窗口必须是至少 4096 的整数')
    return parsed


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    source: str = 'server'


def extract_usage(response, protocol):
    existing = _value(response, 'usage')
    if isinstance(existing, ModelUsage):
        return existing
    u = response if protocol == 'ollama' else existing
    if u is None:
        return None
    def token(key, obj=u):
        n = _value(obj, key)
        return n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else None
    read = write = None
    if protocol == 'anthropic_messages':
        inp, out = token('input_tokens'), token('output_tokens')
        read, write = token('cache_read_input_tokens'), token('cache_creation_input_tokens')
        if inp is not None:
            inp += (read or 0) + (write or 0)
    elif protocol == 'ollama':
        inp, out = token('prompt_eval_count'), token('eval_count')
    elif protocol == 'openai_chat':
        inp, out = token('prompt_tokens'), token('completion_tokens')
        read = token('cached_tokens', _value(u, 'prompt_tokens_details'))
    else:
        inp, out = token('input_tokens'), token('output_tokens')
        read = token('cached_tokens', _value(u, 'input_tokens_details'))
    return None if inp is None and out is None else ModelUsage(inp, out, read, write)


@dataclass(frozen=True)
class ContextBudget:
    window: int
    output_tokens: int

    def __post_init__(self):
        validate_window(self.window)
        if isinstance(self.output_tokens, bool) or not isinstance(self.output_tokens, int) or self.output_tokens <= 0:
            raise ValueError('输出预算必须是正整数')
        object.__setattr__(self, 'output_tokens', min(self.output_tokens, self.window // 4))

    @property
    def input_limit(self):
        return self.window - self.output_tokens - max(512, math.ceil(self.window * .05))

    @property
    def trigger(self): return self.input_limit * 80 // 100
    @property
    def target(self): return self.input_limit * 60 // 100


def _clean(value):
    from ..adapters.common import plain
    return plain(value)


def _estimate_payload(payload, messages, tools):
    size = len(json.dumps(_clean(payload), ensure_ascii=False, separators=(',', ':'), default=str).encode('utf-8'))
    return math.ceil((math.ceil(size / 2) + 16 * messages + 64 * tools + 256) * 1.1)


def estimate_request(system, messages, tools):
    from ..adapters.common import generic_blocks
    projected = [{'role': _value(m, 'role'), 'content': generic_blocks(_value(m, 'content'))} for m in messages]
    return _estimate_payload({'system': system, 'messages': projected, 'tools': tools}, len(messages), len(tools or []))


def target_key(runtime, model=None):
    router = runtime.model
    adapter = getattr(router, 'llm', None) or getattr(runtime, 'llm', None)
    return (getattr(router, 'protocol', 'anthropic_messages'), (getattr(router, 'base_url', '') or getattr(adapter, 'origin', '')).rstrip('/'), model or router.primary_model)


def _state(runtime):
    session = runtime.session
    state = getattr(session, 'context_state', None)
    if state is None:
        state = session.context.setdefault('context_budget', {})
    return state


def resolve_window(runtime, model=None):
    protocol, origin, selected = target_key(runtime, model)
    configured = getattr(runtime.config, 'context_window', None)
    bound = getattr(runtime.config, 'context_window_target', None)
    if configured is not None and (tuple(bound) == (protocol, origin, selected) if bound else selected == runtime.config.primary_model):
        return validate_window(configured), '明确配置 PAMU_CONTEXT_WINDOW'
    key = '|'.join((protocol, origin, selected))
    override = _state(runtime).get('window_overrides', {}).get(key)
    if override is not None:
        return validate_window(override), '当前模型会话覆盖'
    if protocol == 'anthropic_messages':
        if origin == 'https://api.anthropic.com' and selected in ('claude-sonnet-4-6', 'claude-opus-4-6'):
            return 200000, 'Anthropic 官方目标的保守规划值（200k；可明确配置调整）'
        if is_deepseek_messages_endpoint(origin) and selected in ('deepseek-v4-pro', 'deepseek-flash', 'deepseek-v4-flash'):
            return 1_000_000, 'DeepSeek 官方模型窗口（1M；可明确配置调整）'
    return (4096 if protocol == 'ollama' else 16384), '假定窗口，请按实际服务配置调整'


@dataclass(frozen=True)
class BudgetEstimate:
    budget: ContextBudget
    input_tokens: int
    source: str
    window_source: str
    categories: dict

    @property
    def remaining(self): return self.budget.input_limit - self.input_tokens


def prepare_budget(runtime, system, messages, tools, requested_max_tokens, model=None):
    window, source = resolve_window(runtime, model)
    selected = model or runtime.model.primary_model
    adapter = getattr(runtime.model, 'llm', None) or runtime.llm
    if hasattr(adapter, 'project_request'):
        payload = adapter.project_request(system=system, messages=messages, tools=tools, model=selected)
        count = _estimate_payload(payload, len(messages), len(tools or []))
    else:
        count = estimate_request(system, messages, tools)
    ratios = _state(runtime).get('usage_ratios', {}).get(json.dumps(target_key(runtime, selected)), [])
    count = math.ceil(count * max([1.0, *ratios]))
    return BudgetEstimate(ContextBudget(window, requested_max_tokens), count, '离线估算（UTF-8 字节启发式，含安全余量）', source,
                          {'system': estimate_request(system, [], []), 'messages': estimate_request(None, messages, []), 'tools': estimate_request(None, [], tools)})


def record_usage(runtime, response, model=None, estimated_input=None):
    usage = extract_usage(response, target_key(runtime, model)[0])
    if usage is None:
        return None
    from dataclasses import asdict
    state = _state(runtime)
    state['last_usage'] = {**asdict(usage), 'target': list(target_key(runtime, model))}
    if estimated_input and usage.input_tokens:
        key = json.dumps(target_key(runtime, model))
        ratios = state.setdefault('usage_ratios', {}).setdefault(key, [])
        ratios.append(max(1.0, usage.input_tokens / estimated_input))
        del ratios[:-8]
    return usage
