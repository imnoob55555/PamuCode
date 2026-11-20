from dataclasses import dataclass, field
from pathlib import Path

import yaml

LIST_SKILLS_SCHEMA = {
    "name": "list_skills",
    "description": "List the skills registered for this workspace and their descriptions.",
    "input_schema": {"type": "object", "properties": {}},
}


@dataclass(slots=True)
class SkillState:
    root: Path
    registry: dict[str, dict] = field(default_factory=dict)


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
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        metadata = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        metadata = {}
    return metadata, parts[2].strip()


def scan_skills(state: SkillState):
    if not state.root.exists():
        return
    for directory in sorted(state.root.iterdir()):
        if not directory.is_dir():
            continue
        manifest = directory / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text()
            metadata, body = _parse_skill_frontmatter(raw)
            name = metadata.get("name", directory.name)
            description = metadata.get(
                "description", raw.split("\n")[0].lstrip("#").strip()
            )
            state.registry[directory.name] = {
                "name": name,
                "description": description,
                "content": raw,
            }


def list_skills(state: SkillState) -> str:
    if not state.registry:
        return "(no skills found)"
    return "\n".join(
        f"- **{skill['name']}**: {skill['description']}"
        for skill in state.registry.values()
    )


def load_skill(state: SkillState, name: str) -> str:
    skill = state.registry.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]
