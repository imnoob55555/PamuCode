import json
from types import SimpleNamespace

import pytest

from agent_app.adapters.common import ModelResponse, TextBlock, ToolUseBlock
from agent_app.bootstrap import build_runtime
from agent_app.config import AppConfig
from agent_app.core.prompt import assemble_system_prompt
from agent_app.features import dreaming
from agent_app.features.memory import (
    BACKUPS_KEPT, MemoryStore, backup_memories, forget_note, injected_index, list_notes, memory_handler,
    restore_backup, run_memory_command,
)
from agent_app.features.sessions import SessionPersistence
from agent_app.features.transcripts import read_transcript, search_transcripts, transcript_entries


def _store(tmp_path):
    return MemoryStore(tmp_path / "memory", tmp_path / "memory" / "MEMORY.md")


class ScriptedAdapter:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return next(self.responses)


def _runtime(tmp_path, adapter=None, **environ):
    config = AppConfig.from_env(tmp_path, environ={"MODEL_ID": "local", "PAMU_CONTEXT_WINDOW": "256000", **environ})
    return build_runtime(config, object(), adapter=adapter or ScriptedAdapter(), provider="ollama", protocol="ollama")


def _save_session(runtime, words):
    persistence = SessionPersistence(runtime.config.workdir)
    runtime.session.history = [
        {"role": "user", "content": f"please remember {words}"},
        {"role": "assistant", "content": [
            {"type": "text", "text": f"I will use {words}."},
            {"type": "tool_use", "id": "c1", "name": "bash", "input": {"command": "ls"}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "TOOL-OUTPUT-SECRET"}]},
        {"role": "user", "_pamu_source": "system", "content": "RUNTIME-NOTICE"},
        {"role": "assistant", "content": [{"type": "text", "text": f"Done with {words}."}]},
    ]
    runtime.session.context_state = {}
    persistence.checkpoint(runtime)
    persistence.close()
    return persistence.session_id


def test_memory_tool_writes_nested_notes_and_views_them(tmp_path):
    store = _store(tmp_path)
    assert run_memory_command(store, "view") == "(memory folder is empty)"
    assert run_memory_command(store, "create", "MEMORY.md", "- [style](style/python.md) — code style") == "Created MEMORY.md"
    assert run_memory_command(store, "create", "style/python.md", "# Python style\nUse tabs.") == "Created style/python.md"
    assert run_memory_command(store, "str_replace", "style/python.md", old_str="tabs", new_str="4 spaces") == "Updated style/python.md"
    assert "4 spaces" in run_memory_command(store, "view", "style/python.md")
    listing = run_memory_command(store, "view")
    assert listing.splitlines()[0].startswith("- MEMORY.md") and "style/python.md" in listing and "Python style" in listing
    assert list_notes(store) == [{"path": "style/python.md", "bytes": 29, "summary": "Python style"}]
    assert "old_str" in run_memory_command(store, "str_replace", "style/python.md", old_str="missing", new_str="x")
    assert run_memory_command(store, "delete", "style/python.md") == "Deleted style/python.md"
    assert not (store.root / "style").exists()


@pytest.mark.parametrize("path", ["../escape.md", ".backup/x.md", ".dream.json", "a/b/c/d.md", "notes.txt", ""])
def test_memory_tool_rejects_paths_outside_the_note_tree(tmp_path, path):
    store = _store(tmp_path)
    assert run_memory_command(store, "create", path, "text").startswith("Error:")
    assert not (tmp_path / "escape.md").exists()


def test_memory_tool_refuses_symlinks_secrets_and_read_only_writes(tmp_path):
    store = _store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    store.root.mkdir()
    (store.root / "link").symlink_to(outside, target_is_directory=True)
    assert "符号链接" in run_memory_command(store, "create", "link/x.md", "text")
    assert list(outside.iterdir()) == []
    assert "凭证" in run_memory_command(store, "create", "key.md", "api_key = sk-abcdefghijklmnopqrstuv")
    assert "只读" in run_memory_command(store, "create", "a.md", "text", writable=False)
    handle = memory_handler(store)
    assert handle("create", path="a.md", file_text="hello") == "Created a.md"
    assert handle("view", bogus=1).startswith("Error:")
    assert not (store.root / "key.md").exists()


def test_injected_index_is_capped_and_falls_back_to_a_listing(tmp_path):
    store = _store(tmp_path)
    assert injected_index(store) == ""
    run_memory_command(store, "create", "prefs.md", "---\nname: prefs\ndescription: prefers tabs\ntype: user\n---\n\nUse tabs.")
    assert injected_index(store) == "[No MEMORY.md yet; notes in the memory folder:]\n- prefs.md — prefers tabs"
    store.index_path.write_text("".join(f"- item {i}\n" for i in range(300)))
    text = injected_index(store)
    assert "- item 199" in text and "- item 200" not in text and "truncated" in text


def test_forget_backup_and_restore(tmp_path):
    store = _store(tmp_path)
    run_memory_command(store, "create", "topic/tabs.md", "Use tabs.")
    run_memory_command(store, "create", "MEMORY.md", "- tabs")
    backup = backup_memories(store)
    assert forget_note(store, "TABS") == "topic/tabs.md"
    assert forget_note(store, "missing") is None
    run_memory_command(store, "create", "new.md", "new")
    restore_backup(store, backup)
    assert sorted(path.relative_to(store.root).as_posix() for path in store.root.rglob("*.md")
                  if ".backup" not in path.parts) == ["MEMORY.md", "topic/tabs.md"]
    for second in range(BACKUPS_KEPT + 2):
        backup_memories(store, clock=lambda: __import__("datetime").datetime(2026, 1, 1, 0, 0, second))
    assert len(list((store.root / ".backup").iterdir())) == BACKUPS_KEPT


def test_transcripts_expose_authored_text_and_tool_calls_but_not_tool_output(tmp_path):
    runtime = _runtime(tmp_path)
    session_id = _save_session(runtime, "tabs")
    snapshot = runtime.sessions.store.load(session_id)
    entries = transcript_entries(runtime.config.workdir, snapshot)
    assert entries == [
        "user: please remember tabs", "assistant: I will use tabs.", 'assistant → bash {"command": "ls"}',
        "assistant: Done with tabs.",
    ]
    found = search_transcripts(runtime.config.workdir, "REMEMBER tabs")
    assert session_id[:8] in found and "#0" in found
    assert search_transcripts(runtime.config.workdir, "TOOL-OUTPUT-SECRET") == "(no matches in earlier sessions)"
    page = read_transcript(runtime.config.workdir, session_id[:8], offset=1, limit=2)
    assert "#1 assistant: I will use tabs." in page and "offset=3" in page
    assert "RUNTIME-NOTICE" not in page


def test_runtime_registers_memory_tools_with_mode_and_setting_limits(tmp_path):
    runtime = _runtime(tmp_path)
    tools, handlers = runtime.tools.snapshot()
    names = {tool["name"] for tool in tools}
    assert {"memory", "transcript_search", "transcript_read"} <= names
    assert handlers["memory"]("create", path="a.md", file_text="note") == "Created a.md"
    runtime.session.context_state["working_mode"] = "plan"
    assert "规划模式" in runtime.tools.snapshot()[1]["memory"]("create", path="b.md", file_text="x")
    assert "a.md" in runtime.tools.snapshot()[1]["memory"]("view")
    from agent_app.core.context import build_context
    assert build_context(runtime, runtime.tools.snapshot()[0])["memory_writable"] is False

    readonly = _runtime(tmp_path / "off", PAMU_AUTO_MEMORY="off")
    schema = next(tool for tool in readonly.tools.snapshot()[0] if tool["name"] == "memory")
    assert schema["input_schema"]["properties"]["command"]["enum"] == ["view"]
    assert "只读" in readonly.tools.snapshot()[1]["memory"]("create", path="a.md", file_text="x")


def test_prompt_tells_what_memory_is_for_and_injects_the_index():
    writable = assemble_system_prompt({"enabled_tools": ["memory"], "memories": "- [a](a.md)"})
    assert "clearly worth remembering" in writable and "MEMORY.md:\n- [a](a.md)" in writable
    readonly = assemble_system_prompt({"enabled_tools": ["memory"], "memories": "", "memory_writable": False})
    assert "Memory (read-only)" in readonly and "MEMORY.md:\n(empty)" in readonly
    assert "Memory index:\n- x" in assemble_system_prompt({"enabled_tools": [], "memories": "- x"})


def test_auto_dream_cadence(tmp_path):
    runtime = _runtime(tmp_path)
    store, workdir = runtime.memory, runtime.config.workdir
    _save_session(runtime, "tabs")
    _save_session(runtime, "ruff")
    assert len(dreaming.pending_sessions(store, workdir)) == 2
    assert not dreaming.should_auto_dream(store, workdir)
    _save_session(runtime, "pytest")
    assert dreaming.should_auto_dream(store, workdir)
    dreaming.run_dream(runtime, agent=lambda *_: "nothing to change", clock=lambda: 4_000_000_000)
    assert dreaming.pending_sessions(store, workdir) == []
    _save_session(runtime, "later")
    assert not dreaming.should_auto_dream(store, workdir, clock=lambda: 4_000_000_000 + 10)
    assert dreaming.should_auto_dream(store, workdir, clock=lambda: 4_000_000_000 + dreaming.AUTO_MAX_AGE_SECONDS)


def test_dream_agent_reads_transcripts_and_reorganizes_memory(tmp_path):
    adapter = ScriptedAdapter()
    runtime = _runtime(tmp_path, adapter)
    session_id = _save_session(runtime, "tabs")
    run_memory_command(runtime.memory, "create", "stale.md", "Uses spaces.")
    adapter.responses = iter([
        ModelResponse([ToolUseBlock("t1", "transcript_read", {"session": session_id[:8]})], "tool_use"),
        ModelResponse([ToolUseBlock("t2", "memory", {"command": "create", "path": "prefs.md", "file_text": "Use tabs."})], "tool_use"),
        ModelResponse([ToolUseBlock("t3", "memory", {"command": "delete", "path": "stale.md"})], "tool_use"),
        ModelResponse([ToolUseBlock("t4", "memory", {"command": "create", "path": "MEMORY.md", "file_text": "- [prefs](prefs.md)"})], "tool_use"),
        ModelResponse([TextBlock("Replaced the stale spacing note with the tab preference.")], "end_turn"),
    ])
    report = dreaming.run_dream(runtime)
    assert (report.added, report.deleted, report.sessions) == (["MEMORY.md", "prefs.md"], ["stale.md"], 1)
    assert "新增：MEMORY.md、prefs.md" in report.describe() and "删除：stale.md" in report.describe()
    first = adapter.requests[0]
    assert "offline reflection" in first["system"] and session_id[:8] in first["messages"][0]["content"]
    assert {tool["name"] for tool in first["tools"]} >= {"memory", "transcript_read", "transcript_search"}
    transcript_result = json.dumps(adapter.requests[1]["messages"], default=str)
    assert "please remember tabs" in transcript_result and "TOOL-OUTPUT-SECRET" not in transcript_result
    assert dreaming.pending_sessions(runtime.memory, runtime.config.workdir) == []


@pytest.mark.parametrize("failure", [RuntimeError("boom"), KeyboardInterrupt(), "[Subagent error] APIError: down"])
def test_failed_dream_restores_memory_and_keeps_sessions_pending(tmp_path, failure):
    runtime = _runtime(tmp_path)
    _save_session(runtime, "tabs")
    run_memory_command(runtime.memory, "create", "keep.md", "original")

    def agent(runtime, _pending):
        run_memory_command(runtime.memory, "create", "keep.md", "clobbered")
        run_memory_command(runtime.memory, "create", "partial.md", "half done")
        if isinstance(failure, BaseException):
            raise failure
        return failure

    expected = dreaming.DreamError if isinstance(failure, str) else type(failure)
    with pytest.raises(expected):
        dreaming.run_dream(runtime, agent=agent)
    assert [note["path"] for note in list_notes(runtime.memory)] == ["keep.md"]
    assert (runtime.memory.root / "keep.md").read_text() == "original\n"
    assert len(dreaming.pending_sessions(runtime.memory, runtime.config.workdir)) == 1


def test_exit_dream_respects_setting_and_can_be_skipped(tmp_path, monkeypatch):
    from dataclasses import replace
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(dreaming, "should_auto_dream", lambda *_a, **_k: True)
    monkeypatch.setattr(dreaming, "_dream_agent", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    printed = []
    dreaming.maybe_dream_on_exit(runtime, print_fn=printed.append)
    assert printed[-1] == "已跳过记忆整理，记忆保持原样。"
    runtime.config = replace(runtime.config, auto_memory=False)
    printed.clear()
    dreaming.maybe_dream_on_exit(runtime, print_fn=printed.append)
    assert printed == []


def test_memory_and_dream_commands(tmp_path, capsys, monkeypatch):
    import threading
    from agent_app import cli
    store = _store(tmp_path)
    runtime = SimpleNamespace(memory=store, config=SimpleNamespace(auto_memory=True, sandbox_mode="off", headless=False),
                              model=None, agent_lock=threading.Lock())
    cli._handle_slash_command(runtime, "/memory")
    assert "没有记忆" in capsys.readouterr().out
    run_memory_command(store, "create", "MEMORY.md", "- [tabs](style/tabs.md)")
    run_memory_command(store, "create", "style/tabs.md", "# Prefer tabs\n")
    cli._handle_slash_command(runtime, "/memory")
    assert capsys.readouterr().out.splitlines() == ["MEMORY.md  1 行（每个会话加载前 200 行）", "style/tabs.md  Prefer tabs"]
    cli._handle_slash_command(runtime, "/memory forget style/tabs")
    assert "已删除记忆：style/tabs.md" in capsys.readouterr().out
    monkeypatch.setattr(dreaming, "run_dream", lambda _runtime: dreaming.DreamReport(added=["a.md"], sessions=2, summary="ok"))
    cli._handle_slash_command(runtime, "/dream")
    assert "记忆整理完成（参考 2 个会话）\n新增：a.md\nok" in capsys.readouterr().out
    runtime.config.sandbox_mode = "confined"
    cli._handle_slash_command(runtime, "/dream")
    assert "当前沙箱模式不支持整理记忆" in capsys.readouterr().out
