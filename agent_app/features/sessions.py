"""Semantic conversation checkpoints; restoring never dispatches historical work."""
from __future__ import annotations

import copy
import re
import threading
import time
import uuid
from pathlib import Path

from .session_codec import decode_history, encode_history, safe_preview
from .session_store import SessionError, SessionLease, SessionStore

_SELECTION = ('provider', 'protocol', 'base_url', 'primary_model', 'fallback_model', 'thinking_level')
_INTERRUPTED_BACKGROUND = '[会话恢复] 之前的后台任务或子代理无法自动恢复，请检查实际状态。'


def _blocks(message, kind):
    content = message.get('content', [])
    return [b for b in content if isinstance(b, dict) and b.get('type') == kind] if isinstance(content, list) else []


class SessionPersistence:
    def __init__(self, workdir: Path):
        self.store = SessionStore(Path(workdir).resolve())
        self.session_id: str | None = None
        self.failed = False
        self._closed = False
        self._lock = threading.RLock()
        self._lease: SessionLease | None = None
        self._snapshot = None
        self._archive = []
        self._tool_states = {}
        self._batch = None
        self._status = 'idle'

    def _check(self):
        if self._closed or self.failed:
            raise SessionError('会话保存已停止，请重新启动后恢复会话')

    def checkpoint(self, runtime, *, status=None, tool_id=None, tool_status=None):
        with self._lock:
            self._check()
            try:
                history = encode_history(runtime.session.history)
                if not history and self.session_id is None:
                    return
                router = runtime.model
                with router.lock:
                    selection = {key: getattr(router, key) for key in _SELECTION}
                if status is not None:
                    self._status = status
                # IDs are only meaningful within the latest assistant batch.
                batch = next((i for i in range(len(history) - 1, -1, -1)
                              if history[i]['role'] == 'assistant'), None)
                calls = _blocks(history[batch], 'tool_use') if batch is not None else []
                signature = (batch, tuple(b['id'] for b in calls))
                if signature != self._batch:
                    self._batch = signature
                    self._tool_states = {b['id']: 'pending' for b in calls}
                if calls and batch + 1 < len(history):
                    for b in _blocks(history[batch + 1], 'tool_result'):
                        self._tool_states[b['tool_use_id']] = 'completed'
                if tool_id is not None:
                    self._tool_states[tool_id] = tool_status
                now = time.time()
                if self.session_id is None:
                    self.session_id = str(uuid.uuid4())
                    self._lease = self.store.acquire(self.session_id)
                first = next((m['content'] for m in history if m['role'] == 'user'), '')
                if not isinstance(first, str):
                    first = ' '.join(b.get('text', '') for b in first if b.get('type') == 'text')
                snapshot = dict(
                    schema_version=1, id=self.session_id, workspace=str(self.store.workdir),
                    created_at=self._snapshot['created_at'] if self._snapshot else now,
                    updated_at=self._snapshot['updated_at'] if self._snapshot else now,
                    title=self._snapshot['title'] if self._snapshot else safe_preview(first),
                    history=history, archive=copy.deepcopy(self._archive),
                    todos=[{key: todo[key] for key in ('content', 'status')} for todo in runtime.session.todos],
                    rounds_since_todo=runtime.session.rounds_since_todo,
                    selection=selection, status=self._status, tool_states=dict(self._tool_states),
                )
                if snapshot == self._snapshot:
                    return
                snapshot['updated_at'] = now
                self.store.save(snapshot)
                self._snapshot = snapshot
            except Exception:
                self.failed = True
                raise SessionError('无法保存会话，请停止操作并检查存储状态') from None

    def archive_history(self, before: list, after: list):
        with self._lock:
            self._check()
            encoded = encode_history(before)
            if encoded != encode_history(after) and (not self._archive or self._archive[-1] != encoded):
                self._archive.append(encoded)

    def restore(self, runtime, snapshot: dict, lease: SessionLease):
        with self._lock:
            self._check()
            if self.session_id is not None:
                raise SessionError('当前运行时已有会话')
            history = decode_history(snapshot['history'])
            latest_batch = next((i for i in range(len(history) - 1, -1, -1)
                                 if history[i]['role'] == 'assistant'), None)
            repaired = []
            background = False
            background_starts = set()
            for i, message in enumerate(history):
                repaired.append(message)
                content = message.get('content', '')
                texts = [content] if isinstance(content, str) else [
                    block.get('text', '') for block in content
                    if isinstance(block, dict) and block.get('type') == 'text'
                ]
                for text in texts:
                    if _INTERRUPTED_BACKGROUND in text:
                        background = False
                        background_starts.clear()
                    for notification in re.findall(r'<task_notification>(.*?)</task_notification>', text, re.DOTALL):
                        match = re.search(r'<task_id>(bg_\d+)</task_id>.*?<status>(completed|failed)</status>', notification, re.DOTALL)
                        if match:
                            background_starts.discard(match.group(1))
                calls = _blocks(message, 'tool_use') if message['role'] == 'assistant' else []
                if not calls:
                    continue
                following = history[i + 1] if i + 1 < len(history) and history[i + 1]['role'] == 'user' else None
                results = _blocks(following, 'tool_result') if following else []
                done = {b['tool_use_id'] for b in results}
                missing = []
                for call in calls:
                    if call['id'] in done:
                        continue
                    state = snapshot['tool_states'].get(call['id']) if i == latest_batch else None
                    if call['name'] in ('task', 'spawn_teammate') and state != 'pending':
                        background = True
                    missing.append(dict(type='tool_result', tool_use_id=call['id'], is_error=True,
                        content='上次会话中断，此工具未执行' if state == 'pending' else
                        '执行结果未知，可能已产生副作用，请先检查实际状态'))
                if missing:
                    if following is not None and isinstance(following['content'], list):
                        content = following['content']
                        following['content'] = ([b for b in content if isinstance(b, dict) and b.get('type') == 'tool_result']
                                                + missing + [b for b in content if not (isinstance(b, dict) and b.get('type') == 'tool_result')])
                    else:
                        repaired.append({'role': 'user', 'content': missing})
                for result in results:
                    match = re.search(r'\[Background task (bg_\d+) started\]', str(result['content']))
                    if match:
                        background_starts.add(match.group(1))
                background |= any(call['name'] == 'spawn_teammate' and any(
                    result['tool_use_id'] == call['id'] and "' spawned as " in str(result['content'])
                    for result in results) for call in calls)
            background |= bool(background_starts)
            if background:
                repaired.append({'role': 'user', 'content': _INTERRUPTED_BACKGROUND})
            runtime.session.history[:] = repaired
            runtime.session.todos[:] = copy.deepcopy(snapshot['todos'])
            runtime.session.rounds_since_todo = snapshot['rounds_since_todo']
            runtime.session.context.clear()
            self.session_id = snapshot['id']
            self._lease = lease
            self._snapshot = copy.deepcopy(snapshot)
            self._archive = copy.deepcopy(snapshot['archive'])
            self._status = 'interrupted' if snapshot['status'] != 'idle' else 'idle'
            self._tool_states = dict(snapshot['tool_states'])
            self._batch = (latest_batch, tuple(b['id'] for b in _blocks(history[latest_batch], 'tool_use'))) if latest_batch is not None else (None, ())

    def close(self):
        with self._lock:
            self._closed = True
            if self._lease is not None:
                self._lease.close()
                self._lease = None


def checkpoint(runtime, **kwargs):
    coordinator = getattr(runtime, 'sessions', None)
    if coordinator is not None:
        coordinator.checkpoint(runtime, **kwargs)


def archive_history(runtime, before, after):
    coordinator = getattr(runtime, 'sessions', None)
    if coordinator is not None:
        coordinator.archive_history(before, after)
