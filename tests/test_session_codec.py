from dataclasses import dataclass
import pytest
from agent_app.adapters.common import ToolUseBlock, attach_replay, generic_blocks, native_replay, anthropic_history
from agent_app.features.session_codec import encode_history, decode_history, safe_preview


def test_roundtrip_replay_and_scope():
    blocks = [ToolUseBlock('call-1', 'read_file', {'path': 'a.py'})]
    attach_replay(blocks, 'openai_responses', 'http://localhost/v1', 'm', [{'type': 'function_call', 'call_id': 'call-1'}])
    history = [{'role': 'assistant', 'content': blocks}, {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call-1', 'content': 'bad', 'is_error': True}]}, {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'reason', 'signature': 'sig', '_pamu_scope': ['anthropic_messages', 'origin', 'm']}]}]
    restored = decode_history(encode_history(history))
    assert generic_blocks(restored[0]['content']) == generic_blocks(blocks)
    assert native_replay(restored[0]['content'], 'openai_responses', 'http://localhost/v1', 'm')
    assert native_replay(restored[0]['content'], 'ollama', 'http://localhost/v1', 'm') is None
    assert anthropic_history(restored[2:], origin='origin', model='m')
    assert not anthropic_history(restored[2:], origin='other', model='m')
    restored[0]['content'][0]['input']['path'] = 'changed'
    assert native_replay(restored[0]['content'], 'openai_responses', 'http://localhost/v1', 'm') is None


def test_sdk_and_text():
    @dataclass
    class Block:
        type: str = 'text'
        text: str = 'hello'
    assert decode_history(encode_history([{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': [Block()]}])) == [{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': [{'type': 'text', 'text': 'hello'}]}]


@pytest.mark.parametrize('history', [[{'role': 'user', 'content': [{'type': 'unknown'}]}], [{'role': 'user', 'content': [{'type': 'tool_use', 'id': 'x', 'name': 'x', 'input': []}]}], [{'role': 'bad', 'content': 'x'}], [{'role': 'user', 'content': object()}]])
def test_reject_bad_history(history):
    with pytest.raises(ValueError):
        encode_history(history)


def test_preview():
    assert safe_preview('\x1b[31mhello\x1b[0m\nworld\x00\x07') == 'hello world'
    assert len(safe_preview('a' * 100, 8)) <= 8


def test_sdk_namespace_and_redacted_thinking():
    from types import SimpleNamespace
    history = [SimpleNamespace(role='assistant', content=[SimpleNamespace(type='text', text='sdk'), {'type': 'redacted_thinking', 'data': 'opaque', '_pamu_scope': ['anthropic_messages', '', 'm']}])]
    restored = decode_history(encode_history(history))
    assert restored[0]['content'][0] == {'type': 'text', 'text': 'sdk'}
    assert anthropic_history(restored, origin='', model='m')[0]['content'][1]['data'] == 'opaque'


@pytest.mark.parametrize('history', [
    [{'role': 'user', 'content': 'hello', 'headers': {'secret': 'fake'}}],
    [{'role': 'assistant', 'content': [{'type': 'text', 'text': 'x', 'unknown': 1}]}],
    [{'role': 'assistant', 'content': [{'type': 'text', 'text': 'x', '_pamu_replay': {'protocol':'unknown'}}]}],
    [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'content': [{'type': 'image', 'source': 'x'}]}]}],
])
def test_decode_rejects_unknown_or_invalid_metadata(history):
    with pytest.raises(ValueError): decode_history(history)


@pytest.mark.parametrize('block', [
    {'type': []},
    {'type': 'text', 'text': 'x', '_pamu_replay': {'protocol': [], 'origin': '', 'model': 'm', 'payload': [], 'fingerprint': ''}},
])
def test_bad_types_raise_value_error(block):
    with pytest.raises(ValueError):
        decode_history([{'role': 'assistant', 'content': [block]}])
