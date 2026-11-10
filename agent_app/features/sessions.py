"""Semantic conversation checkpoints; restoring never dispatches historical work."""
from __future__ import annotations

import copy
import re
import threading
import time
import uuid
from pathlib import Path

from .context_archive import ContextArchive
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
        self._live_records = []

    def _check(self):
        if self._closed or self.failed:
            raise SessionError('会话保存已停止，请重新启动后恢复会话')

    def checkpoint(self, runtime, *, status=None, tool_id=None, tool_status=None, _history=None, _context_state=None):
        with self._lock:
            self._check()
            try:
                history = encode_history(runtime.session.history if _history is None else _history)
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
                source_history = runtime.session.history if _history is None else _history
                records = self._message_records(source_history)
                context_state = copy.deepcopy(getattr(runtime.session, 'context_state', {}) if _context_state is None else _context_state)
                context_state['_message_records'] = records
                first = next((m['content'] for m in history if m['role'] == 'user'), '')
                if not isinstance(first, str):
                    first = ' '.join(b.get('text', '') for b in first if b.get('type') == 'text')
                snapshot = dict(
                    schema_version=2, id=self.session_id, workspace=str(self.store.workdir),
                    created_at=self._snapshot['created_at'] if self._snapshot else now,
                    updated_at=self._snapshot['updated_at'] if self._snapshot else now,
                    title=self._snapshot['title'] if self._snapshot else safe_preview(first),
                    history=history, archive=self._archive_catalog(),
                    context_state=context_state,
                    todos=[{key: todo[key] for key in ('content', 'status')} for todo in runtime.session.todos],
                    rounds_since_todo=runtime.session.rounds_since_todo,
                    selection=selection, status=self._status, tool_states=dict(self._tool_states),
                )
                if snapshot == self._snapshot:
                    return
                snapshot['updated_at'] = now
                if self._snapshot and self._snapshot['schema_version'] == 1:
                    self.store.backup_v1(self._snapshot)
                self.store.save(snapshot)
                self._snapshot = snapshot
                self._live_records = list(zip(source_history, records))
                if _context_state is None:
                    runtime.session.context_state = copy.deepcopy(context_state)
            except Exception:
                self.failed = True
                raise SessionError('无法保存会话，请停止操作并检查存储状态') from None

    @property
    def context_archive(self):
        return ContextArchive(self.store.workdir, self.session_id) if self.session_id else None

    def _archive_catalog(self):
        if not self._archive or isinstance(self._archive[0], dict):
            return copy.deepcopy(self._archive)
        old = self._archive
        self._archive = []
        for history in old:
            self._record_history(history)
        return copy.deepcopy(self._archive)

    def _message_records(self, history):
        """Track occurrences without adding provider-irrelevant fields to messages."""
        entries = []
        used = set()
        turn = ''
        for message in history:
            entry = None
            for equality in (False, True):
                for index, (previous, record) in enumerate(self._live_records):
                    if index not in used and (previous is message if not equality else previous == message):
                        entry = copy.deepcopy(record)
                        used.add(index)
                        break
                if entry is not None:
                    break
            if entry is None:
                entry = dict(id=message.get('_pamu_id', str(uuid.uuid4())), source='', turn_id='')
            entry['id'] = message.get('_pamu_id', entry['id'])
            content = message.get('content')
            source = message.get('_pamu_source')
            if not source:
                source = 'tool' if _blocks(message, 'tool_result') else message['role']
            entry['source'] = source
            if source == 'user' and isinstance(content, str) and not content.startswith(('[Compacted', '<reminder>', '[Scheduled:', '[Team inbox]', '<task_notification>')):
                turn = entry['id']
            entry['turn_id'] = message.get('_pamu_turn_id', entry['turn_id'] or turn)
            entries.append(entry)
        return entries

    def _record_history(self, history):
        if self.session_id is None:
            self.session_id = str(uuid.uuid4())
            self._lease = self.store.acquire(self.session_id)
        archive = self.context_archive
        refs = archive.save_history(history)
        records = self._message_records(history)
        for record, ref in zip(records, refs):
            entry = dict(record, ref=ref, artifacts=sorted(archive.active_artifacts([{'content': archive._read(ref)}])))
            if entry not in self._archive:
                self._archive.append(entry)
        for message, record in zip(history, records):
            if not any(previous is message for previous, _ in self._live_records):
                self._live_records.append((message, record))
        return refs

    def archive_history(self, before: list, after: list):
        with self._lock:
            self._check()
            try:
                if encode_history(before) != encode_history(after):
                    self._archive_catalog()
                    self._record_history(before)
            except Exception:
                self.failed = True
                raise SessionError('无法保存会话归档，请停止操作并检查存储状态') from None

    def commit_history(self, runtime, candidate, context_state):
        """Archive evidence and durably save a candidate before publishing it."""
        with self._lock:
            self._check()
            previous_archive = copy.deepcopy(self._archive)
            try:
                self.archive_history(runtime.session.history, candidate)
                self.checkpoint(runtime, _history=candidate, _context_state=context_state)
            except Exception:
                self._archive = previous_archive
                raise
            runtime.session.history[:] = candidate
            runtime.session.context_state = copy.deepcopy(self._snapshot['context_state'])

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
            runtime.session.context_state = copy.deepcopy(snapshot.get('context_state', {}))
            self.session_id = snapshot['id']
            self._lease = lease
            self._snapshot = copy.deepcopy(snapshot)
            restored_records = snapshot.get('context_state', {}).get('_message_records', [])
            if len(restored_records) == len(history):
                self._live_records = list(zip(history, copy.deepcopy(restored_records)))
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


def commit_history(runtime, candidate, context_state):
    coordinator = getattr(runtime, 'sessions', None)
    if coordinator is not None:
        coordinator.commit_history(runtime, candidate, context_state)
    else:
        runtime.session.history[:] = candidate
        runtime.session.context_state = copy.deepcopy(context_state)
