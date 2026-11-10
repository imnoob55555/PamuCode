"""Explicit, JSON-only conversation snapshots with scoped native replay."""
from __future__ import annotations

import math
import re
import unicodedata

from ..adapters.common import NativeReplay, value

_PROTOCOLS = {'anthropic_messages', 'openai_responses', 'openai_chat', 'ollama'}


def _json(data):
    if data is None or isinstance(data, (str, bool, int)):
        return data
    if isinstance(data, float) and math.isfinite(data):
        return data
    if isinstance(data, (list, tuple)):
        return [_json(item) for item in data]
    if isinstance(data, dict) and all(isinstance(key, str) for key in data):
        return {key: _json(item) for key, item in data.items()}
    raise ValueError('会话数据格式无效')


def _string(data, nonempty=False):
    if not isinstance(data, str) or (nonempty and not data):
        raise ValueError('会话文本格式无效')
    return data


def _block(block, decoding=False):
    kind = value(block, 'type')
    fields = {'text': ('text',), 'tool_use': ('id', 'name', 'input'), 'tool_result': ('tool_use_id', 'content'), 'thinking': ('thinking', 'signature'), 'redacted_thinking': ('data',)}
    if not isinstance(kind, str) or kind not in fields:
        raise ValueError('会话内容块类型无效')
    allowed = {'type', *fields[kind]}
    if kind == 'tool_result':
        allowed.add('is_error')
    if kind in ('thinking', 'redacted_thinking'):
        allowed.add('_pamu_scope')
    allowed.add('_pamu_replay' if decoding else 'replay')
    if isinstance(block, dict) and not set(block) <= allowed:
        raise ValueError('会话内容块字段无效')
    result = {'type': kind}
    for key in fields[kind]:
        item = value(block, key)
        if key == 'input':
            if not isinstance(item, dict):
                raise ValueError('会话工具参数格式无效')
            result[key] = _json(item)
        elif key == 'content':
            if not isinstance(item, (str, list)):
                raise ValueError('会话工具结果格式无效')
            result[key] = item if isinstance(item, str) else [_block(b, decoding) for b in item]
        else:
            result[key] = _string(item, key in ('id', 'name', 'tool_use_id'))
    error = value(block, 'is_error')
    if kind == 'tool_result' and error is not None:
        if not isinstance(error, bool):
            raise ValueError('会话工具结果格式无效')
        result['is_error'] = error
    scope = value(block, '_pamu_scope')
    if scope is not None:
        if kind not in ('thinking', 'redacted_thinking') or not isinstance(scope, (list, tuple)) or len(scope) != 3 or scope[0] != 'anthropic_messages' or not all(isinstance(s, str) for s in scope):
            raise ValueError('会话思考范围无效')
        result['_pamu_scope'] = list(scope)
    replay = value(block, '_pamu_replay') if decoding else value(block, 'replay')
    if replay is not None:
        if decoding:
            if not isinstance(replay, dict) or set(replay) != {'protocol', 'origin', 'model', 'payload', 'fingerprint'}:
                raise ValueError('会话重放数据无效')
            replay = NativeReplay(**replay)
        if not isinstance(replay, NativeReplay) or not isinstance(replay.protocol, str) or replay.protocol not in _PROTOCOLS:
            raise ValueError('会话重放协议无效')
        data = dict(protocol=replay.protocol, origin=_string(replay.origin), model=_string(replay.model), payload=_json(replay.payload), fingerprint=_string(replay.fingerprint))
        result['replay' if decoding else '_pamu_replay'] = NativeReplay(**data) if decoding else data
    return result


def _history(messages, decoding):
    if not isinstance(messages, list):
        raise ValueError('会话历史必须是列表')
    result = []
    for message in messages:
        if isinstance(message, dict) and not set(message) <= {'role', 'content', '_pamu_id', '_pamu_source', '_pamu_turn_id'}:
            raise ValueError('会话消息字段无效')
        role, content = value(message, 'role'), value(message, 'content')
        if role not in ('user', 'assistant', 'system'):
            raise ValueError('会话角色无效')
        if not isinstance(content, (str, list, tuple)):
            raise ValueError('会话内容格式无效')
        item = {'role': role, 'content': content if isinstance(content, str) else [_block(block, decoding) for block in content]}
        for key in ('_pamu_id', '_pamu_source', '_pamu_turn_id'):
            metadata = value(message, key)
            if metadata is not None:
                item[key] = _string(metadata, True)
        result.append(item)
    return result


def encode_history(messages: list) -> list[dict]:
    return _history(messages, False)


def decode_history(data: list) -> list[dict]:
    return _history(data, True)


def safe_preview(text: str, limit: int = 80) -> str:
    text = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', text)
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    text = ''.join(' ' if char.isspace() else char for char in text)
    text = ''.join(char for char in text if not unicodedata.category(char).startswith('C'))
    return ' '.join(text.split())[:max(0, limit)]
