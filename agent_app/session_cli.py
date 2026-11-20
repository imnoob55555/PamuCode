"""Select and display workspace sessions."""
import re
from datetime import datetime
from itertools import chain, pairwise
from pathlib import Path

from . import theme
from .rendering import section, transcript_entry
from .core.context_history import compacted_prefix_length
from .features.context_archive import ContextArchive
from .features.session_codec import safe_preview, safe_terminal_text
from .features.session_store import SessionError, SessionStore
from .tools.executor import TOOL_REPEAT_ERROR


def _session_label(snapshot):
    try:
        date = datetime.fromtimestamp(snapshot['updated_at']).strftime('%Y-%m-%d %H:%M')
    except (OverflowError, OSError, ValueError):
        date = '日期不可显示'
    status = {'idle': '空闲', 'running': '运行中', 'interrupted': '已中断'}[snapshot['status']]
    return f"{snapshot['id'][:8]}  {safe_preview(snapshot['title'])}  {date}  {safe_preview(snapshot['selection']['primary_model'])}  {status}"


def select_session(args, ui, *, exclude_id=None, slash=False, workdir=None, latest=False):
    usage = "/resume" if slash else "pamu resume"
    if latest and args:
        raise SessionError('用法：pamu continue 或 pamu -c')
    if len(args) > 1 or (args and args[0].startswith('-') and not (slash and args[0] == '--last')):
        options = '[--last | 会话编号]' if slash else '[会话编号]'
        raise SessionError(f'用法：{usage} {options}')
    store = SessionStore(workdir or Path.cwd())
    if args and args[0] != '--last':
        snapshot = store.load(args[0])
        if snapshot['id'] == exclude_id:
            print('当前已在此会话中。')
            return None
    else:
        snapshots, skipped = store.list_sessions()
        snapshots = [s for s in snapshots if s['id'] != exclude_id]
        if skipped:
            print(f'已跳过 {skipped} 个无法读取的会话。')
        if not snapshots:
            print('没有可恢复的会话。')
            return None
        if latest or args:
            snapshot = snapshots[0]
        else:
            choices = [(s['id'], _session_label(s)) for s in snapshots]
            if ui is None:
                print('可恢复的会话：')
                for sid, label in choices:
                    print(f'  {sid}  {label}')
                recent = f'{usage} --last' if slash else 'pamu continue 或 pamu -c'
                raise SessionError(f'请使用 {recent} 或 {usage} <会话编号> 选择会话。')
            selected = ui.choose('选择会话', choices)
            if selected is None:
                return None
            snapshot = store.load(selected)
    lease = store.acquire(snapshot['id'])
    try:
        return store.load(snapshot['id']), lease
    except BaseException:
        lease.close()
        raise


_INTERNAL_USER_TEXT = (
    '<task_notification>', '<reminder>', '[Scheduled:', '[Team inbox]',
    '[Compacted]', '[Compacted. Continue', '[Reactive compact]', '[snipped ',
    '[会话恢复]', 'Output token limit hit. Resume directly',
)


def _dialogue_entry(message):
    role = message['role']
    if role not in ('user', 'assistant'):
        return None
    content = message['content']
    texts = [content] if isinstance(content, str) else [
        block['text'] for block in content if block['type'] == 'text'
    ]
    if role == 'user':
        if any(text.startswith('[用户中断] 用户主动停止了上一轮。') for text in texts):
            return '系统', '上一轮由用户中断。'
        if message.get('_pamu_source') == 'system':
            return None
        texts = [text for text in texts if not text.startswith(_INTERNAL_USER_TEXT)]
    text = safe_terminal_text('\n'.join(texts))
    return ('用户' if role == 'user' else 'PamuCode', text) if text.strip() else None


def _tool_blocks(message, role, kind):
    if message is None or message['role'] != role or isinstance(message['content'], str):
        return ()
    return (block for block in message['content'] if block['type'] == kind)


def _shell_status(result, state):
    if result is None:
        return '未执行' if state == 'pending' else '结果未知'
    output = result['content']
    if isinstance(output, str):
        if output in ('Permission denied by user', 'Permission denied by deny list',
                      '上次会话中断，此工具未执行', '用户已取消，此命令未执行。',
                      '上下文压缩，工具未执行',
                      'Tool call was not executed because the response hit the output token limit.'):
            return '未执行'
        if result.get('is_error'):
            if output == TOOL_REPEAT_ERROR or output.startswith('Error: 规划模式禁止执行 bash'):
                return '未执行'
            if output.startswith('执行结果未知'):
                return '结果未知'
            if output.startswith('命令已中断'):
                return '已中断'
        if match := re.match(r'\[Background task (bg_\d+) started\]', output):
            return f'已提交后台 {match[1]}'
    return '结果已记录'


def _transcript_entries(messages, latest_assistant, tool_states):
    for message, following in pairwise(chain(messages, (None,))):
        if message is None:
            yield None
            continue
        if entry := _dialogue_entry(message):
            yield entry
        calls = [block for block in _tool_blocks(message, 'assistant', 'tool_use')
                 if block['name'] == 'bash' and isinstance(block['input'].get('command'), str)]
        if not calls:
            continue
        # Tool IDs can recur in later turns. Only the following user message
        # carries results for this batch of calls.
        results = {block['tool_use_id']: block for block in _tool_blocks(following, 'user', 'tool_result')}
        for call in calls:
            command = safe_terminal_text(call['input']['command']).replace('\n', '\n  ')
            state = tool_states.get(call['id']) if message is latest_assistant else None
            status = _shell_status(results.get(call['id']), state)
            yield f'Shell（{status}）', f'$ {command}'


def _legacy_dialogue(snapshot, latest_assistant):
    # V1 archives contain successive snapshots without message IDs. Their
    # retained tail may reappear at the start of the next snapshot.
    previous = []
    previous_start = None
    for history in (*snapshot['archive'], snapshot['history']):
        dialogue = list(_transcript_entries(history, latest_assistant, snapshot['tool_states']))
        start = history[0]['content'] if history else None
        reset = isinstance(start, str) and start.startswith('[Compacted]\n') and start != previous_start
        overlap = 0
        if not reset:
            for size in range(min(len(previous), len(dialogue)), 0, -1):
                if previous[-size:] == dialogue[:size]:
                    overlap = size
                    break
        yield from dialogue[overlap:]
        previous, previous_start = dialogue, start


def _session_messages(snapshot):
    history = snapshot['history']
    state = snapshot['context_state']
    records = state.get('_message_records', [])
    live = {record['id']: message for record, message in zip(records, history)}
    archived = {entry['id']: entry for entry in snapshot['archive']}
    retained_prefix = 0
    if archived:
        if fingerprint := state.get('last_compacted_output'):
            # Older compactions could assign new IDs to retained messages.
            # A matching prefix proves they were already written to the archive.
            retained_prefix = compacted_prefix_length(history, fingerprint)
        archive = ContextArchive(Path(snapshot['workspace']), snapshot['id'])
        # Archive order records first appearance, including for retained live
        # messages that compaction may have moved to anchor the latest turn.
        for key, entry in archived.items():
            message = live.get(key)
            if message is None:
                try:
                    message = archive.read_message(entry)
                except (OSError, ValueError, RecursionError):
                    yield None
                    continue
            yield message
    for index in range(retained_prefix, len(history)):
        if index < len(records) and records[index]['id'] in archived:
            continue
        yield history[index]


def _session_dialogue(snapshot):
    latest_assistant = next((m for m in reversed(snapshot['history']) if m['role'] == 'assistant'), None)
    if snapshot['schema_version'] == 1:
        yield from _legacy_dialogue(snapshot, latest_assistant)
    else:
        yield from _transcript_entries(_session_messages(snapshot), latest_assistant, snapshot['tool_states'])


def show_restored_session(snapshot, runtime):
    """Replay saved dialogue and shell records."""
    rich = theme.rich_output()
    print(theme.success(f"已恢复会话：{snapshot['id']}"))
    print(f"当前模型：{safe_preview(runtime.model.primary_model)}（{safe_preview(runtime.model.provider)}）")
    todos = runtime.session.todos
    pending = sum(item['status'] == 'pending' for item in todos)
    in_progress = sum(item['status'] == 'in_progress' for item in todos)
    completed = sum(item['status'] == 'completed' for item in todos)
    print(f"待办：{len(todos)} 项（待处理 {pending} · 进行中 {in_progress} · 已完成 {completed}）")
    labels = {'provider': '供应商', 'protocol': '协议', 'base_url': '服务地址',
              'primary_model': '模型', 'fallback_model': '降级模型', 'thinking_level': '思考程度'}
    for key, label in labels.items():
        current = getattr(runtime.model, key)
        saved = snapshot['selection'][key]
        if current != saved:
            print(f"当前配置覆盖{label}：{safe_preview(str(saved or '（无）'))} → {safe_preview(str(current or '（无）'))}")

    missing = shown = False
    try:
        if rich:
            print('\n' + section('历史对话') + '\n')
        else:
            print('\n历史对话\n')
        for entry in _session_dialogue(snapshot):
            if entry is None:
                missing = True
                continue
            role, text = entry
            if rich:
                print(transcript_entry(role, text) + '\n')
            else:
                print(f'{role}：\n{text}\n')
            shown = True
        if missing:
            print('部分归档消息无法读取。')
        elif not shown:
            print('此会话暂无历史对话。')
    except KeyboardInterrupt:
        print('\n已跳过剩余历史。')
    print('已回到此会话，可以继续输入。')
