"""Skill discovery across project and user directories.

A skill is ``<root>/<identifier>/SKILL.md``. Roots are searched in priority
order and the first root that defines an identifier wins, so project skills
override user skills. The directory name is the identifier the model and
``/skill`` use; a frontmatter ``name`` is accepted as an alias.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..logging_setup import get_logger

LOGGER = get_logger("pamu.skills")

LIST_SKILLS_SCHEMA = {
    "name": "list_skills",
    "description": "List the skills available from the project and user skill directories.",
    "input_schema": {"type": "object", "properties": {}},
}
_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)^---[ \t]*(?:\n|\Z)", re.DOTALL | re.MULTILINE)


@dataclass(slots=True)
class SkillState:
    roots: tuple[Path, ...] = ()
    workspace: Path | None = None
    home: Path | None = None
    registry: dict[str, dict] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    signature: tuple | None = None
    project_roots: tuple[Path, ...] = ()
    trust_store: object | None = None
    pending: dict[str, dict] = field(default_factory=dict)
    pending_fingerprint: str = ""
    asked_fingerprint: str = ""


@dataclass(frozen=True)
class SkillInvocation:
    name: str
    content: str
    task: str = ""

    @property
    def prompt(self) -> str:
        return (
            f"请使用技能 {self.name} 完成下面的任务，遵守当前工作模式和用户已有约束。\n"
            f"任务：{self.task or '根据当前对话执行此技能；缺少必要信息时先询问。'}\n\n"
            f"技能正文：\n{self.content}"
        )


def _parse_skill_frontmatter(text: str) -> tuple[dict, str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text
    try:
        metadata = yaml.safe_load(match[1])
    except yaml.YAMLError:
        metadata = None
    return (metadata if isinstance(metadata, dict) else {}), text[match.end():]


def _first_line(body: str) -> str:
    return next((line.strip().lstrip("#").strip() for line in body.splitlines() if line.strip()), "")


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _skill_directories(root: Path) -> list[Path]:
    try:
        return sorted(path for path in root.iterdir() if path.is_dir())
    except OSError:
        return []


def _signature(roots) -> tuple:
    entries = []
    for root in roots:
        for directory in _skill_directories(root):
            manifest = directory / "SKILL.md"
            try:
                entries.append((str(manifest), manifest.stat().st_mtime_ns))
            except OSError:
                continue
    return tuple(entries)


def _read_roots(roots, errors: list[str]) -> dict[str, dict]:
    """Skills from ``roots`` in priority order; the first root defining a name wins."""
    found: dict[str, dict] = {}
    for root in dict.fromkeys(roots):
        for directory in _skill_directories(root):
            manifest = directory / "SKILL.md"
            if directory.name in found or not manifest.is_file():
                continue
            try:
                raw = manifest.read_text(encoding="utf-8").removeprefix("\ufeff").replace("\r\n", "\n")
            except (OSError, UnicodeDecodeError) as error:
                errors.append(f"{manifest}: {type(error).__name__}")
                LOGGER.warning("skipped unreadable skill %s: %s", manifest, error)
                continue
            metadata, body = _parse_skill_frontmatter(raw)
            found[directory.name] = {
                "name": _text(metadata.get("name")) or directory.name,
                "description": " ".join((_text(metadata.get("description")) or _first_line(body)).split()),
                "content": raw,
                "path": manifest,
                "source": root,
            }
    return found


def _fingerprint(state: SkillState, skills: dict[str, dict]) -> str:
    entries = sorted(
        (root_label(state, skill["source"]), key, hashlib.sha256(skill["content"].encode("utf-8")).hexdigest())
        for key, skill in skills.items()
    )
    return hashlib.sha256(json.dumps(entries).encode("utf-8")).hexdigest() if entries else ""


def scan_skills(state: SkillState) -> dict[str, dict]:
    """Rebuild the registry, holding back project skills the user has not trusted."""
    errors: list[str] = []
    project = [root for root in state.roots if root in state.project_roots]
    pending = _read_roots(project, errors) if project and state.trust_store is not None else {}
    fingerprint = _fingerprint(state, pending)
    trusted = not pending or state.trust_store.is_trusted(state.workspace, fingerprint)
    roots = state.roots if trusted else tuple(root for root in state.roots if root not in project)
    state.registry = _read_roots(roots, errors if trusted else [])
    state.errors = errors
    state.pending, state.pending_fingerprint = ({}, "") if trusted else (pending, fingerprint)
    state.signature = _signature(state.roots)
    return state.registry


def trust_project_skills(state: SkillState) -> list[str]:
    """Trust the current set of project skills; return the names now loaded."""
    refresh_skills(state)
    names = sorted(state.pending)
    if names:
        state.trust_store.trust(state.workspace, state.pending_fingerprint)
        scan_skills(state)
    return names


def describe_pending(state: SkillState) -> str:
    return ", ".join(f"{key} [{root_label(state, skill['source'])}]" for key, skill in sorted(state.pending.items()))


def refresh_skills(state: SkillState) -> dict[str, dict]:
    """Rescan only when a skill was added, removed or edited since the last scan."""
    if state.signature is None or _signature(state.roots) != state.signature:
        scan_skills(state)
    return state.registry


def resolve_skill(state: SkillState, name) -> str | None:
    registry = refresh_skills(state)
    if isinstance(name, str) and name in registry:
        return name
    folded = str(name).strip().casefold()
    for key, skill in registry.items():
        if folded in (key.casefold(), skill["name"].casefold()):
            return key
    return None


def root_label(state: SkillState, root: Path) -> str:
    for base, prefix in ((state.workspace, ""), (state.home, "~/")):
        if base is not None:
            try:
                return prefix + root.relative_to(base).as_posix()
            except ValueError:
                continue
    return str(root)


def describe_skill(state: SkillState, key: str) -> str:
    skill = state.registry[key]
    alias = f" (name: {skill['name']})" if skill["name"].casefold() != key.casefold() else ""
    return f"{key}: {skill['description']}{alias} [{root_label(state, skill['source'])}]"


def list_skills(state: SkillState) -> str:
    registry = refresh_skills(state)
    if registry:
        lines = [f"- {describe_skill(state, key)}" for key in registry]
    else:
        lines = ["(no skills found)", "Searched, highest priority first:"]
        lines += [f"- {root_label(state, root)}" for root in dict.fromkeys(state.roots)]
        lines.append("Add a skill as <directory>/<name>/SKILL.md in one of these directories.")
    if state.pending:
        lines.append(f"Project skills awaiting trust (not loaded): {describe_pending(state)}. "
                     "Ask the user to run /skill trust to load them.")
    if state.errors:
        lines.append(f"Skipped {len(state.errors)} unreadable skill(s): " + "; ".join(state.errors))
    return "\n".join(lines)


def load_skill(state: SkillState, name: str) -> str:
    key = resolve_skill(state, name)
    if key is None:
        if isinstance(name, str) and name.strip() in state.pending:
            return f"Skill {name} belongs to this project and is not trusted yet; ask the user to run /skill trust."
        available = ", ".join(state.registry) or "(none)"
        return f"Skill not found: {name}. Available skills: {available}"
    return state.registry[key]["content"]
