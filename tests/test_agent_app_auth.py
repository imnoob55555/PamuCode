"""Acceptance tests for the provider credential store."""

from __future__ import annotations

import stat

from agent_app.features.auth import (
    get_login,
    load_credentials,
    remove_login,
    set_active,
    set_model,
    store_login,
)


def test_login_roundtrip_sets_active_and_model(tmp_path):
    path = tmp_path / "credentials.json"

    store_login("deepseek", "sk-ds", "deepseek-chat", path)

    data = load_credentials(path)
    assert data["active"] == "deepseek"
    assert get_login(data, "deepseek") == {
        "api_key": "sk-ds",
        "model": "deepseek-chat",
    }


def test_stored_credentials_are_owner_only(tmp_path):
    path = tmp_path / "credentials.json"
    store_login("zhipu", "sk-zp", None, path)

    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def test_logout_removes_record_and_clears_active(tmp_path):
    path = tmp_path / "credentials.json"
    store_login("deepseek", "sk-ds", None, path)
    store_login("moonshot", "sk-ms", None, path)

    data = remove_login("deepseek", path)

    assert "deepseek" not in data["providers"]
    assert data["active"] == "moonshot"
    remove_login("moonshot", path)
    assert load_credentials(path) == {"providers": {}, "active": None}


def test_malformed_or_missing_file_degrades_to_empty(tmp_path):
    path = tmp_path / "credentials.json"
    assert load_credentials(path) == {"providers": {}, "active": None}

    path.write_text("{not json", encoding="utf-8")
    assert load_credentials(path) == {"providers": {}, "active": None}

    path.write_text('["a list"]', encoding="utf-8")
    assert load_credentials(path) == {"providers": {}, "active": None}


def test_set_active_and_model_persist(tmp_path):
    path = tmp_path / "credentials.json"
    store_login("deepseek", "sk-ds", "deepseek-chat", path)
    store_login("moonshot", "sk-ms", None, path)

    set_active("deepseek", path)
    set_model("deepseek", "deepseek-reasoner", path)

    data = load_credentials(path)
    assert data["active"] == "deepseek"
    assert get_login(data, "deepseek")["model"] == "deepseek-reasoner"


def test_set_model_without_login_is_a_noop(tmp_path):
    path = tmp_path / "credentials.json"

    data = set_model("deepseek", "deepseek-chat", path)

    assert data == {"providers": {}, "active": None}
