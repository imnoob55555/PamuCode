"""Bounded optional context rebuilt from previously successful local accesses."""
from __future__ import annotations
import hashlib
import math
from pathlib import Path


def text_tokens(text):
    return math.ceil(len(text.encode('utf-8')) * .55)


def bounded_text(text, tokens):
    if tokens <= 0:
        return ''
    return text.encode('utf-8')[:int(tokens / .55)].decode('utf-8', errors='ignore')


def safe_material(workdir, value):
    root = Path(workdir).resolve()
    candidate = Path(value)
    candidate = candidate if candidate.is_absolute() else root / candidate
    # Reject symlinks throughout the path, even when their target is local.
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    current = root
    for part in relative.parts:
        lower = part.lower()
        if part in ('.', '..') or lower in ('.pamu', '.aws', '.ssh') or lower.startswith(('.env', '.settings', 'credentials')):
            return None
        current = current / part
        if current.is_symlink():
            return None
    if not current.is_file() or not current.resolve().is_relative_to(root):
        return None
    return current


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def remember_file(session, workdir, path, *, offset=0, limit=None, cwd=None):
    try:
        candidate = Path(cwd or workdir) / path
        actual = safe_material(workdir, candidate)
        if actual is None:
            return
        # Binary files are never automatically supplemented.
        with actual.open('rb') as stream:
            if b'\0' in stream.read(4096):
                return
        record = {'path': str(actual.relative_to(Path(workdir).resolve())),
                  'offset': max(0, offset), 'limit': 1000 if limit is None else max(1, min(limit, 1000)),
                  'sha256': file_hash(actual)}
        recent = session.context_state.setdefault('recent_files', [])
        recent[:] = [item for item in recent if item.get('path') != record['path']]
        recent.append(record)
        del recent[:-5]
    except (OSError, ValueError, TypeError):
        pass  # Context bookkeeping cannot turn a successful tool into a failure.


def remember_skill(session, name, content):
    records = session.context_state.setdefault('loaded_skills', [])
    records[:] = [item for item in records if item.get('name') != name]
    records.append({'name': name, 'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()})
    del records[:-32]


def _read_prefix(path, bytes_limit):
    with path.open('r', encoding='utf-8') as stream:
        return stream.read(max(0, bytes_limit))


def optional_materials(runtime, tokens):
    """Account index, catalogs, loaded skill text and file evidence together."""
    if getattr(runtime.config, 'sandbox_mode', 'off') == 'confined':
        # Re-opening remembered project paths in the privileged controller
        # would bypass descriptor-safe tool I/O. Require explicit tool reads.
        return {'loaded_skills': '', 'recent_files': '', 'memories': '', 'skills': '',
                'optional_remaining_tokens': tokens, 'optional_budget_tokens': tokens}
    remaining = tokens
    result = {}
    def allocate(key, text, maximum=None):
        nonlocal remaining
        value = bounded_text(text, min(remaining, maximum) if maximum is not None else remaining)
        result[key] = value
        remaining -= text_tokens(value)
    state = runtime.session.context_state
    # References and already-loaded instructions have priority over broad catalogs.
    skill_parts = []
    for record in state.get('loaded_skills', [])[-32:]:
        name = record.get('name', '')
        if name not in runtime.skills.registry:
            continue
        raw_path = runtime.skills.root / name / 'SKILL.md'
        path = safe_material(runtime.config.workdir, raw_path)
        # Workspace skill roots are often under .pamu: permit that specific root,
        # still rejecting symlinks and never guessing new skill paths.
        if path is None and raw_path.is_file() and not any(p.is_symlink() for p in [raw_path, *raw_path.parents]):
            try:
                raw_path.resolve().relative_to(runtime.skills.root.resolve())
                if Path(name).name == name:
                    path = raw_path
            except ValueError:
                pass
        header = f"技能 {name[:128]}（load_skill({name[:128]!r})；原哈希 {record.get('sha256', '')[:64]}）"
        if path is None:
            skill_parts.append(header + '：当前不可读取')
            continue
        try:
            changed = file_hash(path) != record.get('sha256')
            body = bounded_text(_read_prefix(path, remaining * 2), max(0, remaining // 3))
            skill_parts.append(header + (' [已变化，以当前正文为准]' if changed else '') + '\n' + body)
        except (OSError, UnicodeError):
            skill_parts.append(header + '：当前不可读取')
    allocate('loaded_skills', '\n\n'.join(skill_parts), tokens // 2)
    file_parts = []
    snippets = 0
    for record in reversed(state.get('recent_files', [])[-5:]):
        value = record.get('path', '')
        path = safe_material(runtime.config.workdir, value)
        if path is None:
            continue
        header = f"文件 {value[:256]} offset={record.get('offset', 0)} limit={record.get('limit', 1000)} 原哈希={record.get('sha256', '')[:64]}"
        try:
            changed = file_hash(path) != record.get('sha256')
            if changed:
                header += ' [已变化：旧证据已过时，请重新 read_file]'
            elif snippets < 2:
                # Read only the recorded successful line range, with bounded IO.
                with path.open('r', encoding='utf-8') as stream:
                    offset = min(100000, max(0, record.get('offset', 0)))
                    for _ in range(offset):
                        if not stream.readline(65536):
                            break
                    lines = []
                    count = 0
                    for _ in range(min(1000, record.get('limit', 1000))):
                        line = stream.readline(1024)
                        if not line:
                            break
                        lines.append(line)
                        count += len(line.encode('utf-8'))
                        if count >= 900:
                            break
                body = bounded_text(''.join(lines), 512)
                if '\0' not in body:
                    header += '\n片段：\n' + body
                    snippets += 1
        except (OSError, UnicodeError, TypeError):
            header += ' [当前不可读取]'
        file_parts.append(header)
    allocate('recent_files', '\n\n'.join(file_parts), remaining // 2)
    try:
        path = runtime.memory.index_path
        memory = _read_prefix(path, remaining * 2) if path.is_file() and not path.is_symlink() else ''
    except (OSError, UnicodeError):
        memory = ''
    allocate('memories', memory, remaining // 2)
    catalog = '\n'.join(f"- {str(item['name'])[:128]}: {str(item['description'])[:256]}" for item in list(runtime.skills.registry.values())[:64])
    allocate('skills', catalog, remaining // 2)
    result['optional_remaining_tokens'] = remaining
    result['optional_budget_tokens'] = tokens
    return result
