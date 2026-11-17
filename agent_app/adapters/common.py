"""Protocol-neutral blocks and history-bound native replay."""
from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from ..core.recovery import PartialStreamError


@dataclass(frozen=True, slots=True)
class NativeReplay:
    protocol: str
    origin: str
    model: str
    payload: Any = field(repr=False)
    fingerprint: str = field(repr=False)


@dataclass(slots=True)
class TextBlock:
    text: str
    type: str = field(default='text', init=False)
    replay: NativeReplay | None = field(default=None, repr=False, compare=False)


@dataclass(slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = field(default='tool_use', init=False)
    replay: NativeReplay | None = field(default=None, repr=False, compare=False)


@dataclass(slots=True)
class ModelResponse:
    content: list
    stop_reason: str
    usage: Any = None


class ProtocolResponseError(ValueError):
    """Structured error delivered inside a successful HTTP/SSE response."""

    def __init__(self, error, message):
        code = value(error, 'code') or value(error, 'type')
        self.code = code if isinstance(code, str) else ''
        self.status_code = {
            'rate_limit_exceeded': 429, 'overloaded_error': 529,
            'invalid_api_key': 401, 'authentication_error': 401,
            'server_error': 500,
        }.get(self.code)
        # Keep only a bounded machine-readable code, not raw service details.
        safe_code = self.code if self.code.replace('_', '').isalnum() and len(self.code) <= 80 else 'unknown'
        super().__init__(f'{message}（{safe_code}）')


def value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def plain(obj):
    """Copy SDK objects to JSON data, excluding internal metadata."""
    if isinstance(obj, dict):
        return {k: plain(v) for k, v in obj.items() if not k.startswith('_pamu') and k != 'replay'}
    if isinstance(obj, (list, tuple)):
        return [plain(v) for v in obj]
    if hasattr(obj, 'model_dump'):
        return plain(obj.model_dump(exclude_none=True))
    if hasattr(obj, '__dict__'):
        return plain(vars(obj))
    return obj


def arguments(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError('模型返回的工具参数不是完整 JSON') from exc
    if not isinstance(raw, dict):
        raise ValueError('模型返回的工具参数必须是 JSON 对象')
    return deepcopy(raw)


def tool_block(id, name, raw):
    if not isinstance(id, str) or not id or not isinstance(name, str) or not name:
        raise ValueError('模型返回的工具调用缺少 ID 或名称')
    return ToolUseBlock(id, name, arguments(raw))


def content_blocks(content):
    if isinstance(content, str):
        return [TextBlock(content)]
    if not isinstance(content, (list, tuple)):
        raise ValueError('消息内容必须是文本或内容块列表')
    return content


def generic_blocks(content):
    result = []
    for block in content_blocks(content):
        kind = value(block, 'type')
        if kind == 'text':
            result.append({'type': 'text', 'text': value(block, 'text', '')})
        elif kind == 'tool_use':
            call = tool_block(value(block, 'id'), value(block, 'name'), value(block, 'input'))
            result.append({'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.input})
        elif kind == 'tool_result':
            item = {'type': 'tool_result', 'tool_use_id': value(block, 'tool_use_id'), 'content': plain(value(block, 'content', ''))}
            if value(block, 'is_error'):
                item['is_error'] = True
            result.append(item)
    return result


def fingerprint(blocks):
    return json.dumps(generic_blocks(blocks), sort_keys=True, ensure_ascii=False)


def attach_replay(blocks, protocol, origin, model, payload):
    if blocks:
        blocks[0].replay = NativeReplay(protocol, origin, model, deepcopy(plain(payload)), fingerprint(blocks))


def native_replay(content, protocol, origin, model):
    for block in content_blocks(content):
        replay = value(block, 'replay')
        if isinstance(replay, NativeReplay) and (replay.protocol, replay.origin, replay.model) == (protocol, origin, model) and replay.fingerprint == fingerprint(content):
            return deepcopy(replay.payload)
    return None


def finish(blocks, reason):
    ids = [block.id for block in blocks if block.type == 'tool_use']
    if len(ids) != len(set(ids)):
        raise ValueError('模型返回重复的工具调用 ID')
    if not blocks and reason != 'max_tokens':
        raise ValueError('模型返回空响应')
    return ModelResponse(blocks, reason)


def check_thinking(thinking):
    if thinking is not None and thinking != 'off' and thinking != {'type': 'disabled'}:
        raise ValueError('当前协议暂不支持非 off 思考设置')


def function_tools(tools, *, responses=False):
    converted = []
    for tool in tools or []:
        fn = {'name': tool['name'], 'description': tool.get('description', ''), 'parameters': tool.get('input_schema', {'type': 'object', 'properties': {}})}
        converted.append({'type': 'function', **fn, 'strict': False} if responses else {'type': 'function', 'function': fn})
    return converted


def output_text(content, *, separator="\n"):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return separator.join(str(value(b, 'text', '')) for b in content if value(b, 'type') == 'text')
    return json.dumps(plain(content), ensure_ascii=False)


def chat_history(system, messages, *, ollama=False, origin='', model=''):
    result, names = [], {}
    if system is not None:
        result.append({'role': 'system', 'content': output_text(system)})
    for message in messages:
        role, content = value(message, 'role'), value(message, 'content')
        blocks = generic_blocks(content)
        text = ''.join(b['text'] for b in blocks if b['type'] == 'text')
        calls = [b for b in blocks if b['type'] == 'tool_use']
        if role == 'assistant':
            item = {'role': 'assistant', 'content': text if text or ollama else None}
            native = native_replay(content, 'ollama', origin, model) if ollama else None
            if native and native.get('thinking'):
                item['thinking'] = native['thinking']
            if calls:
                item['tool_calls'] = []
                for call in calls:
                    names[call['id']] = call['name']
                    fn = {'name': call['name'], 'arguments': call['input'] if ollama else json.dumps(call['input'], separators=(',', ':'), ensure_ascii=False)}
                    item['tool_calls'].append({'function': fn} if ollama else {'id': call['id'], 'type': 'function', 'function': fn})
            if text or calls:
                result.append(item)
        else:
            for block in blocks:
                if block['type'] == 'text' and block['text']:
                    result.append({'role': role, 'content': block['text']})
                elif block['type'] == 'tool_result':
                    id = block['tool_use_id']
                    if id not in names:
                        raise ValueError('工具结果缺少对应调用')
                    result.append({'role': 'tool', 'tool_name' if ollama else 'tool_call_id': names[id] if ollama else id, 'content': output_text(block['content'])})
    return result


class NullProgress:
    def __enter__(self): return self
    def stop(self): pass
    def __exit__(self, *exc): return False


def null_progress(_label):
    return NullProgress()


@contextmanager
def visible_stream(progress_factory):
    """Share progress, visible partial-text errors and newline semantics."""
    chunks = []
    try:
        with progress_factory('Working') as progress:
            def emit(text):
                if not isinstance(text, str):
                    raise ValueError('模型返回的文本格式无效')
                if text:
                    if not chunks:
                        progress.stop()
                    chunks.append(text)
                    print(text, end='', flush=True)
            yield emit
    except Exception as exc:
        if chunks:
            raise PartialStreamError(''.join(chunks), exc) from exc
        raise
    finally:
        if chunks and not chunks[-1].endswith('\n'):
            print()


def anthropic_history(messages, *, origin='', model=''):
    cleaned = []
    for message in messages:
        content = value(message, 'content')
        if isinstance(content, str):
            cleaned.append({'role': value(message, 'role'), 'content': content})
            continue
        blocks = []
        for block in content_blocks(content):
            kind = value(block, 'type')
            if kind in ('thinking', 'redacted_thinking'):
                scope = value(block, '_pamu_scope')
                if isinstance(scope, (list, tuple)) and tuple(scope) == ('anthropic_messages', origin, model):
                    blocks.append(plain(block))
            else:
                blocks.extend(generic_blocks([block]))
        if blocks:
            cleaned.append({'role': value(message, 'role'), 'content': blocks})
    return cleaned


def scope_anthropic(response, origin, model):
    # Return the original SDK response, preserving the established injection API.
    for block in value(response, 'content', []) or []:
        if value(block, 'type') in ('thinking', 'redacted_thinking'):
            scope = ('anthropic_messages', origin, model)
            if isinstance(block, dict):
                block['_pamu_scope'] = scope
            else:
                object.__setattr__(block, '_pamu_scope', scope)
    return response
