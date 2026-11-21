from dataclasses import replace

import pytest

from agent_app import cli
from agent_app.adapters.common import ModelResponse, TextBlock
from agent_app.bootstrap import build_runtime
from agent_app.config import AppConfig
from agent_app.features.skills import SkillInvocation
from agent_app.working_mode import switch_mode


class Model:
    def __init__(self):
        self.requests = []

    def create_streaming(self, **kwargs):
        self.requests.append(kwargs)
        return ModelResponse([TextBlock("Reviewed.")], "end_turn")


class UI:
    def __init__(self, commands=(), selection="review"):
        self.commands = iter(commands)
        self.selection = selection
        self.notices = []
        self.choices = []

    def prompt(self):
        return next(self.commands)

    def choose(self, title, choices):
        self.choices = choices
        return self.selection

    def notify(self, message):
        self.notices.append(message)


@pytest.fixture
def runtime(tmp_path):
    manifest = tmp_path / ".pamu" / "skills" / "review" / "SKILL.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("---\nname: Code Review\ndescription: Inspect changes\n---\nRead the diff and report defects.")
    config = replace(AppConfig.from_env(tmp_path, environ={"MODEL_ID": "test"}), headless=True)
    rt = build_runtime(config, None, adapter=Model())
    yield rt
    rt.sessions.close()


def test_direct_skill_loads_existing_tool_and_preserves_task(runtime, capsys):
    result = cli.handle_slash_command(runtime, "/skill review Inspect the auth changes")
    assert isinstance(result, SkillInvocation)
    assert result.name == "review"
    assert result.task == "Inspect the auth changes"
    assert "Read the diff" in result.prompt
    assert runtime.session.context_state["loaded_skills"][0]["name"] == "review"
    assert not runtime.llm.requests
    assert "已调用技能：review" in capsys.readouterr().out


def test_skill_picker_uses_directory_key_and_display_name(runtime):
    ui = UI()
    result = cli.handle_slash_command(runtime, "/skill", ui=ui)
    assert isinstance(result, SkillInvocation)
    assert ui.choices == [("review", "review: Inspect changes (name: Code Review) [.pamu/skills]")]


@pytest.mark.parametrize("command,selection", [("/skill", None), ("/skill missing", "review")])
def test_cancelled_or_missing_skill_leaves_no_chat_or_loaded_context(runtime, capsys, command, selection):
    ui = UI(selection=selection)
    assert cli.handle_slash_command(runtime, command, ui=ui) is None
    assert ui.notices
    assert not runtime.session.history
    assert not runtime.session.context_state.get("loaded_skills")
    assert not runtime.llm.requests
    assert not capsys.readouterr().out


def test_no_ui_lists_skills_without_invoking(runtime, capsys):
    assert cli.handle_slash_command(runtime, "/skill") is None
    assert "review: Inspect changes (name: Code Review) [.pamu/skills]" in capsys.readouterr().out
    assert not runtime.session.context_state.get("loaded_skills")


def test_repl_skill_runs_model_with_full_skill_and_retains_plan_mode(runtime, tmp_path):
    switch_mode(runtime, "plan")
    ui = UI(["/skill review Explain the changes", "/quit"])
    cli.main(
        argv=[], runtime_factory=lambda: runtime, ui=ui,
        credentials_path=tmp_path / "credentials.json",
        start_threads=lambda rt: [], stop_threads=lambda rt, threads: None,
    )
    request = runtime.llm.requests[0]
    assert "Working mode: plan" in request["system"]
    assert "Read the diff" in str(request["messages"])
    assert "Explain the changes" in str(request["messages"])
    assert not any(t["name"] == "bash" for t in request["tools"])
    assert not any(m["content"] == "/skill review Explain the changes" for m in runtime.session.history)
    assert runtime.session.history[-1]["role"] == "assistant"


def test_confined_skill_command_does_not_load(runtime):
    runtime.config = replace(runtime.config, sandbox_mode="confined")
    ui = UI()
    assert cli.handle_slash_command(runtime, "/skill review", ui=ui) is None
    assert "沙箱" in ui.notices[0]
    assert not runtime.session.context_state.get("loaded_skills")


def test_no_skills_or_no_runtime_only_notifies(runtime):
    ui = UI()
    cli.handle_slash_command(None, "/skill", ui=ui)
    assert "配置" in ui.notices[-1]
    (runtime.config.skills_dir / "review" / "SKILL.md").unlink()
    cli.handle_slash_command(runtime, "/skill", ui=ui)
    assert "没有可用技能" in ui.notices[-1] and ".pamu/skills" in ui.notices[-1]
