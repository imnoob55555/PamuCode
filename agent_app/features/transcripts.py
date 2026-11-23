"""Read-only view of past sessions: the transcript layer under memory notes.

Entries hold what the user typed, what the assistant wrote and which tools it
called with which input. Tool results and runtime notices are never exposed,
so file or command output cannot steer what is written into memory.
"""

import json
from datetime import datetime
from pathlib import Path

from ..adapters.common import value
from ..logging_setup import get_logger
from .session_codec import decode_history
from .session_store import SessionError, SessionStore

LOGGER = get_logger("pamu.transcripts")
ENTRY_MAX_CHARS = 2_000
TOOL_INPUT_MAX_CHARS = 300
READ_MAX_CHARS = 12_000
SEARCH_MAX_SESSIONS = 100
_INTERNAL_TEXT = (
    "<task_notification>", "<reminder>", "[Scheduled:", "[Team inbox]", "[Compacted",
    "[Reactive compact]", "[snipped ", "[会话恢复]", "[用户中断]", "[用户执行了",
)
TRANSCRIPT_SEARCH_SCHEMA = {
    "name": "transcript_search",
    "description": (
        "Search earlier sessions in this workspace (user and assistant text and tool calls; tool output "
        "is not kept). Every word must appear in an entry. Returns session ids and entry numbers for transcript_read."
    ),
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}
TRANSCRIPT_READ_SCHEMA = {
    "name": "transcript_read",
    "description": "Read entries of an earlier session by its id (at least 8 characters), starting at an entry number.",
    "input_schema": {
        "type": "object",
        "properties": {"session": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
        "required": ["session"],
    },
}


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _entries(message: dict, source: str) -> list[str]:
    role = message.get("role")
    if source in ("system", "tool") or role not in ("user", "assistant"):
        return []
    content = message.get("content", "")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
    entries = []
    for block in blocks:
        kind = value(block, "type")
        if kind == "text":
            text = str(value(block, "text", "") or "")
            if text.strip() and not (role == "user" and text.strip().startswith(_INTERNAL_TEXT)):
                entries.append(f"{role}: {_clip(text, ENTRY_MAX_CHARS)}")
        elif kind == "tool_use" and role == "assistant":
            arguments = json.dumps(value(block, "input", {}), ensure_ascii=False, default=str)
            entries.append(f"assistant → {value(block, 'name', '')} {_clip(arguments, TOOL_INPUT_MAX_CHARS)}")
    return entries


def transcript_entries(workdir: Path, snapshot: dict) -> list[str]:
    """Archived (compacted-away) messages first, then the live history, without duplicates."""
    entries, seen = [], set()
    if snapshot["schema_version"] == 1:
        for history in snapshot["archive"]:
            for message in decode_history(history):
                entries.extend(_entries(message, message.get("_pamu_source", "")))
    else:
        from .context_archive import ContextArchive
        archive = ContextArchive(workdir, snapshot["id"])
        for entry in snapshot["archive"]:
            if entry["id"] in seen:
                continue
            seen.add(entry["id"])
            try:
                entries.extend(_entries(archive.read_message(entry), entry["source"]))
            except (SessionError, OSError, ValueError):
                LOGGER.warning("skipped an unreadable archived message in session %s", snapshot["id"][:8])
    records = snapshot.get("context_state", {}).get("_message_records") or [{} for _ in snapshot["history"]]
    for message, record in zip(decode_history(snapshot["history"]), records):
        if record.get("id") in seen:
            continue
        entries.extend(_entries(message, record.get("source") or message.get("_pamu_source", "")))
    return entries


def list_transcripts(workdir: Path) -> list[dict]:
    """Sessions of this workspace, newest first."""
    try:
        sessions, _skipped = SessionStore(workdir).list_sessions()
    except SessionError:
        return []
    return sessions


def describe(snapshot: dict) -> str:
    when = datetime.fromtimestamp(snapshot["updated_at"]).strftime("%Y-%m-%d %H:%M")
    return f"{snapshot['id'][:8]}  {when}  {snapshot['title'] or '(untitled)'}"


def search_transcripts(workdir: Path, query: str, *, limit: int = 10) -> str:
    terms = [term.casefold() for term in str(query).split()]
    if not terms:
        return "Error: query 不能为空。"
    matches = []
    for snapshot in list_transcripts(workdir)[:SEARCH_MAX_SESSIONS]:
        try:
            entries = transcript_entries(workdir, snapshot)
        except (SessionError, OSError, ValueError):
            continue
        for number, entry in enumerate(entries):
            folded = entry.casefold()
            if all(term in folded for term in terms):
                start = max(0, folded.find(terms[0]) - 80)
                matches.append(f"{describe(snapshot)}  #{number}: {entry[start:start + 300]}")
                if len(matches) >= limit:
                    return "\n".join(matches)
    return "\n".join(matches) if matches else "(no matches in earlier sessions)"


def read_transcript(workdir: Path, session: str, offset: int = 0, limit: int = 80) -> str:
    try:
        snapshot = SessionStore(workdir).load(str(session).strip())
        entries = transcript_entries(workdir, snapshot)
    except SessionError as error:
        return f"Error: {error}"
    offset = max(0, int(offset or 0))
    end = min(len(entries), offset + max(1, min(int(limit or 80), 200)))
    lines, size = [describe(snapshot) + f"  ({len(entries)} entries)"], 0
    for number in range(offset, end):
        line = f"#{number} {entries[number]}"
        size += len(line)
        if size > READ_MAX_CHARS and number > offset:
            end = number
            break
        lines.append(line)
    if end < len(entries):
        lines.append(f"[more: transcript_read(session={snapshot['id'][:8]!r}, offset={end})]")
    return "\n".join(lines)


def transcript_handlers(workdir: Path) -> dict:
    def search(query="", **_ignored):
        return search_transcripts(workdir, str(query))

    def read(session="", offset=0, limit=80, **_ignored):
        try:
            return read_transcript(workdir, str(session), int(offset or 0), int(limit or 80))
        except (TypeError, ValueError) as error:
            return f"Error: {error}"
    return {"transcript_search": search, "transcript_read": read}
