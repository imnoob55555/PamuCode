from dataclasses import replace

from agent_app import cli
from agent_app.bootstrap import build_runtime
from agent_app.config import AppConfig
from agent_app.features.checkpoints import FileCheckpoints


class UI:
    def __init__(self, answer="undo"):
        self.answer = answer
        self.titles = []
        self.notices = []

    def choose(self, title, choices):
        self.titles.append(title)
        return self.answer

    def notify(self, message):
        self.notices.append(message)


def checkpoints(tmp_path, turn, **options):
    return FileCheckpoints(tmp_path, tmp_path / ".pamu" / "checkpoints",
                           session_id=lambda: "s1", turn_id=lambda: turn[0], **options)


def change(store, path, text):
    store.before_change(path)
    if text is None:
        path.unlink()
    else:
        path.write_text(text)
    store.after_change(path)


def test_restore_covers_edited_overwritten_and_created_files(tmp_path):
    turn = ["t1"]
    store = checkpoints(tmp_path, turn)
    (tmp_path / "edit.py").write_text("old edit")
    (tmp_path / "write.py").write_text("old write")
    change(store, tmp_path / "edit.py", "new edit")
    change(store, tmp_path / "edit.py", "newer edit")
    change(store, tmp_path / "write.py", "new write")
    (tmp_path / "pkg").mkdir()
    change(store, tmp_path / "pkg" / "created.py", "brand new")
    latest = store.latest()
    assert store.files(latest) == ["edit.py", "pkg/created.py", "write.py"]
    assert store.conflicts(latest) == []
    assert store.restore(latest) == ["edit.py", "pkg/created.py", "write.py"]
    assert (tmp_path / "edit.py").read_text() == "old edit"
    assert (tmp_path / "write.py").read_text() == "old write"
    assert not (tmp_path / "pkg" / "created.py").exists()
    assert store.latest() is None


def test_turns_are_undone_newest_first(tmp_path):
    turn = ["t1"]
    clock = iter(range(100))
    store = checkpoints(tmp_path, turn, clock=lambda: next(clock))
    target = tmp_path / "a.txt"
    target.write_text("v0")
    change(store, target, "v1")
    turn[0] = "t2"
    change(store, target, "v2")
    store.restore(store.latest())
    assert target.read_text() == "v1"
    store.restore(store.latest())
    assert target.read_text() == "v0"


def test_later_outside_edits_are_reported_as_conflicts(tmp_path):
    store = checkpoints(tmp_path, ["t1"])
    target = tmp_path / "a.txt"
    target.write_text("v0")
    change(store, target, "v1")
    target.write_text("edited by hand")
    assert store.conflicts(store.latest()) == ["a.txt"]


def test_only_the_most_recent_turns_are_kept(tmp_path):
    turn = ["t0"]
    clock = iter(range(100))
    store = checkpoints(tmp_path, turn, keep=3, clock=lambda: next(clock))
    target = tmp_path / "a.txt"
    for index in range(5):
        turn[0] = f"t{index}"
        change(store, target, f"v{index}")
    assert [path.name for path in store.turns()] == ["t4", "t3", "t2"]


def _runtime(tmp_path):
    config = replace(AppConfig.from_env(tmp_path, environ={"MODEL_ID": "m"}), headless=True)
    runtime = build_runtime(config, None, adapter=object())
    runtime.session.context_state["current_request"] = {"id": "turn-1", "text": "edit"}
    return runtime


def test_undo_command_restores_file_tool_changes_after_confirmation(tmp_path, capsys):
    runtime = _runtime(tmp_path)
    try:
        (tmp_path / "note.txt").write_text("before\n")
        _, handlers = runtime.tools.snapshot()
        assert handlers["edit_file"]("note.txt", "before", "after") == "Edited note.txt"
        assert handlers["write_file"]("new.txt", "created") .startswith("Wrote")
        ui = UI(answer="cancel")
        cli.handle_slash_command(runtime, "/undo", ui=ui)
        assert (tmp_path / "note.txt").read_text() == "after\n"
        assert "new.txt、note.txt" in ui.titles[0] and ui.notices == ["已取消撤销。"]
        cli.handle_slash_command(runtime, "/undo", ui=UI())
        assert (tmp_path / "note.txt").read_text() == "before\n"
        assert not (tmp_path / "new.txt").exists()
        out = capsys.readouterr().out
        assert "已撤销：new.txt、note.txt" in out and "Shell 命令造成的改动不会被撤销" in out
        assert runtime.session.history[-1]["content"].startswith("[用户执行了 /undo]")
        ui = UI()
        cli.handle_slash_command(runtime, "/undo", ui=ui)
        assert "没有可撤销的文件修改" in ui.notices[0]
    finally:
        runtime.sessions.close()


def test_undo_warns_before_overwriting_later_edits(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        (tmp_path / "note.txt").write_text("before")
        _, handlers = runtime.tools.snapshot()
        handlers["write_file"]("note.txt", "agent")
        (tmp_path / "note.txt").write_text("by hand")
        ui = UI()
        cli.handle_slash_command(runtime, "/undo", ui=ui)
        assert "在那之后又被修改过" in ui.titles[0]
        assert (tmp_path / "note.txt").read_text() == "before"
    finally:
        runtime.sessions.close()


def test_failed_or_refused_changes_leave_no_checkpoint(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        _, handlers = runtime.tools.snapshot()
        assert handlers["edit_file"]("missing.txt", "a", "b").startswith("Error:")
        assert handlers["write_file"](".env", "x").startswith("Error:")
        assert runtime.checkpoints.latest() is None
        (tmp_path / "kept.txt").write_text("v0")
        handlers["write_file"]("kept.txt", "v1")
        assert handlers["edit_file"]("kept.txt", "absent", "x").startswith("Error:")
        assert runtime.checkpoints.files(runtime.checkpoints.latest()) == ["kept.txt"]
    finally:
        runtime.sessions.close()


def test_confined_runtime_has_no_checkpoints(tmp_path):
    config = replace(AppConfig.from_env(tmp_path, environ={"MODEL_ID": "m"}), headless=True, sandbox_mode="confined")
    runtime = build_runtime(config, None, adapter=object())
    try:
        assert runtime.checkpoints is None
        ui = UI()
        cli.handle_slash_command(runtime, "/undo", ui=ui)
        assert "不支持 /undo" in ui.notices[0]
    finally:
        runtime.sessions.close()


def test_deleting_the_session_discards_its_checkpoints(tmp_path):
    import threading
    from agent_app.features.sessions import checkpoint
    from agent_app.repl_sessions import delete_current_session
    runtime = _runtime(tmp_path)
    runtime.session.history.append({"role": "user", "content": "edit"})
    checkpoint(runtime, status="idle")
    _, handlers = runtime.tools.snapshot()
    handlers["write_file"]("a.txt", "x")
    session_dir = runtime.config.checkpoints_dir / runtime.sessions.session_id
    assert session_dir.is_dir()
    runtime.agent_lock = threading.Lock()
    assert delete_current_session(runtime) is None
    assert not session_dir.exists()
