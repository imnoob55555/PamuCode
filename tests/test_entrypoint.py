import runpy


def test_module_entrypoint_delegates_to_cli(monkeypatch):
    calls = []
    monkeypatch.setattr("agent_app.cli.main", lambda: calls.append("main"))

    runpy.run_module("agent_app", run_name="__main__")

    assert calls == ["main"]
