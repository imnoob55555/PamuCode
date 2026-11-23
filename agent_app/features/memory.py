"""Workspace memory notes, the agent-written layer of the memory hierarchy.

transcripts (past sessions, read-only) -> notes (``.pamu/memory/**/*.md``)
-> ``MEMORY.md`` (index loaded into every session). The model writes notes
through the ``memory`` tool when it judges something worth keeping; dreaming
(``features/dreaming.py``) later corrects and reorganizes them offline.
"""

import hashlib
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from ..logging_setup import get_logger

LOGGER = get_logger("pamu.memory")
INDEX_NAME = "MEMORY.md"
INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 25_000
NOTE_MAX_BYTES = 32_000
MAX_DEPTH = 3
MAX_NOTES = 200
BACKUPS_KEPT = 5
SECRET_PATTERN = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[abpr]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:api[_-]?key|token|secret|password|passwd)\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+=]{12,}"
)
MEMORY_COMMANDS = ("view", "create", "str_replace", "delete")


def memory_tool_schema(writable: bool = True) -> dict:
    commands = list(MEMORY_COMMANDS) if writable else ["view"]
    return {
        "name": "memory",
        "description": (
            "Read and write this workspace's persistent memory folder. Paths are relative to the folder, "
            "e.g. 'MEMORY.md' (the index loaded into every session) or 'topic/note.md'. "
            "view lists the folder or shows a note with line numbers; create writes a whole note; "
            "str_replace replaces one exact, unique snippet; delete removes a note."
            if writable else
            "Read this workspace's persistent memory folder (read-only: PAMU_AUTO_MEMORY=off). "
            "view lists the folder or shows a note, e.g. 'MEMORY.md' or 'topic/note.md'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": commands},
                "path": {"type": "string", "description": "Note path relative to the memory folder; empty for the folder."},
                **({
                    "file_text": {"type": "string", "description": "Full note content for create."},
                    "old_str": {"type": "string", "description": "Exact text to replace for str_replace."},
                    "new_str": {"type": "string", "description": "Replacement text for str_replace."},
                } if writable else {}),
            },
            "required": ["command"],
        },
    }


class MemoryPathError(ValueError):
    """A note path that is outside, hidden in, or not a markdown note of the memory folder."""


@dataclass(frozen=True, slots=True)
class MemoryStore:
    root: Path
    index_path: Path


def contains_secret(text: str) -> bool:
    return bool(SECRET_PATTERN.search(text))


def resolve_note(store: MemoryStore, path: str, *, allow_dir: bool = False) -> Path:
    text = str(path or "").strip().replace("\\", "/").lstrip("/")
    parts = PurePosixPath(text).parts if text else ()
    if any(part in (".", "..") or part.startswith(".") for part in parts):
        raise MemoryPathError(f"无效的记忆路径：{path}")
    if len(parts) > MAX_DEPTH:
        raise MemoryPathError(f"记忆目录最多 {MAX_DEPTH} 层：{path}")
    if not allow_dir and (not parts or not parts[-1].endswith(".md")):
        raise MemoryPathError(f"记忆笔记必须是 .md 文件：{path}")
    current = store.root
    for part in ("", *parts):
        current = current / part if part else current
        if current.is_symlink():
            raise MemoryPathError(f"记忆路径不能经过符号链接：{path}")
    if current.exists() and not current.resolve().is_relative_to(store.root.resolve()):
        raise MemoryPathError(f"记忆路径越界：{path}")
    return current


def _note_paths(store: MemoryStore) -> list[Path]:
    if not store.root.is_dir() or store.root.is_symlink():
        return []
    paths = []
    for path in sorted(store.root.rglob("*.md")):
        relative = path.relative_to(store.root)
        if any(part.startswith(".") for part in relative.parts) or len(relative.parts) > MAX_DEPTH:
            continue
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != store.root and parent.is_relative_to(store.root)):
            continue
        if path.is_file():
            paths.append(path)
    return paths


def _summary(text: str) -> str:
    lines = text.splitlines()
    if lines[:1] == ["---"]:
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line.startswith("description:"):
                return line.split(":", 1)[1].strip().strip("'\"")[:120]
    for line in lines:
        line = line.strip().lstrip("#").strip()
        if line and line != "---":
            return line[:120]
    return ""


def list_notes(store: MemoryStore) -> list[dict]:
    """Every note except the index, with size and a one-line summary."""
    notes = []
    for path in _note_paths(store):
        if path == store.index_path:
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                head = stream.read(2048)
            size = path.stat().st_size
        except (OSError, UnicodeError):
            continue
        notes.append({"path": path.relative_to(store.root).as_posix(), "bytes": size, "summary": _summary(head)})
    return notes


def injected_index(store: MemoryStore) -> str:
    """The top of the hierarchy: first 200 lines / 25 KB of MEMORY.md, or a listing if there is none."""
    path = store.index_path
    try:
        if path.is_file() and not path.is_symlink():
            with path.open(encoding="utf-8", errors="replace") as stream:
                raw = stream.read(INDEX_MAX_BYTES + 1)
            lines = raw[:INDEX_MAX_BYTES].splitlines()
            text = "\n".join(lines[:INDEX_MAX_LINES]).strip()
            if len(raw) > INDEX_MAX_BYTES or len(lines) > INDEX_MAX_LINES:
                text += (f"\n[MEMORY.md truncated: only the first {INDEX_MAX_LINES} lines / "
                         f"{INDEX_MAX_BYTES // 1000} KB are loaded; move detail into notes]")
            if text:
                return text
    except OSError:
        return ""
    notes = list_notes(store)[:50]
    if not notes:
        return ""
    return "[No MEMORY.md yet; notes in the memory folder:]\n" + "\n".join(
        f"- {note['path']}" + (f" — {note['summary']}" if note["summary"] else "") for note in notes
    )


def _view(store: MemoryStore, path: str) -> str:
    target = resolve_note(store, path, allow_dir=True)
    if target.is_dir() or not path.strip():
        notes = [note for note in list_notes(store) if not path.strip() or note["path"].startswith(
            target.relative_to(store.root).as_posix().rstrip("/") + "/")]
        lines = [f"- {note['path']} ({note['bytes']} B)" + (f" — {note['summary']}" if note["summary"] else "")
                 for note in notes]
        if not path.strip() and store.index_path.is_file():
            lines.insert(0, f"- {INDEX_NAME} (index, loaded into every session)")
        return "\n".join(lines) if lines else "(memory folder is empty)"
    if not target.is_file():
        return f"Error: 记忆不存在：{path}"
    with target.open(encoding="utf-8", errors="replace") as stream:
        text = stream.read(NOTE_MAX_BYTES)
    return "\n".join(f"{number:4}\t{line}" for number, line in enumerate(text.splitlines(), 1)) or "(empty note)"


def _check_content(path: str, text: str) -> str | None:
    if contains_secret(text):
        LOGGER.warning("refused a memory write that looks like it contains a credential")
        return "Error: 内容看起来包含凭证，记忆不会保存凭证。"
    if len(text.encode("utf-8")) > NOTE_MAX_BYTES:
        return f"Error: 单条记忆不能超过 {NOTE_MAX_BYTES} 字节；请拆分或精简 {path}。"
    return None


def _create(store: MemoryStore, path: str, file_text: str) -> str:
    target = resolve_note(store, path)
    if error := _check_content(path, file_text):
        return error
    existed = target.exists()
    if not existed and len(_note_paths(store)) >= MAX_NOTES:
        return f"Error: 记忆已有 {MAX_NOTES} 条笔记；请先合并或删除旧笔记。"
    target.parent.mkdir(parents=True, exist_ok=True)
    resolve_note(store, path)
    target.write_text(file_text if file_text.endswith("\n") else file_text + "\n", encoding="utf-8")
    return f"{'Updated' if existed else 'Created'} {target.relative_to(store.root).as_posix()}"


def _str_replace(store: MemoryStore, path: str, old_str: str, new_str: str) -> str:
    target = resolve_note(store, path)
    if not target.is_file():
        return f"Error: 记忆不存在：{path}"
    text = target.read_text(encoding="utf-8")
    count = text.count(old_str) if old_str else 0
    if count != 1:
        return f"Error: old_str 在 {path} 中出现 {count} 次，必须恰好出现一次。"
    updated = text.replace(old_str, new_str, 1)
    if error := _check_content(path, updated):
        return error
    target.write_text(updated, encoding="utf-8")
    return f"Updated {target.relative_to(store.root).as_posix()}"


def _remove(store: MemoryStore, target: Path) -> None:
    target.unlink()
    parent = target.parent
    while parent != store.root and parent.is_relative_to(store.root) and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def _delete(store: MemoryStore, path: str) -> str:
    target = resolve_note(store, path)
    if not target.is_file():
        return f"Error: 记忆不存在：{path}"
    _remove(store, target)
    return f"Deleted {target.relative_to(store.root).as_posix()}"


def run_memory_command(store: MemoryStore, command: str, path: str = "", file_text: str | None = None,
                       old_str: str | None = None, new_str: str | None = None, *, writable: bool = True) -> str:
    try:
        if command == "view":
            return _view(store, path)
        if command not in MEMORY_COMMANDS:
            return f"Error: 未知的 memory 命令：{command}"
        if not writable:
            return "Error: 记忆为只读（PAMU_AUTO_MEMORY=off）。"
        if command == "create":
            return _create(store, path, file_text or "") if file_text else "Error: create 需要 file_text。"
        if command == "str_replace":
            return _str_replace(store, path, old_str or "", new_str or "")
        return _delete(store, path)
    except (MemoryPathError, OSError, UnicodeError) as error:
        return f"Error: {error}"


def forget_note(store: MemoryStore, target: str) -> str | None:
    """Delete the note matching a path, a path without .md, or a file stem; return its path."""
    folded = target.strip().casefold().removesuffix(".md")
    for path in _note_paths(store):
        relative = path.relative_to(store.root).as_posix()
        if folded and folded in (relative.casefold().removesuffix(".md"), path.stem.casefold()):
            _remove(store, path)
            return relative
    return None


def snapshot_tree(store: MemoryStore) -> dict[str, str]:
    return {path.relative_to(store.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in _note_paths(store)}


def backup_memories(store: MemoryStore, clock=datetime.now) -> Path | None:
    """Copy every note aside, keeping only the most recent backups."""
    files = _note_paths(store)
    if not files:
        return None
    backups = store.root / ".backup"
    target = backups / clock().strftime("%Y%m%d-%H%M%S-%f")
    target.mkdir(parents=True)
    for path in files:
        copy = target / path.relative_to(store.root)
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, copy)
    for old in sorted(path for path in backups.iterdir() if path.is_dir())[:-BACKUPS_KEPT]:
        shutil.rmtree(old, ignore_errors=True)
    return target


def restore_backup(store: MemoryStore, backup: Path | None) -> None:
    """Put the notes back exactly as they were in ``backup`` (``None`` means there were none)."""
    for path in _note_paths(store):
        _remove(store, path)
    if backup is None:
        return
    for path in sorted(backup.rglob("*.md")):
        target = store.root / path.relative_to(backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def memory_handler(store: MemoryStore, *, writable: bool = True):
    def handle(command="view", path="", file_text=None, old_str=None, new_str=None, **unexpected):
        if unexpected:
            return f"Error: memory 不接受参数：{', '.join(sorted(unexpected))}"
        return run_memory_command(store, str(command), str(path or ""), file_text, old_str, new_str, writable=writable)
    return handle
