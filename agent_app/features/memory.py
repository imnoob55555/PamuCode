import json
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..adapters.common import output_text
from ..logging_setup import get_logger

LOGGER = get_logger("pamu.memory")
MEMORY_TYPES = ["user", "feedback", "project", "reference"]
CONSOLIDATE_THRESHOLD = 10
BACKUPS_KEPT = 5
SECRET_PATTERN = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[abpr]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:api[_-]?key|token|secret|password|passwd)\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+=]{12,}"
)
_INTERNAL_TEXT = (
    "<task_notification>", "<reminder>", "[Scheduled:", "[Team inbox]", "[Compacted",
    "[Reactive compact]", "[snipped ", "[会话恢复]", "[用户中断]",
)


@dataclass(frozen=True, slots=True)
class MemoryStore:
    root: Path
    index_path: Path


def _parse_memory_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    metadata = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip('"').strip("'")
    return metadata, parts[2].strip()


def write_memory_file(
    store: MemoryStore,
    name: str,
    memory_type: str,
    description: str,
    body: str,
):
    slug = name.lower().replace(" ", "-").replace("/", "-")
    path = store.root / f"{slug}.md"
    store.root.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {memory_type}\n---\n\n{body}\n"
    )
    rebuild_index(store)
    return path


def rebuild_index(store: MemoryStore):
    lines = []
    for path in sorted(store.root.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        raw = path.read_text()
        metadata, body = _parse_memory_frontmatter(raw)
        name = metadata.get("name", path.stem)
        description = metadata.get("description", body.split("\n")[0][:80])
        lines.append(f"- [{name}]({path.name}) — {description}")
    store.index_path.write_text("\n".join(lines) + "\n" if lines else "")


def read_memory_index(store: MemoryStore) -> str:
    if not store.index_path.exists():
        return ""
    text = store.index_path.read_text().strip()
    return text if text else ""


def read_memory_file(store: MemoryStore, filename: str, *, max_chars=None) -> str | None:
    path = store.root / filename
    if path.name.lower().startswith((".env", ".settings", "credentials")) or not path.exists() or path.is_symlink() or not path.resolve().is_relative_to(store.root.resolve()):
        return None
    with path.open(encoding="utf-8") as stream:
        return stream.read(max_chars) if max_chars is not None else stream.read()


def list_memory_files(store: MemoryStore, *, max_files=None, max_chars=None) -> list[dict]:
    result = []
    if not store.root.exists():
        return result
    for path in sorted(store.root.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        if path.is_symlink() or path.name.lower().startswith((".env", ".settings", "credentials")):
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                raw = stream.read(max_chars) if max_chars is not None else stream.read()
        except (OSError, UnicodeError):
            continue
        metadata, body = _parse_memory_frontmatter(raw)
        result.append(
            {
                "filename": path.name,
                "name": metadata.get("name", path.stem),
                "description": metadata.get("description", ""),
                "type": metadata.get("type", "user"),
                "body": body,
            }
        )
        if max_files is not None and len(result) >= max_files:
            break
    return result


def is_internal_reminder_text(text: str) -> bool:
    text = text.strip().lower()
    return text.startswith(("<reminder>", "</reminder>"))


def select_relevant_memories(
    store: MemoryStore,
    messages: list,
    summarize,
    max_items: int = 5,
) -> list[str]:
    files = list_memory_files(store, max_files=64, max_chars=2048)
    if not files:
        return []
    recent_texts = []
    for message in reversed(messages):
        content = message.get("content", "")
        if isinstance(content, list):
            content = output_text(content, separator=" ")
        if is_internal_reminder_text(content.strip()):
            continue
        if isinstance(content, str):
            recent_texts.append(content)
        if len(recent_texts) >= 5:
            break
    recent = " ".join(reversed(recent_texts))[:2000]
    if not recent.strip():
        return []
    catalog = "\n".join(
        f"{index}: {file['name'][:128]} — {file['description'][:256]}"
        for index, file in enumerate(files)
    )
    prompt = (
        "Given the recent conversation and the memory catalog below, "
        "select the indices of memories that are clearly relevant. "
        "Return ONLY a JSON array of integers, e.g. [0, 3]. "
        "If none are relevant, return [].\n\n"
        f"Recent conversation:\n{recent}\n\nMemory catalog:\n{catalog[:4000]}"
    )
    try:
        text = summarize(prompt, 200).strip()
        match = re.search(r"\[.*?\]", text, re.DOTALL)
        if match:
            indices = json.loads(match.group())
            selected = []
            for index in indices:
                if isinstance(index, int) and 0 <= index < len(files):
                    selected.append(files[index]["filename"])
                    if len(selected) >= max_items:
                        break
            return selected
    except Exception:
        ...  # fall back to keyword matching below
    keywords = [word.lower() for word in recent.split() if len(word) > 3]
    selected = []
    for file in files:
        text = (file["name"] + " " + file["description"]).lower()
        if any(keyword in text for keyword in keywords):
            selected.append(file["filename"])
            if len(selected) >= max_items:
                break
    return selected


def load_memories(store: MemoryStore, messages: list, summarize, *, max_tokens=1024) -> str:
    from ..core.context_materials import bounded_text
    if max_tokens <= 0:
        return ""
    selected_files = select_relevant_memories(store, messages, summarize)
    if not selected_files:
        return ""
    parts = ["<relevant_memories>"]
    for filename in selected_files:
        content = read_memory_file(store, filename, max_chars=max_tokens * 2)
        if content:
            parts.append(content)
    parts.append("</relevant_memories>")
    return bounded_text("\n\n".join(parts), max_tokens)


def contains_secret(text: str) -> bool:
    return bool(SECRET_PATTERN.search(text))


def _authored_text(message) -> str:
    """User-typed and assistant-written text only; tool results and runtime notices are excluded."""
    role = message.get("role")
    if role not in ("user", "assistant") or message.get("_pamu_source") == "system":
        return ""
    content = message.get("content", "")
    blocks = [content] if isinstance(content, str) else [
        block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
        for block in content
        if (block.get("type") if isinstance(block, dict) else getattr(block, "type", None)) == "text"
    ]
    texts = [text for text in blocks if isinstance(text, str) and text.strip()]
    if role == "user":
        texts = [text for text in texts if not text.strip().startswith(_INTERNAL_TEXT)]
    return " ".join(texts)


def _safe_items(items) -> list[dict]:
    safe = []
    for memory in items if isinstance(items, list) else []:
        if not isinstance(memory, dict):
            continue
        fields = [str(memory.get(key, "")) for key in ("name", "description", "body")]
        if contains_secret("\n".join(fields)):
            LOGGER.warning("skipped a memory that looks like it contains a credential")
            continue
        safe.append(memory)
    return safe


def backup_memories(store: MemoryStore, clock=datetime.now) -> Path | None:
    """Copy the memory files aside, keeping only the most recent backups."""
    files = [path for path in store.root.glob("*.md")] if store.root.exists() else []
    if not files:
        return None
    backups = store.root / ".backup"
    target = backups / clock().strftime("%Y%m%d-%H%M%S-%f")
    target.mkdir(parents=True)
    for path in files:
        shutil.copy2(path, target / path.name)
    for old in sorted(path for path in backups.iterdir() if path.is_dir())[:-BACKUPS_KEPT]:
        shutil.rmtree(old, ignore_errors=True)
    return target


def forget_memory(store: MemoryStore, name: str) -> str | None:
    """Delete the memory whose name or file stem matches; return its name."""
    folded = name.strip().casefold()
    for item in list_memory_files(store):
        if folded in (item["name"].casefold(), Path(item["filename"]).stem.casefold()):
            (store.root / item["filename"]).unlink()
            rebuild_index(store)
            return item["name"]
    return None


def extract_memories(store: MemoryStore, messages: list, summarize) -> list[str]:
    """Persist new memories from the dialogue; return the names that were written."""
    dialogue_parts = []
    for message in messages:
        text = _authored_text(message)
        if text:
            dialogue_parts.append(f"{message['role']}: {text}")
    dialogue = "\n".join(dialogue_parts)
    if not dialogue.strip():
        return []
    existing = list_memory_files(store)
    existing_description = (
        "\n".join(f"- {item['name']}: {item['description']}" for item in existing)
        if existing
        else "(none)"
    )
    prompt = (
        "Extract user preferences, constraints, or project facts from this dialogue.\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n"
        "- name: short kebab-case identifier (e.g. 'user-preference-tabs')\n"
        "- type: one of 'user' (user preference), 'feedback' (guidance), "
        "'project' (project fact), 'reference' (external pointer)\n"
        "- description: one-line summary for index lookup\n"
        "- body: full detail in markdown\n"
        "If nothing new or already covered by existing memories, return [].\n\n"
        f"Existing memories:\n{existing_description}\n\nDialogue:\n{dialogue[:4000]}"
    )
    try:
        text = summarize(prompt, 800).strip()
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return []
        written = []
        for memory in _safe_items(json.loads(match.group())):
            name = str(memory.get("name") or f"memory_{int(time.time())}")
            description, body = str(memory.get("description", "")), str(memory.get("body", ""))
            if description and body:
                write_memory_file(store, name, str(memory.get("type", "user")), description, body)
                written.append(name)
        if written:
            LOGGER.info("extracted %d new memories", len(written))
        return written
    except Exception:
        LOGGER.warning("memory extraction failed", exc_info=True)
        return []


def consolidate_memories(store: MemoryStore, summarize):
    files = list_memory_files(store)
    if len(files) < CONSOLIDATE_THRESHOLD:
        return
    catalog = "\n".join(
        f"## {file['filename']}\nname: {file['name']}\n"
        f"description: {file['description']}\n{file['body']}"
        for file in files
    )
    prompt = (
        "Consolidate the following memory files. Rules:\n"
        "1. Merge duplicates into one\n"
        "2. Remove outdated/contradicted memories\n"
        "3. Keep the total under 30 memories\n"
        "4. Preserve important user preferences above all\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n\n"
        f"{catalog[:16000]}"
    )
    try:
        text = summarize(prompt, 3000).strip()
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return
        items = _safe_items(json.loads(match.group()))
        if not items:
            LOGGER.warning("memory consolidation returned nothing; keeping existing memories")
            return
        backup_memories(store)
        for path in store.root.glob("*.md"):
            if path.name != "MEMORY.md":
                path.unlink()
        for memory in items:
            name = memory.get("name", f"memory_{int(time.time())}")
            memory_type = memory.get("type", "user")
            description = memory.get("description", "")
            body = memory.get("body", "")
            if description and body:
                write_memory_file(store, name, memory_type, description, body)
        LOGGER.info("consolidated %d memories into %d", len(files), len(items))
    except Exception:
        LOGGER.warning("memory consolidation failed", exc_info=True)


def find_latest_text_user_message(messages: list) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if content.strip().startswith("<reminder>"):
            continue
        return index
    return None


def build_request_messages_with_memories(
    store: MemoryStore,
    messages: list,
    summarize,
    *, max_tokens=1024,
):
    memories = load_memories(store, messages, summarize, max_tokens=max_tokens)
    if not memories:
        return messages
    target = find_latest_text_user_message(messages)
    if target is None:
        return messages
    request_messages = messages.copy()
    request_messages[target] = {
        **messages[target],
        "content": messages[target]["content"] + "\n\n" + memories,
    }
    return request_messages
