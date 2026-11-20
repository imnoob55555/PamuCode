"""Pure, protocol-neutral planning at complete tool-batch boundaries."""
from copy import deepcopy
from hashlib import sha256
import json

from ..adapters.common import generic_blocks


def portable_history(messages, *, sources=None):
    result = []
    for message in messages:
        content = message['content']
        if not isinstance(content, str):
            content = generic_blocks(content)
        if content:
            metadata = {k: v for k, v in message.items() if k in ('_pamu_id', '_pamu_source', '_pamu_turn_id')}
            projected = {'role': message['role'], 'content': deepcopy(content), **metadata}
            result.append(projected)
            if sources is not None:
                sources[id(projected)] = message
    return result


def compacted_prefix_length(history, fingerprint):
    """Find a saved compaction result still present at the start of history."""
    # Match the JSON list used for the compaction fingerprint in one pass.
    digest = sha256(b'[')
    separator = b''
    for index, message in enumerate(history, 1):
        projected = portable_history([message])
        if not projected:
            continue
        digest.update(separator)
        digest.update(json.dumps(projected[0], ensure_ascii=False).encode('utf-8', errors='surrogatepass'))
        candidate = digest.copy()
        candidate.update(b']')
        if candidate.hexdigest() == fingerprint:
            return index
        separator = b', '
    return 0


def user_text(message):
    if message['role'] != 'user' or message.get('_pamu_source') == 'system':
        return ''
    content = message['content']
    if isinstance(content, str):
        return content
    texts = [b['text'] for b in generic_blocks(content) if b['type'] == 'text']
    return next((t for t in texts if not t.startswith(('<task_notification>', '[Scheduled:', '[Team inbox]', '<reminder>'))), '')


def split_history(messages, keep_batches=2, current_request=None, *, tail_sources=None):
    """Keep recent closed batches and the latest explicit user request once."""
    calls = []
    for index, message in enumerate(messages):
        blocks = generic_blocks(message['content'])
        ids = {b['id'] for b in blocks if b['type'] == 'tool_use'}
        if ids:
            if index + 1 >= len(messages):
                return [], deepcopy(messages)
            results = generic_blocks(messages[index + 1]['content'])
            if not ids <= {b['tool_use_id'] for b in results if b['type'] == 'tool_result'}:
                return [], deepcopy(messages)
            calls.append(index)
    users = [i for i, m in enumerate(messages) if user_text(m)
             and not user_text(m).startswith(('[Compacted]', '[Reactive compact]', '<reminder>', '[Scheduled:', '[Team inbox]', 'Output token limit hit.', '[Compacted. Continue'))]
    actual = [i for i in users if user_text(messages[i]) == current_request] if current_request else []
    if actual:
        users = [i for i in users if i <= actual[-1]]
    anchor = users[-1] if users else None
    if len(calls) > keep_batches:
        cut = calls[-keep_batches]
    elif len(users) > 2:
        cut = users[-2]
    else:
        return [], deepcopy(messages)
    old = list(messages[:cut])
    tail = list(messages[cut:])
    if anchor is not None and anchor < cut:
        old = [m for i, m in enumerate(messages[:cut]) if i != anchor]
        tail.insert(0, messages[anchor])
    return portable_history(old), portable_history(tail, sources=tail_sources)
