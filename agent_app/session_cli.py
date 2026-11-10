"""Workspace session selection before runtime construction."""
from datetime import datetime
from pathlib import Path

from .features.session_codec import safe_preview
from .features.session_store import SessionError, SessionStore


def _session_label(snapshot):
    try:
        date = datetime.fromtimestamp(snapshot['updated_at']).strftime('%Y-%m-%d %H:%M')
    except (OverflowError, OSError, ValueError):
        date = '日期不可显示'
    status = {'idle': '空闲', 'running': '运行中', 'interrupted': '已中断'}[snapshot['status']]
    return f"{snapshot['id'][:8]}  {safe_preview(snapshot['title'])}  {date}  {safe_preview(snapshot['selection']['primary_model'])}  {status}"


def select_session(args, ui, *, exclude_id=None, slash=False, workdir=None):
    usage = "/resume" if slash else "pamu resume"
    if len(args) > 1 or (args and args[0].startswith('-') and args[0] != '--last'):
        raise SessionError(f'用法：{usage} [--last | 会话编号]')
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
        if args:
            snapshot = snapshots[0]
        else:
            choices = [(s['id'], _session_label(s)) for s in snapshots]
            if ui is None:
                print('可恢复的会话：')
                for sid, label in choices:
                    print(f'  {sid}  {label}')
                raise SessionError(f'请使用 {usage} --last 或 {usage} <会话编号> 选择会话。')
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


def show_restored_session(snapshot, runtime):
    """Show only bounded visible conversation text, never tool/reasoning payloads."""
    print(f"已恢复会话：{snapshot['id']}")
    print(f"当前模型：{safe_preview(runtime.model.primary_model)}（{safe_preview(runtime.model.provider)}）")
    todos = runtime.session.todos
    pending = sum(item['status'] == 'pending' for item in todos)
    in_progress = sum(item['status'] == 'in_progress' for item in todos)
    completed = sum(item['status'] == 'completed' for item in todos)
    print(f"待办：{len(todos)} 项（待处理 {pending} · 进行中 {in_progress} · 已完成 {completed}）")
    previews = []
    for message in snapshot['history']:
        if message['role'] not in ('user', 'assistant'):
            continue
        content = message['content']
        text = content if isinstance(content, str) else ' '.join(
            block.get('text', '') for block in content if block.get('type') == 'text'
        )
        text = safe_preview(text, 160)
        if text:
            previews.append(('用户' if message['role'] == 'user' else '助手', text))
    for role, text in previews[-4:]:
        print(f"  {role}：{text}")
    labels = {'provider': '供应商', 'protocol': '协议', 'base_url': '服务地址',
              'primary_model': '模型', 'fallback_model': '降级模型', 'thinking_level': '思考程度'}
    for key, label in labels.items():
        current = getattr(runtime.model, key)
        saved = snapshot['selection'][key]
        if current != saved:
            print(f"当前配置覆盖{label}：{safe_preview(str(saved or '（无）'))} → {safe_preview(str(current or '（无）'))}")
