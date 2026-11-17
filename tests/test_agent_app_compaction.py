from agent_app.config import AppConfig
from agent_app.core.compaction import persist_large_output


def config_for(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ID", "test-model")
    return AppConfig.from_env(tmp_path)


def test_persist_large_output_writes_below_configured_root(
    tmp_path,
    monkeypatch,
):
    config = config_for(tmp_path, monkeypatch)
    output = "x" * (config.persist_threshold + 1)

    persisted = persist_large_output(config, "tool-1", output)
    path = config.tool_result_dir / "tool-1.txt"

    assert path.is_relative_to(config.tool_result_dir)
    assert path.read_text(encoding="utf-8") == output
    assert str(path) in persisted
