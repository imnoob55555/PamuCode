from agent_app.config import AppConfig
from agent_app.runtime import SessionState


def test_app_config_separates_installation_workspace_and_state_paths(
    tmp_path, monkeypatch
):
    install_root = tmp_path / "install"
    workspace = tmp_path / "project"
    monkeypatch.setenv("MODEL_ID", "primary")
    monkeypatch.setenv("FALLBACK_MODEL_ID", "fallback")

    config = AppConfig.from_env(install_root, workspace)

    assert config.repo_root == install_root.resolve()
    assert config.workdir == workspace.resolve()
    assert config.state_dir == workspace.resolve() / ".pamu"
    assert config.skills_dir == workspace.resolve() / "skills"
    assert config.memory_dir == config.state_dir / "memory"
    assert config.memory_index == config.state_dir / "memory" / "MEMORY.md"
    assert config.transcripts_dir == config.state_dir / "transcripts"
    assert config.tool_result_dir == config.state_dir / "task_outputs" / "tool-results"
    assert config.task_dir == config.state_dir / "tasks"
    assert config.mailbox_dir == config.state_dir / "mailboxes"
    assert config.scheduled_tasks_path == config.state_dir / "scheduled_tasks.json"
    assert config.worktrees_dir == config.state_dir / "worktrees"
    assert not config.state_dir.exists()


def test_session_state_is_fresh_per_instance():
    first = SessionState()
    second = SessionState()

    first.history.append({"role": "user", "content": "one"})

    assert second.history == []
    assert second.context == {}
    assert second.todos == []
