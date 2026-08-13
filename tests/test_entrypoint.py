import runpy
from pathlib import Path

from agent_app.bootstrap import INSTALL_ROOT


def test_module_entrypoint_delegates_to_cli(monkeypatch):
    calls = []
    monkeypatch.setattr("agent_app.cli.main", lambda: calls.append("main"))

    runpy.run_module("agent_app", run_name="__main__")

    assert calls == ["main"]


def test_install_root_is_the_standalone_project_root():
    assert INSTALL_ROOT == Path(__file__).resolve().parents[1]
