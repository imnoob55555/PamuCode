"""Workspace-local atomic snapshots and OS-held session leases."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import stat
import uuid
from contextlib import contextmanager

from .session_codec import decode_history, safe_preview

_MAX_BYTES = 32 * 1024 * 1024
_PROTOCOLS = {'anthropic_messages', 'openai_responses', 'openai_chat', 'ollama'}
# Windows keeps the validated-path backend; only POSIX supports anchoring
# operations against concurrent directory replacement using stdlib primitives.
_SECURE_DIR_FDS = os.name == 'posix' and hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY')


class SessionError(ValueError):
    """Safe user-facing storage error."""


def _id(text):
    try:
        if str(uuid.UUID(text)) == text:
            return text
    except (ValueError, TypeError, AttributeError):
        pass
    raise SessionError('会话编号无效')


def _regular(path, missing=False, *, dir_fd=None):
    if isinstance(dir_fd, Path):
        path, dir_fd = dir_fd / path, None
    try:
        mode = os.stat(path, dir_fd=dir_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        if missing:
            return
        raise SessionError('会话文件不存在') from None
    if not stat.S_ISREG(mode):
        raise SessionError('会话路径必须是普通文件')


def _open(path, flags, *, dir_fd):
    _regular(path, missing=bool(flags & os.O_CREAT), dir_fd=dir_fd)
    if isinstance(dir_fd, Path):
        path, dir_fd = dir_fd / path, None
    fd = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), 0o600, dir_fd=dir_fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise SessionError('会话路径必须是普通文件')
    return fd


def _replace(source, target, directory):
    if isinstance(directory, Path):
        os.replace(directory / source, directory / target)
    else:
        os.replace(source, target, src_dir_fd=directory, dst_dir_fd=directory)


def _unlink(name, directory):
    if isinstance(directory, Path):
        os.unlink(directory / name)
    else:
        os.unlink(name, dir_fd=directory)


class SessionLease:
    def __init__(self, fd):
        self._fd = fd

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _validate(snapshot, workspace):
    required = {'schema_version', 'id', 'workspace', 'created_at', 'updated_at', 'title', 'history', 'archive', 'todos', 'rounds_since_todo', 'selection', 'status', 'tool_states'}
    if not isinstance(snapshot, dict) or set(snapshot) != required:
        raise SessionError('会话快照格式无效')
    if type(snapshot['schema_version']) is not int or snapshot['schema_version'] != 1:
        raise SessionError('会话版本不受支持')
    _id(snapshot['id'])
    if snapshot['workspace'] != str(workspace):
        raise SessionError('会话不属于当前工作目录')
    for key in ('created_at', 'updated_at'):
        number = snapshot[key]
        if type(number) not in (int, float) or not math.isfinite(number):
            raise SessionError('会话时间格式无效')
    if not isinstance(snapshot['title'], str) or safe_preview(snapshot['title']) != snapshot['title']:
        raise SessionError('会话标题格式无效')
    decode_history(snapshot['history'])
    if not isinstance(snapshot['archive'], list):
        raise SessionError('会话归档格式无效')
    for history in snapshot['archive']:
        decode_history(history)
    if not isinstance(snapshot['todos'], list):
        raise SessionError('会话任务格式无效')
    for todo in snapshot['todos']:
        if not isinstance(todo, dict) or set(todo) != {'content', 'status'} or not isinstance(todo['content'], str) or todo['status'] not in ('pending', 'in_progress', 'completed'):
            raise SessionError('会话任务格式无效')
    if type(snapshot['rounds_since_todo']) is not int or snapshot['rounds_since_todo'] < 0:
        raise SessionError('会话任务计数无效')
    selection = snapshot['selection']
    fields = {'provider', 'protocol', 'base_url', 'primary_model', 'fallback_model', 'thinking_level'}
    if not isinstance(selection, dict) or set(selection) != fields:
        raise SessionError('会话模型选择格式无效')
    if any(not isinstance(selection[key], str) for key in fields - {'fallback_model'}):
        raise SessionError('会话模型选择格式无效')
    if selection['fallback_model'] is not None and not isinstance(selection['fallback_model'], str):
        raise SessionError('会话模型选择格式无效')
    if selection['protocol'] not in _PROTOCOLS:
        raise SessionError('会话协议不受支持')
    if snapshot['status'] not in ('idle', 'running', 'interrupted'):
        raise SessionError('会话状态无效')
    states = snapshot['tool_states']
    if not isinstance(states, dict) or any(not isinstance(key, str) or not key or value not in ('pending', 'running', 'completed') for key, value in states.items()):
        raise SessionError('会话工具状态无效')


class SessionStore:
    def __init__(self, workdir: Path):
        given = Path(os.path.abspath(workdir))
        self._check_dirs(given)
        self.workdir = given
        self.directory = self.workdir / '.pamu' / 'sessions'

    @staticmethod
    def _check_dirs(path):
        for part in reversed((path, *path.parents)):
            try:
                mode = part.lstat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(mode):
                raise SessionError('会话目录路径无效')

    @contextmanager
    def _directory(self, create=False):
        """Hold every traversal step with no-follow directory descriptors.

        Renaming/replacing an ancestor cannot redirect subsequent operations.
        Windows retains static path validation; it lacks this race guarantee.
        """
        if not _SECURE_DIR_FDS:
            yield self._path_directory(create)
            return
        fd = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            fd = os.open(self.workdir.anchor, flags)
            parts = [*self.workdir.parts[1:], '.pamu', 'sessions']
            for index, part in enumerate(parts):
                if create and index >= len(parts) - 2:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(part, flags, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        yield None
                        return
                    raise
                os.close(fd)
                fd = child
                if create and index == len(parts) - 2:
                    _regular('.gitignore', missing=True, dir_fd=fd)
                    try:
                        ignore_fd = _open('.gitignore', os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd=fd)
                    except FileExistsError:
                        pass
                    else:
                        with os.fdopen(ignore_fd, 'w', encoding='utf-8') as handle:
                            handle.write('*\n!.gitignore\n')
            yield fd
        finally:
            if fd is not None:
                os.close(fd)

    def _path_directory(self, create):
        """Windows-compatible baseline, without active directory-swap protection."""
        self._check_dirs(self.directory)
        if create:
            self.directory.parent.mkdir(exist_ok=True, mode=0o700)
            self.directory.mkdir(exist_ok=True, mode=0o700)
            _regular('.gitignore', missing=True, dir_fd=self.directory.parent)
            try:
                fd = _open('.gitignore', os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd=self.directory.parent)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                    handle.write('*\n!.gitignore\n')
        return self.directory if self.directory.exists() else None

    def _read(self, name, directory_fd):
        try:
            fd = _open(name, os.O_RDONLY, dir_fd=directory_fd)
            with os.fdopen(fd, 'rb') as handle:
                if os.fstat(handle.fileno()).st_size > _MAX_BYTES:
                    raise SessionError('会话文件过大')
                raw = handle.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise SessionError('会话文件过大')
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError('duplicate')
                    result[key] = value
                return result
            snapshot = json.loads(raw, object_pairs_hook=pairs)
            _validate(snapshot, self.workdir)
            if snapshot['id'] != Path(name).stem:
                raise SessionError('会话编号不匹配')
            return snapshot
        except SessionError:
            raise
        except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            raise SessionError('无法读取会话快照') from None

    def list_sessions(self) -> tuple[list[dict], int]:
        try:
            with self._directory() as directory_fd:
                if directory_fd is None:
                    return [], 0
                result, skipped = [], 0
                for name in os.listdir(directory_fd):
                    path = Path(name)
                    if path.suffix != '.json':
                        continue
                    try:
                        _id(path.stem)
                        result.append(self._read(name, directory_fd))
                    except SessionError:
                        skipped += 1
                result.sort(key=lambda s: (s['updated_at'], s['id']), reverse=True)
                return result, skipped
        except OSError:
            raise SessionError('无法列出会话') from None

    def load(self, selector: str) -> dict:
        if not isinstance(selector, str) or len(selector) < 8 or any(char not in '0123456789abcdef-' for char in selector) or len(selector) > 36:
            raise SessionError('请输入至少八位会话编号')
        try:
            with self._directory() as directory_fd:
                if directory_fd is None:
                    raise SessionError('未找到会话')
                candidates = []
                for name in os.listdir(directory_fd):
                    path = Path(name)
                    if path.suffix == '.json' and path.stem.startswith(selector):
                        try:
                            _id(path.stem)
                        except SessionError:
                            continue
                        candidates.append(name)
                if len(candidates) != 1:
                    raise SessionError('会话编号不唯一' if candidates else '未找到会话')
                return self._read(candidates[0], directory_fd)
        except OSError:
            raise SessionError('无法读取会话') from None

    def acquire(self, session_id: str) -> SessionLease:
        _id(session_id)
        fd = None
        try:
            with self._directory(create=True) as directory_fd:
                fd = _open(session_id + '.lock', os.O_RDWR | os.O_CREAT, dir_fd=directory_fd)
                if os.name == 'nt':
                    import msvcrt
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b'\0')
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return SessionLease(fd)
        except OSError:
            if fd is not None:
                os.close(fd)
            raise SessionError('会话正在使用或无法加锁') from None
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise

    def save(self, snapshot: dict) -> None:
        try:
            _validate(snapshot, self.workdir)
            encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(encoded) > _MAX_BYTES:
                raise SessionError('会话文件过大')
            with self._directory(create=True) as directory_fd:
                target = snapshot['id'] + '.json'
                temporary = None
                try:
                    _regular(target, missing=True, dir_fd=directory_fd)
                    name = '.' + snapshot['id'] + '-' + uuid.uuid4().hex + '.tmp'
                    fd = _open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd=directory_fd)
                    temporary = name
                    with os.fdopen(fd, 'wb') as handle:
                        handle.write(encoded)
                        handle.flush()
                        os.fsync(handle.fileno())
                    _regular(target, missing=True, dir_fd=directory_fd)
                    _replace(temporary, target, directory_fd)
                    temporary = None
                    if isinstance(directory_fd, int):
                        os.fsync(directory_fd)
                finally:
                    if temporary is not None:
                        try:
                            _unlink(temporary, directory_fd)
                        except OSError:
                            pass
        except SessionError:
            raise
        except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            raise SessionError('无法保存会话快照') from None
