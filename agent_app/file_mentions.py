"""Workspace file and directory discovery and ``@`` mention handling.

The module is deliberately free of prompt-toolkit types so the token
grammar, ranking and completion rules can be tested without a terminal.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INDEX_LIMIT = 20_000
MATCH_LIMIT = 10
GIT_TIMEOUT = 2.0

IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".pamu",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "dist",
        "build",
    }
)
IGNORED_FILES = frozenset({".env", ".settings", "credentials.json", ".DS_Store"})
IGNORED_SUFFIXES = (".pem", ".key", ".egg-info")


@dataclass(frozen=True)
class Mention:
    """A ``@`` token under the cursor.

    ``start`` points at ``@``, ``end`` at the first character after the
    token, and ``query`` holds the text between them (without quotes).
    """

    start: int
    end: int
    query: str


def mention_token(text: str, cursor: int) -> Mention | None:
    """Return the ``@`` token under ``cursor``, or ``None``.

    A token ends at the next whitespace unless it is written as a quoted
    ``@"path with spaces"`` mention.  An ``@`` preceded by an ASCII
    identifier character is treated as an email address or decorator, not
    as a mention.
    """
    cursor = max(0, min(cursor, len(text)))
    start = text.rfind("@", 0, cursor)
    while start != -1:
        mention = _mention_at(text, start, cursor)
        if mention is not None:
            return mention
        start = text.rfind("@", 0, start)
    return None


def _mention_at(text: str, start: int, cursor: int) -> Mention | None:
    if start > 0:
        previous = text[start - 1]
        if previous.isascii() and (previous.isalnum() or previous == "_"):
            return None
    if text.startswith('@"', start):
        closing = text.find('"', start + 2)
        if closing != -1 and closing < cursor:
            return None
        query = text[start + 2:cursor] if cursor > start + 1 else ""
        end = closing + 1 if closing != -1 else len(text)
        return Mention(start, end, query)
    if any(char.isspace() for char in text[start:cursor]):
        return None
    end = cursor
    while end < len(text) and not text[end].isspace():
        end += 1
    return Mention(start, end, text[start + 1:cursor])


def format_mention(path: str) -> str:
    """Render ``path`` as an ``@`` mention, quoting paths with spaces."""
    if any(char.isspace() for char in path):
        return f'@"{path}"'
    return f"@{path}"


def complete_mention(text: str, cursor: int, path: str) -> tuple[str, int]:
    """Replace the mention under ``cursor`` with ``path``.

    A single space separates the mention from the following text; the
    returned cursor sits after that separator.
    """
    token = mention_token(text, cursor)
    if token is None:
        return text, cursor
    replacement = format_mention(path)
    updated = text[: token.start] + replacement + text[token.end:]
    position = token.start + len(replacement)
    if path.endswith("/"):
        return updated, position
    if position >= len(updated) or not updated[position].isspace():
        updated = updated[:position] + " " + updated[position:]
    return updated, position + 1


def workspace_entries(
    root: Path,
    *,
    run: Callable[..., Any] = subprocess.run,
    limit: int = INDEX_LIMIT,
) -> list[str]:
    """List files and directories under ``root`` as relative POSIX paths.

    Files keep their plain path; directories are marked with a trailing
    slash.  ``git ls-files`` is preferred because it respects
    ``.gitignore`` and already includes untracked-but-not-ignored files.
    Without git the directory walk skips build, cache and credential
    paths.  Directories are derived from their files, so empty ones never
    appear.
    """
    root = Path(root).resolve()
    names = _git_files(root, run)
    if names is None:
        files = _walk_files(root, limit)
    else:
        files = [
            name for name in sorted(set(names))[:limit] if (root / name).is_file()
        ]
    entries = set(files)
    for name in files:
        cut = name.rfind("/")
        while cut != -1:
            entries.add(name[: cut + 1])
            cut = name.rfind("/", 0, cut)
    return sorted(entries)[:limit]


def _git_files(root: Path, run: Callable[..., Any]) -> list[str] | None:
    try:
        result = run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "surrogateescape")
    return [name for name in raw.split("\0") if name]


def _walk_files(root: Path, limit: int) -> list[str]:
    found: list[str] = []
    for current, directories, filenames in os.walk(root):
        current_path = Path(current)
        directories[:] = sorted(
            name
            for name in directories
            if not _ignored((current_path / name).relative_to(root))
        )
        for name in sorted(filenames):
            relative = (current_path / name).relative_to(root)
            if _ignored(relative):
                continue
            found.append(relative.as_posix())
            if len(found) >= limit:
                return sorted(found)
    return sorted(found)


def _ignored(relative: Path) -> bool:
    if any(
        part in IGNORED_DIRECTORIES or part.endswith(".egg-info")
        for part in relative.parts
    ):
        return True
    name = relative.name
    if name in IGNORED_FILES:
        return True
    return any(name.endswith(suffix) for suffix in IGNORED_SUFFIXES)


def match_entries(
    query: str,
    entries: Sequence[str],
    *,
    limit: int | None = None,
) -> list[str]:
    """Rank ``entries`` against ``query`` (case-insensitive, deterministic).

    Entries in the directory named by the query rank first: a bare ``@``
    lists top-level entries before nested ones, and ``@agent_app/`` lists
    that directory's direct children before deeper matches.  Anything
    outside that directory falls back to whole-path matching.  Ties
    break on depth and then lexicographically, so callers can rely on a
    stable order.
    """
    folded = query.casefold()
    directory = ""
    remainder = folded
    if "/" in folded:
        cut = folded.rfind("/") + 1
        directory, remainder = folded[:cut], folded[cut:]
    scored = []
    for path in entries:
        if path.casefold().startswith(directory):
            rest = path[len(directory):]
            if not rest and directory:
                continue
            score = _match_score(remainder, rest) if remainder else 0
            direct = _is_direct_child(rest)
        else:
            score = _match_score(folded, path)
            direct = False
        if score is None:
            continue
        scored.append(
            (0 if direct else 1, score, path.rstrip("/").count("/"), path)
        )
    scored.sort()
    ranked = [path for _, _, _, path in scored]
    return ranked[:limit] if limit is not None else ranked


def _is_direct_child(rest: str) -> bool:
    return "/" not in rest.rstrip("/")


def _match_score(query: str, path: str) -> int | None:
    folded = path.casefold().rstrip("/")
    name = folded.rsplit("/", 1)[-1]
    if name == query:
        return 0
    if name.startswith(query):
        return 1
    if query in name:
        return 2
    if folded.startswith(query):
        return 3
    if query in folded:
        return 4
    return None


def render_matches(
    matches: Sequence[str], total: int, index: int
) -> list[tuple[str, str]]:
    """Build the hint rows shown below the input for the mention menu."""
    if not matches:
        return []
    rows: list[tuple[str, str]] = []
    for position, path in enumerate(matches):
        selected = position == index % len(matches)
        style = "class:selected" if selected else (
            "class:menu.directory" if path.endswith("/") else "class:menu.name"
        )
        rows.append((style, f"{'❯' if selected else ' '} {path}"))
        rows.append(("", "\n"))
    if total > len(matches):
        rows.append(("class:muted", f"  匹配 {total} 个，显示前 {len(matches)} 个\n"))
    rows.append(("class:muted", "  ↑↓ 选择 · Enter 选中 · Tab 补全 · Esc 关闭"))
    return rows
