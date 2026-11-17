from agent_app.features.memory import (
    MemoryStore,
    list_memory_files,
    read_memory_index,
    write_memory_file,
)
from agent_app.features.skills import SkillState, scan_skills
import pytest


def test_skill_scan_has_no_import_time_side_effect(tmp_path):
    state = SkillState(root=tmp_path / "skills")

    assert state.registry == {}
    assert not state.root.exists()


def test_skill_scan_populates_explicit_state(tmp_path):
    root = tmp_path / "skills"
    manifest = root / "review" / "SKILL.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "---\nname: review\ndescription: Review code\n---\n\nInstructions",
        encoding="utf-8",
    )
    state = SkillState(root=root)

    scan_skills(state)

    assert state.registry["review"]["content"].endswith("Instructions")


def test_memory_store_writes_and_indexes_under_its_root(tmp_path):
    store = MemoryStore(
        root=tmp_path / "memory",
        index_path=tmp_path / "memory" / "MEMORY.md",
    )

    path = write_memory_file(
        store,
        "project constraints",
        "project",
        "keep tests offline",
        "Never make live API calls in tests.",
    )

    assert path.is_relative_to(store.root)
    assert [item["filename"] for item in list_memory_files(store)] == [
        "project-constraints.md"
    ]
    assert "keep tests offline" in read_memory_index(store)


@pytest.mark.parametrize("operation", ["select", "extract"])
def test_memory_reads_same_visible_dialogue_after_session_roundtrip(tmp_path, operation):
    from agent_app.adapters.common import TextBlock, ToolUseBlock
    from agent_app.features.memory import select_relevant_memories, extract_memories
    from agent_app.features.session_codec import encode_history, decode_history

    store = MemoryStore(tmp_path / "memory", tmp_path / "memory" / "MEMORY.md")
    write_memory_file(store, "offline", "project", "offline tests", "Use fake clients.")
    messages = [{"role": "assistant", "content": [
        TextBlock("Keep tests"), TextBlock("offline."),
        {"type": "thinking", "thinking": "HIDDEN_REASONING", "signature": "sig"},
        ToolUseBlock("c1", "bash", {"command": "HIDDEN_TOOL_INPUT"}),
    ]}]
    restored = decode_history(encode_history(messages))
    prompts = []
    def summarize(prompt, max_tokens):
        prompts.append(prompt)
        return "[0]" if operation == "select" else "[]"
    run = select_relevant_memories if operation == "select" else extract_memories
    assert run(store, messages, summarize) == run(store, restored, summarize)
    assert len(prompts) == 2
    assert prompts[0] == prompts[1]
    assert "Keep tests offline." in prompts[0]
    assert "HIDDEN_REASONING" not in prompts[0]
    assert "HIDDEN_TOOL_INPUT" not in prompts[0]
