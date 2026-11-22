import os
from pathlib import Path

import pytest

from agent_app import config as config_module
from agent_app.config import AppConfig
from agent_app.core.context import build_context
from agent_app.features.skills import (
    SkillState,
    list_skills,
    load_skill,
    refresh_skills,
    resolve_skill,
    scan_skills,
)


def write_skill(root: Path, name: str, text: str) -> Path:
    manifest = root / name / "SKILL.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(text, encoding="utf-8")
    return manifest


def state_for(tmp_path, *roots):
    state = SkillState(roots=tuple(roots), workspace=tmp_path / "ws", home=tmp_path / "home")
    scan_skills(state)
    return state


def test_config_searches_project_then_user_directories_but_not_root_skills(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(config_module, "USER_HOME", home)
    workspace = tmp_path / "ws"
    config = AppConfig.from_env(workspace, environ={"MODEL_ID": "m"})
    assert config.skills_dir == workspace / ".pamu" / "skills"
    assert config.skill_dirs == (
        workspace / ".pamu" / "skills",
        workspace / ".agents" / "skills",
        workspace / ".claude" / "skills",
        home / ".pamu" / "skills",
        home / ".agents" / "skills",
        home / ".claude" / "skills",
    )
    assert workspace / "skills" not in config.skill_dirs


def test_config_does_not_repeat_directories_when_workspace_is_home(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "USER_HOME", tmp_path)
    config = AppConfig.from_env(tmp_path, environ={"MODEL_ID": "m"})
    assert len(config.skill_dirs) == len(set(config.skill_dirs)) == 3


def test_project_skills_override_user_skills_with_the_same_identifier(tmp_path):
    project = tmp_path / "ws" / ".pamu" / "skills"
    user = tmp_path / "home" / ".claude" / "skills"
    write_skill(user, "review", "---\ndescription: user review\n---\nUSER")
    write_skill(user, "graphify", "---\ndescription: graphs\n---\nGRAPH")
    write_skill(project, "review", "---\ndescription: project review\n---\nPROJECT")
    state = state_for(tmp_path, project, user)
    assert list(state.registry) == ["review", "graphify"]
    assert load_skill(state, "review").endswith("PROJECT")
    assert state.registry["graphify"]["source"] == user
    listing = list_skills(state)
    assert "- review: project review [.pamu/skills]" in listing
    assert "- graphify: graphs [~/.claude/skills]" in listing


def test_listed_identifier_is_the_one_load_skill_accepts(tmp_path):
    root = tmp_path / "ws" / ".agents" / "skills"
    write_skill(root, "code-review", "---\nname: Reviewer\ndescription: Review code\n---\nBody")
    state = state_for(tmp_path, root)
    assert "- code-review: Review code (name: Reviewer)" in list_skills(state)
    for name in ("code-review", "Reviewer", "reviewer", " CODE-REVIEW "):
        assert resolve_skill(state, name) == "code-review"
        assert load_skill(state, name).endswith("Body")
    missing = load_skill(state, "nope")
    assert missing.startswith("Skill not found: nope") and "code-review" in missing


@pytest.mark.parametrize("text", [
    "---\n---\nBody",
    "---\n- a\n- b\n---\nBody",
    "---\nname: [unclosed\n---\nBody",
    "---\nname: 42\ndescription: [1, 2]\n---\nBody",
])
def test_malformed_frontmatter_never_breaks_discovery(tmp_path, text):
    root = tmp_path / "skills"
    write_skill(root, "odd", text)
    write_skill(root, "fine", "---\ndescription: ok\n---\nBody")
    state = state_for(tmp_path, root)
    assert set(state.registry) == {"odd", "fine"}
    assert state.registry["odd"]["name"] == "odd"
    assert state.registry["odd"]["description"] == "Body"


def test_unreadable_skills_are_skipped_and_reported(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "fine", "---\ndescription: ok\n---\nBody")
    (root / "binary").mkdir()
    (root / "binary" / "SKILL.md").write_bytes(b"\xff\xfe\x00bad")
    state = state_for(tmp_path, root)
    assert list(state.registry) == ["fine"]
    assert len(state.errors) == 1 and "binary" in state.errors[0]
    assert "binary" in list_skills(state)


def test_description_falls_back_to_the_first_body_line(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "titled", "\ufeff---\r\nname: titled\r\n---\r\n\r\n# Real title\r\nBody")
    write_skill(root, "plain", "\n\n## Plain heading\nBody")
    write_skill(root, "folded", "---\ndescription: |\n  first line\n  second line\n---\nBody")
    state = state_for(tmp_path, root)
    assert state.registry["folded"]["description"] == "first line second line"
    assert state.registry["titled"]["description"] == "Real title"
    assert state.registry["plain"]["description"] == "Plain heading"


def test_directories_without_manifest_and_files_are_ignored(tmp_path):
    root = tmp_path / "skills"
    (root / "synced" / "nested" / "deep").mkdir(parents=True)
    write_skill(root / "synced" / "nested", "deep", "Body")
    (root / "notes.md").parent.mkdir(parents=True, exist_ok=True)
    (root / "notes.md").write_text("x")
    write_skill(root, "real", "Body")
    state = state_for(tmp_path, root, tmp_path / "missing")
    assert list(state.registry) == ["real"]


def test_refresh_picks_up_added_changed_and_removed_skills_without_restart(tmp_path):
    root = tmp_path / "skills"
    state = state_for(tmp_path, root)
    assert state.registry == {}
    manifest = write_skill(root, "new", "---\ndescription: first\n---\nBody")
    assert "new" in refresh_skills(state)
    manifest.write_text("---\ndescription: second version\n---\nBody")
    stat = manifest.stat()
    os.utime(manifest, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert list_skills(state).startswith("- new: second version")
    manifest.unlink()
    assert load_skill(state, "new").startswith("Skill not found")


def test_empty_listing_explains_where_to_add_skills(tmp_path):
    project = tmp_path / "ws" / ".pamu" / "skills"
    user = tmp_path / "home" / ".agents" / "skills"
    listing = list_skills(state_for(tmp_path, project, user))
    assert listing.startswith("(no skills found)")
    assert ".pamu/skills" in listing and "~/.agents/skills" in listing
    assert "<name>/SKILL.md" in listing


def _runtime(tmp_path, monkeypatch, **overrides):
    from dataclasses import replace

    from agent_app.bootstrap import build_runtime

    monkeypatch.setattr(config_module, "USER_HOME", tmp_path / "home")
    config = replace(AppConfig.from_env(tmp_path / "ws", environ={"MODEL_ID": "m", "PAMU_TRUST_PROJECT_SKILLS": "1"}), headless=True, **overrides)
    return build_runtime(config, None, adapter=object())


def test_runtime_starts_with_a_broken_skill_and_loads_user_skills_by_alias(tmp_path, monkeypatch):
    write_skill(tmp_path / "ws" / ".pamu" / "skills", "broken", "---\n---\nBody")
    write_skill(tmp_path / "home" / ".agents" / "skills", "grill-me", "---\nname: Grill Me\ndescription: Ask hard questions\n---\nGRILL")
    runtime = _runtime(tmp_path, monkeypatch)
    try:
        _, handlers = runtime.tools.snapshot()
        listing = handlers["list_skills"]()
        assert "- broken: Body [.pamu/skills]" in listing
        assert "- grill-me: Ask hard questions (name: Grill Me) [~/.agents/skills]" in listing
        assert handlers["load_skill"](name="Grill Me").endswith("GRILL")
        assert runtime.session.context_state["loaded_skills"][-1]["name"] == "grill-me"
        assert "- grill-me: Ask hard questions (name: Grill Me)" in runtime.prompt_builder.build(
            build_context(runtime, []))
    finally:
        runtime.sessions.close()


def test_confined_runtime_never_reads_skill_directories(tmp_path, monkeypatch):
    write_skill(tmp_path / "ws" / ".pamu" / "skills", "secret", "Body")
    runtime = _runtime(tmp_path, monkeypatch, sandbox_mode="confined")
    try:
        assert runtime.skills.roots == ()
        _, handlers = runtime.tools.snapshot()
        assert handlers["list_skills"]().startswith("(no skills found)")
        assert runtime.skills.registry == {}
    finally:
        runtime.sessions.close()
