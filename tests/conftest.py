import pytest


@pytest.fixture(autouse=True)
def isolated_user_skills(tmp_path_factory, monkeypatch):
    """Keep the developer's ~/.claude, ~/.agents and ~/.pamu skills out of tests."""
    from agent_app import config

    monkeypatch.setattr(config, "USER_HOME", tmp_path_factory.mktemp("home"))
