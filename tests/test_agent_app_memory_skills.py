from agent_app.features.skills import SkillState, scan_skills
import pytest


def test_skill_scan_has_no_import_time_side_effect(tmp_path):
    state = SkillState((tmp_path / "skills",))

    assert state.registry == {}
    assert not state.roots[0].exists()


def test_skill_scan_populates_explicit_state(tmp_path):
    root = tmp_path / "skills"
    manifest = root / "review" / "SKILL.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "---\nname: review\ndescription: Review code\n---\n\nInstructions",
        encoding="utf-8",
    )
    state = SkillState((root,))

    scan_skills(state)

    assert state.registry["review"]["content"].endswith("Instructions")


def test_auto_memory_can_be_turned_off(tmp_path, monkeypatch):
    from agent_app.config import AppConfig
    assert AppConfig.from_env(tmp_path, environ={"MODEL_ID": "m"}).auto_memory is True
    assert AppConfig.from_env(tmp_path, environ={"MODEL_ID": "m", "PAMU_AUTO_MEMORY": "off"}).auto_memory is False
    with pytest.raises(ValueError, match="PAMU_AUTO_MEMORY"):
        AppConfig.from_env(tmp_path, environ={"MODEL_ID": "m", "PAMU_AUTO_MEMORY": "maybe"})
