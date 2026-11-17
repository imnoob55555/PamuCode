from agent_app.core.context_history import split_history, portable_history
from agent_app.adapters.common import ToolUseBlock


def test_tail_keeps_complete_multi_tool_batch_and_current_request():
    history = [{'role': 'user', 'content': 'keep this exact requirement'}]
    for index in range(6):
        history.extend([
            {'role': 'assistant', 'content': [ToolUseBlock(str(index), 'read_file', {})]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': str(index), 'content': 'evidence'}]},
        ])
    older, recent = split_history(history)
    assert older
    assert recent[0]['content'] == 'keep this exact requirement'
    assert len([m for m in recent if m['role'] == 'assistant']) == 2
    assert recent[-1]['content'][0]['tool_use_id'] == '5'


def test_open_batch_is_not_summarized():
    history = [{'role': 'user', 'content': 'goal'},
               {'role': 'assistant', 'content': [ToolUseBlock('open', 'bash', {})]}]
    older, recent = split_history(history)
    assert not older
    assert recent == history


def test_mixed_notification_does_not_hide_latest_user_request():
    history = [{'role': 'user', 'content': [{'type': 'text', 'text': 'do not change API'},
                {'type': 'text', 'text': '<task_notification>completed</task_notification>'}]}]
    for i in range(5):
        history.extend([{'role': 'assistant', 'content': [ToolUseBlock(str(i), 'bash', {})]},
                        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': str(i), 'content': 'done'}]}])
    old, tail = split_history(history, current_request='do not change API')
    assert tail[0] == history[0]
    assert history[0] not in old


def test_portable_history_preserves_text_without_native_payloads():
    history = [{'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'private', 'signature': 'sig'}, {'type': 'text', 'text': 'public'}]}]
    assert portable_history(history) == [{'role': 'assistant', 'content': [{'type': 'text', 'text': 'public'}]}]


def test_split_keeps_tool_pair_with_notification_at_cut():
    from agent_app.adapters.common import chat_history
    history = [
        *[{'role': 'user', 'content': f'old {i}'} for i in range(5)],
        {'role': 'assistant', 'content': [ToolUseBlock('c1', 'read_file', {})]},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'c1', 'content': 'file contents'},
            {'type': 'text', 'text': 'background notification'},
        ]},
        *[{'role': 'user', 'content': f'recent {i}'} for i in range(4)],
    ]
    older, tail = split_history(history)
    # Each side must contain either both halves of the batch or neither.
    portable = portable_history(history)
    for part in (older, tail):
        assert (portable[5] in part) == (portable[6] in part)
    assert 'file contents' in str(chat_history(None, older + tail))
