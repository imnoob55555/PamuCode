"""Session-local immutable evidence, with bounded visible-content retrieval."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .session_codec import encode_history
from .session_store import SessionError, SessionStore, _id, _open, _regular, _unlink

_REF = re.compile(r'(sha256|artifact):([0-9a-f]{64})\Z')
_ARTIFACT_REF = re.compile(r'artifact:[0-9a-f]{64}')
_METADATA = {'_pamu_id', '_pamu_source', '_pamu_turn_id'}


def _visible(message):
    content = message.get('content', '')
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        kind = block.get('type')
        if kind == 'text':
            parts.append(block['text'])
        elif kind == 'tool_use':
            parts.append(block['name'] + ' ' + json.dumps(block['input'], ensure_ascii=False))
        elif kind == 'tool_result':
            value = block['content']
            parts.append(value if isinstance(value, str) else _visible({'content': value}))
    return '\n'.join(parts)


class ContextArchive:
    def __init__(self, workdir, session_id):
        self.store = SessionStore(Path(workdir))
        self.session_id = _id(session_id)
        self.directory = self.store.directory / session_id
        self._ephemeral_refs = []

    @contextmanager
    def _directory(self, kind=None, create=False):
        opened = []
        try:
            with self.store._directory(create=create) as parent:
                if parent is None:
                    yield None
                    return
                for name in (self.session_id, *([kind] if kind else [])):
                    if isinstance(parent, Path):
                        child = parent / name
                        if create:
                            child.mkdir(mode=0o700, exist_ok=True)
                        self.store._check_dirs(child)
                        if not child.exists():
                            yield None
                            return
                    else:
                        if create:
                            try:
                                os.mkdir(name, 0o700, dir_fd=parent)
                                os.fsync(parent)
                            except FileExistsError:
                                pass
                        try:
                            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                        except FileNotFoundError:
                            yield None
                            return
                        opened.append(child)
                    parent = child
                yield parent
        except OSError:
            raise SessionError('无法访问会话归档') from None
        finally:
            for fd in reversed(opened):
                os.close(fd)

    def _save(self, kind, data, suffix, name=None):
        digest = hashlib.sha256(data).hexdigest()
        name = name or digest + suffix
        with self._directory(kind, create=True) as directory:
            try:
                if isinstance(directory, Path):
                    os.stat(directory / name, follow_symlinks=False)
                else:
                    os.stat(name, dir_fd=directory, follow_symlinks=False)
                fd = _open(name, os.O_RDONLY, dir_fd=directory)
            except FileNotFoundError:
                temporary = '.' + uuid.uuid4().hex + '.tmp'
                try:
                    fd = _open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd=directory)
                    with os.fdopen(fd, 'wb') as handle:
                        handle.write(data); handle.flush(); os.fsync(handle.fileno())
                    # Hard-link publication never replaces an immutable object.
                    try:
                        if isinstance(directory, Path):
                            os.link(directory / temporary, directory / name)
                        else:
                            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    except FileExistsError:
                        existing = _open(name, os.O_RDONLY, dir_fd=directory)
                        with os.fdopen(existing, 'rb') as handle:
                            if handle.read(len(data) + 1) != data:
                                raise SessionError('会话归档内容校验失败')
                    if isinstance(directory, int):
                        os.fsync(directory)
                finally:
                    try:
                        _unlink(temporary, directory)
                    except FileNotFoundError:
                        pass
            else:
                with os.fdopen(fd, 'rb') as handle:
                    if handle.read(len(data) + 1) != data:
                        raise SessionError('会话归档内容校验失败')
        return ('sha256:' if kind == 'objects' else 'artifact:') + digest

    def save_artifact(self, text):
        if not isinstance(text, str):
            raise SessionError('归档产物必须是文本')
        ref = self._save('artifacts', text.encode('utf-8'), '.txt')
        self._ephemeral_refs.append(ref)
        return ref

    def save_history(self, messages):
        refs = []
        for message in encode_history(messages):
            canonical = {key: value for key, value in message.items() if key not in _METADATA}
            if isinstance(canonical['content'], list):
                for block in canonical['content']:
                    if block['type'] == 'tool_result' and isinstance(block['content'], str) and len(block['content'].encode('utf-8')) > 8000:
                        full = block['content']
                        artifact = self.save_artifact(full)
                        block['content'] = full[:500] + '\n[完整结果 ' + artifact + ']\n' + full[-500:]
            refs.append(self._save('objects', json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8'), '.json'))
        self._ephemeral_refs.extend(refs)
        return refs

    def _read(self, ref, max_bytes=8 * 1024 * 1024):
        match = _REF.fullmatch(ref) if isinstance(ref, str) else None
        if not match:
            raise SessionError('归档引用无效')
        kind, digest = match.groups()
        with self._directory('objects' if kind == 'sha256' else 'artifacts') as directory:
            if directory is None:
                raise SessionError('归档记录不存在')
            fd = _open(digest + ('.json' if kind == 'sha256' else '.txt'), os.O_RDONLY, dir_fd=directory)
            with os.fdopen(fd, 'rb') as handle:
                if os.fstat(handle.fileno()).st_size > max_bytes:
                    raise SessionError('归档记录超出本次读取上限，请使用分段读取')
                data = handle.read(max_bytes + 1)
        if hashlib.sha256(data).hexdigest() != digest:
            raise SessionError('会话归档内容校验失败')
        text = data.decode('utf-8')
        return _visible(json.loads(text)) if kind == 'sha256' else text

    @staticmethod
    def _verify_handle(handle, digest):
        hasher = hashlib.sha256()
        while chunk := handle.read(65536):
            hasher.update(chunk)
        if hasher.hexdigest() != digest:
            raise SessionError('会话归档内容校验失败')
        handle.seek(0)

    def validate_artifact(self, ref):
        match = _REF.fullmatch(ref) if isinstance(ref, str) else None
        if not match or match[1] != 'artifact':
            raise SessionError('归档引用无效')
        with self._directory('artifacts') as directory:
            if directory is None:
                raise SessionError('归档记录不存在')
            fd = _open(match[2] + '.txt', os.O_RDONLY, dir_fd=directory)
            with os.fdopen(fd, 'rb') as handle:
                self._verify_handle(handle, match[2])

    @staticmethod
    def active_artifacts(history):
        texts = []
        for message in history:
            content = message.get('content')
            if 'role' not in message:
                texts.append(_visible(message))
            elif isinstance(content, list):
                texts.extend(_visible({'content': [block]}) for block in content if isinstance(block, dict) and block.get('type') == 'tool_result')
        return set(re.findall(r'\[(?:归档结果|完整结果) (artifact:[0-9a-f]{64})\]', '\n'.join(texts)))

    def _catalog(self):
        # Only a published snapshot authorizes persisted evidence. Immutable
        # objects written by an aborted transaction intentionally stay invisible.
        with self.store._directory() as directory:
            if directory is None:
                snapshot = None
            else:
                try:
                    if isinstance(directory, Path):
                        os.stat(directory / (self.session_id + '.json'), follow_symlinks=False)
                    else:
                        os.stat(self.session_id + '.json', dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    snapshot = None
                else:
                    snapshot = self.store._read(self.session_id + '.json', directory)
        warnings = []
        if snapshot is None:
            refs = list(dict.fromkeys(self._ephemeral_refs))
        else:
            refs = [entry['ref'] for entry in snapshot['archive']] if snapshot['schema_version'] == 2 else []
            refs.extend(sorted(self.active_artifacts(snapshot['history'])))
        if snapshot is not None and snapshot['schema_version'] == 2:
            for entry in snapshot['archive']:
                refs.extend(entry.get('artifacts', []))
        return list(dict.fromkeys(refs)), warnings

    def _artifact_chunk(self, digest, offset, size):
        with self._directory('artifacts') as directory:
            if directory is None:
                raise SessionError('归档记录不存在')
            fd = _open(digest + '.txt', os.O_RDONLY, dir_fd=directory)
            with os.fdopen(fd, 'rb') as binary:
                self._verify_handle(binary, digest)
                import io
                handle = io.TextIOWrapper(binary, encoding='utf-8')
                # Offsets count Unicode characters; discard incrementally.
                remaining = offset
                while remaining:
                    discarded = handle.read(min(remaining, 8192))
                    if not discarded:
                        break
                    remaining -= len(discarded)
                text = handle.read(size + 1)
        return text

    def read(self, ref, offset=0, limit=2000):
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            raise SessionError('归档读取范围无效')
        match = _REF.fullmatch(ref) if isinstance(ref, str) else None
        if not match:
            raise SessionError('归档引用无效')
        try:
            refs, _ = self._catalog()
            if ref not in refs:
                raise SessionError('归档引用未提交或不存在')
            size = max(1, min(limit, 1900) * 2 - 160)
            if match[1] == 'artifact':
                remainder = self._artifact_chunk(match[2], offset, size)
            else:
                remainder = self._read(ref)[offset:]
            chunk = remainder[:size]
            while len(chunk.encode('utf-8')) > size:
                chunk = chunk[:-1]
            end = offset + len(chunk)
            return json.dumps(dict(ref=ref, text=chunk, next_offset=end, done=len(chunk) >= len(remainder)), ensure_ascii=False)
        except (OSError, ValueError, UnicodeError):
            raise SessionError('无法读取归档记录') from None

    def _artifact_search_chunk(self, digest, offset, size):
        with self._directory('artifacts') as directory:
            if directory is None:
                raise SessionError('归档记录不存在')
            fd = _open(digest + '.txt', os.O_RDONLY, dir_fd=directory)
            with os.fdopen(fd, 'rb') as handle:
                self._verify_handle(handle, digest)
                handle.seek(offset)
                raw = handle.read(size)
                done = handle.tell() >= os.fstat(handle.fileno()).st_size
        return raw.decode('utf-8', errors='replace'), len(raw), done

    def search(self, query, cursor=None, limit=10):
        if not isinstance(query, str) or not query.strip() or type(limit) is not int or limit < 1:
            raise SessionError('归档检索参数无效')
        if cursor is not None and (not isinstance(cursor, str) or not re.fullmatch(r'[0-9]+(?::[0-9]+)?', cursor)):
            raise SessionError('归档检索游标无效')
        refs, warnings = self._catalog()
        pieces = (cursor or '0').split(':')
        position, offset = int(pieces[0]), int(pieces[1]) if len(pieces) > 1 else 0
        matches, scanned, output = [], 0, 0
        while position < len(refs) and len(matches) < min(limit, 10):
            ref = refs[position]
            allowance = 8 * 1024 * 1024 - scanned
            if allowance < 4096:
                break
            try:
                if ref.startswith('artifact:'):
                    text, consumed, finished = self._artifact_search_chunk(ref.split(':')[1], offset, allowance)
                else:
                    text = self._read(ref, max_bytes=allowance)
                    finished = True
            except (SessionError, OSError, ValueError):
                warnings.append('历史归档缺失或损坏：' + ref)
                position += 1
                offset = 0
                continue
            size = len(text.encode('utf-8'))
            scanned += size
            if finished:
                position += 1
                offset = 0
            else:
                offset += max(1, consumed - min(len(query.encode('utf-8')), 1024))
            found = text.casefold().find(query.casefold())
            if found >= 0:
                snippet = text[max(0, found - 80):found + 240]
                output += len(snippet.encode('utf-8'))
                if output > 2400:
                    break
                matches.append(dict(ref=ref, text=snippet))
        return json.dumps(dict(matches=matches, cursor=(str(position) + ':' + str(offset)) if position < len(refs) else None, complete=position >= len(refs) and not warnings, scanned_bytes=scanned, warnings=list(dict.fromkeys(warnings))[:2]), ensure_ascii=False)
