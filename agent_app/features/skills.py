"""Skill discovery across project and user directories.

A skill is ``<root>/<identifier>/SKILL.md``. Roots are searched in priority
order and the first root that defines an identifier wins, so project skills
override user skills. The directory name is the identifier the model and
``/skill`` use; a frontmatter ``name`` is accepted as an alias.
"""

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


def scan_skills(state: SkillState) -> dict[str, dict]:
    """Rebuild the registry from every root, skipping unreadable skills."""
    registry: dict[str, dict] = {}
    errors: list[str] = []
    for root in dict.fromkeys(state.roots):
        for directory in _skill_directories(root):
            manifest = directory / "SKILL.md"
            if directory.name in registry or not manifest.is_file():
                continue
            try:
                raw = manifest.read_text(encoding="utf-8").removeprefix("\ufeff").replace("\r\n", "\n")
            except (OSError, UnicodeDecodeError) as error:
                errors.append(f"{manifest}: {type(error).__name__}")
                LOGGER.warning("skipped unreadable skill %s: %s", manifest, error)
                continue
            metadata, body = _parse_skill_frontmatter(raw)
            registry[directory.name] = {
                "name": _text(metadata.get("name")) or directory.name,
                "description": " ".join((_text(metadata.get("description")) or _first_line(body)).split()),
                "content": raw,
                "path": manifest,
                "source": root,
            }
    state.registry, state.errors = registry, errors
    state.signature = _signature(state.roots)
    return registry


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
    if state.errors:
        lines.append(f"Skipped {len(state.errors)} unreadable skill(s): " + "; ".join(state.errors))
    return "\n".join(lines)


def load_skill(state: SkillState, name: str) -> str:
    key = resolve_skill(state, name)
    if key is None:
        available = ", ".join(state.registry) or "(none)"
        return f"Skill not found: {name}. Available skills: {available}"
    return state.registry[key]["content"]
